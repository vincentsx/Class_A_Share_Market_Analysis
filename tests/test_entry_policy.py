from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone

import pytest
from sqlalchemy import select
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.backtest import simulation_profile
from stock_daily.db import Database, bars, paper_accounts, paper_quote_batches, signals, trade_alerts
from stock_daily.entry_policy import PULLBACK_TOLERANCE, VERSION, confirmation
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


@pytest.mark.parametrize('price', [9.4, 9.5])
def test_intraday_invalidation_pauses_then_requires_two_recovered_observations_after_restart(settings, db, price):
    setup(db, observed=False)
    bad = batch()
    bad['quotes']['600001.SH'].update(price=price, low=9.3)
    execute_quotes(db, settings, bad, CURRENT)
    assert evaluation(db, settings)['status'] == 'paused'
    saved = db.rows(select(paper_accounts))[0]['payload']
    key = '20261014|600001.SH|breakout'
    assert key not in saved['attempts']
    confirmed = deepcopy(saved['account']['fills'])
    alerts_before = deepcopy(db.rows(select(trade_alerts)))
    restarted = Database(settings.database_url)
    first_recovery = CURRENT + timedelta(minutes=10)
    assert execute_quotes(restarted, settings, batch(first_recovery), first_recovery)['new_fills'] == 0
    assert evaluation(restarted, settings, first_recovery)['status'] == 'waiting'
    assert db.rows(select(trade_alerts)) == alerts_before
    second_recovery = first_recovery + timedelta(minutes=10)
    payload = batch(second_recovery, 10.31)
    assert execute_quotes(restarted, settings, payload, second_recovery)['new_fills'] == 1
    account = current_account(restarted, settings, '20261014', second_recovery)
    assert account['fills'][1:] == confirmed
    buy = account['fills'][0]
    assert buy['side'] == 'buy' and buy['code'] == '600001.SH'
    assert buy['entry_confirmation']['version'] == VERSION == 'entry-intraday-v3'
    assert buy['entry_confirmation']['previous_quote_time'] == first_recovery.isoformat()
    assert buy['entry_confirmation']['quote_time'] == second_recovery.isoformat()
    assert account['summary']['available_cash'] == pytest.approx(
        account['summary']['initial_capital'] + sum(fill['cash_flow'] for fill in account['fills']))
    assert account['summary']['fees'] == pytest.approx(sum(fill['fee'] for fill in account['fills']))
    alerts = deepcopy(db.rows(select(trade_alerts)))
    assert len(alerts) == len(alerts_before) + 1
    assert next(row['payload'] for row in alerts if row['payload']['id'] == buy['id']) == buy
    assert execute_quotes(restarted, settings, payload, second_recovery)['status'] == 'unchanged'
    later = second_recovery + timedelta(minutes=10)
    assert execute_quotes(restarted, settings, batch(later, 10.32), later)['new_fills'] == 0
    assert current_account(restarted, settings, '20261014', later)['fills'] == account['fills']
    assert db.rows(select(trade_alerts)) == alerts
    restarted.engine.dispose()


@pytest.mark.parametrize('opening', [9.4, 9.5])
def test_open_below_or_at_invalidation_can_recover_without_rewriting_open(settings, db, opening):
    setup(db, observed=False)
    first = batch()
    first['quotes']['600001.SH'].update(open=opening, price=opening, low=9.3)
    execute_quotes(db, settings, first, CURRENT)
    assert evaluation(db, settings)['status'] == 'paused'
    for minutes, expected in [(10, 0), (20, 1)]:
        at = CURRENT + timedelta(minutes=minutes)
        payload = batch(at, 10.3 + minutes / 1000)
        payload['quotes']['600001.SH'].update(open=opening, low=9.3)
        assert execute_quotes(db, settings, payload, at)['new_fills'] == expected
    account = current_account(db, settings, '20261014', at)
    assert account['fills'][0]['open_price'] == opening
    assert evaluation(db, settings, at)['status'] == 'filled'


def test_open_above_gap_limit_remains_cancelled_after_price_returns(settings, db):
    setup(db, observed=False)
    for minutes in (0, 10, 20):
        at = CURRENT + timedelta(minutes=minutes)
        payload = batch(at)
        payload['quotes']['600001.SH'].update(open=10.6, high=10.8)
        result = execute_quotes(db, settings, payload, at)
        assert not any(fill['side'] == 'buy' for fill in current_account(
            db, settings, '20261014', at)['fills'] if fill['day'] == '20261015')
        assert evaluation(db, settings, at)['status'] == 'cancelled'
        assert '追高上限' in evaluation(db, settings, at)['reason']
        if minutes:
            assert result['new_fills'] == 0


