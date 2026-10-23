"""Forward-only AI/news inputs to paper trading, bounded by execution risk checks."""

import json
from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import paper_accounts, realtime_reports, signals
from .entry_policy import vwap
from .news import risk_stock_codes

VERSION = 'evidence-decision-v1'
METHOD = ('仅使用决策前已生成、同日同交易时段且未过期的AI与新闻证据；'
          'AI支持且评分≥60才允许新增买入，等待/退出意见或直接归属于该股的风险片段暂停买入；'
          '市场减仓意见将单笔权重与风险预算减半，暂停意见停止新增。'
          'AI退出评分≥70且当前价格弱于开盘或成交均价时触发退出，仍遵守T+1及成交约束。'
          '既有止损、移动保护、资金及仓位上限始终优先；AI不可用时暂停新增、继续风控退出。')


def attach_universe(db, settings, report, quotes):
    if not settings.ai_trading_enabled:
        return
    basis = report['basis_day']
    candidates = db.rows(select(signals).where(signals.c.source == settings.source, signals.c.day == basis))
    candidates.sort(key=lambda r: -r['payload'].get('relative20', 0))
    accounts = db.rows(select(paper_accounts.c.payload).where(paper_accounts.c.source == settings.source,
        paper_accounts.c.day <= report['day']).order_by(paper_accounts.c.day.desc()).limit(1))
    positions = accounts[0]['payload']['account'].get('positions', []) if accounts else []
    stocks = {p['code']: {'code': p['code'], 'name': p['name'], 'role': 'holding',
        'shares': p['shares'], 'entry_price': p['entry_price']} for p in positions}
    intraday = report.get('intraday_candidates', [])
    # Each pool has reserved model capacity, with remaining slots shared.
    selected = candidates[:20] + intraday[:20]
    seen = {r['code'] for r in selected}
    for row in candidates[20:] + intraday[20:]:
        if len(seen) >= 40:
            break
        if row['code'] not in seen:
            selected.append(row)
            seen.add(row['code'])
    for row in selected:
        code, item = row['code'], row['payload']
        stocks.setdefault(code, {'code': code, 'name': item['name'], 'role': 'candidate'})
        signal = {key: item.get(key) for key in ('reasons', 'risks', 'relative20', 'invalidation_adj',
            'origin', 'rule_version', 'basis_day', 'published_at', 'discovery_price')}
        stocks[code].setdefault('signals', []).append(signal)
        stocks[code].setdefault('signal', signal)
    report['decision_universe'] = sorted(stocks)
    for code, item in stocks.items():
        item['quote'] = quotes.get(code)
        report['evidence'].append({'id': 'stock-' + code, 'title': item['name'] + '模拟交易观察',
            'text': json.dumps(item, ensure_ascii=False)})
    included = {e['id'] for e in report['evidence']}
    for item in report['news']['items']:
        identifier = 'news-' + item['id']
        if identifier not in included and any(s['code'] in stocks for s in item.get('mentioned_stocks', [])):
            report['evidence'].append({'id': identifier, 'title': item['title'], 'text': item['excerpt'],
                'kind': item['kind'], 'sources': item['sources'], 'published_at': item['published_at'],
                **{key: deepcopy(item[key]) for key in ('risk_attribution_version', 'stock_risks') if key in item}})
            included.add(identifier)
            if len(included) >= 100:
                break


def policy_schema(statement):
    return {'type': 'object', 'properties': {
        'market_action': {'type': 'string', 'enum': ['normal', 'reduce', 'pause']},
        'text': {'type': 'string'}, 'evidence_ids': statement['properties']['evidence_ids'],
        'stocks': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'code': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['support', 'wait', 'exit']},
            'score': {'type': 'integer', 'minimum': 0, 'maximum': 100},
            **statement['properties']}, 'required': ['code', 'action', 'score', 'text', 'evidence_ids'],
            'additionalProperties': False}}},
        'required': ['market_action', 'text', 'evidence_ids', 'stocks'], 'additionalProperties': False}


def validate_policy(policy, report, valid_statement):
    if (not isinstance(policy, dict) or set(policy) != {'market_action', 'text', 'evidence_ids', 'stocks'}
            or policy['market_action'] not in {'normal', 'reduce', 'pause'} or not valid_statement(policy)
            or not isinstance(policy['stocks'], list)):
        raise ValueError('AI模拟交易意见格式无效')
    seen = set()
    for item in policy['stocks']:
        if (not isinstance(item, dict) or set(item) != {'code', 'action', 'score', 'text', 'evidence_ids'}
                or item['code'] not in report['decision_universe'] or item['code'] in seen
                or item['action'] not in {'support', 'wait', 'exit'} or type(item['score']) is not int
                or not 0 <= item['score'] <= 100 or not valid_statement(item)
                or 'stock-' + item['code'] not in item['evidence_ids']):
            raise ValueError('AI模拟交易股票或引用无效')
        seen.add(item['code'])
    if seen != set(report['decision_universe']):
        raise ValueError('AI模拟交易观察名单不完整')
    return {**policy, 'status': 'available', 'version': VERSION}


def latest_analysis(db, settings, current):
    rows = db.rows(select(realtime_reports.c.payload).where(realtime_reports.c.source == settings.source,
        realtime_reports.c.captured_at <= current.isoformat()).order_by(realtime_reports.c.captured_at.desc()).limit(1))
    if not rows:
        return {'status': 'not_generated', 'message': '尚无已保存的盘中AI分析。'}
    report = rows[0]['payload']
    return {**report['ai'], 'as_of': report['captured_at'], 'window': report['window'],
        'coverage': report['coverage'], 'evidence': report['evidence']}


