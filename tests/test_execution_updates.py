from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.backtest import fees, simulation_profile
from stock_daily.db import Database, paper_accounts, signals, trade_alerts
from stock_daily.paper_trading import current_account, execute_quotes
from stock_daily.trade_mail import compose_trade
from stock_daily.wecom import trade_content


@pytest.mark.parametrize('atr', [.4, 1.0])
def test_new_position_sizes_risk_from_its_frozen_stop_with_atr_floor_and_loss_cap(settings, db, atr):
    setup(db)
    row = db.rows(select(signals).where(signals.c.day == '20261014'))[0]
    row['payload'].update(invalidation_adj=10.29, atr14=atr)
    with db.engine.begin() as connection:
        db.upsert(connection, signals, row)
    execute_quotes(db, settings, quotes(), CURRENT)
    account = current_account(db, settings, '20261014', CURRENT)
    position = account['positions'][0]
    state, risk = position['exit_policy'], position['entry_risk']
    entry = position['entry_price']
    distance = min(atr, entry * .06)
    assert state['version'] == risk['exit_policy_version'] == 'exit-v3'
    assert state['initial_stop'] == pytest.approx(entry - distance)
    assert risk['risk_stop'] == state['initial_stop']
    assert risk['risk_distance'] == pytest.approx(distance)
    assert risk['invalidation_price'] == 10.29  # The original signal reference remains available.
    expected = position['shares'] * distance + position['entry_fee'] + fees(
        position['shares'] * risk['risk_stop'], simulation_profile(), sell=True)
    assert risk['estimated_risk'] == pytest.approx(expected)
    assert risk['estimated_risk'] <= risk['risk_budget']
    fill = account['fills'][0]
    assert fill['entry_risk'] == risk and fill['exit_policy'] == state
    assert next(row['payload'] for row in db.rows(select(trade_alerts))
                if row['payload']['id'] == fill['id']) == fill
    assert account['summary']['available_cash'] == pytest.approx(
        account['summary']['initial_capital'] + sum(f['cash_flow'] for f in account['fills']))


def test_existing_v2_holding_keeps_its_initial_stop_and_parameters_after_restart(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    row = db.rows(select(paper_accounts))[0]
    position = row['payload']['account']['positions'][0]
    state = position['exit_policy']
    state.update(version='exit-v2', initial_stop=10.28, stop=10.28, min_initial_atr=0)
    frozen = {key: deepcopy(state[key]) for key in (
        'version', 'initial_stop', 'stop', 'atr', 'min_initial_atr', 'initial_atr',
        'trailing_atr', 'activate_r', 'max_loss', 'risk_unit')}
    fills = deepcopy(row['payload']['account']['fills'])
    cash = row['payload']['account']['summary']['available_cash']
    with db.engine.begin() as connection:
        db.upsert(connection, paper_accounts, row)
    restarted = Database(settings.database_url)
    later = CURRENT + timedelta(minutes=10)
    assert execute_quotes(restarted, settings, quotes(later), later)['new_fills'] == 0
    account = current_account(restarted, settings, '20261014', later)
    state = account['positions'][0]['exit_policy']
    assert {key: state[key] for key in frozen} == frozen
    assert account['fills'] == fills and account['summary']['available_cash'] == cash
    restarted.engine.dispose()


def test_sell_at_observed_low_executes_with_slippage_and_durable_notice_once(settings, db):
    setup(db)
    payload = quotes()
    payload['quotes']['600000.SH'].update(price=10.2, low=10.2)
    del payload['quotes']['600001.SH']
    assert execute_quotes(db, settings, payload, CURRENT)['new_fills'] == 1
    account = current_account(db, settings, '20261014', CURRENT)
    fill = account['fills'][0]
    assert fill['side'] == 'sell' and fill['code'] == '600000.SH'
    assert fill['price'] == pytest.approx(10.2 * .9995)
    assert fill['exit_slippage_below_low'] and fill['exit_observed_low'] == 10.2
    assert fill['price'] < fill['exit_observed_low']
    assert account['trades'][-1]['exit_slippage_below_low']
    assert account['summary']['available_cash'] == pytest.approx(
        account['summary']['initial_capital'] + sum(f['cash_flow'] for f in account['fills']))
    events = deepcopy(db.rows(select(trade_alerts)))
    assert len(events) == 1 and events[0]['payload'] == fill
    config = replace(settings, smtp_username='sender@example.com', mail_to='receiver@example.com')
    mail = compose_trade(config, events[0]).get_body(preferencelist=('plain',)).get_content()
    assert '低于已观察日内最低' in mail and '低于已观察日内最低' in trade_content(fill)
    assert execute_quotes(db, settings, payload, CURRENT)['status'] == 'unchanged'
    restarted = Database(settings.database_url)
    later = CURRENT + timedelta(minutes=10)
    followup = quotes(later)
    del followup['quotes']['600001.SH']
    assert execute_quotes(restarted, settings, followup, later)['new_fills'] == 0
    assert current_account(restarted, settings, '20261014', later)['fills'] == account['fills']
    assert db.rows(select(trade_alerts)) == events
    restarted.engine.dispose()


def test_limit_down_still_delays_sell_even_without_low_price_slippage_gate(settings, db):
    setup(db)
    payload = quotes()
    payload['quotes']['600000.SH'].update(price=9.18, low=9.18)
    del payload['quotes']['600001.SH']
    result = execute_quotes(db, settings, payload, CURRENT)
    assert result['new_fills'] == 0 and '跌停' in result['decisions']['600000.SH']
    assert db.rows(select(trade_alerts)) == []
    later = CURRENT + timedelta(minutes=10)
    payload = quotes(later)
    payload['quotes']['600000.SH'].update(price=9.6, low=9.6)
    del payload['quotes']['600001.SH']
    assert execute_quotes(db, settings, payload, later)['new_fills'] == 1
    fill = current_account(db, settings, '20261014', later)['fills'][0]
    assert fill['side'] == 'sell' and fill['exit_slippage_below_low']