def test_stock_sold_today_remains_cancelled_after_recovery(settings, db):
    setup(db, observed=False)
    with db.engine.begin() as connection:
        candidate = db.rows(select(signals).where(signals.c.day == '20261014'))[0]
        db.upsert(connection, signals, {**candidate, 'code': '600000.SH'})
    for minutes in (0, 10, 20):
        at = CURRENT + timedelta(minutes=minutes)
        execute_quotes(db, settings, batch(at, 10.3 + minutes / 1000), at)
    account = current_account(db, settings, '20261014', at)
    same_stock = [fill for fill in account['fills'] if fill['code'] == '600000.SH'
        and fill['day'] == '20261015']
    assert len(same_stock) == 1 and same_stock[0]['side'] == 'sell'
    evaluation_ = next(item for item in account['entry_policy']['candidates'] if item['code'] == '600000.SH')
    assert evaluation_['status'] == 'cancelled' and '当日已退出' in evaluation_['reason']


@pytest.mark.parametrize('old_reason', [
    '盘中已达到信号失效位置，取消当日入场',
    '开盘已达到失效位置，取消本次入场',
])
def test_v1_invalidation_attempt_migrates_once_and_requires_fresh_v3_observations(settings, db, old_reason):
    setup(db, observed=False)
    previous = CURRENT - timedelta(minutes=10)
    execute_quotes(db, settings, batch(previous), previous)
    row = db.rows(select(paper_accounts))[0]
    record = row['payload']
    key = '20261014|600001.SH|breakout'
    retained = {
        '20261014|600002.SH|breakout': '开盘涨幅超过追高上限，取消本次入场',
        '20261014|600003.SH|breakout': 'filled',
        '20261014|600004.SH|breakout': '当日已退出，取消重复入场',
        '20261014|600005.SH|breakout': '排除事后发布、时间不明或重建信号',
    }
    record['attempts'] = {key: old_reason, **retained}
    record['account']['entry_policy']['version'] = 'entry-intraday-v1'
    record['account']['entry_policy'].pop('migration', None)
    old_batch = db.rows(select(paper_quote_batches))[0]
    old_batch['payload']['version'] = 'entry-intraday-v1'
    with db.engine.begin() as connection:
        db.upsert(connection, paper_accounts, row)
        db.upsert(connection, paper_quote_batches, old_batch)
    before = deepcopy(record['account'])
    alerts_before = deepcopy(db.rows(select(trade_alerts)))
    assert execute_quotes(db, settings, batch(), CURRENT)['new_fills'] == 0
    account = current_account(db, settings, '20261014', CURRENT)
    assert evaluation(db, settings)['status'] == 'waiting'
    assert account['fills'] == before['fills'] and account['trades'] == before['trades']
    assert account['summary']['available_cash'] == before['summary']['available_cash']
    assert db.rows(select(trade_alerts)) == alerts_before
    migration = deepcopy(account['entry_policy']['migration'])
    assert migration == {
        'from_version': 'entry-intraday-v1', 'to_version': 'entry-intraday-v3',
        'activated_at': CURRENT.isoformat(), 'released_attempts': [{'key': key, 'reason': old_reason}],
    }
    migrated = db.rows(select(paper_accounts))[0]['payload']
    assert migrated['attempts'] == retained
    assert execute_quotes(db, settings, batch(), CURRENT)['status'] == 'unchanged'
    restarted = Database(settings.database_url)
    later = CURRENT + timedelta(minutes=10)
    assert execute_quotes(restarted, settings, batch(later, 10.31), later)['new_fills'] == 1
    account = current_account(restarted, settings, '20261014', later)
    assert account['entry_policy']['migration'] == migration
    assert account['fills'][0]['entry_confirmation']['previous_quote_time'] == CURRENT.isoformat()
    assert account['fills'][0]['entry_confirmation']['version'] == 'entry-intraday-v3'
    assert account['fills'][1:] == before['fills']
    assert account['summary']['available_cash'] == pytest.approx(account['summary']['initial_capital']
        + sum(fill['cash_flow'] for fill in account['fills']))
    attempts = db.rows(select(paper_accounts))[0]['payload']['attempts']
    assert attempts == {key: 'filled', **retained}
    restarted.engine.dispose()


