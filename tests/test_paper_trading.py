from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from stock_daily.backtest import simulate
from stock_daily.config import SHANGHAI
from stock_daily.db import (
    Database,
    bars,
    paper_accounts,
    paper_quote_batches,
    reports,
    sessions,
    signals,
    trading_days,
)
from stock_daily.entry_policy import VERSION as ENTRY_VERSION
from stock_daily.paper_trading import annotate_plan, current_account, execute_quotes
from stock_daily.trade_plan import build_trade_plan
from stock_daily.web import create_app

CURRENT = datetime(2026, 10, 15, 10, 0, tzinfo=SHANGHAI)


def setup(db, source="demo", observed=True):
    codes = ["600000.SH", "600001.SH"]
    with db.engine.begin() as c:
        for day in ("20261013", "20261014", "20261015", "20261016"):
            db.upsert(c, trading_days, {"source": source, "day": day, "is_open": 1})
        for day in ("20261013", "20261014"):
            db.upsert(c, sessions, {"source": source, "day": day, "quality": {"suspended": []},
                "benchmark": {"close": 100}, "universe": {code: {"name": code} for code in codes},
                "events": [], "event_status": "disabled", "acquired_at": day})
            for code in codes:
                db.upsert(c, bars, {"source": source, "day": day, "code": code, "open": 10,
                    "high": 11, "low": 9, "close": 10.2, "volume": 1e7, "amount": 1e8, "adj": 1, "pct": 1})
        for day, code, stop in [("20261013", codes[0], 10.3), ("20261014", codes[1], 9.5)]:
            stamp = f"{day[:4]}-{day[4:6]}-{day[6:]}T18:30:00+08:00"
            db.upsert(c, signals, {"source": source, "day": day, "code": code, "strategy": "breakout",
                "payload": {"name": code, "invalidation_adj": stop, "atr14": .4, "industry": "行业A",
                    "published_at": stamp, "relative20": 1, "reasons": ["原始信号依据"], "rule_version": "v1"}})
        report = {"day": "20261014", "generated_at": "2026-10-14T18:30:00+08:00", "stocks": [],
            "candidates": [], "all_candidates": [], "quality": {"suspended": []}, "changes": {}, "evidence": {},
            "market": {"above_ma20_ratio": 80, "benchmark_return20": 5}}
        db.upsert(c, reports, {"source": source, "day": "20261014", "payload": report})
        if observed:
            # Most account/exit/notification tests begin after one current-policy observation.
            stamp = CURRENT - timedelta(minutes=10)
            before = quotes(stamp)
            for quote in before["quotes"].values():
                quote.update(volume=900000, amount=900000 * (100.5 if quote["code"] == "000300.SH" else 10.25))
            db.upsert(c, paper_quote_batches, {"source": source, "day": "20261015",
                "captured_at": stamp.isoformat(),
                "payload": {"version": ENTRY_VERSION, "quotes": before["quotes"]}})
    return report


def quotes(current=CURRENT):
    return {"day": current.strftime("%Y%m%d"), "captured_at": current.isoformat(), "quotes": {
        code: {"code": code, "name": code, "previous": 10.2, "open": 10.2,
            "price": 10.3, "high": 10.8, "low": 9.8, "volume": 1e6, "amount": 10250000,
            "quote_time": current.isoformat()}
        for code in ("600000.SH", "600001.SH")}
        | {"000300.SH": {"code": "000300.SH", "name": "沪深300", "previous": 100, "open": 100,
            "price": 101, "high": 102, "low": 99, "volume": 1e6, "amount": 100500000,
            "quote_time": current.isoformat()}}}


def close_day(db, day="20261015", price=9, source="demo"):
    with db.engine.begin() as c:
        db.upsert(c, sessions, {"source": source, "day": day, "quality": {"suspended": []},
            "benchmark": {"close": 101}, "universe": {code: {"name": code} for code in ("600000.SH", "600001.SH")},
            "events": [], "event_status": "disabled", "acquired_at": day})
        for code in ("600000.SH", "600001.SH"):
            db.upsert(c, bars, {"source": source, "day": day, "code": code,
                "open": 50, "high": 55, "low": 1, "close": price, "volume": 1e7, "amount": 1e8, "adj": 1, "pct": 1})


