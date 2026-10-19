from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update

from stock_daily.config import SHANGHAI
from stock_daily.db import bars, reports, sessions, signals, trading_days
from stock_daily.html_export import daily_payload, render_daily
from stock_daily.pipeline import run_daily
from stock_daily.providers import DataError, DemoProvider
from stock_daily.service import dashboard
from stock_daily.trade_plan import build_trade_plan

DAY = "20261009"
STAMP = "2026-10-09T18:30:00+08:00"


def seeded_plan(db, holdings=1, exiting=False):
    codes = [f"{600000 + i:06d}.SH" for i in range(holdings + 1)]
    with db.engine.begin() as c:
        for day in ["20261008", DAY]:
            db.upsert(c, trading_days, {"source": "demo", "day": day, "is_open": 1})
            db.upsert(c, sessions, {"source": "demo", "day": day, "quality": {"suspended": []},
                "benchmark": {"close": 100}, "universe": {code: {"name": code} for code in codes},
                "events": [], "event_status": "disabled", "acquired_at": STAMP})
            for code in codes:
                db.upsert(c, bars, {"source": "demo", "day": day, "code": code, "open": 10,
                    "close": 10.2, "high": 10.5, "low": 9.5, "adj": 1, "volume": 1e7, "amount": 1e8, "pct": 1})
        for day, opened in [("20261010", 0), ("20261011", 0), ("20261012", 1)]:
            db.upsert(c, trading_days, {"source": "demo", "day": day, "is_open": opened})
        for index, code in enumerate(codes[:-1]):
            db.upsert(c, signals, {"source": "demo", "day": "20261008", "code": code, "strategy": "breakout",
                "payload": {"name": code, "invalidation_adj": 10.3 if exiting and index == 0 else 9,
                    "published_at": "2026-10-08T18:30:00+08:00"}})
        db.upsert(c, signals, {"source": "demo", "day": DAY, "code": codes[-1], "strategy": "breakout",
            "payload": {"name": "待买入公司", "label": "放量突破", "invalidation_adj": 9,
                "published_at": STAMP, "reasons": ["原信号依据"], "risks": ["原信号风险"], "relative20": 3}})
        report = {"day": DAY, "generated_at": STAMP}
        db.upsert(c, reports, {"source": "demo", "day": DAY, "payload": report})
    return report


def test_plan_verifies_next_session_and_uses_close_reference(settings, db):
    report = seeded_plan(db)
    plan = build_trade_plan(db, settings, report)
    assert plan["target_day"] == "20261012" and plan["calendar_verified"]
    entry = plan["entries"][0]
    assert entry["status"] == "planned" and not entry["depends_on_sales"]
    assert entry["reference_price"] == pytest.approx(10.2 * 1.0005)
    assert entry["reference_shares"] % 100 == 0
    assert entry["reference_spent"] <= entry["reference_budget"]
    assert entry["reasons"] == ["原信号依据"]
    assert entry["invalidation_price"] == 9
    assert len(plan["holds"]) == 1 and not plan["exits"]
    assert plan["origin"] == "derived" and plan["created_at"] is None


def test_future_quotes_and_events_cannot_change_plan(settings, db):
    report = seeded_plan(db)
    before = build_trade_plan(db, settings, report)
    with db.engine.begin() as c:
        for code in ["600000.SH", "600001.SH"]:
            db.upsert(c, bars, {"source": "demo", "day": "20261012", "code": code, "open": 99,
                "close": 50, "high": 100, "low": 40, "adj": 7, "volume": 1, "amount": 10, "pct": 900})
        db.upsert(c, sessions, {"source": "demo", "day": "20261012", "quality": {"suspended": []},
            "benchmark": {"close": 1}, "universe": {"600001.SH": {"name": "ST未来信息", "is_st": True}},
            "events": [{"title": "未来公告"}], "event_status": "available", "acquired_at": "2026-10-12T18:30:00+08:00"})
    assert build_trade_plan(db, settings, report) == before


