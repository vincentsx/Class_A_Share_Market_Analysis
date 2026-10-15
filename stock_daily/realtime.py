"""Independent ten-minute market/news/AI snapshots, never trade execution inputs."""

import hashlib
import json
import math
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from .config import SHANGHAI, now
from .db import bars, realtime_reports, sessions, trading_days
from .html_export import ASSETS, script_json
from .news import build_news, collect_news
from .operations import state
from .portfolio import market_window
from .providers import DataError
from .static_report import template_helpers

VERSION = 'realtime-analysis-v1'


def window_for(current, interval=10):
    return market_window(current, interval)


def due(db, settings, current):
    if not settings.realtime_enabled or settings.source == 'demo' or current.weekday() >= 5:
        return False
    window = window_for(current, settings.realtime_interval_minutes)
    if not window:
        return False
    calendar = db.rows(select(trading_days.c.is_open).where(trading_days.c.source == settings.source,
        trading_days.c.day == current.strftime('%Y%m%d')))
    # Daily collection already warms the exchange calendar. Unknown dates are not assumed open.
    if not calendar or not calendar[0]['is_open']:
        return False
    return state(db, settings, 'realtime_refresh').get('window') != window


def query_reports(settings, day=None, window=None):
    query = select(realtime_reports).where(realtime_reports.c.source == settings.source)
    if day:
        query = query.where(realtime_reports.c.day == day)
    if window:
        query = query.where(realtime_reports.c.window == window)
    return query.order_by(realtime_reports.c.captured_at.desc())


def view(db, settings, day=None, window=None, current=None):
    current = (current or now()).astimezone(SHANGHAI)
    saved = db.rows(query_reports(settings, day, window).limit(1))
    report = saved[0]['payload'] if saved else None
    history = db.rows(select(realtime_reports.c.day, realtime_reports.c.window, realtime_reports.c.captured_at)
        .where(realtime_reports.c.source == settings.source)
        .order_by(realtime_reports.c.captured_at.desc()).limit(100))
    refresh = state(db, settings, 'realtime_refresh') if not day and not window else {}
    freshness_at = current
    if '11:30' < current.strftime('%H:%M') < '13:00':
        freshness_at = current.replace(hour=11, minute=30, second=0, microsecond=0)
    elif current.strftime('%H:%M') > '15:05':
        freshness_at = current.replace(hour=15, minute=5, second=0, microsecond=0)
    stale = bool(report and (report['day'] != current.strftime('%Y%m%d') or
        freshness_at - datetime.fromisoformat(report['captured_at']) > timedelta(
            minutes=settings.realtime_interval_minutes + 5)))
    return {'version': VERSION, 'source': settings.source, 'report': report, 'history': history,
        'refresh': refresh, 'status': 'unavailable' if not report else 'stale' if stale else report['status'],
        'interval_minutes': settings.realtime_interval_minutes,
        'message': '尚未取得盘中分析，等待交易时段采集。' if not report else
            '行情已过期，保留最近已保存的分析，请核对截止时间。' if stale else
            f'交易时段每{settings.realtime_interval_minutes}分钟重新采集和分析；浏览器每分钟检查新结果。'}


def quote_valid(quote, day, captured):
    try:
        stamp = datetime.fromisoformat(quote['quote_time'])
        clock = stamp.astimezone(SHANGHAI).strftime('%H:%M')
        return (stamp.tzinfo is not None and stamp.astimezone(SHANGHAI).strftime('%Y%m%d') == day
            and stamp <= captured + timedelta(seconds=5) and captured - stamp <= timedelta(minutes=10)
            and ('09:30' <= clock <= '11:30' or '13:00' <= clock <= '15:05')
            and all(isinstance(quote[k], (int, float)) and math.isfinite(quote[k]) and quote[k] > 0
                for k in ('price', 'open', 'previous', 'high', 'low', 'volume', 'amount'))
            and quote['low'] <= min(quote['price'], quote['open'])
            and quote['high'] >= max(quote['price'], quote['open']))
    except (KeyError, ValueError, TypeError):
        return False


