from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_entry_policy import batch
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.db import realtime_reports, trade_alerts
from stock_daily.decision_ai import decision_context
from stock_daily.paper_trading import current_account, execute_quotes


def save_analysis(db, settings, *, at=None, action='support', score=80, market='normal', status='available'):
    at = at or CURRENT - timedelta(minutes=1)
    evidence = [{'id': 'market', 'title': '行情', 'text': '市场观察'},
        *[{'id': 'stock-' + code, 'title': code, 'text': '该股报价与已发布信号'}
          for code in ('600000.SH', '600001.SH')]]
    policy = {'status': 'available', 'market_action': market, 'text': '已核对市场', 'evidence_ids': ['market'],
        'stocks': [{'code': code, 'action': action, 'score': score, 'text': '按证据判断，需量价确认',
            'evidence_ids': ['stock-' + code, 'market']} for code in ('600000.SH', '600001.SH')]}
    report = {'day': at.strftime('%Y%m%d'), 'window': at.strftime('%Y%m%d-%H%M'), 'source': settings.source,
        'captured_at': at.isoformat(), 'coverage': 100, 'evidence': evidence,
        'decision_universe': ['600000.SH', '600001.SH'], 'news': {'items': []},
        'ai': {'status': status, 'generated_at': (at + timedelta(seconds=10)).isoformat(),
            'as_of': at.isoformat(), 'model': 'test-model', 'trade_policy': policy}}
    def persist():
        with db.engine.begin() as c:
            db.upsert(c, realtime_reports, {'source': settings.source, 'day': report['day'],
                'window': report['window'], 'captured_at': report['captured_at'], 'payload': report})
    return report, persist


def account(db, settings, at=CURRENT):
    return current_account(db, settings, '20261014', at)


def test_ai_support_records_evidence_cash_and_exactly_once_alerts(db, settings):
    setup(db)
    config = replace(settings, ai_trading_enabled=True)
    report, save = save_analysis(db, config)
    save()
    result = execute_quotes(db, config, quotes(), CURRENT)
    assert result['new_fills'] == 2
    a = account(db, config)
    buy = a['fills'][0]
    assert buy['side'] == 'buy'
    assert buy['decision_context']['opinion']['action'] == 'support'
    assert buy['decision_context']['evidence'][0]['id'] == 'market'
    assert buy['decision_context']['generated_at'] < buy['confirmed_at']
    assert a['summary']['available_cash'] == pytest.approx(a['summary']['initial_capital'] + sum(f['cash_flow'] for f in a['fills']))
    alerts = db.rows(select(trade_alerts))
    assert alerts
    assert execute_quotes(db, config, quotes(), CURRENT)['status'] == 'unchanged'
    assert db.rows(select(trade_alerts)) == alerts
    assert account(db, config)['fills'] == a['fills']


@pytest.mark.parametrize('problem', ['missing', 'failed', 'pending', 'stale', 'future_generated', 'low_score',
    'wait', 'exit', 'pause', 'risk_news', 'unknown_reference', 'source_mismatch', 'low_coverage', 'missing_stock'])
def test_ai_failure_or_news_gate_never_blocks_required_risk_exit_but_blocks_buys(db, settings, problem):
    setup(db)
    config = replace(settings, ai_trading_enabled=True)
    r, save = save_analysis(db, config,
        at=CURRENT - timedelta(minutes=30) if problem == 'stale' else None,
        action=problem if problem in {'wait', 'exit'} else 'support',
        score=40 if problem == 'low_score' else 80,
        market='pause' if problem == 'pause' else 'normal',
        status=problem if problem in {'failed', 'pending'} else 'available')
    if problem == 'future_generated':
        r['ai']['generated_at'] = (CURRENT + timedelta(minutes=1)).isoformat()
    elif problem == 'risk_news':
        r['news']['items'] = [{'kind': '风险事项待核对', 'title': '该股风险公告',
            'mentioned_stocks': [{'code': '600001.SH'}], 'after_quote': False}]
    elif problem == 'unknown_reference':
        r['ai']['trade_policy']['stocks'][1]['evidence_ids'] = ['invented']
    elif problem == 'low_coverage':
        r['coverage'] = 50
    elif problem == 'missing_stock':
        r['ai']['trade_policy']['stocks'].pop()
    if problem == 'source_mismatch':
        r['source'] = 'baostock'
        with db.engine.begin() as c:
            db.upsert(c, realtime_reports, {'source': 'baostock', 'day': r['day'], 'window': r['window'],
                'captured_at': r['captured_at'], 'payload': r})
    elif problem != 'missing':
        save()
    execute_quotes(db, config, quotes(), CURRENT)
    a = account(db, config)
    assert not a['positions']  # Existing hard-risk exit executes while AI cannot authorise new buys.
    assert a['fills'][0]['side'] == 'sell'
    assert a['decision_context']['enabled']