def test_missing_calendar_does_not_guess_weekday(settings, db):
    report = seeded_plan(db)
    from sqlalchemy import delete

    with db.engine.begin() as c:
        c.execute(delete(trading_days).where(trading_days.c.day == "20261010"))
    plan = build_trade_plan(db, settings, report)
    assert plan["status"] == "calendar_pending"
    assert plan["target_day"] is None and not plan["calendar_verified"]


def test_full_account_only_plans_new_buy_after_exit(settings, db):
    report = seeded_plan(db, holdings=10, exiting=True)
    plan = build_trade_plan(db, settings, report)
    assert plan["counts"] == {"planned": 0, "conditional": 1, "watch": 0, "exits": 1, "holds": 9}
    entry = plan["entries"][0]
    assert entry["depends_on_sales"] and entry["reference_shares"] > 0
    exit_plan = plan["exits"][0]
    assert exit_plan["exit_trigger_day"] == DAY
    assert exit_plan["reference_price"] == pytest.approx(10.2 * .9995)
    assert exit_plan["reference_proceeds"] > 0
    assert plan["scenario"]["positions_before_sales"] == 10
    assert plan["scenario"]["positions_after_sales"] == 9
    with db.engine.begin() as c:
        c.execute(update(signals).where(signals.c.day == "20261008").values(payload={
            "name": "持仓公司", "invalidation_adj": 9, "published_at": "2026-10-08T18:30:00+08:00"}))
    unchanged = build_trade_plan(db, settings, report)
    assert unchanged["entries"][0]["status"] == "watch"
    assert "持仓上限" in unchanged["entries"][0]["reason"]


@pytest.mark.parametrize("damage", ["reconstructed", "late", "naive"])
def test_untradable_signals_cannot_be_planned(settings, db, damage):
    report = seeded_plan(db)
    payload = {"name": "排除公司", "invalidation_adj": 9, "published_at": STAMP}
    if damage == "reconstructed":
        payload["reconstructed"] = True
    else:
        payload["published_at"] = "2026-10-12T10:00:00+08:00" if damage == "late" else "2026-10-09T18:30:00"
    with db.engine.begin() as c:
        c.execute(update(signals).where(signals.c.day == DAY).values(payload=payload))
    entry = build_trade_plan(db, settings, report)["entries"][0]
    assert entry["status"] == "watch" and entry["reference_shares"] is None


def test_incomplete_account_blocks_new_trade_plan(settings, db):
    report = seeded_plan(db)
    prior_session = db.rows(select(sessions).where(sessions.c.day == "20261008"))[0]
    prior_bars = db.rows(select(bars).where(bars.c.day == "20261008"))
    with db.engine.begin() as c:
        db.upsert(c, trading_days, {"source": "demo", "day": "20261007", "is_open": 1})
        db.upsert(c, sessions, {**prior_session, "day": "20261007"})
        for row in prior_bars:
            db.upsert(c, bars, {**row, "day": "20261007"})
        c.execute(update(signals).where(signals.c.day == "20261008").values(day="20261007", payload={
            "name": "持仓公司", "invalidation_adj": 9, "published_at": "2026-10-07T18:30:00+08:00"}))
        c.execute(update(bars).where(bars.c.day == DAY, bars.c.code == "600000.SH").values(adj=2))
    plan = build_trade_plan(db, settings, report)
    assert plan["status"] == "blocked"
    assert not plan["entries"] and not plan["exits"]


def test_pipeline_freezes_plan_and_reexport_keeps_it(settings, db, monkeypatch):
    stamp = datetime(2026, 10, 9, 18, 30, tzinfo=SHANGHAI)
    monkeypatch.setattr("stock_daily.pipeline.now", lambda: stamp)
    monkeypatch.setattr("stock_daily.trade_plan.now", lambda: stamp)
    provider = DemoProvider(settings)
    run_daily(db, settings, DAY, provider=provider)
    plan = deepcopy(db.latest_report("demo", DAY)["trade_plan"])
    assert plan["origin"] == "published" and plan["status"] == "ready"
    assert plan["target_day"] == "20261012"
    assert plan["created_at"] == stamp.isoformat()
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day == DAY).values(close=1))
    assert dashboard(db, settings, DAY)["report"]["trade_plan"] == plan
    assert daily_payload(db, settings, DAY)["dashboard"]["report"]["trade_plan"] == plan


