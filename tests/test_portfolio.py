from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from stock_daily.backtest import simulate
from stock_daily.config import SHANGHAI
from stock_daily.db import bars, reports, sessions, signals, trading_days
from stock_daily.intraday import parse_quotes
from stock_daily.operations import state
from stock_daily.portfolio import market_window, portfolio_due, refresh_portfolio, value_portfolio
from stock_daily.web import create_app

CURRENT = datetime(2026, 10, 14, 10, 40, tzinfo=SHANGHAI)


def seed(db, settings):
    source = settings.source
    with db.engine.begin() as connection:
        for i, day in enumerate(['20261012', '20261013']):
            db.upsert(connection, trading_days, {'source': source, 'day': day, 'is_open': 1})
            db.upsert(connection, sessions, {'source': source, 'day': day, 'quality': {'suspended': []},
                'benchmark': {'close': 100 + i}, 'universe': {'600000.SH': {'name': '观察公司'}},
                'events': [], 'event_status': 'disabled', 'acquired_at': CURRENT.isoformat()})
            db.upsert(connection, bars, {'source': source, 'day': day, 'code': '600000.SH',
                'open': 10, 'high': 11, 'low': 9.5, 'close': 10 + i * .1, 'adj': 1,
                'volume': 1e7, 'amount': 1e8, 'pct': 1})
        db.upsert(connection, trading_days, {'source': source, 'day': '20261014', 'is_open': 1})
        db.upsert(connection, signals, {'source': source, 'day': '20261012', 'code': '600000.SH',
            'strategy': 'breakout', 'payload': {'name': '观察公司', 'invalidation_adj': 1,
            'published_at': '2026-10-12T18:30:00+08:00'}})
    stamp = CURRENT.isoformat()
    db.save_state(source, 'portfolio_quotes', {'day': '20261014', 'captured_at': stamp,
        'quotes': {'600000.SH': {'price': 10.6, 'previous': 10.1, 'quote_time': stamp},
                   '000300.SH': {'price': 102, 'previous': 101, 'quote_time': stamp}}, 'missing': {}})


@pytest.mark.parametrize('clock,expected', [('09:29', None), ('09:30', '0930'), ('09:39', '0930'),
    ('09:40', '0940'), ('11:30', '1130'), ('11:31', None), ('12:59', None),
    ('13:00', '1300'), ('15:00', '1500'), ('15:05', 'close'), ('15:14', 'close'), ('15:15', None)])
def test_only_exchange_windows_and_final_close(clock, expected):
    hour, minute = map(int, clock.split(':'))
    result = market_window(CURRENT.replace(hour=hour, minute=minute))
    assert result == ('20261014-' + expected if expected else None)


def test_marks_do_not_mutate_daily_trades_cash_or_curve(settings, db):
    seed(db, settings)
    simulation = simulate(db, settings)
    original = deepcopy(simulation)
    result = value_portfolio(db, settings, simulation, CURRENT)
    assert result['status'] == 'available'
    shares = simulation['positions'][0]['shares']
    assert result['summary']['market_value'] == pytest.approx(shares * 10.6)
    assert result['summary']['available_cash'] == simulation['summary']['available_cash']
    assert result['summary']['equity'] == pytest.approx(shares * 10.6 + simulation['summary']['available_cash'])
    assert result['summary']['profit'] == pytest.approx(result['summary']['equity'] - 100000)
    assert result['summary']['benchmark_return'] == pytest.approx(2)
    assert result['basis_day'] == '20261013'
    assert simulation == original
    position = result['positions'][0]
    assert position['profit'] == pytest.approx(shares * 10.6 - position['spent'])
    assert position['market_value'] == pytest.approx(shares * 10.6)
    assert position['net_return'] == pytest.approx(position['profit'] / position['spent'] * 100)
    assert position['entry_price'] == original['positions'][0]['entry_price']
    assert position['held_sessions'] == original['positions'][0]['held_sessions']
    assert result['summary']['profit'] == pytest.approx(
        result['summary']['realized_profit'] + result['summary']['unrealized_profit'])
    assert result['positions'][0]['mark'] == 10.6
    assert simulate(db, settings) == original


@pytest.mark.parametrize('problem', ['missing', 'scale', 'gap', 'stopped', 'future', 'wrong_day'])
def test_incomplete_or_unsafe_marks_never_publish_partial_account(settings, db, problem):
    seed(db, settings)
    simulation = simulate(db, settings)
    saved = state(db, settings, 'portfolio_quotes')
    if problem == 'missing':
        saved['quotes'].pop('600000.SH')
    elif problem == 'scale':
        saved['quotes']['600000.SH']['previous'] = 5.05
    elif problem == 'gap':
        saved['day'] = '20261015'
        with db.engine.begin() as connection:
            db.upsert(connection, trading_days, {'source': settings.source, 'day': '20261015', 'is_open': 1})
    elif problem == 'stopped':
        simulation['stop'] = {'day': '20261014', 'reason': '缺失'}
    else:
        saved['quotes']['600000.SH']['quote_time'] = (
            CURRENT + timedelta(hours=1) if problem == 'future' else CURRENT - timedelta(days=1)).isoformat()
    db.save_state(settings.source, 'portfolio_quotes', saved)
    result = value_portfolio(db, settings, simulation, CURRENT)
    assert result['status'] == 'unavailable' and 'summary' not in result


