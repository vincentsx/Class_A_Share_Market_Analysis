from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select
from test_decision_ai import save_analysis
from test_entry_policy import batch
from test_paper_trading import CURRENT, setup

from stock_daily.candidate_pool import VERSION, active_candidates, discover, persist
from stock_daily.db import Database, intraday_candidates, realtime_reports, signals, trade_alerts
from stock_daily.paper_trading import current_account, execute_quotes


def scan(at=CURRENT):
    row = deepcopy(batch(at)["quotes"]["600001.SH"])
    row.update(
        industry="行业A",
        ma20=10.1,
        ma60=10.0,
        prior_high20=10.25,
        atr14=0.4,
        base_adj=1,
        history_complete=True,
        vwap=10.25,
        pct=(row["price"] / row["previous"] - 1) * 100,
        volume=5e6,
        amount=51250000,
    )
    report = {
        "day": at.strftime("%Y%m%d"),
        "window": at.strftime("%Y%m%d-%H%M"),
        "source": "demo",
        "basis_day": "20261014",
        "captured_at": at.isoformat(),
        "coverage": 100,
        "candidate_pool_version": VERSION,
        "market": {"benchmark": {"pct": 0.1}},
    }
    return row, report


def save_scan(db, config, rows, report):
    r, save = save_analysis(db, config, at=datetime.fromisoformat(report["captured_at"]))
    report.update({k: r[k] for k in ("evidence", "decision_universe", "news", "ai")})
    report["intraday_candidates"] = discover(db, config, report, rows, {q["code"]: {} for q in rows})
    with db.engine.begin() as c:
        persist(c, db, report)
        db.upsert(
            c,
            realtime_reports,
            {
                "source": config.source,
                "day": report["day"],
                "window": report["window"],
                "captured_at": report["captured_at"],
                "payload": report,
            },
        )
    return report


def live_batch(at, price=10.3):
    p = batch(at, price=price)
    for q in p["quotes"].values():
        q.update(
            volume=q["volume"] + 5e6,
            amount=(q["volume"] + 5e6) * (100.5 if q["code"] == "000300.SH" else 10.25),
        )
    return p


def test_stock_absent_from_daily_signals_can_be_bought_same_day_only_after_discovery_and_new_quotes(
    db, settings
):
    setup(db)
    with db.engine.begin() as c:
        c.execute(delete(signals).where(signals.c.day == "20261014"))
    config = replace(settings, ai_trading_enabled=True)
    first_seen = CURRENT + timedelta(minutes=1)
    row, report = scan(first_seen)
    save_scan(db, config, [row], report)
    assert not db.rows(select(signals).where(signals.c.day == "20261014"))
    at = CURRENT + timedelta(minutes=10)
    execute_quotes(db, config, live_batch(at), at)
    a = current_account(db, config, "20261014", at)
    assert not a["positions"]
    old_fills, old_alerts = deepcopy(a["fills"]), deepcopy(db.rows(select(trade_alerts)))
    restarted = Database(settings.database_url)
    at += timedelta(minutes=10)
    assert execute_quotes(restarted, config, live_batch(at, 10.31), at)["new_fills"] == 1
    a = current_account(restarted, config, "20261014", at)
    buy = a["fills"][0]
    assert buy["signal_origin"] == "intraday" and buy["strategy"] == "intraday_breakout"
    assert buy["published_at"] == first_seen.isoformat() and buy["signal_day"] == "20261015"
    assert buy["entry_confirmation"]["previous_quote_time"] > first_seen.isoformat()
    assert a["fills"][1:] == old_fills
    assert a["summary"]["available_cash"] == pytest.approx(
        a["summary"]["initial_capital"] + sum(f["cash_flow"] for f in a["fills"])
    )
    assert len(db.rows(select(trade_alerts))) == len(old_alerts) + 1
    assert execute_quotes(restarted, config, live_batch(at, 10.31), at)["status"] == "unchanged"
    assert current_account(restarted, config, "20261014", at)["fills"] == a["fills"]


