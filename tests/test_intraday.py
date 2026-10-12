from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from stock_daily.config import SHANGHAI
from stock_daily.db import bars, sessions, signals, snapshots
from stock_daily.intraday import parse_quotes, run_snapshot
from stock_daily.providers import DataError

DAY = '20261012'
CAPTURED = datetime(2026, 10, 12, 11, 40, tzinfo=SHANGHAI)


def response(symbol='sh600000', date='2026-10-12', time='11:30:00', price='9.67'):
    fields = ['浦发银行', '9.59', '9.54', price, '9.70', '9.55', '0', '0', '39201752', '378644591']
    fields += ['0'] * 20 + [date, time, '00']
    return f'var hq_str_{symbol}="{",".join(fields)}";'


def test_quote_units_and_each_vendor_timestamp_are_preserved():
    rows, missing = parse_quotes(response(), {'600000.SH'}, DAY, 'midday', CAPTURED)
    assert not missing
    row = rows['600000.SH']
    assert row['volume'] == 39201752 and row['amount'] == 378644591
    assert row['pct'] == pytest.approx((9.67 / 9.54 - 1) * 100)
    assert row['quote_time'] == '2026-10-12T11:30:00+08:00'


@pytest.mark.parametrize('kwargs,slot', [
    ({'date':'2026-10-09'}, 'midday'), ({'time':'13:05:00'}, 'midday'),
    ({'time':'11:25:00'}, 'close'), ({'price':'NaN'}, 'midday'),
    ({'price':'-1'}, 'midday'), ({'price':'10.5'}, 'midday'),
    ({'time':'12:00:00'}, 'midday'),
])
def test_stale_wrong_period_future_and_invalid_quotes_are_excluded(kwargs, slot):
    rows, missing = parse_quotes(response(**kwargs), {'600000.SH'}, DAY, slot, CAPTURED)
    assert not rows and '600000.SH' in missing


def test_duplicates_rejected_and_absent_symbols_are_not_suspended():
    with pytest.raises(DataError, match='重复'):
        parse_quotes(response() * 2, {'600000.SH'}, DAY, 'midday', CAPTURED)
    _, missing = parse_quotes('', {'600000.SH'}, DAY, 'midday', CAPTURED)
    assert missing['600000.SH'] == '接口未返回该股票'


class Calendar:
    def calendar(self, start, end):
        return [DAY]


class Feed:
    def __init__(self, empty=False):
        self.empty = empty

    def fetch(self, stocks, day, slot):
        if self.empty:
            return {}, dict.fromkeys(stocks, '未返回')
        stamp = '11:30:00' if slot == 'midday' else '15:00:00'
        return {code: {'code':code, 'name':'<script>alert(1)</script>', 'price':10, 'pct':1,
                       'previous':9.9, 'open':10, 'high':10, 'low':10, 'volume':100,
                       'amount':1000, 'quote_time':f'2026-10-12T{stamp}+08:00'}
                for code in set(stocks) | {'000300.SH'}}, {}


def setup_real_metadata(populated, tmp_path, monkeypatch):
    db, settings, _ = populated
    saved = db.rows(select(sessions))[0]
    saved['source'] = 'baostock'
    saved['acquired_at'] = CAPTURED.isoformat()
    with db.engine.begin() as connection:
        db.upsert(connection, sessions, saved)
    monkeypatch.setattr('stock_daily.intraday.now', lambda: CAPTURED)
    return db, replace(settings, source='baostock', export_html=True, html_report_dir=str(tmp_path))


def counts(db):
    return [db.rows(select(func.count().label('n')).select_from(table))[0]['n'] for table in (bars, signals)]


def test_slots_save_separately_without_changing_daily_data_or_signals(populated, tmp_path, monkeypatch):
    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    before = counts(db)
    midday = run_snapshot(db, settings, DAY, 'midday', Feed(), Calendar())
    close = run_snapshot(db, settings, DAY, 'close', Feed(), Calendar())
    assert before == counts(db)
    assert len(db.rows(select(snapshots))) == 2
    assert db.latest_report('baostock', DAY) is None
    assert midday['html'].endswith('-20261012-1135.html')
    assert close['html'].endswith('-20261012-1505.html')
    assert Path(midday['html']).read_text() != Path(close['html']).read_text()
    assert (tmp_path / 'baostock-latest.html').read_text() == Path(close['html']).read_text()
    assert '<script>alert(1)</script>' not in Path(close['html']).read_text()


def test_failed_quote_validation_preserves_latest_artifact_and_snapshot(populated, tmp_path, monkeypatch):
    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    run_snapshot(db, settings, DAY, 'midday', Feed(), Calendar())
    previous = (tmp_path / 'baostock-latest.html').read_bytes()
    with pytest.raises(DataError, match='沪深300'):
        run_snapshot(db, settings, DAY, 'close', Feed(empty=True), Calendar())
    assert (tmp_path / 'baostock-latest.html').read_bytes() == previous
    assert len(db.rows(select(snapshots))) == 1


def test_realtime_does_not_fabricate_historical_snapshots(populated, tmp_path, monkeypatch):
    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    class HistoricalCalendar:
        def calendar(self, start, end):
            return [end]
    with pytest.raises(DataError, match='历史时段'):
        run_snapshot(db, settings, '20261009', 'midday', Feed(), HistoricalCalendar())