def test_live_sell_then_buy_updates_cash_positions_fills_without_rewriting_daily(settings, db):
    report = setup(db)
    original = simulate(db, settings, "20261014")
    tables = {t.name: db.rows(select(t)) for t in (bars, reports, sessions, signals)}
    result = execute_quotes(db, settings, quotes(), CURRENT)
    assert result["new_fills"] == 2
    account = current_account(db, settings, report["day"], CURRENT)
    assert account["summary"]["through"] == "20261015"
    assert [p["code"] for p in account["positions"]] == ["600001.SH"]
    assert [f["side"] for f in account["fills"][:2]] == ["buy", "sell"]
    sold, bought = account["fills"][1], account["fills"][0]
    assert sold["price"] == pytest.approx(10.3 * .9995)
    assert sold["execution_time"] == CURRENT.isoformat() and sold["period"] == "盘中报价模拟"
    assert sold["reference_price"] == 10.3 and sold["open_price"] == 10.2
    assert sold["slippage_cost"] == pytest.approx(sold["shares"] * 10.3 * .0005)
    assert bought["entry_risk"]["estimated_risk"] <= bought["entry_risk"]["risk_budget"]
    summary = account["summary"]
    assert summary["initial_capital"] + sum(f["cash_flow"] for f in account["fills"]) == pytest.approx(summary["available_cash"])
    assert sum(f["fee"] for f in account["fills"]) == pytest.approx(summary["fees"])
    assert summary["profit"] == pytest.approx(summary["realized_profit"] + summary["unrealized_profit"])
    assert simulate(db, settings, "20261014") == original
    assert tables == {t.name: db.rows(select(t)) for t in (bars, reports, sessions, signals)}


