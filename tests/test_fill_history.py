import csv
import json
from copy import deepcopy
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from test_mail_preview import browser as browser

from stock_daily.backtest import Simulation, simulate
from stock_daily.db import bars, sessions, signals, trading_days
from stock_daily.fills import history_fills
from stock_daily.html_export import daily_payload, render_daily
from stock_daily.web import create_app


def seed_history(db, count=60):
    days, day = [], date(2026, 10, 9)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    days.reverse()
    with db.engine.begin() as c:
        c.execute(delete(signals).where(signals.c.source == "demo"))
        for index, day in enumerate(days):
            price = 10 + index * .1
            stamp = f"{day[:4]}-{day[4:6]}-{day[6:]}T18:30:00+08:00"
            db.upsert(c, trading_days, {"source": "demo", "day": day, "is_open": 1})
            db.upsert(c, sessions, {"source": "demo", "day": day, "quality": {"suspended": []},
                "benchmark": {"close": 100 + index}, "universe": {"600000.SH": {"name": "=观察公司"}},
                "events": [], "event_status": "disabled", "acquired_at": stamp})
            db.upsert(c, bars, {"source": "demo", "day": day, "code": "600000.SH", "open": price,
                "close": price + .05, "high": price + .5, "low": price - .5, "volume": 1e7,
                "amount": 1e8, "pct": 1, "adj": 1})
            db.upsert(c, signals, {"source": "demo", "day": day, "code": "600000.SH", "strategy": "breakout",
                "payload": {"name": "=观察公司", "label": "放量突破", "rule_version": "v1",
                    "published_at": stamp, "invalidation_adj": 1, "relative20": 1,
                    "reasons": ['原始依据 <img src=x onerror="window.UNSAFE=1">']}})
    return days


def test_every_fill_is_recorded_in_actual_order_and_reconciles_cash_and_fees(settings, db):
    days = seed_history(db)
    result = simulate(db, settings, config=Simulation(holding_days=1))
    fills = result["fills"]
    assert len(fills) == 2 * result["summary"]["closed_trades"] + result["summary"]["open_positions"] == 117
    assert len({f["id"] for f in fills}) == 117
    assert [f["sequence"] for f in fills] == list(range(117, 0, -1))
    assert fills[0]["day"] == days[-1] and fills[0]["side"] == "buy"
    assert fills[1]["day"] == days[-1] and fills[1]["side"] == "sell"
    cash = result["summary"]["initial_capital"]
    for fill in reversed(fills):
        cash += fill["cash_flow"]
        assert fill["cash_after"] == pytest.approx(cash)
        assert fill["notional"] == pytest.approx(fill["price"] * fill["shares"])
        assert fill["cash_after"] >= 0
        assert (fill["cash_flow"] < 0) == (fill["side"] == "buy")
        if fill["side"] == "buy":
            assert fill["realized_profit"] is None and fill["exit_trigger_day"] is None
    assert cash == pytest.approx(result["summary"]["available_cash"])
    assert sum(f["fee"] for f in fills) == pytest.approx(result["summary"]["fees"])
    assert sum(f["realized_profit"] or 0 for f in fills) == pytest.approx(result["summary"]["realized_profit"])
    json.dumps(fills, allow_nan=False)


def test_historical_cutoff_never_exposes_later_sales_and_buy_record_stays_stable(settings, db):
    days = seed_history(db, 5)
    config = Simulation(holding_days=1)
    early = simulate(db, settings, days[1], config)
    full = simulate(db, settings, days[-1], config)
    assert len(early["fills"]) == 1 and early["fills"][0]["side"] == "buy"
    first = early["fills"][0]
    assert next(f for f in full["fills"] if f["id"] == first["id"]) == first
    assert all(f["day"] <= days[1] for f in early["fills"])
    mutated = deepcopy(full)
    mutated["valuation"] = {"positions": [{"code": "600999.SH", "entry_day": "20990101"}]}
    assert history_fills(mutated, "demo") == full["fills"]


def test_blocked_entries_and_incomplete_exits_cannot_invent_fills(settings, db):
    days = seed_history(db, 4)
    config = Simulation(holding_days=1)
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day == days[1]).values(open=20, close=20, high=21))
    assert simulate(db, settings, days[1], config)["fills"] == []
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day == days[1]).values(open=10.1, close=10.15, high=10.6))
        c.execute(delete(bars).where(bars.c.day == days[2]))
    stopped = simulate(db, settings, days[-1], config)
    assert stopped["status"] == "incomplete"
    assert len(stopped["fills"]) == 1 and stopped["fills"][0]["side"] == "buy"


def test_legacy_history_recovers_all_fills_without_fabricating_balances(settings, db):
    seed_history(db, 5)
    result = simulate(db, settings, config=Simulation(holding_days=1))
    original = result.pop("fills")
    recovered = history_fills(result, "demo")
    assert {f["id"] for f in recovered} == {f["id"] for f in original}
    for fill in recovered:
        current = next(f for f in original if f["id"] == fill["id"])
        assert fill["cash_after"] is None and fill["sequence"] is None
        assert fill["origin"] == "legacy_derived"
        for key in ("price", "notional", "fee", "cash_flow", "realized_profit"):
            assert fill[key] == current[key]


