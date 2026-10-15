from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.backtest import fees, simulation_profile
from stock_daily.db import Database, bars, paper_accounts, trading_days
from stock_daily.exit_policy import advance, break_even, context
from stock_daily.paper_trading import annotate_plan, current_account, execute_quotes
from stock_daily.trade_plan import build_trade_plan


def position():
    return {"code": "600000.SH", "name": "测试", "entry_price": 10, "shares": 1000,
        "spent": 10005.1, "invalidation_price": 9, "exit_due": None}


def tick(price, minute=0, high=100):
    return {"price": price, "high": high, "low": 1,
            "quote_time": (CURRENT + timedelta(minutes=minute)).isoformat()}


def test_initial_stop_uses_tighter_structure_atr_and_loss_cap():
    p = position()
    advance(p, tick(10), {"atr": .2, "basis_day": "20261014"}, simulation_profile())
    assert p["exit_policy"]["stop"] == pytest.approx(9.6)
    assert p["exit_policy"]["peak"] == 10  # today's high=100 is not a post-entry observation
    advance(p, tick(9.59, 10), {"atr": .2, "basis_day": "20261014"}, simulation_profile())
    assert p["exit_due"] == "盘中风险止损触发"
    assert p["exit_trigger_kind"] == "quote" and p["exit_trigger_price"] == 9.59
    assert p["exit_trigger_close"] is None


def test_trailing_activation_high_watermark_and_break_even_include_costs():
    config = simulation_profile()
    p = position()
    advance(p, tick(10), {"atr": .2, "basis_day": "20261014"}, config)
    advance(p, tick(10.79, 10), {"atr": .2, "basis_day": "20261014"}, config)
    assert not p["exit_policy"]["trailing_active"]
    advance(p, tick(10.81, 20), {"atr": .2, "basis_day": "20261014"}, config)
    assert p["exit_policy"]["trailing_active"] and p["exit_policy"]["stop"] == pytest.approx(10.41)
    advance(p, tick(10.7, 30), {"atr": .9, "basis_day": "20261014"}, config)
    assert p["exit_policy"]["peak"] == 10.81 and p["exit_policy"]["stop"] == pytest.approx(10.41)
    advance(p, tick(10.4, 40), {"atr": .2, "basis_day": "20261014"}, config)
    assert p["exit_due"] == "盘中移动盈利保护触发"
    reason, stamp = p["exit_due"], p["exit_trigger_time"]
    advance(p, tick(11, 50), {"atr": .2, "basis_day": "20261014"}, config)
    assert p["exit_due"] == reason and p["exit_trigger_time"] == stamp
    for shares in (100, 1000, 100000):
        p.update(shares=shares, spent=10 * shares + fees(10 * shares, config))
        sale = break_even(p, config) * (1 - config.slippage_bps / 10000) * shares
        assert sale - fees(sale, config, sell=True) == pytest.approx(p["spent"])


def test_missing_atr_falls_back_to_cap_and_never_invents_an_atr():
    p = position()
    advance(p, tick(10), {"atr": None, "basis_day": "20261014"}, simulation_profile())
    assert p["exit_policy"]["atr"] is None
    assert p["exit_policy"]["stop"] == pytest.approx(9.4)