def test_market_reduce_caps_size_without_changing_fee_cash_accounting(db, settings):
    setup(db)
    config = replace(settings, ai_trading_enabled=True)
    _, save = save_analysis(db, config, market='reduce')
    save()
    execute_quotes(db, config, quotes(), CURRENT)
    buy = account(db, config)['fills'][0]
    assert buy['side'] == 'buy'
    assert buy['entry_risk']['risk_budget'] == pytest.approx((account(db, config)['summary']['available_cash'] - buy['cash_flow']) * .0025)
    assert buy['decision_context']['market_action'] == 'reduce'


def test_ai_exit_requires_price_confirmation_and_preserves_t_plus_one(db, settings):
    setup(db)
    config = replace(settings, ai_trading_enabled=True)
    _, save = save_analysis(db, config)
    save()
    execute_quotes(db, config, quotes(), CURRENT)
    later = CURRENT + timedelta(minutes=10)
    _, save = save_analysis(db, config, at=later-timedelta(minutes=1), action='exit')
    save()
    execute_quotes(db, config, batch(later, price=10.19), later)
    a = account(db, config, later)
    assert a['positions'][0]['code'] == '600001.SH'
    assert 'AI与行情共振退出' in a['positions'][0]['exit_due']
    assert 'T+1' in a['paper_trading']['decisions']['600001.SH']
    assert len([f for f in a['fills'] if f['code'] == '600001.SH' and f['side'] == 'sell']) == 0


def test_ai_cannot_override_bad_quote_or_intraday_confirmation(db, settings):
    setup(db, observed=False)
    config = replace(settings, ai_trading_enabled=True)
    _, save = save_analysis(db, config)
    save()
    execute_quotes(db, config, quotes(), CURRENT)
    assert not account(db, config)['positions']  # First observation cannot buy even with AI support.


def test_newer_failed_snapshot_prevents_falling_back_to_older_good_ai(db, settings):
    config = replace(settings, ai_trading_enabled=True)
    _, save = save_analysis(db, config, at=CURRENT-timedelta(minutes=5))
    save()
    _, save = save_analysis(db, config, status='failed')
    save()
    assert decision_context(db, config, CURRENT)['status'] == 'waiting'


def test_ai_can_exit_an_overnight_holding_without_a_fixed_stop_trigger(db, settings):
    from sqlalchemy import update

    from stock_daily.db import signals

    setup(db)
    signal = db.rows(select(signals).where(signals.c.day == '20261013'))[0]
    signal['payload']['invalidation_adj'] = 9.0
    with db.engine.begin() as c:
        c.execute(update(signals).where(signals.c.day == '20261013').values(payload=signal['payload']))
    config = replace(settings, ai_trading_enabled=True)
    _, save = save_analysis(db, config, action='exit')
    save()
    q = quotes()
    q['quotes']['600000.SH']['price'] = 10.19  # Weak vs open, well above the fixed 9.0 stop.
    execute_quotes(db, config, q, CURRENT)
    sold = account(db, config)['fills'][0]
    assert sold['side'] == 'sell' and sold['code'] == '600000.SH'
    assert sold['price'] > 10
    assert 'AI与行情共振退出' in sold['reason']
    assert sold['decision_context']['opinion']['action'] == 'exit'
    assert sold['decision_context']['evidence']


def test_ai_score_changes_candidate_order_with_existing_quote_confirmation(db, settings):
    from copy import deepcopy

    from stock_daily.db import bars, paper_quote_batches, sessions, signals

    setup(db)
    config = replace(settings, ai_trading_enabled=True)
    r, save = save_analysis(db, config)
    r['decision_universe'].append('600002.SH')
    r['evidence'].append({'id': 'stock-600002.SH', 'title': '高排序候选', 'text': '已发布信号与报价'})
    r['ai']['trade_policy']['stocks'].append({'code': '600002.SH', 'action': 'support', 'score': 95,
        'text': '同样满足量价确认，证据判断强度更高', 'evidence_ids': ['stock-600002.SH']})
    save()
    with db.engine.begin() as c:
        for row in db.rows(select(bars).where(bars.c.code == '600001.SH')):
            db.upsert(c, bars, {**row, 'code': '600002.SH'})
        row = db.rows(select(signals).where(signals.c.day == '20261014', signals.c.code == '600001.SH'))[0]
        db.upsert(c, signals, {**row, 'code': '600002.SH'})
        row = db.rows(select(sessions).where(sessions.c.day == '20261014'))[0]
        row['universe']['600002.SH'] = {'name': '高排序候选'}
        db.upsert(c, sessions, row)
        row = db.rows(select(paper_quote_batches))[0]
        row['payload']['quotes']['600002.SH'] = {**row['payload']['quotes']['600001.SH'], 'code': '600002.SH'}
        db.upsert(c, paper_quote_batches, row)
    q = quotes()
    q['quotes']['600002.SH'] = deepcopy(q['quotes']['600001.SH'])
    q['quotes']['600002.SH']['code'] = '600002.SH'
    execute_quotes(db, config, q, CURRENT)
    a = account(db, config)
    assert a['entry_policy']['candidates'][0]['code'] == '600002.SH'
    bought = sorted((f for f in a['fills'] if f['side'] == 'buy' and f['day'] == '20261015'), key=lambda f:f['sequence'])
    assert bought[0]['code'] == '600002.SH'