def test_backtest_api_includes_all_fills_through_selected_date(settings, db):
    days = seed_history(db)
    with TestClient(create_app(settings, db)) as client:
        full = client.get(f"/api/backtest?day={days[-1]}&holding_days=1").json()
        assert len(full["fills"]) == 117
        early = client.get(f"/api/backtest?day={days[1]}&holding_days=1").json()
        assert len(early["fills"]) == 1
        assert all(f["source"] == "demo" and f["day"] <= days[1] for f in early["fills"])


@pytest.mark.parametrize("legacy", [False, True])
def test_phone_history_all_pages_details_filters_csv_and_refresh(browser, populated, tmp_path, legacy):
    db, settings, _ = populated
    payload = daily_payload(db, settings)
    days = seed_history(db)
    payload["backtest"] = simulate(db, settings, days[-1], Simulation(holding_days=1))
    if legacy:
        payload["backtest"].pop("fills")
    path = tmp_path / "history.html"
    path.write_text(render_daily(payload, settings))
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(path.as_uri())
        page.wait_for_function("() => window.REPORT_READY === true")
        page.locator('[data-view="backtest"]').click()
        page.locator("#show-fill-history").click()
        assert page.locator("#fill-history").is_visible() and page.locator("#fill-history").get_attribute("open") is not None
        assert "117 / 117" in page.locator("#fill-count").inner_text()
        assert page.locator("#fill-table [data-fill-id]").count() == 20
        page.locator("#fill-table [data-fill-id]").first.click()
        detail = page.locator("#fill-dialog")
        assert detail.is_visible()
        assert "买入成交本身不确认已实现盈亏" in detail.inner_text()
        assert '原始依据 <img src=x onerror="window.UNSAFE=1">' in detail.inner_text()
        assert page.locator("#fill-detail-body img").count() == 0
        assert page.evaluate("() => window.UNSAFE") is None
        if legacy:
            assert "旧版成交" in detail.inner_text()
        else:
            assert "模拟执行序号" in detail.inner_text()
        assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
        detail.locator(".close-dialog").click()
        page.locator("#fill-next").click()
        assert "第 2 / 6 页" in page.locator("#fill-page").inner_text()
        page.evaluate("() => loadBacktest()")
        assert "第 2 / 6 页" in page.locator("#fill-page").inner_text()
        assert page.locator("#fill-history").get_attribute("open") is not None
        with page.expect_download() as download:
            page.locator("#export-fills").click()
        csv_path = tmp_path / "all-fills.csv"
        download.value.save_as(csv_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 117  # export the whole filtered history, not only page two.
        assert len({r["成交编号"] for r in rows}) == 117
        assert all(r["名称"] == "'=观察公司" for r in rows)
        assert all(r["卖出对应交易已实现盈亏"] == "" for r in rows if r["方向"] == "买入")
        page.locator("#fill-prev").click()
        all_ids = []
        while True:
            all_ids.extend(page.locator("#fill-table [data-fill-id]").evaluate_all("buttons => buttons.map(b => b.dataset.fillId)"))
            if page.locator("#fill-next").is_disabled():
                break
            page.locator("#fill-next").click()
        assert len(set(all_ids)) == len(all_ids) == 117
        page.locator("#fill-side").select_option("sell")
        assert "58 / 117" in page.locator("#fill-count").inner_text()
        assert "第 1 / 3 页" in page.locator("#fill-page").inner_text()
        page.locator("#fill-table [data-fill-id]").first.click()
        assert "卖出成交详情" in detail.inner_text()
        assert "包含买卖双边费用" in detail.inner_text()
        detail.locator(".close-dialog").click()
        page.locator("#fill-reset").click()
        page.locator("#fill-from").fill("2026-10-09")
        page.locator("#fill-to").fill("2026-10-09")
        assert "2 / 117" in page.locator("#fill-count").inner_text()
        page.locator("#fill-to").fill("2026-10-08")
        assert "起始日期不能晚于" in page.locator("#fill-count").inner_text()
        assert page.locator("#export-fills").is_disabled()
        page.locator("#fill-reset").click()
        page.locator("#fill-search").fill("不存在")
        assert "0 / 117" in page.locator("#fill-count").inner_text()
        page.locator("#fill-search").fill("放量突破")
        assert "117 / 117" in page.locator("#fill-count").inner_text()
        page.locator("#fill-size").select_option("100")
        assert page.locator("#fill-table [data-fill-id]").count() == 100
        page.set_viewport_size({"width": 1440, "height": 1000})
        assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
        assert not errors
    with browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        page.goto(path.as_uri())
        page.locator("#static-fill-history > summary").click()
        assert page.locator("#static-fill-history .mail-card").count() == 117
        assert "现金变化" in page.locator("#static-fill-history").inner_text()
        assert page.locator("#static-fill-history img").count() == 0
        assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