def test_bad_quote_for_one_stock_does_not_block_another_stock_exit(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    # Retain a second real holding to test isolation, with its own valid entry history.
    saved = db.rows(select(paper_accounts))[0]
    account = saved["payload"]["account"]
    held = deepcopy(account["positions"][0])
    held.update(code="600002.SH", entry_day="20261014", exit_due="测试已触发退出")
    account["positions"].append(held)
    with db.engine.begin() as c:
        db.upsert(c, paper_accounts, saved)
        row = db.rows(select(bars).where(bars.c.code == "600001.SH", bars.c.day == "20261014"))[0]
        db.upsert(c, bars, {**row, "code": "600002.SH"})
    later = CURRENT + timedelta(minutes=10)
    payload = quotes(later)
    payload["quotes"]["600002.SH"] = {**payload["quotes"]["600001.SH"], "code": "600002.SH"}
    del payload["quotes"]["600001.SH"]
    result = execute_quotes(db, settings, payload, later)
    assert result["status"] == "partial" and result["new_fills"] == 1
    a = current_account(db, settings, "20261014", later)
    assert a["fills"][0]["code"] == "600002.SH" and a["fills"][0]["side"] == "sell"
    assert len(a["positions"]) == 1 and a["positions"][0]["quote_error"]
    assert a["valuation"]["status"] == "partial"


def test_intraday_stop_latches_through_t_plus_one_and_restart(settings, db):
    report = setup(db)
    plan = build_trade_plan(db, settings, report)
    execute_quotes(db, settings, quotes(), CURRENT)
    later = CURRENT + timedelta(minutes=10)
    payload = quotes(later)
    payload["quotes"]["600001.SH"].update(price=9.6, low=9.5)
    assert execute_quotes(db, settings, payload, later)["new_fills"] == 0
    a = current_account(db, settings, report["day"], later)
    p = a["positions"][0]
    assert p["exit_due"] == "盘中风险止损触发" and p["exit_trigger_kind"] == "quote"
    assert "T+1" in a["paper_trading"]["decisions"][p["code"]]
    overlay = annotate_plan(plan, a)
    assert any(i["code"] == p["code"] and not i.get("execution") for i in overlay["exits"])
    restarted = Database(settings.database_url)
    rebound = quotes(CURRENT + timedelta(minutes=20))
    execute_quotes(restarted, settings, rebound, CURRENT + timedelta(minutes=20))
    still = current_account(restarted, settings, report["day"], CURRENT + timedelta(minutes=20))
    assert still["positions"][0]["exit_due"] == p["exit_due"]
    assert still["positions"][0]["exit_trigger_time"] == p["exit_trigger_time"]
    assert still["fills"] == a["fills"]
    restarted.engine.dispose()


def test_intraday_stop_executes_without_waiting_for_close_and_does_not_reenter(settings, db):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    saved = db.rows(select(paper_accounts))[0]
    p = saved["payload"]["account"]["positions"][0]
    p["entry_day"] = "20261014"  # eligible for today's sell
    with db.engine.begin() as c:
        db.upsert(c, paper_accounts, saved)
    later = CURRENT + timedelta(minutes=10)
    payload = quotes(later)
    payload["quotes"]["600001.SH"].update(price=9.6, low=9.5)
    result = execute_quotes(db, settings, payload, later)
    assert result["new_fills"] == 1
    a = current_account(db, settings, "20261014", later)
    assert not a["positions"] and a["fills"][0]["reason"] == "盘中风险止损触发"
    assert a["fills"][0]["price"] == pytest.approx(9.6 * .9995)
    before = deepcopy(a["fills"])
    execute_quotes(db, settings, quotes(CURRENT + timedelta(minutes=20)), CURRENT + timedelta(minutes=20))
    assert current_account(db, settings, "20261014", CURRENT + timedelta(minutes=20))["fills"] == before


def test_trend_context_uses_only_complete_prior_sessions_not_future_bars(settings, db):
    setup(db)
    base = CURRENT.date() - timedelta(days=30)
    with db.engine.begin() as c:
        c.execute(trading_days.delete().where(trading_days.c.day < "20261015"))
        c.execute(bars.delete().where(bars.c.day < "20261015"))
        for offset in range(22):
            day = (base + timedelta(days=offset)).strftime("%Y%m%d")
            db.upsert(c, trading_days, {"source": "demo", "day": day, "is_open": 1})
            db.upsert(c, bars, {"source": "demo", "code": "600000.SH", "day": day,
                "open": 10, "high": 11, "low": 8, "close": 9 if offset >= 19 else 10,
                "volume": 1e7, "amount": 1e8, "adj": 1, "pct": 0})
    p = {**position(), "signal_day": "20261013", "strategy": "breakout"}
    cutoff = (base + timedelta(days=20)).strftime("%Y%m%d")
    prior = context(db, "demo", p, cutoff)
    assert prior["trend_exit"]
    advance(p, tick(10.5), prior, simulation_profile())
    assert "趋势退出" in p["exit_due"] and p["exit_trigger_day"] == cutoff
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day > cutoff).values(close=100000))
    assert context(db, "demo", p, cutoff) == prior
    with db.engine.begin() as c:
        c.execute(bars.delete().where(bars.c.day == cutoff))
    assert not context(db, "demo", p, cutoff)["trend_exit"]