def indicators(db, settings, basis, rows):
    dates = [r['day'] for r in db.rows(select(trading_days.c.day).where(
        trading_days.c.source == settings.source, trading_days.c.is_open == 1,
        trading_days.c.day <= basis).order_by(trading_days.c.day.desc()).limit(20))]
    histories = defaultdict(list)
    if dates:
        for row in db.rows(select(bars.c.code, bars.c.day, bars.c.close, bars.c.high, bars.c.volume, bars.c.adj)
                .where(bars.c.source == settings.source, bars.c.day.in_(dates))):
            histories[row['code']].append(row)
    for row in rows:
        row.update(ma20=None, distance_ma20=None, prior_high20=None, cumulative_daily_volume_ratio=None,
            vwap=None, tags=[])
        average = row['amount'] / row['volume']
        if row['low'] - .0051 <= average <= row['high'] + .0051:
            row['vwap'] = average
        prior = sorted(histories[row['code']], key=lambda r: r['day'], reverse=True)
        if (len(dates) == len(prior) == 20 and len({r['adj'] for r in prior}) == 1
                and all(r['volume'] > 0 for r in prior)
                and math.isclose(row['previous'], prior[0]['close'], abs_tol=.0051, rel_tol=0)):
            row.update(ma20=mean(r['close'] for r in prior), prior_high20=max(r['high'] for r in prior),
                cumulative_daily_volume_ratio=row['volume'] / mean(r['volume'] for r in prior))
            row['distance_ma20'] = (row['price'] / row['ma20'] - 1) * 100
            if row['price'] > row['prior_high20']:
                row['tags'].append('高于前20日高点')
            if row['price'] < row['ma20'] <= row['previous']:
                row['tags'].append('跌破MA20参考')
        if (row['high'] - row['price']) / row['previous'] >= .03:
            row['tags'].append('冲高回落')
        if 'ST' in row['name'].upper() or '退' in row['name']:
            row['tags'].append('风险名称标记')