def test_future_calendar_failure_preserves_daily_publication(settings, db, monkeypatch):
    provider = DemoProvider(settings)
    calendar = provider.calendar

    def future_unavailable(start, end):
        if start == DAY and end > DAY:
            raise DataError("未来日历不可用")
        return calendar(start, end)

    monkeypatch.setattr(provider, "calendar", future_unavailable)
    run_daily(db, settings, DAY, provider=provider)
    plan = db.latest_report("demo", DAY)["trade_plan"]
    assert plan["status"] == "calendar_pending" and plan["target_day"] is None


def test_plan_failure_rolls_back_report_and_signals(populated, monkeypatch):
    db, settings, provider = populated
    before = db.latest_report("demo", DAY)
    before_signals = db.rows(select(signals).where(signals.c.day == DAY))

    def fail(*args, **kwargs):
        raise ValueError("计划计算失败")

    monkeypatch.setattr("stock_daily.trade_plan.build_trade_plan", fail)
    with pytest.raises(ValueError, match="计划计算失败"):
        run_daily(db, settings, DAY, force=True, provider=provider)
    assert db.latest_report("demo", DAY) == before
    assert db.rows(select(signals).where(signals.c.day == DAY)) == before_signals


def test_phone_and_scriptless_daily_plan(populated, tmp_path, monkeypatch):
    api = pytest.importorskip("playwright.sync_api")
    db, settings, provider = populated
    stamp = datetime(2026, 10, 14, 18, 30, tzinfo=SHANGHAI)
    monkeypatch.setattr("stock_daily.pipeline.now", lambda: stamp)
    monkeypatch.setattr("stock_daily.trade_plan.now", lambda: stamp)
    run_daily(db, settings, "20261014", provider=provider)
    payload = daily_payload(db, settings)
    plan = payload["dashboard"]["report"]["trade_plan"]
    assert plan["status"] == "ready"
    observations = [("paused", "暂缓买入", "报价不高于失效参考价，等待重新站回确认"),
                    ("waiting", "等待连续确认", "已站回失效参考价，等待连续有效报价确认")]
    assert len(plan["entries"]) >= len(observations)
    for item, (status, _, reason) in zip(plan["entries"], observations):
        item.update(status="watch", intraday={"version": "entry-intraday-v3", "status": status,
            "reason": reason, "evaluated_at": "2026-10-15T10:00:00+08:00"})
    path = tmp_path / "planned.html"
    path.write_text(render_daily(payload, settings))
    with api.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip("Chromium未安装")
        browser = p.chromium.launch()
        with browser.new_context(viewport={"width": 390, "height": 844}) as context:
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(path.as_uri())
            page.locator("#trade-plan-content .notice").wait_for()
            assert "2026.10.15" in page.locator("#trade-plan-content").inner_text()
            page.locator("#trade-plan-content details").filter(
                has=page.locator('[data-entry-status="paused"]')).locator("summary").first.click()
            for label in ["待卖出", "待买入候选", "继续持有", "仅观察", "参考股数"]:
                assert label in page.locator("#trade-plan-content").inner_text()
            for status, label, reason in observations:
                node = page.locator(f'#trade-plan-content [data-entry-status="{status}"]')
                assert label in node.inner_text()
                assert reason in node.inner_text()
                assert "entry-intraday-v3" in node.inner_text()
                assert "当日已取消" not in node.inner_text()
            assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
            assert not errors
        with browser.new_context(java_script_enabled=False) as context:
            page = context.new_page()
            page.goto(path.as_uri())
            page.locator("#static-report details").filter(
                has=page.locator('[data-entry-status="paused"]')).locator("summary").first.click()
            text = page.locator("#static-report").inner_text()
            assert "下一交易日模拟交易计划" in text
            assert "20261015" in text
            assert "参考股数" in text
            for status, label, reason in observations:
                node = page.locator(f'#static-report [data-entry-status="{status}"]')
                assert label in node.text_content()
                assert reason in node.text_content()
                assert "entry-intraday-v3" in node.text_content()
        browser.close()
    assert all("intraday" not in i for i in db.latest_report(settings.source)["trade_plan"]["entries"])