def test_first_discovery_and_reference_are_immutable_across_rescans_and_restart(db, settings):
    setup(db)
    row, report = scan()
    save_scan(db, settings, [row], report)
    before = db.rows(select(intraday_candidates))[0]
    later = CURRENT + timedelta(minutes=10)
    row, report = scan(later)
    row.update(atr14=0.7, prior_high20=10.27)
    save_scan(Database(settings.database_url), settings, [row], report)
    assert db.rows(select(intraday_candidates))[0] == before
    assert report["intraday_candidates"][0]["payload"] == before["payload"]


@pytest.mark.parametrize(
    "problem",
    [
        "stale",
        "future",
        "missing_history",
        "adjustment",
        "st",
        "low_amount",
        "weak_relative",
        "below_vwap",
        "no_start",
        "partial",
        "lunch",
        "calendar_gap",
    ],
)
def test_invalid_or_unconfirmed_market_discovery_is_excluded(db, settings, problem):
    setup(db)
    row, report = scan()
    if problem == "stale":
        row["quote_time"] = (CURRENT - timedelta(minutes=3)).isoformat()
    elif problem == "future":
        row["quote_time"] = (CURRENT + timedelta(minutes=1)).isoformat()
    elif problem == "missing_history":
        row["history_complete"] = False
    elif problem == "adjustment":
        row["base_adj"] = None
    elif problem == "st":
        row["name"] = "ST测试"
    elif problem == "low_amount":
        row["amount"] = 1e6
    elif problem == "weak_relative":
        row["pct"] = 0
    elif problem == "below_vwap":
        row["vwap"] = 10.4
    elif problem == "no_start":
        row.update(prior_high20=11, ma20=10.1)
    elif problem == "partial":
        report["coverage"] = 90
    elif problem == "lunch":
        report["captured_at"] = CURRENT.replace(hour=12).isoformat()
    elif problem == "calendar_gap":
        report["basis_day"] = "20261013"
    assert discover(db, settings, report, [row], {row["code"]: {}}) == []


def test_reclaim_does_not_require_yesterdays_bullish_trend_or_existing_daily_candidate(db, settings):
    setup(db)
    row, report = scan()
    row.update(ma20=10.21, ma60=10.24, prior_high20=11)
    candidates = discover(db, settings, report, [row], {row["code"]: {}})
    assert candidates[0]["strategy"] == "intraday_reclaim"
    assert candidates[0]["payload"]["invalidation_adj"] == 10.24


@pytest.mark.parametrize("problem", ["expired", "next_session", "other_source", "missing_row", "basis"])
def test_candidate_requires_fresh_same_session_saved_snapshot(db, settings, problem):
    setup(db)
    row, report = scan()
    save_scan(db, settings, [row], report)
    at = CURRENT + timedelta(minutes=1)
    source = settings
    basis = "20261014"
    if problem == "expired":
        at += timedelta(minutes=23)
    elif problem == "next_session":
        at = CURRENT.replace(hour=13)
    elif problem == "other_source":
        source = replace(settings, source="baostock")
    elif problem == "missing_row":
        with db.engine.begin() as c:
            c.execute(delete(intraday_candidates))
    elif problem == "basis":
        basis = "20261013"
    assert active_candidates(db, source, "20261015", basis, at) == []


def test_candidate_and_report_save_are_atomic(db, settings):
    setup(db)
    row, report = scan()
    report["intraday_candidates"] = discover(db, settings, report, [row], {row["code"]: {}})
    with pytest.raises(RuntimeError):
        with db.engine.begin() as c:
            persist(c, db, report)
            raise RuntimeError("report save failed")
    assert db.rows(select(intraday_candidates)) == []