def test_unversioned_observation_cannot_authorise_first_v3_buy(settings, db):
    setup(db)
    old_batch = db.rows(select(paper_quote_batches))[0]
    old_batch['payload'].pop('version', None)
    with db.engine.begin() as connection:
        db.upsert(connection, paper_quote_batches, old_batch)
    assert execute_quotes(db, settings, batch(), CURRENT)['new_fills'] == 1  # only the existing exit
    assert evaluation(db, settings)['status'] == 'waiting'
    later = CURRENT + timedelta(minutes=10)
    assert execute_quotes(db, settings, batch(later, 10.31), later)['new_fills'] == 1


def test_recovery_requires_previous_price_strictly_above_invalidation():
    previous = batch(CURRENT - timedelta(minutes=10))['quotes']
    current = batch()['quotes']
    for rows in (previous, current):
        quote = rows['600001.SH']
        quote.update(open=9.4, low=9.3, amount=quote['volume'] * 9.5)
    previous['600001.SH']['price'] = 9.5
    result = confirmation(current['600001.SH'], previous['600001.SH'], current['000300.SH'],
        previous['000300.SH'], stop=9.5, interval=10)
    assert result['status'] == 'waiting'


@pytest.mark.parametrize('gap', [8, 10, 22])
def test_two_continuous_quotes_below_stop_cancel_without_benchmark(gap):
    previous = batch(CURRENT - timedelta(minutes=gap), price=9.4)['quotes']['600001.SH']
    current = batch(price=9.3)['quotes']['600001.SH']
    result = confirmation(current, previous, None, None, stop=9.5, interval=10)
    assert result['status'] == 'cancelled'
    assert '连续两次' in result['reason']
    assert result['previous_quote_time'] == previous['quote_time']


@pytest.mark.parametrize('problem', ['missing', 'short', 'long', 'lunch', 'other_day', 'open_scale', 'previous_scale'])
def test_below_stop_quotes_require_same_session_continuity(problem):
    previous = batch(CURRENT - timedelta(minutes=10), price=9.4)['quotes']['600001.SH']
    current = batch(price=9.3)['quotes']['600001.SH']
    if problem == 'missing':
        previous = None
    elif problem == 'short':
        previous['quote_time'] = (CURRENT - timedelta(minutes=7, seconds=59)).isoformat()
    elif problem == 'long':
        previous['quote_time'] = (CURRENT - timedelta(minutes=22, seconds=1)).isoformat()
    elif problem == 'lunch':
        previous['quote_time'] = CURRENT.replace(hour=11, minute=30).isoformat()
        current['quote_time'] = CURRENT.replace(hour=13, minute=0).isoformat()
    elif problem == 'other_day':
        previous['quote_time'] = (CURRENT - timedelta(days=1, minutes=10)).isoformat()
    elif problem == 'open_scale':
        previous['open'] += .02
    elif problem == 'previous_scale':
        previous['previous'] += .02
    result = confirmation(current, previous, None, None, stop=9.5, interval=10)
    assert result['status'] == 'paused'


@pytest.mark.parametrize(('prior_price', 'price'), [(9.4, 9.5), (9.5, 9.4), (9.5, 9.5)])
def test_equal_stop_is_paused_and_never_counts_as_strictly_below(prior_price, price):
    previous = batch(CURRENT - timedelta(minutes=10), price=prior_price)['quotes']['600001.SH']
    current = batch(price=price)['quotes']['600001.SH']
    assert confirmation(current, previous, None, None, stop=9.5, interval=10)['status'] == 'paused'


@pytest.mark.parametrize(('clock', 'price', 'status'), [
    ((14, 29, 59), 9.4, 'paused'), ((14, 30, 0), 9.4, 'cancelled'),
    ((14, 30, 0), 9.5, 'paused'), ((15, 0, 0), 9.4, 'cancelled'), ((15, 0, 0), 9.5, 'paused'),
])
@pytest.mark.parametrize('utc', [False, True])
def test_afternoon_cancellation_uses_beijing_quote_time_and_strict_stop(clock, price, status, utc):
    at = CURRENT.replace(hour=clock[0], minute=clock[1], second=clock[2])
    quote = batch(at, price=price)['quotes']['600001.SH']
    if utc:
        quote['quote_time'] = at.astimezone(timezone.utc).isoformat()
    result = confirmation(quote, None, None, None, stop=9.5, interval=10)
    assert result['status'] == status
    if status == 'cancelled':
        assert '14:30' in result['reason']


