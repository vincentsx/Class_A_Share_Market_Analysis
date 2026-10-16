from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_paper_trading import CURRENT, close_day, quotes, setup

from stock_daily.backtest import simulate
from stock_daily.db import paper_accounts
from stock_daily.paper_trading import current_account, execute_quotes
from stock_daily.performance import account_pnl


@pytest.mark.parametrize('curve,day,equity,basis,basis_day', [
    ([], '20261009', 99990, 100000, None),
    ([{'day': '20261009', 'equity': 100100}], '20261012', 100250, 100100, '20261009'),
    ([{'day': '20261015', 'equity': 102000}, {'day': '20261016', 'equity': 102100},
      {'day': '20261016', 'equity': 102200}], '20261016', 102300, 102000, '20261015'),
    ([{'day': '20261015', 'equity': 102000}], '20261016', 101900, 102000, '20261015'),
])
def test_daily_pnl_uses_preceding_close_and_cumulative_uses_initial_capital(curve, day, equity, basis, basis_day):
    summary = {'through': day, 'equity': equity, 'initial_capital': 100000}
    before = deepcopy((summary, curve))
    pnl = account_pnl(summary, curve)
    assert pnl['daily_profit'] == pytest.approx(equity - basis)
    assert pnl['daily_return'] == pytest.approx((equity / basis - 1) * 100)
    assert pnl['daily_profit_basis_day'] == basis_day
    assert pnl['daily_profit_basis_equity'] == basis
    assert pnl['cumulative_profit'] == pytest.approx(equity - 100000)
    assert pnl['cumulative_return'] == pytest.approx((equity / 100000 - 1) * 100)
    assert (summary, curve) == before


def test_missing_equity_never_becomes_zero_profit():
    pnl = account_pnl({'through': None, 'equity': None, 'initial_capital': 100000}, [])
    assert pnl['daily_profit'] is None and pnl['cumulative_profit'] is None


def test_live_pnl_includes_fees_and_keeps_same_close_baseline_across_quote_updates(settings, db):
    setup(db)
    previous = simulate(db, settings, '20261014')['summary']['equity']
    execute_quotes(db, settings, quotes(), CURRENT)
    first = current_account(db, settings, '20261014', CURRENT)
    summary = first['summary']
    assert summary['daily_profit'] == pytest.approx(summary['equity'] - previous)
    assert summary['daily_profit_basis_day'] == '20261014'
    assert summary['cumulative_profit'] == pytest.approx(summary['realized_profit'] + summary['unrealized_profit'])
    assert summary['daily_profit'] != pytest.approx(first['fills'][1]['realized_profit'])
    later = CURRENT + timedelta(minutes=10)
    batch = quotes(later)
    batch['quotes']['600001.SH']['price'] = 10.6
    execute_quotes(db, settings, batch, later)
    before = db.rows(select(paper_accounts))
    updated = current_account(db, settings, '20261014', later)
    assert updated['summary']['daily_profit_basis_equity'] == pytest.approx(previous)
    assert updated['summary']['daily_profit'] == pytest.approx(updated['summary']['equity'] - previous)
    assert updated['fills'] == first['fills']
    assert db.rows(select(paper_accounts)) == before


def test_close_reconciliation_and_historical_read_use_same_daily_pnl(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    close_day(db, price=10.5)
    current = current_account(db, settings, '20261015', CURRENT.replace(hour=18))
    historical = simulate(db, settings, '20261015')
    assert current['summary']['daily_profit'] == historical['summary']['daily_profit']
    assert historical['summary']['daily_profit_basis_day'] == '20261014'
    assert historical['summary']['cumulative_profit'] == pytest.approx(historical['summary']['profit'])