def build(db, settings, day, window, quotes, stocks, basis, captured):
    rows = [{**q, 'industry': stocks[code].get('industry') or '未分类',
        'pct': (q['price'] / q['previous'] - 1) * 100} for code, q in quotes.items() if code in stocks]
    if not rows or '000300.SH' not in quotes:
        raise DataError('盘中沪深300或股票报价缺失，保留此前分析')
    indicators(db, settings, basis, rows)
    benchmark = {**quotes['000300.SH'],
        'pct': (quotes['000300.SH']['price'] / quotes['000300.SH']['previous'] - 1) * 100}
    groups = defaultdict(list)
    for row in rows:
        groups[row['industry']].append(row)
    industries = [{'name': name, 'count': len(group), 'pct': mean(r['pct'] for r in group),
        'up_ratio': sum(r['pct'] > 0 for r in group) / len(group) * 100,
        'amount': sum(r['amount'] for r in group),
        'active': len(group) >= 5 and mean(r['pct'] for r in group) > benchmark['pct']
            and sum(r['pct'] > 0 for r in group) / len(group) >= .6,
        'leaders': [{'code': r['code'], 'name': r['name'], 'pct': r['pct']}
            for r in sorted(group, key=lambda r: -r['pct'])[:3]]} for name, group in groups.items()]
    industries.sort(key=lambda r: (r['count'] >= 5, r['pct']), reverse=True)
    coverage = len(rows) / len(stocks) * 100
    comparable = [r for r in rows if r['ma20'] is not None]
    report = {'version': VERSION, 'source': settings.source, 'day': day, 'window': window,
        'captured_at': captured.isoformat(), 'quote_start': min(q['quote_time'] for q in quotes.values()),
        'quote_end': max(q['quote_time'] for q in quotes.values()), 'basis_day': basis,
        'expected': len(stocks), 'coverage': coverage,
        'status': 'available' if coverage >= settings.min_coverage * 100 else 'partial',
        'market': {'count': len(rows), 'up': sum(r['pct'] > 0 for r in rows),
            'down': sum(r['pct'] < 0 for r in rows), 'flat': sum(r['pct'] == 0 for r in rows),
            'up_ratio': sum(r['pct'] > 0 for r in rows) / len(rows) * 100,
            'amount': sum(r['amount'] for r in rows), 'benchmark': benchmark,
            'ma20_count': len(comparable), 'above_ma20_ratio':
                sum(r['price'] > r['ma20'] for r in comparable) / len(comparable) * 100 if comparable else None},
        'industries': industries[:20],
        'leaders': sorted(rows, key=lambda r: -r['pct'])[:12],
        'laggards': sorted(rows, key=lambda r: r['pct'])[:12],
        'risk_stocks': sorted([r for r in rows if r['tags']], key=lambda r: -len(r['tags']))[:20],
        'notes': ['行情范围为缓存的沪深A股样本，行业为等权样本，均标注覆盖率；不含北交所。',
            'MA20及20日高点仅使用前一交易日及以前的完整日线；近期复权变化或历史不足时留空。',
            '累计成交量/前20日全天均量只是累计量比较，不是同时间量比或全天成交量预测。',
            '新闻舆情仅覆盖已采集公开报道，不包含股吧、社交媒体评论或投资者情绪调查。',
            '盘中分析不写入日线信号、不自动修改策略；模拟成交及提醒继续独立运行。']}
    news_input = {'day': day, 'slot': 'realtime', 'captured_at': captured.isoformat(),
        'quotes': rows, 'industries': industries, 'focus': []}
    report['news'] = build_news(db, settings, news_input)
    for item in report['news']['items'] + report['news']['topics']:
        for stock in item.get('mentioned_stocks', []):
            stock.pop('watched', None)
    items = report['news']['items']
    report['sentiment'] = {'sample_count': len(items),
        'risk_count': sum(r['kind'] == '风险事项待核对' for r in items),
        'unverified_count': sum(r['kind'] == '传闻/澄清待核对' for r in items),
        'policy_count': sum(r['kind'] == '政策/监管报道' for r in items),
        'market_reports': sum(r['kind'] == '行情报道' for r in items),
        'topics': report['news']['topics'],
        'method': '按当前展示的去重报道样本统计热度与类别；风险词和传闻标记仅供核对，不判断利好利空。'}
    m = report['market']
    evidence = [{'id': 'market', 'title': '本次行情与数据覆盖', 'text': json.dumps({
        **m, 'coverage': coverage, 'expected': len(stocks), 'basis_day': basis}, ensure_ascii=False)},
        {'id': 'coverage', 'title': '数据范围与限制', 'text': '；'.join(report['notes'])}]
    for sector in industries[:8]:
        identifier = 'sector-' + hashlib.sha256(sector['name'].encode()).hexdigest()[:12]
        evidence.append({'id': identifier, 'title': sector['name'],
            'text': json.dumps(sector, ensure_ascii=False)})
    for item in items[:24]:
        evidence.append({'id': 'news-' + item['id'], 'title': item['title'],
            'text': item['excerpt'], 'kind': item['kind'], 'sources': item['sources'],
            'published_at': item['published_at']})
    report['evidence'] = evidence
    report['ai'] = {'status': 'pending', 'message': '本次行情和新闻已保存，AI分析生成中'}
    return report