@pytest.mark.parametrize('cause', ['consecutive', 'late'])
def test_confirmed_invalidation_cancels_day_durably_without_changing_fills_cash_or_notifications(settings, db, cause):
    setup(db, observed=False)
    first = CURRENT if cause == 'consecutive' else CURRENT.replace(hour=14, minute=29)
    second = first + timedelta(minutes=10 if cause == 'consecutive' else 1)
    payload = batch(first)
    payload['quotes']['600001.SH'].update(price=9.4, low=9.3)
    execute_quotes(db, settings, payload, first)
    assert evaluation(db, settings, first)['status'] == 'paused'
    original = deepcopy(current_account(db, settings, '20261014', first))
    alerts = deepcopy(db.rows(select(trade_alerts)))
    restarted = Database(settings.database_url)
    payload = batch(second)
    payload['quotes']['600001.SH'].update(price=9.4, low=9.3)
    del payload['quotes']['000300.SH']
    assert execute_quotes(restarted, settings, payload, second)['new_fills'] == 0
    assert evaluation(restarted, settings, second)['status'] == 'cancelled'
    key = '20261014|600001.SH|breakout'
    reason = db.rows(select(paper_accounts))[0]['payload']['attempts'][key]
    assert ('连续两次' if cause == 'consecutive' else '14:30') in reason
    assert execute_quotes(restarted, settings, payload, second)['status'] == 'unchanged'
    rebound_at = second + timedelta(minutes=10)
    assert execute_quotes(restarted, settings, batch(rebound_at), rebound_at)['new_fills'] == 0
    account = current_account(restarted, settings, '20261014', rebound_at)
    assert evaluation(restarted, settings, rebound_at)['status'] == 'cancelled'
    assert account['fills'] == original['fills'] and account['trades'] == original['trades']
    assert account['summary']['available_cash'] == original['summary']['available_cash']
    assert db.rows(select(trade_alerts)) == alerts
    restarted.engine.dispose()


def test_equal_stop_interrupts_below_stop_sequence_and_recovery_still_needs_two_quotes(settings, db):
    setup(db, observed=False)
    for minutes, price in [(0, 9.4), (10, 9.5), (20, 9.4), (30, 10.3), (40, 10.31)]:
        at = CURRENT + timedelta(minutes=minutes)
        payload = batch(at)
        payload['quotes']['600001.SH'].update(price=price, low=9.3)
        execute_quotes(db, settings, payload, at)
        status = evaluation(db, settings, at)['status']
        assert status == ('filled' if minutes == 40 else 'waiting' if minutes == 30 else 'paused')


def test_v2_below_stop_observation_cannot_retroactively_confirm_v3_cancellation(settings, db):
    setup(db, observed=False)
    first = batch()
    first['quotes']['600001.SH'].update(price=9.4, low=9.3)
    execute_quotes(db, settings, first, CURRENT)
    row = db.rows(select(paper_accounts))[0]
    row['payload']['account']['entry_policy']['version'] = 'entry-intraday-v2'
    old_batch = db.rows(select(paper_quote_batches))[0]
    old_batch['payload']['version'] = 'entry-intraday-v2'
    with db.engine.begin() as connection:
        db.upsert(connection, paper_accounts, row)
        db.upsert(connection, paper_quote_batches, old_batch)
    for minutes, status in [(10, 'paused'), (20, 'cancelled')]:
        at = CURRENT + timedelta(minutes=minutes)
        payload = batch(at)
        payload['quotes']['600001.SH'].update(price=9.4, low=9.3)
        execute_quotes(db, settings, payload, at)
        assert evaluation(db, settings, at)['status'] == status


