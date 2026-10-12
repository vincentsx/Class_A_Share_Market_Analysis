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


def test_missing_evening_does_not_turn_old_history_into_yesterday(populated, tmp_path, monkeypatch):
    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    class FreshCalendar:
        def calendar(self, start, end):
            assert start < '20261008'
            return ['20261008', '20261009', DAY]
    run_snapshot(db, settings, DAY, 'midday', Feed(), FreshCalendar())
    from stock_daily.db import trading_days
    dates = db.rows(select(trading_days.c.day).where(trading_days.c.source == 'baostock'))
    assert {r['day'] for r in dates} == {'20261008', '20261009', DAY}


def test_same_day_report_does_not_hide_the_earlier_signal_reference(populated, tmp_path, monkeypatch):
    from copy import deepcopy

    from stock_daily.db import reports

    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    previous = deepcopy(db.latest_report('demo'))
    with db.engine.begin() as connection:
        db.upsert(connection, reports, {'source':'baostock', 'day':previous['day'], 'payload':previous})
        current = {**previous, 'day':DAY, 'candidates':[]}
        db.upsert(connection, reports, {'source':'baostock', 'day':DAY, 'payload':current})
    run_snapshot(db, settings, DAY, 'midday', Feed(), Calendar())
    result = db.rows(select(snapshots.c.payload))[0]['payload']
    assert result['reference_day'] == '20261009'
    assert result['analysis']['version'] == 'intraday-v2'
    assert all('volume_ratio' in r for r in result['focus'])


def test_old_saved_snapshot_upgrade_preserves_quotes_time_and_storage(populated, tmp_path, monkeypatch):
    import json
    import re

    from stock_daily.intraday import export_saved_snapshot

    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    run_snapshot(db, settings, DAY, 'midday', Feed(), Calendar())
    stored = db.rows(select(snapshots))[0]
    legacy = stored['payload']
    legacy.pop('analysis')
    stored['payload'] = legacy
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, stored)
    result = export_saved_snapshot(db, settings, DAY, 'midday')
    content = Path(result['html']).read_text()
    upgraded = json.loads(re.search(r'<script id="quote-snapshot" type="application/json">(.*?)</script>', content, re.S).group(1))
    assert upgraded['analysis']['version'] == 'intraday-v2'
    assert upgraded['captured_at'] == legacy['captured_at']
    assert [(r['code'], r['price'], r['volume'], r['quote_time']) for r in upgraded['quotes']] == [
        (r['code'], r['price'], r['volume'], r['quote_time']) for r in legacy['quotes']]
    assert 'analysis' not in db.rows(select(snapshots))[0]['payload']


def test_news_failure_does_not_block_valid_quotes_or_write_daily_data(populated, tmp_path, monkeypatch):
    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    settings = replace(settings, news_enabled=True)
    monkeypatch.setattr('stock_daily.news.now', lambda: CAPTURED)
    class BrokenNews:
        def fetch(self, provider, captured):
            raise ConnectionError('news unavailable')
    before = counts(db)
    result = run_snapshot(db, settings, DAY, 'midday', Feed(), Calendar(), news_feed=BrokenNews())
    assert result['status'] == 'success'
    assert counts(db) == before
    saved = db.rows(select(snapshots.c.payload))[0]['payload']
    assert saved['news']['status'] == 'unavailable'
    assert saved['news']['items'] == []


def test_news_supplement_preserves_market_capture_and_never_sends_mail(populated, tmp_path, monkeypatch):
    from stock_daily.news import collect_news, refresh_snapshot_news

    db, settings = setup_real_metadata(populated, tmp_path, monkeypatch)
    close_time = datetime(2026, 10, 12, 15, 5, tzinfo=SHANGHAI)
    news_time = datetime(2026, 10, 12, 16, 30, tzinfo=SHANGHAI)
    monkeypatch.setattr('stock_daily.intraday.now', lambda: close_time)
    run_snapshot(db, settings, DAY, 'close', Feed(), Calendar())
    original = db.rows(select(snapshots.c.payload))[0]['payload']
    before = counts(db)
    class NewsFeed:
        def fetch(self, provider, captured):
            if provider == 'em':
                return [{'title':'人工智能新政策发布', 'summary':'600000公司发布算力产品',
                         'showTime':'2026-10-12 16:00:00', 'code':'202610123456789'}]
            return [{'title':'人工智能新政策发布', 'content':'600000公司发布算力产品',
                     'ctime':datetime(2026, 10, 12, 16, 0, tzinfo=SHANGHAI).timestamp(), 'id':123}]
    def no_email(*args, **kwargs):
        pytest.fail('supplement must not enqueue or send email')
    monkeypatch.setattr('stock_daily.mailer.enqueue_safely', no_email)
    monkeypatch.setattr('stock_daily.mailer.flush_outbox', no_email)
    monkeypatch.setattr('stock_daily.news.now', lambda: news_time)
    monkeypatch.setattr('stock_daily.news.collect_news', lambda db, config, **kwargs: collect_news(
        db, config, force=True, feed=NewsFeed(), captured=news_time))
    result = refresh_snapshot_news(db, replace(settings, news_enabled=True), DAY, 'close')
    updated = db.rows(select(snapshots.c.payload))[0]['payload']
    assert result['status'] == 'success' and result['count'] == 1
    assert updated['captured_at'] == original['captured_at']
    assert updated['quotes'] == original['quotes']
    assert updated['news']['supplemental'] and updated['news']['items'][0]['after_quote']
    assert counts(db) == before
