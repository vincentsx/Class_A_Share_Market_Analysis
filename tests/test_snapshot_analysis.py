from copy import deepcopy
from dataclasses import replace

import pytest

from stock_daily.db import bars, snapshots, trading_days
from stock_daily.snapshot_analysis import analyze_snapshot

DAY = "20261012"
DATES = [f"202609{d:02}" for d in range(1, 21)]
CODES = [f"60000{i}.SH" for i in range(10)]


def quote(code, pct=2, volume=15_000_000, amount=150_000_000, slot="close", day=DAY):
    price = 10 * (1 + pct / 100)
    return {"code": code, "name": code[:6], "industry": "科技" if code in CODES[:5] else "银行",
            "price": price, "previous": 10, "open": 10, "high": max(10.5, price),
            "low": min(10, price), "pct": pct, "volume": volume, "amount": amount,
            "quote_time": f"{day[:4]}-{day[4:6]}-{day[6:]}T{'11:30:00' if slot == 'midday' else '15:00:00'}+08:00"}


def payload(slot="close", day=DAY):
    rows = [quote(c, pct=2 if c in CODES[:5] else -1, slot=slot, day=day) for c in CODES]
    return {"day": day, "slot": slot, "quotes": rows,
            "market": {"amount": sum(r["amount"] for r in rows), "benchmark": {"pct": 0.5}}}


def seed(db, source="demo", adj_change=False, missing=False):
    with db.engine.begin() as connection:
        for day in DATES:
            db.upsert(connection, trading_days, {"source": source, "day": day, "is_open": 1})
        for code in CODES:
            for day in DATES:
                if missing and code == CODES[0] and day == DATES[-2]:
                    continue
                db.upsert(connection, bars, {
                    "source": source, "code": code, "day": day,
                    "open": 10, "high": 10.5, "low": 9.8, "close": 10,
                    "volume": 10_000_000, "amount": 100_000_000,
                    "adj": 2 if adj_change and day == DATES[-2] else 1,
                    "pct": 0, "turnover": None, "pe": None, "pb": None,
                })


def save(db, p, source="demo"):
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, {"source": source, "day": p["day"], "slot": p["slot"], "payload": p})


def analyze(db, settings, p):
    stocks = {r["code"]: {"industry": r["industry"]} for r in p["quotes"]}
    return analyze_snapshot(db, settings, p, stocks)


def test_noon_never_calls_half_day_volume_shrinkage_against_full_day(db, settings):
    seed(db)
    p = payload("midday")
    p["quotes"][0]["volume"] = 6_000_000
    p["quotes"][1]["volume"] = 16_000_000
    result = analyze(db, settings, p)
    first, second = result["quotes"][:2]
    assert first["daily_volume_ratio"] == pytest.approx(0.6)
    assert first["volume_ratio"] is None and not any(t.startswith("缩量") for t in first["tags"])
    assert "上午量已超全天均量1.5倍" in second["tags"]
    assert not result["analysis"]["volume_down"]


def test_noon_compares_contiguous_same_slot_history_and_ignores_future_and_other_source(db, settings):
    seed(db)
    for d in DATES[-5:]:
        old = payload("midday", d)
        for r in old["quotes"]:
            r["volume"] = 5_000_000
        save(db, old)
    save(db, payload("midday", DAY))
    save(db, payload("midday", "20261013"))
    save(db, payload("midday", DATES[-1]), source="baostock")
    result = analyze(db, settings, payload("midday"))
    row = result["quotes"][0]
    assert row["volume_ratio"] == pytest.approx(3)
    assert row["volume_samples"] == 5
    assert "5个连续交易日午间" in row["volume_baseline"]
    assert "放量上涨" in row["tags"]


def test_noon_does_not_bridge_missing_session_or_stock(db, settings):
    seed(db)
    for d in DATES[-6:]:
        old = payload("midday", d)
        if d == DATES[-3]:
            old["quotes"] = old["quotes"][1:]
        save(db, old)
    result = analyze(db, settings, payload("midday"))
    assert result["quotes"][0]["volume_samples"] == 2
    assert result["quotes"][0]["volume_ratio"] is None
    assert result["quotes"][1]["volume_samples"] == 6


def test_close_uses_prior_twenty_sessions_not_today_or_another_source(db, settings):
    seed(db)
    seed(db, "baostock")
    with db.engine.begin() as connection:
        row = {"source": "demo", "day": DAY, "code": CODES[0], "open": 10,
               "high": 10.5, "low": 9.8, "close": 10, "volume": 1_000_000_000,
               "amount": 10_000_000_000, "adj": 1, "pct": 0}
        db.upsert(connection, bars, row)
    p = payload()
    p["quotes"][0]["volume"] = 6_000_000
    result = analyze(db, settings, p)
    assert result["quotes"][0]["volume_ratio"] == pytest.approx(0.6)
    assert "缩量上涨" in result["quotes"][0]["tags"]
    assert result["quotes"][1]["volume_ratio"] == pytest.approx(1.5)
    assert result["quotes"][1]["ma20"] == 10
    assert result["quotes"][1]["prior_high20"] == 10.5