def test_repeated_refresh_restart_and_price_changes_never_duplicate_or_rewrite_fills(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    first = current_account(db, settings, "20261014", CURRENT)
    assert execute_quotes(db, settings, quotes(), CURRENT)["status"] == "unchanged"
    restarted = Database(settings.database_url)
    later = CURRENT + timedelta(minutes=10)
    new_quotes = quotes(later)
    new_quotes["quotes"]["600001.SH"]["price"] = 10.4
    assert execute_quotes(restarted, settings, new_quotes, later)["new_fills"] == 0
    second = current_account(restarted, settings, "20261014", later)
    assert second["fills"] == first["fills"] and second["trades"] == first["trades"]
    assert second["summary"]["available_cash"] == first["summary"]["available_cash"]
    assert second["positions"][0]["mark"] == 10.4
    assert second["positions"][0]["held_sessions"] == 0  # ticks are not completed sessions
    restarted.engine.dispose()


@pytest.mark.parametrize("problem", ["stale", "future", "missing", "scale", "lunch", "closed", "bad_batch"])
def test_unsafe_quotes_or_non_trading_times_cannot_create_fills(settings, db, problem):
    setup(db)
    payload, current = quotes(), CURRENT
    if problem == "stale":
        payload["quotes"]["600000.SH"]["quote_time"] = (CURRENT - timedelta(minutes=3)).isoformat()
    elif problem == "future":
        payload["quotes"]["600000.SH"]["quote_time"] = (CURRENT + timedelta(minutes=1)).isoformat()
    elif problem == "missing":
        payload["quotes"].pop("600000.SH")
    elif problem == "scale":
        payload["quotes"]["600000.SH"]["previous"] = 5.1
    elif problem in {"lunch", "closed"}:
        current = CURRENT.replace(hour=12 if problem == "lunch" else 16)
        payload = quotes(current)
    else:
        payload["captured_at"] = (CURRENT + timedelta(hours=1)).isoformat()
    before = simulate(db, settings, "20261014")["fills"]
    result = execute_quotes(db, settings, payload, current)
    assert result.get("new_fills", 0) == 0
    stored = db.rows(select(paper_accounts))
    assert not stored or stored[0]["payload"]["account"]["fills"] == before


def test_sell_restriction_waits_and_later_refresh_can_release_buy_budget(settings, db):
    setup(db)
    payload = quotes()
    payload["quotes"]["600000.SH"].update(price=9.18, low=9.18)
    first = execute_quotes(db, settings, payload, CURRENT)
    assert "退出等待" in first["decisions"]["600000.SH"]
    later = CURRENT + timedelta(minutes=10)
    result = execute_quotes(db, settings, quotes(later), later)
    assert result["new_fills"] >= 1
    a = current_account(db, settings, "20261014", later)
    assert "600000.SH" not in {p["code"] for p in a["positions"]}


def test_close_reconciliation_keeps_confirmed_prices_and_next_day_t_plus_one(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    live = current_account(db, settings, "20261014", CURRENT)
    close_day(db)
    closed = simulate(db, settings, "20261015")
    assert closed["fills"] == live["fills"]
    assert closed["summary"]["available_cash"] == live["summary"]["available_cash"]
    assert closed["positions"][0]["held_sessions"] == 1
    assert closed["positions"][0]["exit_trigger_day"] == "20261015"
    assert closed["summary"]["through"] == closed["curve"][-1]["day"] == "20261015"
    assert len({p["day"] for p in closed["curve"]}) == len(closed["curve"])
    assert simulate(db, settings, "20261015") == closed
    # A second quote on the same day must not use newly published close information.
    assert execute_quotes(db, settings, quotes(CURRENT.replace(hour=14)), CURRENT.replace(hour=14))["new_fills"] == 0
    with db.engine.begin() as c:
        report = {"day": "20261015", "generated_at": "2026-10-15T18:30:00+08:00"}
        db.upsert(c, reports, {"source": "demo", "day": "20261015", "payload": report})
    tomorrow = CURRENT + timedelta(days=1)
    payload = quotes(tomorrow)
    for q in payload["quotes"].values():
        q.update(previous=9, open=9.1, price=9.2, low=8.8, high=9.5)
    assert execute_quotes(db, settings, payload, tomorrow)["new_fills"] == 1
    final = current_account(db, settings, "20261015", tomorrow)
    assert not final["positions"]
    assert final["fills"][0]["side"] == "sell" and final["fills"][0]["entry_day"] == "20261015"


def test_missing_close_data_pauses_without_losing_confirmed_trades(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    live = current_account(db, settings, "20261014", CURRENT)
    close_day(db)
    with db.engine.begin() as c:
        c.execute(delete(bars).where(bars.c.day == "20261015", bars.c.code == "600001.SH"))
    failed = simulate(db, settings, "20261015")
    assert failed["stop"] and failed["fills"] == live["fills"]
    assert failed["summary"]["available_cash"] == live["summary"]["available_cash"]


def test_plan_overlay_and_live_api_do_not_rewrite_archive_or_show_future_sales(settings, db, monkeypatch):
    report = setup(db)
    plan = build_trade_plan(db, settings, report)
    report["trade_plan"] = deepcopy(plan)
    with db.engine.begin() as c:
        db.upsert(c, reports, {"source": "demo", "day": "20261014", "payload": report})
    execute_quotes(db, settings, quotes(), CURRENT)
    live = current_account(db, settings, "20261014", CURRENT)
    overlay = annotate_plan(plan, live)
    assert overlay["exits"][0]["execution"]["side"] == "sell"
    assert overlay["entries"][0]["status"] == "filled"
    assert db.latest_report("demo", "20261014")["trade_plan"] == plan
    monkeypatch.setattr("stock_daily.paper_trading.now", lambda: CURRENT)
    with TestClient(create_app(settings, db)) as client:
        daily = client.get("/api/backtest?day=20261014").json()
        current = client.get("/api/backtest?day=20261014&live=true").json()
        assert len(current["fills"]) == len(daily["fills"]) + 2
        assert current["trade_plan"]["entries"][0]["status"] == "filled"
        assert client.get("/api/backtest?day=20261014&live=true&capital=50000").json()["parameters"]["capital"] == 50000


def test_cancelled_entry_leaves_pending_plan_and_preserves_frozen_reference(settings, db):
    report = setup(db)
    plan = build_trade_plan(db, settings, report)
    payload = quotes()
    payload["quotes"]["600001.SH"].update(open=10.7, high=11)
    execute_quotes(db, settings, payload, CURRENT)
    overlay = annotate_plan(plan, current_account(db, settings, report["day"], CURRENT))
    assert overlay["entries"][0]["status"] == "watch"
    assert "取消" in overlay["entries"][0]["reason"]
    assert overlay["counts"]["planned"] == overlay["counts"]["conditional"] == overlay["counts"]["exits"] == 0
    assert plan["entries"][0]["status"] == "planned"