@pytest.mark.parametrize(('drop', 'status'), [(0, 'eligible'), (.001, 'eligible'), (.002, 'eligible'), (.00201, 'waiting')])
def test_small_pullback_tolerance_preserves_relative_strength_requirement(drop, status):
    previous = batch(CURRENT - timedelta(minutes=10))['quotes']
    current = batch(price=10.3 * (1 - drop), benchmark=101 * (1 - drop))['quotes']
    result = confirmation(current['600001.SH'], previous['600001.SH'], current['000300.SH'],
        previous['000300.SH'], stop=9.5, interval=10)
    assert result['status'] == status
    assert result['max_quote_pullback'] == PULLBACK_TOLERANCE == .002
    if status == 'waiting':
        assert '0.2%' in result['reason']


def test_allowed_small_pullback_still_rejects_underperformance_of_flat_benchmark():
    previous = batch(CURRENT - timedelta(minutes=10))['quotes']
    current = batch(price=10.3 * .999)['quotes']
    result = confirmation(current['600001.SH'], previous['600001.SH'], current['000300.SH'],
        previous['000300.SH'], stop=9.5, interval=10)
    assert result['status'] == 'waiting' and '弱于沪深300' in result['reason']


@pytest.mark.parametrize('guard', ['open', 'vwap', 'stop', 'weak_market'])
def test_small_pullback_does_not_override_other_entry_guards(guard):
    previous = batch(CURRENT - timedelta(minutes=10))['quotes']
    current = batch(price=10.3 * .998, benchmark=101 * .998)['quotes']
    stop = 9.5
    if guard == 'open':
        for rows in (previous, current):
            rows['600001.SH']['open'] = 10.29
    elif guard == 'vwap':
        current['600001.SH']['amount'] = current['600001.SH']['volume'] * 10.29
    elif guard == 'stop':
        stop = 10.28
    elif guard == 'weak_market':
        current['000300.SH']['price'] = 99.9
    result = confirmation(current['600001.SH'], previous['600001.SH'], current['000300.SH'],
        previous['000300.SH'], stop=stop, interval=10)
    assert result['status'] == ('paused' if guard == 'stop' else 'waiting')


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
                'captured_at': at.isoformat(), 'payload': {'quotes': batch(at)['quotes'], 'version': VERSION}})
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
    assert 'entry-intraday-v3' in mail.get_body(preferencelist=('plain',)).get_content()
    assert '区间新增成交量' in mail.get_body(preferencelist=('plain',)).get_content()
    assert 'entry-intraday-v3' in trade_content(fill)
    assert '区间新增成交' in trade_content(fill)


@pytest.mark.parametrize('price', [10.3, 9.4])
def test_intraday_drawdown_pause_prevents_new_buy(settings, db, price):
    setup(db, observed=False)
    execute_quotes(db, settings, batch(), CURRENT)
    saved = db.rows(select(paper_accounts))[0]
    saved['payload']['account']['execution_state']['quote_peak_equity'] = 200000
    with db.engine.begin() as c:
        db.upsert(c, paper_accounts, saved)
    later = CURRENT + timedelta(minutes=10)
    payload = batch(later)
    payload['quotes']['600001.SH'].update(price=price, low=9.3)
    assert execute_quotes(db, settings, payload, later)['new_fills'] == 0
    assert '回撤' in evaluation(db, settings, later)['reason']
    assert evaluation(db, settings, later)['status'] == 'waiting'


def test_held_quote_error_takes_precedence_over_temporary_price_pause(settings, db):
    setup(db, observed=False)
    payload = batch()
    payload['quotes']['600001.SH'].update(price=9.4, low=9.3)
    payload['quotes']['600000.SH']['quote_time'] = (CURRENT - timedelta(minutes=3)).isoformat()
    assert execute_quotes(db, settings, payload, CURRENT)['new_fills'] == 0
    e = evaluation(db, settings)
    assert e['status'] == 'waiting' and '持仓报价待核验' in e['reason']


def test_another_dip_resets_the_two_observation_recovery(settings, db):
    setup(db, observed=False)
    for minutes, price in [(0, 9.4), (10, 10.3), (20, 9.4), (30, 10.3), (40, 10.31)]:
        at = CURRENT + timedelta(minutes=minutes)
        payload = batch(at)
        payload['quotes']['600001.SH'].update(price=price, low=9.3)
        execute_quotes(db, settings, payload, at)
        account = current_account(db, settings, '20261014', at)
        buys = [f for f in account['fills'] if f['day'] == '20261015' and f['side'] == 'buy']
        assert len(buys) == (1 if minutes == 40 else 0)
