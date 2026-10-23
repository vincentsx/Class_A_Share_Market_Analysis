"""Forward-only intraday discoveries, separate from published daily signals."""

import math
from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .backtest import board
from .config import SHANGHAI
from .db import intraday_candidates, realtime_reports, trading_days
from .entry_policy import entry_limits

VERSION = 'intraday-start-v1'
MAX_CANDIDATES = 20
METHOD = ('盘中全市场扫描独立候选：突破前20个完整交易日高点，或昨收不高于MA20后盘中重新站上MA20与MA60；'
          '报价须高于当日开盘价与成交均价、当日相对沪深300为正、成交额达门槛。'
          '使用完整历史冻结ATR与失效参考位，不要求昨日已入选，不用累计量冒充全天放量。'
          '首次发现后仍须两次后续有效观察、AI支持及全部成交风控；不回填历史成交。')


def discover(db, settings, report, rows, stocks):
    """Read-only selection; persistence shares the new report's transaction."""
    captured = datetime.fromisoformat(report['captured_at']).astimezone(SHANGHAI)
    report.update(intraday_candidate_count=0, intraday_candidate_limit=MAX_CANDIDATES,
                  candidate_pool_scanned=len(rows))
    if report['coverage'] < settings.min_coverage * 100:
        return []
    clock = captured.strftime('%H:%M')
    if not ('09:30' <= clock <= '11:30' or '13:00' <= clock <= '15:00'):
        return []
    opened = db.rows(select(trading_days.c.day).where(trading_days.c.source == settings.source,
        trading_days.c.is_open == 1, trading_days.c.day > report['basis_day'],
        trading_days.c.day <= report['day']).order_by(trading_days.c.day))
    if [r['day'] for r in opened] != [report['day']]:
        return []
    saved = {(r['code'], r['strategy']): r for r in db.rows(select(intraday_candidates).where(
        intraday_candidates.c.source == settings.source, intraday_candidates.c.day == report['day']))}
    benchmark = report['market']['benchmark']['pct']
    candidates = []
    for row in rows:
        code, stock = row['code'], stocks.get(row['code'], {})
        limit, _ = board(code)
        from .paper_trading import _quote_error

        reference = [row.get(k) for k in ('ma20', 'ma60', 'prior_high20', 'atr14', 'base_adj', 'vwap')]
        if (limit is None or stock.get('is_st') or 'ST' in row['name'].upper() or '退' in row['name']
                or not row.get('history_complete') or row['amount'] < settings.min_amount
                or not all(isinstance(v, (float, int)) and math.isfinite(v) and v > 0 for v in reference)
                or _quote_error(row, {'close': row['previous']}, report['day'], captured)
                or row['price'] <= max(row['ma20'], row['ma60'])
                or row['price'] < max(row['open'], row['vwap']) or row['pct'] <= benchmark):
            continue
        if row['price'] > row['prior_high20']:
            strategy, label, stop = 'intraday_breakout', '盘中突破20日高点', max(row['prior_high20'], row['ma60'])
        elif row['previous'] <= row['ma20']:
            strategy, label, stop = 'intraday_reclaim', '盘中均线收复', max(row['ma20'], row['ma60'])
        else:
            continue
        previous = saved.get((code, strategy))
        if previous and previous['basis_day'] != report['basis_day']:
            continue
        payload = {'name': row['name'], 'industry': row['industry'], 'label': label,
            'origin': 'intraday', 'rule_version': VERSION, 'basis_day': report['basis_day'],
            'published_at': report['captured_at'], 'discovered_quote_time': row['quote_time'],
            'discovery_quote': {k: row[k] for k in ('quote_time', 'open', 'previous', 'volume', 'amount')},
            'discovery_price': row['price'], 'atr14': row['atr14'], 'relative20': None,
            'invalidation_adj': stop * row['base_adj'],
            'reasons': [label, f"当日领先沪深300 {row['pct'] - benchmark:.2f} 个百分点",
                f"发现价 {row['price']:.4f}，MA20 {row['ma20']:.4f}、MA60 {row['ma60']:.4f}",
                '量能仅用已观察累计量额；不声称达到全天放量标准'],
            'risks': ['盘中条件可失效，首次发现不代表已确认买入']}
        record = deepcopy(previous) if previous else {'source': settings.source, 'day': report['day'],
            'code': code, 'strategy': strategy, 'basis_day': report['basis_day'],
            'discovered_at': report['captured_at'], 'payload': payload}
        cap = entry_limits(row['previous'], record['payload']['atr14'])['max_entry_gap']
        # Reserve model attention for candidates that can presently pass the
        # price ceiling; high-gap/limit stocks can still be observed afterwards.
        candidates.append((row['price'] > row['previous'] * (1 + cap)
            or row['price'] >= row['previous'] * (1 + limit - .001),
            -(row['pct'] - benchmark), -row['amount'], code, record))
    candidates.sort(key=lambda item: item[:-1])
    report['intraday_candidate_count'] = len(candidates)
    return [item[-1] for item in candidates[:MAX_CANDIDATES]]


def persist(connection, db, report):
    """First discovery and its reference data are immutable, including retries."""
    constructor = sqlite_insert if db.engine.dialect.name == 'sqlite' else pg_insert
    for candidate in report.get('intraday_candidates', []):
        connection.execute(constructor(intraday_candidates).values(candidate).on_conflict_do_nothing())


def active_candidates(db, settings, day, basis, current):
    """Only a fresh, saved, same-session report can activate a discovered stock."""
    rows = db.rows(select(realtime_reports).where(realtime_reports.c.source == settings.source,
        realtime_reports.c.day == day, realtime_reports.c.captured_at <= current.isoformat())
        .order_by(realtime_reports.c.captured_at.desc()).limit(1))
    if not rows:
        return []
    report = rows[0]['payload']
    captured = datetime.fromisoformat(report['captured_at']).astimezone(SHANGHAI)
    if (report.get('candidate_pool_version') != VERSION or report['basis_day'] != basis
            or report['coverage'] < settings.min_coverage * 100
            or current - captured > timedelta(minutes=2 * settings.realtime_interval_minutes + 2)
            or (current.hour < 12) != (captured.hour < 12)):
        return []
    active = {(r['code'], r['strategy']) for r in report.get('intraday_candidates', [])}
    saved = db.rows(select(intraday_candidates).where(intraday_candidates.c.source == settings.source,
        intraday_candidates.c.day == day, intraday_candidates.c.basis_day == basis,
        intraday_candidates.c.discovered_at <= report['captured_at']))
    return [row for row in saved if (row['code'], row['strategy']) in active]