@pytest.mark.parametrize("problem", ["missing", "adjustment", "previous"])
def test_invalid_price_windows_do_not_produce_volume_or_price_signals(db, settings, problem):
    seed(db, adj_change=problem == "adjustment", missing=problem == "missing")
    p = payload()
    if problem == "previous":
        p["quotes"][0]["previous"] = 9
    row = analyze(db, settings, p)["quotes"][0]
    assert row["volume_ratio"] is None and row["ma20"] is None
    assert row["risks"]


def test_hotspots_track_breadth_continuity_and_participation(db, settings):
    seed(db)
    for d in DATES[-2:]:
        save(db, payload("close", d))
    result = analyze(db, settings, payload())
    hot = result["analysis"]["hot_sectors"][0]
    assert hot["name"] == "科技" and hot["status"] == "持续活跃"
    assert hot["active_streak"] == 3 and hot["up_ratio"] == 100
    assert hot["amount_share"] == 50 and hot["share_change_pp"] == 0
    assert hot["volume_up_count"] == 5
    assert result["industries"][1]["status"] == "偏弱"


def test_new_hotspot_requires_comparable_prior_snapshot(db, settings):
    seed(db)
    old = payload(day=DATES[-1])
    for r in old["quotes"][:5]:
        r["pct"] = 0.5
    save(db, old)
    hot = analyze(db, settings, payload())["analysis"]["hot_sectors"][0]
    assert hot["status"] == "新近活跃" and hot["active_streak"] == 1
    assert hot["pct_change_pp"] == pytest.approx(1.5)
    # Losing one of five stocks makes this industry incomparable.
    old["quotes"] = old["quotes"][1:]
    save(db, old)
    hot = analyze(db, settings, payload())["analysis"]["hot_sectors"][0]
    assert hot["status"] == "活跃" and hot["pct_change_pp"] is None
    assert hot["share_change_pp"] is None


def test_close_afternoon_uses_actual_noon_price_and_cumulative_difference(db, settings):
    seed(db)
    noon = payload("midday")
    for r in noon["quotes"]:
        r["price"] = 10
        r["volume"] = 6_000_000
        r["amount"] = 60_000_000
    save(db, noon)
    result = analyze(db, settings, payload())
    row = result["quotes"][0]
    assert row["afternoon"]["return"] == pytest.approx(2)
    assert row["afternoon"]["volume"] == 9_000_000
    assert row["afternoon"]["amount"] == 90_000_000
    hot = result["analysis"]["hot_sectors"][0]
    assert hot["afternoon"]["return"] == pytest.approx(2)
    assert hot["afternoon"]["amount_share"] == pytest.approx(50)
    assert any("午后走强" in s for s in result["analysis"]["summaries"])


def test_volume_correction_is_excluded_not_negative_afternoon_trading(db, settings):
    seed(db)
    noon = payload("midday")
    for r in noon["quotes"]:
        r["volume"] = 6_000_000
        r["amount"] = 60_000_000
    noon["quotes"][0]["amount"] = 200_000_000
    save(db, noon)
    result = analyze(db, settings, payload())
    assert result["quotes"][0]["afternoon"] is None
    assert any("回退" in s for s in result["quotes"][0]["risks"])
    assert result["analysis"]["afternoon_count"] == 9
    assert result["analysis"]["hot_sectors"][0]["afternoon"] is None


def test_other_source_noon_is_not_used_and_st_is_not_an_opportunity(db, settings):
    seed(db)
    save(db, payload("midday"), source="baostock")
    p = payload()
    p["quotes"][0]["name"] = "*ST测试"
    result = analyze(db, settings, p)
    assert all(r["afternoon"] is None for r in result["quotes"])
    assert CODES[0] not in {r["code"] for r in result["analysis"]["volume_up"]}
    assert result["analysis"]["afternoon_amount"] is None
    assert result["analysis"]["counts"]["volume_up"] == 9


def test_reanalysis_is_deterministic_and_separate_from_other_sources(db, settings):
    seed(db)
    p = payload()
    result = analyze(db, settings, p)
    assert analyze(db, settings, deepcopy(result)) == result
    other = analyze(db, replace(settings, source="baostock"), payload())
    assert other["analysis"]["volume_coverage"] == 0