def test_failed_worker_and_after_close_old_quotes_are_marked_stale(settings, db):
    seed(db, settings)
    simulation = simulate(db, settings)
    assert value_portfolio(db, settings, simulation, CURRENT + timedelta(minutes=16))['status'] == 'stale'
    assert value_portfolio(db, settings, simulation, CURRENT.replace(hour=18))['status'] == 'stale'
    saved = state(db, settings, 'portfolio_quotes')
    for q in saved['quotes'].values():
        q['quote_time'] = CURRENT.replace(hour=15, minute=0).isoformat()
    db.save_state(settings.source, 'portfolio_quotes', saved)
    assert value_portfolio(db, settings, simulation, CURRENT.replace(hour=18))['status'] == 'available'
    assert value_portfolio(db, settings, simulation, CURRENT + timedelta(days=1))['status'] == 'stale'


def test_api_live_is_explicit_and_custom_capital_is_respected(settings, db, monkeypatch):
    seed(db, settings)
    monkeypatch.setattr('stock_daily.portfolio.now', lambda: CURRENT)
    with TestClient(create_app(settings, db)) as client:
        original = client.get('/api/backtest?day=20261013').json()
        marked = client.get('/api/backtest?day=20261013&live=true&capital=50000').json()
        assert 'valuation' not in original
        assert marked['valuation']['status'] == 'available'
        assert marked['valuation']['summary']['initial_capital'] == 50000
        assert client.get('/api/backtest?day=20261012&live=true').json()['valuation']['status'] == 'unavailable'


def test_refresh_is_calendar_gated_persistent_and_never_writes_daily_or_mail(settings, db, monkeypatch):
    settings = replace(settings, source='baostock')
    seed(db, settings)
    before = {t.name: db.rows(select(t)) for t in (bars, signals, reports, sessions)}
    saved = state(db, settings, 'portfolio_quotes')
    class Feed:
        def fetch(self, codes, day, slot):
            assert codes == {'600000.SH'} and day == '20261014' and slot == 'portfolio'
            return saved['quotes'], {}
    def no_mail(*args, **kwargs):
        pytest.fail('盘中估值不发送邮件')
    monkeypatch.setattr('stock_daily.mailer.enqueue_safely', no_mail)
    assert portfolio_due(db, settings, CURRENT)
    assert refresh_portfolio(db, settings, Feed(), current=CURRENT)['status'] == 'success'
    saved = state(db, settings, 'portfolio_quotes')
    assert not portfolio_due(db, settings, CURRENT + timedelta(minutes=9))
    assert portfolio_due(db, settings, CURRENT + timedelta(minutes=10))
    class BrokenFeed:
        def fetch(self, *args):
            raise ConnectionError('offline')
    with pytest.raises(ConnectionError):
        refresh_portfolio(db, settings, BrokenFeed(), current=CURRENT + timedelta(minutes=10))
    assert state(db, settings, 'portfolio_quotes') == saved
    assert state(db, settings, 'portfolio_refresh')['status'] == 'failed'
    assert not portfolio_due(db, settings, CURRENT + timedelta(minutes=11))
    assert before == {t.name: db.rows(select(t)) for t in (bars, signals, reports, sessions)}
    with db.engine.begin() as connection:
        db.upsert(connection, trading_days, {'source': settings.source, 'day': '20261014', 'is_open': 0})
    assert refresh_portfolio(db, settings, BrokenFeed(), current=CURRENT + timedelta(minutes=20))['status'] == 'skipped'
    assert not portfolio_due(db, settings, CURRENT.replace(day=17))


def test_sina_portfolio_quotes_require_fresh_trading_timestamp():
    fields = ['浦发银行', '10', '10.1', '10.6', '10.7', '9.9', '0', '0', '10000', '100000']
    fields += ['0'] * 20 + ['2026-10-14', '10:40:00', '00']
    text = 'var hq_str_sh600000="' + ','.join(fields) + '";'
    quotes, missing = parse_quotes(text, {'600000.SH'}, '20261014', 'portfolio', CURRENT)
    assert not missing and quotes['600000.SH']['price'] == 10.6
    quotes, missing = parse_quotes(text, {'600000.SH'}, '20261014', 'portfolio', CURRENT + timedelta(minutes=11))
    assert not quotes and '过期' in missing['600000.SH']


def test_public_portfolio_updates_without_rewriting_daily_archive(populated, tmp_path, monkeypatch):
    import json
    import re

    from stock_daily.pages import export_pages

    db, settings, _ = populated
    captured = {'at': '2026-10-14T10:40:00+08:00'}
    def value(*args):
        return {'status': 'unavailable', 'day': '20261014', 'captured_at': captured['at'],
                'interval_minutes': 10, 'message': '持仓报价缺失，保留日线账户。'}
    monkeypatch.setattr('stock_daily.portfolio.value_portfolio', value)
    rendered = []
    def png(path):
        rendered.append(path.name)
        path.with_suffix('.png').write_bytes(b'fake-png')
    folder = tmp_path / 'pages'
    export_pages(db, settings, folder, render_image=png)
    archive = folder / 'reports' / 'demo-20261009-evening.html'
    portfolio = folder / 'reports' / 'demo-20261014-portfolio.html'
    original = archive.read_bytes()
    def payload(path):
        match = re.search(r'<script id="offline-snapshot" type="application/json">(.*?)</script>',
                          path.read_text(), re.S)
        return json.loads(match.group(1))
    assert 'valuation' not in payload(archive)['backtest']
    assert payload(portfolio)['portfolio'] is True
    assert '../portfolio.html' in portfolio.read_text()
    assert 'reports/demo-20261014-portfolio.html' in (folder / 'portfolio.html').read_text()
    rendered.clear()
    captured['at'] = '2026-10-14T10:50:00+08:00'
    export_pages(db, settings, folder, render_image=png)
    assert archive.read_bytes() == original
    assert rendered == ['demo-20261014-portfolio.html']
