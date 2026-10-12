from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from stock_daily.db import BusyError, bars, reports, runs, sessions, signals
from stock_daily.pipeline import refresh_events, run_daily, validate_batch
from stock_daily.providers import DataError, DemoProvider
from stock_daily.service import dashboard


def test_bootstrap_publishes_report_and_idempotent_run_does_not_duplicate(populated):
    db, settings, provider = populated
    before = db.rows(select(func.count()).select_from(bars))[0]["count_1"]
    assert run_daily(db, settings, "20261009", provider=provider)["status"] == "exists"
    assert db.rows(select(func.count()).select_from(bars))[0]["count_1"] == before
    report = db.latest_report("demo")
    assert report["day"] == "20261009"
    assert report["quality"]["status"] == "complete"
    assert len(report["candidates"]) >= 1


def test_forced_report_failure_keeps_all_published_today_data(populated, monkeypatch):
    db, settings, provider = populated
    before_report = db.latest_report("demo")
    before_rows = db.rows(select(bars).where(bars.c.day == "20261009"))
    original = provider.fetch_day

    def updated(day):
        batch = deepcopy(original(day))
        for row in batch.bars:
            row["amount"] *= 2
        return batch

    def fail(*args):
        raise ValueError("分析失败测试")

    monkeypatch.setattr(provider, "fetch_day", updated)
    monkeypatch.setattr("stock_daily.pipeline.build_report", fail)
    with pytest.raises(ValueError, match="分析失败测试"):
        run_daily(db, settings, "20261009", force=True, provider=provider)
    assert db.latest_report("demo") == before_report
    assert db.rows(select(bars).where(bars.c.day == "20261009")) == before_rows
    assert db.rows(select(runs).order_by(runs.c.started_at.desc()).limit(1))[0]["status"] == "failed"


def test_bad_coverage_never_publishes_today_report(settings, db):
    provider = DemoProvider(settings)
    batch = deepcopy(provider.fetch_day("20261009"))
    batch.bars = batch.bars[:5]
    provider.fetch_day = lambda _: batch
    with pytest.raises(DataError, match="覆盖率"):
        run_daily(db, settings, "20261009", provider=provider)
    assert not db.rows(select(reports))
    assert not db.rows(select(sessions))


@pytest.mark.parametrize("damage", ["duplicate", "wrong_date", "negative", "nan", "ohlc"])
def test_malformed_bars_rejected(settings, damage):
    provider = DemoProvider(settings)
    batch = deepcopy(provider.fetch_day("20261009"))
    if damage == "duplicate":
        batch.bars.append(batch.bars[0])
    elif damage == "wrong_date":
        batch.bars[0]["day"] = "20261008"
    elif damage == "negative":
        batch.bars[0]["adj"] = -1
    elif damage == "nan":
        batch.bars[0]["close"] = float("nan")
    else:
        batch.bars[0]["high"] = 0.1
    with pytest.raises(DataError):
        validate_batch(batch, provider.universe(), settings)


def test_suspensions_not_counted_as_missing(settings):
    provider = DemoProvider(settings)
    batch = deepcopy(provider.fetch_day("20261009"))
    removed = batch.bars.pop()
    batch.suspended.add(removed["code"])
    result = validate_batch(batch, provider.universe(), settings)
    assert result["coverage"] == 100
    assert result["missing"] == []


def test_calendar_closed_day_skipped_and_live_has_no_demo_fallback(settings, db):
    assert run_daily(db, settings, "20261010")["status"] == "skipped"
    with pytest.raises(DataError, match="TUSHARE_TOKEN"):
        run_daily(db, replace(settings, source="tushare"), "20261009")
    assert db.latest_report("tushare") is None


def test_concurrent_pipeline_lock_prevents_other_writer(settings, db):
    with db.lock("demo"):
        with pytest.raises(BusyError):
            run_daily(db, settings, "20261009")
    with db.lock("demo"):
        pass


def test_resume_reuses_successful_historical_sessions(settings, db):
    provider = DemoProvider(settings)
    original = provider.fetch_day
    calls = []

    def fail_midway(day):
        calls.append(day)
        if len(calls) == 7:
            raise DataError("中途断网")
        return original(day)

    provider.fetch_day = fail_midway
    with pytest.raises(DataError):
        run_daily(db, settings, "20261009", provider=provider)
    stored = db.rows(select(sessions))
    assert len(stored) == 5
    assert db.latest_report("demo") is None
    previous_days = {r["day"] for r in stored}
    resumed = []

    def success(day):
        resumed.append(day)
        return original(day)

    provider.fetch_day = success
    run_daily(db, settings, "20261009", provider=provider)
    assert not previous_days.intersection(resumed)


def test_keyboard_interrupt_records_failure_and_releases_lock(settings, db):
    provider = DemoProvider(settings)
    original = provider.fetch_day
    calls = []

    def interrupted(day):
        calls.append(day)
        if len(calls) == 7:
            raise KeyboardInterrupt
        return original(day)

    provider.fetch_day = interrupted
    with pytest.raises(KeyboardInterrupt):
        run_daily(db, settings, "20261009", provider=provider)
    task = db.rows(select(runs).order_by(runs.c.started_at.desc()).limit(1))[0]
    assert task["status"] == "failed" and "已中断" in task["message"]
    assert len(db.rows(select(sessions))) == 5
    assert db.latest_report("demo") is None
    with db.lock("demo"):
        pass


def test_source_data_isolation(populated):
    db, settings, provider = populated
    assert dashboard(db, replace(settings, source="tushare"))["report"] is None
    assert db.rows(select(signals).where(signals.c.source == "tushare")) == []


def test_events_refresh_preserves_signal_snapshot(populated):
    db, settings, provider = populated
    before = db.rows(select(signals))
    provider.events = lambda day: (
        [{"code": "600000.SH", "day": day, "title": "新增诉讼公告", "url": "", "published_at": ""}],
        "available",
    )
    refresh_events(db, settings, "20261009", provider=provider)
    assert db.rows(select(signals)) == before
    assert dashboard(db, settings)["report"]["events"][0]["title"] == "新增诉讼公告"