def test_full_market_build_freezes_only_prior_daily_references(db, settings):
    from sqlalchemy import delete

    from stock_daily.db import bars, trading_days
    from stock_daily.realtime import build

    setup(db, source="baostock")
    config = replace(settings, source="baostock")
    dates = []
    day = CURRENT.date() - timedelta(days=1)
    while len(dates) < 65:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    with db.engine.begin() as c:
        c.execute(delete(bars).where(bars.c.source == config.source))
        for d in dates:
            db.upsert(c, trading_days, {"source": config.source, "day": d, "is_open": 1})
            db.upsert(
                c,
                bars,
                {
                    "source": config.source,
                    "code": "600001.SH",
                    "day": d,
                    "open": 10.1,
                    "close": 10.2,
                    "low": 10.0,
                    "high": 10.26,
                    "volume": 1e7,
                    "amount": 1e8,
                    "adj": 1,
                    "pct": 1,
                },
            )
        db.upsert(
            c,
            bars,
            {
                "source": config.source,
                "code": "600001.SH",
                "day": "20261015",
                "open": 999,
                "close": 999,
                "low": 999,
                "high": 1000,
                "volume": 1e7,
                "amount": 1e8,
                "adj": 1,
                "pct": 1,
            },
        )
    q = live_batch(CURRENT)["quotes"]
    q['000300.SH']['price'] = 100.5
    quotes = {k: v for k, v in q.items() if k != "600000.SH"}
    r = build(
        db,
        config,
        "20261015",
        "20261015-1000",
        quotes,
        {"600001.SH": {"name": "测试股票", "industry": "行业A"}},
        "20261014",
        CURRENT,
    )
    assert len(r["intraday_candidates"]) == 1
    item = r["intraday_candidates"][0]["payload"]
    assert item["invalidation_adj"] == 10.26
    assert item["atr14"] == pytest.approx(0.26)
    assert item["discovery_price"] == 10.3
    assert r["candidate_pool_scanned"] == r["intraday_candidate_count"] == 1
    assert "盘中新发现" in r["candidate_pool_method"] or "盘中全市场" in r["candidate_pool_method"]


def test_discovery_does_not_backfill_daily_research_or_historical_accounts(db, settings):
    from stock_daily.backtest import simulate
    from stock_daily.db import reports

    setup(db)
    original = simulate(db, settings, "20261014")
    archives = deepcopy(db.rows(select(reports)))
    row, report = scan()
    save_scan(db, settings, [row], report)
    for minutes in (10, 20):
        at = CURRENT + timedelta(minutes=minutes)
        execute_quotes(db, settings, live_batch(at, 10.3 + minutes / 1000), at)
    assert simulate(db, settings, "20261014") == original
    assert db.rows(select(reports)) == archives
    assert all(s["strategy"] in {"breakout", "pullback"} for s in db.rows(select(signals)))


def test_quotes_before_discovery_or_with_reset_amount_wait_instead_of_cancelling_day(db, settings):
    setup(db, observed=False)
    with db.engine.begin() as c:
        c.execute(delete(signals).where(signals.c.day == "20261014"))
    row, report = scan(CURRENT + timedelta(seconds=30))
    save_scan(db, settings, [row], report)
    for minutes in (1, 11, 21):
        at = CURRENT + timedelta(minutes=minutes)
        p = live_batch(at, 10.3 + minutes / 1000)
        if minutes == 1:
            p["quotes"]["600001.SH"]["amount"] = 1e7
        execute_quotes(db, settings, p, at)
        a = current_account(db, settings, "20261014", at)
        item = a["entry_policy"]["candidates"][0]
        assert item["status"] == ("filled" if minutes == 21 else "waiting")
    assert len([f for f in a["fills"] if f.get("signal_origin") == "intraday"]) == 1


def test_held_intraday_stock_continues_receiving_quotes_without_daily_signals(db, settings):
    from stock_daily.db import paper_accounts
    from stock_daily.portfolio import refresh_portfolio
    from stock_daily.providers import DataError

    setup(db, source="baostock")
    config = replace(settings, source="baostock")
    with db.engine.begin() as c:
        db.upsert(
            c,
            paper_accounts,
            {
                "source": config.source,
                "day": "20261015",
                "payload": {"account": {"positions": [{"code": "600099.SH"}]}},
            },
        )

    class Feed:
        def fetch(self, codes, day, slot):
            assert "600099.SH" in codes
            raise DataError("checked held quote coverage")

    with pytest.raises(DataError, match="checked held quote coverage"):
        refresh_portfolio(db, config, feed=Feed(), current=CURRENT)