def decision_context(db, settings, current):
    result = {'version': VERSION, 'enabled': settings.ai_trading_enabled, 'method': METHOD,
        'status': 'disabled', 'message': 'AI参与模拟交易未启用，沿用量价规则。', 'opinions': {}}
    if not settings.ai_trading_enabled:
        return result
    rows = db.rows(select(realtime_reports.c.payload).where(realtime_reports.c.source == settings.source,
        realtime_reports.c.day == current.strftime('%Y%m%d'),
        realtime_reports.c.captured_at <= current.isoformat()).order_by(realtime_reports.c.captured_at.desc()).limit(1))
    result.update(status='waiting', message='等待本交易时段有效的AI、行情与新闻意见；暂停新增买入。')
    if not rows:
        return result
    report = rows[0]['payload']
    ai = report['ai']
    policy = ai.get('trade_policy', {})
    try:
        captured = datetime.fromisoformat(report['captured_at'])
        generated = datetime.fromisoformat(ai.get('generated_at', ''))
        if (captured.tzinfo is None or generated.tzinfo is None or not captured <= generated <= current
                or current - captured > timedelta(minutes=2 * settings.realtime_interval_minutes + 2)
                or (current.hour < 12) != (captured.hour < 12)
                or report['coverage'] < settings.min_coverage * 100
                or ai['status'] != 'available' or policy.get('status') != 'available'):
            return {**result, 'analysis_status': ai['status'], 'as_of': report['captured_at']}
    except (KeyError, ValueError, TypeError):
        return result
    # Recheck the complete persisted policy at the execution boundary.
    allowed = {e['id'] for e in report['evidence']}
    def valid(item):
        return (isinstance(item.get('text'), str) and bool(item['text'].strip())
            and isinstance(item.get('evidence_ids'), list) and bool(item['evidence_ids'])
            and all(isinstance(i, str) and i in allowed for i in item['evidence_ids']))
    try:
        validate_policy({k: policy[k] for k in ('market_action', 'text', 'evidence_ids', 'stocks')}, report, valid)
    except (KeyError, ValueError, TypeError):
        return result
    risks = {}
    for item in report['news']['items']:
        if item['kind'] in {'风险事项待核对', '传闻/澄清待核对'} and not item.get('after_quote'):
            for code in risk_stock_codes(item):
                clues = [c['text'] for c in item.get('stock_risks') or []
                         if isinstance(c, dict) and c.get('code') == code and c.get('text')]
                risks.setdefault(code, []).extend(clues or [item['title']])
    return {**result, 'status': 'available', 'message': '本次已将AI、新闻与量价意见纳入模拟决策。',
        'window': report['window'], 'as_of': report['captured_at'], 'generated_at': ai['generated_at'],
        'model': ai.get('model'), 'market_action': policy['market_action'], 'market_reason': policy['text'],
        'market_evidence_ids': policy['evidence_ids'],
        'opinions': {r['code']: deepcopy(r) for r in policy['stocks']}, 'news_risks': risks,
        'evidence': deepcopy(report['evidence'])}


def assessment(context, code, quote=None):
    if not context['enabled']:
        return {'buy_allowed': True, 'score': 0, 'exit': False}
    opinion = context['opinions'].get(code)
    allowed = (context['status'] == 'available' and opinion and opinion['action'] == 'support'
        and opinion['score'] >= 60 and context['market_action'] != 'pause'
        and not context.get('news_risks', {}).get(code))
    weak = False
    if quote:
        average = vwap(quote)
        weak = quote['price'] < quote['open'] or average is not None and quote['price'] < average
    exit_signal = bool(context['status'] == 'available' and opinion and opinion['action'] == 'exit'
        and opinion['score'] >= 70 and weak)
    reason = ('直接提及该股票的风险/传闻报道待核对，暂停新增买入' if context.get('news_risks', {}).get(code)
        else context.get('market_reason', '') if context.get('market_action') == 'pause'
        else opinion['text'] if opinion else context['message'])
    ids = (set(opinion['evidence_ids']) if opinion else set()) | set(context.get('market_evidence_ids', []))
    return {'buy_allowed': bool(allowed), 'exit': exit_signal, 'score': opinion['score'] if opinion else 0,
        'reason': reason, 'opinion': deepcopy(opinion), 'window': context.get('window'),
        'as_of': context.get('as_of'), 'generated_at': context.get('generated_at'), 'model': context.get('model'),
        'version': VERSION, 'market_action': context.get('market_action'), 'market_reason': context.get('market_reason'),
        'news_risks': context.get('news_risks', {}).get(code, []),
        'evidence': [deepcopy(e) for e in context.get('evidence', []) if e['id'] in ids]}


def configured_policy(settings):
    return {'version': VERSION, 'enabled': settings.ai_trading_enabled, 'method': METHOD,
        'status': 'waiting' if settings.ai_trading_enabled else 'disabled', 'opinions': {},
        'message': '已启用AI与舆情参与，等待后续有效交易报价；启用前成交保持原始依据。'
            if settings.ai_trading_enabled else 'AI参与模拟交易未启用，沿用量价规则。'}
