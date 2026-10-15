from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.backtest import simulation_profile
from stock_daily.db import Database, bars, paper_accounts, paper_quote_batches, signals
from stock_daily.entry_policy import confirmation
from stock_daily.paper_trading import annotate_plan, current_account, execute_quotes
from stock_daily.trade_mail import compose_trade
from stock_daily.trade_plan import build_trade_plan
from stock_daily.wecom import trade_content


def batch(at=CURRENT, price=10.3, benchmark=101):
    payload = quotes(at)
    steps = int((at - CURRENT).total_seconds() // 60) + 10
    for code, q in payload['quotes'].items():
        q.update(volume=900000 + steps * 10000,
                 amount=(900000 + steps * 10000) * (100.5 if code == '000300.SH' else 10.25))
        q['price'] = benchmark if code == '000300.SH' else price
    return payload


def evaluation(db, settings, at=CURRENT):
    account = current_account(db, settings, '20261014', at)
    return next(e for e in account['entry_policy']['candidates'] if e['code'] == '600001.SH')


def test_warmup_then_live_confirmation_survives_restart_and_saves_fill_evidence(settings, db):
    report = setup(db, observed=False)
    plan = build_trade_plan(db, settings, report)
    first = execute_quotes(db, settings, batch(), CURRENT)
    assert first['new_fills'] == 1  # existing pending exit still executes during buy warmup
    assert evaluation(db, settings)['status'] == 'waiting'
    before = db.rows(select(paper_quote_batches))
    assert len(before) == 1 and before[0]['payload']['quotes']['600001.SH']['amount'] > 0
    restarted = Database(settings.database_url)
    later = CURRENT + timedelta(minutes=10)
    assert execute_quotes(restarted, settings, batch(later, 10.31), later)['new_fills'] == 1
    a = current_account(restarted, settings, report['day'], later)
    fill = a['fills'][0]
    assert fill['side'] == 'buy' and fill['execution_time'] == later.isoformat()
    assert fill['entry_confirmation']['previous_quote_time'] == CURRENT.isoformat()
    assert fill['entry_confirmation']['interval_volume'] == 100000
    assert fill['entry_confirmation']['status'] == 'eligible'
    overlay = annotate_plan(plan, a)
    assert overlay['entries'][0]['intraday']['status'] == 'filled'
    assert overlay['entry_policy']['evaluated_at'] == later.isoformat()
    assert plan['entries'][0].get('intraday') is None
    assert execute_quotes(restarted, settings, batch(later), later)['status'] == 'unchanged'
    assert len(restarted.rows(select(paper_quote_batches))) == 2
    restarted.engine.dispose()


@pytest.mark.parametrize('problem', ['weak_price', 'below_vwap', 'volume_reset', 'amount_reset',
    'no_trades', 'bad_amount', 'weak_relative', 'weak_market', 'missing_benchmark', 'stale_benchmark',
    'short_interval', 'long_gap', 'lunch_gap'])
def test_unconfirmed_or_unsafe_observations_wait_without_cancelling(settings, db, problem):
    setup(db, observed=False)
    first = CURRENT.replace(hour=11, minute=30) if problem == 'lunch_gap' else CURRENT
    execute_quotes(db, settings, batch(first), first)
    later = first + timedelta(minutes=10)
    if problem == 'short_interval':
        later = first + timedelta(minutes=1)
    elif problem == 'long_gap':
        later = first + timedelta(minutes=30)
    elif problem == 'lunch_gap':
        later = first.replace(hour=13, minute=0)
    payload = batch(later)
    q, b = payload['quotes']['600001.SH'], payload['quotes']['000300.SH']
    if problem == 'weak_price':
        q['price'] = 10.29
    elif problem == 'below_vwap':
        q['price'] = 10.24
    elif problem == 'volume_reset':
        q['volume'] = 100
    elif problem == 'amount_reset':
        q['amount'] = 100
    elif problem == 'no_trades':
        q.update(volume=batch(first)['quotes']['600001.SH']['volume'],
                 amount=batch(first)['quotes']['600001.SH']['amount'])
    elif problem == 'bad_amount':
        q['amount'] = float('nan')
    elif problem == 'weak_relative':
        b['price'] = 101.5
    elif problem == 'weak_market':
        b['price'] = 99.5
    elif problem == 'missing_benchmark':
        del payload['quotes']['000300.SH']
    elif problem == 'stale_benchmark':
        b['quote_time'] = first.isoformat()
    result = execute_quotes(db, settings, payload, later)
    assert result['new_fills'] == 0
    e = evaluation(db, settings, later)
    assert e['status'] == 'waiting'
    assert not current_account(db, settings, '20261014', later)['positions']


def test_intraday_invalidation_cancels_today_even_after_rebound_and_restart(settings, db):
    setup(db, observed=False)
    bad = batch()
    bad['quotes']['600001.SH'].update(price=9.4, low=9.3)
    execute_quotes(db, settings, bad, CURRENT)
    assert evaluation(db, settings)['status'] == 'cancelled'
    restarted = Database(settings.database_url)
    for minutes in (10, 20):
        at = CURRENT + timedelta(minutes=minutes)
        assert execute_quotes(restarted, settings, batch(at), at)['new_fills'] == 0
        assert evaluation(restarted, settings, at)['status'] == 'cancelled'
    restarted.engine.dispose()


def test_temporary_weakness_can_recover_after_two_stable_observations(settings, db):
    setup(db, observed=False)
    weak = batch(price=10.1)
    execute_quotes(db, settings, weak, CURRENT)
    later = CURRENT + timedelta(minutes=10)
    assert execute_quotes(db, settings, batch(later), later)['new_fills'] == 0
    later += timedelta(minutes=10)
    assert execute_quotes(db, settings, batch(later, 10.31), later)['new_fills'] == 1


def test_interval_liquidity_limits_fill_size(settings, db):
    setup(db)
    payload = batch()
    q = payload['quotes']['600001.SH']
    q.update(volume=900100, amount=900100 * 10.25)
    assert execute_quotes(db, settings, payload, CURRENT)['new_fills'] == 1  # sell only
    assert '流动性' in evaluation(db, settings)['reason']


def test_live_strength_changes_order_when_only_one_position_fits(settings, db, monkeypatch):
    setup(db, observed=False)
    config = replace(simulation_profile(), max_positions=1)
    monkeypatch.setattr('stock_daily.paper_trading.simulation_profile', lambda: config)
    with db.engine.begin() as c:
        row = db.rows(select(bars).where(bars.c.code == '600001.SH', bars.c.day == '20261014'))[0]
        db.upsert(c, bars, {**row, 'code': '600002.SH'})
        signal = db.rows(select(signals).where(signals.c.code == '600001.SH'))[0]
        db.upsert(c, signals, {**signal, 'code': '600002.SH',
            'payload': {**signal['payload'], 'relative20': -100}})
    first = CURRENT - timedelta(minutes=10)
    payload = batch(first)
    payload['quotes']['600002.SH'] = {**payload['quotes']['600001.SH'], 'code': '600002.SH'}
    execute_quotes(db, settings, payload, first)
    payload = batch(price=10.31)
    payload['quotes']['600002.SH'] = {**payload['quotes']['600001.SH'], 'code': '600002.SH', 'price': 10.4}
    execute_quotes(db, settings, payload, CURRENT)
    a = current_account(db, settings, '20261014', CURRENT)
    assert [p['code'] for p in a['positions']] == ['600002.SH']
    assert a['entry_policy']['candidates'][0]['code'] == '600002.SH'
    assert a['entry_policy']['candidates'][0]['decision_rank'] == 1


def test_future_and_other_source_batches_cannot_confirm_buy(settings, db):
    setup(db, observed=False)
    for source, at in [('other', CURRENT - timedelta(minutes=10)), ('demo', CURRENT + timedelta(minutes=10))]:
        with db.engine.begin() as c:
            db.upsert(c, paper_quote_batches, {'source': source, 'day': '20261015',
                'captured_at': at.isoformat(), 'payload': {'quotes': batch(at)['quotes']}})
    execute_quotes(db, settings, batch(), CURRENT)
    assert evaluation(db, settings)['status'] == 'waiting'


def test_paper_quotes_and_fills_rollback_together_on_notification_failure(settings, db, monkeypatch):
    setup(db)
    before = deepcopy(db.rows(select(paper_quote_batches)))
    def fail(*args):
        raise RuntimeError('atomic failure')
    monkeypatch.setattr('stock_daily.trade_mail.record_alerts', fail)
    with pytest.raises(RuntimeError):
        execute_quotes(db, settings, batch(), CURRENT)
    assert db.rows(select(paper_quote_batches)) == before


def test_missing_amount_cannot_confirm():
    previous = batch(CURRENT - timedelta(minutes=10))['quotes']
    current = batch()['quotes']
    del current['600001.SH']['amount']
    e = confirmation(current['600001.SH'], previous['600001.SH'], current['000300.SH'],
        previous['000300.SH'], stop=9.5, interval=10)
    assert e['status'] == 'waiting' and e['vwap'] is None


def test_new_entry_evidence_is_in_both_notification_channels(settings, db):
    setup(db)
    execute_quotes(db, settings, batch(), CURRENT)
    fill = current_account(db, settings, '20261014', CURRENT)['fills'][0]
    s = replace(settings, smtp_username='sender@example.com', mail_to='receiver@example.com')
    mail = compose_trade(s, {'id': 'test', 'payload': fill})
    assert 'entry-intraday-v1' in mail.get_body(preferencelist=('plain',)).get_content()
    assert '区间新增成交量' in mail.get_body(preferencelist=('plain',)).get_content()
    assert 'entry-intraday-v1' in trade_content(fill)
    assert '区间新增成交' in trade_content(fill)


def test_intraday_drawdown_pause_prevents_new_buy(settings, db):
    setup(db, observed=False)
    execute_quotes(db, settings, batch(), CURRENT)
    saved = db.rows(select(paper_accounts))[0]
    saved['payload']['account']['execution_state']['quote_peak_equity'] = 200000
    with db.engine.begin() as c:
        db.upsert(c, paper_accounts, saved)
    later = CURRENT + timedelta(minutes=10)
    assert execute_quotes(db, settings, batch(later), later)['new_fills'] == 0
    assert '回撤' in evaluation(db, settings, later)['reason']