def run(db, settings, feed=None, news_feed=None, analyzer=None, current=None, on_saved=None, retry_ai=False):
    from .intraday import SinaQuotes
    from .realtime_ai import analyze

    started = (current or now()).astimezone(SHANGHAI)
    day, window = started.strftime('%Y%m%d'), window_for(started, settings.realtime_interval_minutes)
    calendar = db.rows(select(trading_days.c.is_open).where(trading_days.c.source == settings.source,
        trading_days.c.day == day))
    if not settings.realtime_enabled or not window or not calendar or not calendar[0]['is_open']:
        return {'status': 'skipped'}
    with db.lock('realtime:' + settings.source, lease_minutes=10):
        existing = db.rows(query_reports(settings, day, window).limit(1))
        if existing and existing[0]['payload']['ai']['status'] != 'pending' and not (
                retry_ai and existing[0]['payload']['ai']['status'] == 'failed'):
            return {'status': 'unchanged'}
        attempt = {'at': started.isoformat(), 'window': window, 'status': 'running'}
        db.save_state(settings.source, 'realtime_refresh', attempt)
        identifier = db.start_run(settings.source, day, 'realtime')
        try:
            if existing:
                report = existing[0]['payload']
                report['ai'] = {'status': 'pending', 'message': '正在基于原快照重新生成AI分析，行情与新闻截止时间保留'}
                with db.engine.begin() as c:
                    db.upsert(c, realtime_reports, {**existing[0], 'payload': report})
            else:
                saved = db.rows(select(sessions).where(sessions.c.source == settings.source,
                    sessions.c.day < day).order_by(sessions.c.day.desc()).limit(1))
                if not saved:
                    raise DataError('缺少前一交易日基础信息，暂不生成盘中分析')
                metadata = saved[0]
                if metadata['day'] < (started - timedelta(days=7)).strftime('%Y%m%d'):
                    raise DataError('基础信息超过7天未更新，保留此前盘中分析')
                stocks = {code: stock for code, stock in metadata['universe'].items()
                    if (not stock.get('list_date') or stock['list_date'] <= day)
                    and (not stock.get('delist_date') or stock['delist_date'] > day)}
                quotes, missing = (feed or SinaQuotes(settings, max_seconds=120)).fetch(stocks, day, 'portfolio')
                # Keep collection failures independent: saved headlines can still be displayed with source status.
                collect_news(db, settings, feed=news_feed, captured=current)
                captured = current or now()
                quotes = {code: q for code, q in quotes.items()
                    if code in stocks or code == '000300.SH' if quote_valid(q, day, captured)}
                report = build(db, settings, day, window, quotes, stocks, metadata['day'], captured)
                report['missing_count'] = len(stocks) - report['market']['count']
                with db.engine.begin() as c:
                    db.upsert(c, realtime_reports, {'source': settings.source, 'day': day, 'window': window,
                        'captured_at': report['captured_at'], 'payload': report})
            if on_saved:
                on_saved()
            try:
                report['ai'] = (analyzer or analyze)(settings, report)
            except ValueError as error:
                report['ai'] = {'status': 'failed', 'message': str(error), 'as_of': report['captured_at']}
            with db.engine.begin() as c:
                db.upsert(c, realtime_reports, {'source': settings.source, 'day': day, 'window': window,
                    'captured_at': report['captured_at'], 'payload': report})
            attempt.update(status='success', completed_at=(current or now()).isoformat())
            db.save_state(settings.source, 'realtime_refresh', attempt)
            db.finish_run(identifier, 'success', f"盘中分析已保存：{report['market']['count']}只；AI {report['ai']['status']}")
            return {'status': 'success', 'window': window, 'count': report['market']['count'],
                'ai_status': report['ai']['status']}
        except Exception:
            attempt.update(status='failed', message='盘中分析采集失败，保留此前结果；不会影响模拟成交')
            db.save_state(settings.source, 'realtime_refresh', attempt)
            db.finish_run(identifier, 'failed', attempt['message'])
            raise


def render(payload, settings, public=False):
    from .live_publish import live_url

    endpoint = live_url(settings).replace('portfolio.json', 'realtime.json') if public else '/api/realtime'
    env = Environment(loader=FileSystemLoader(ASSETS / 'templates'), autoescape=select_autoescape(['html']))
    template_helpers(env)
    return env.get_template('realtime.html').render(data=payload, endpoint=endpoint, public=public,
        appearance_css=(ASSETS / "static/appearance.css").read_text(),
        appearance_javascript=(ASSETS / "static/appearance.js").read_text(),
        snapshot=script_json(payload), nonce=secrets.token_urlsafe(24),
        css=(ASSETS / 'static/realtime.css').read_text(),
        javascript=(ASSETS / 'static/realtime.js').read_text())
