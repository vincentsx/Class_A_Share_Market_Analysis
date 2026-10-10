from copy import deepcopy

import pytest

from stock_daily.analysis import calculate_metrics, signal_outcome
from stock_daily.config import Settings
from stock_daily.providers import DemoProvider


def history():
    provider = DemoProvider(Settings())
    dates = provider.calendar("20260601", "20261009")[-65:]
    provider.fetch_day(dates[-1])
    return [next(r for r in provider.fetch_day(day).bars if r["code"] == "600000.SH") for day in dates]


def test_indicators_do_not_include_today_in_breakout_reference():
    rows = history()
    previous_max = max(r["high"] for r in rows[-21:-1])
    rows[-1]["close"] = previous_max * 1.05
    rows[-1]["high"] = previous_max * 1.06
    rows[-1]["volume"] = sum(r["volume"] for r in rows[-21:-1]) / 20 * 2
    metric = calculate_metrics(rows, 0)
    assert metric["breakout"]
    assert metric["prior_high20"] == previous_max
    assert metric["volume_ratio"] == pytest.approx(2)


def test_split_does_not_create_fake_return_or_trend_break():
    original = history()
    split = deepcopy(original)
    for row in split[-20:]:
        for key in ("open", "high", "low", "close"):
            row[key] /= 2
        row["adj"] *= 2
    before, after = calculate_metrics(original, 0), calculate_metrics(split, 0)
    assert after["return20"] == pytest.approx(before["return20"])
    assert after["trend"] == before["trend"]
    assert after["ma20"] == pytest.approx(before["ma20"] / 2)
    assert after["corporate_action"]


def test_short_history_does_not_generate_indicators():
    assert calculate_metrics(history()[:64], 0) is None


def test_signal_horizon_counts_calendar_days_not_available_bars():
    rows = history()[:22]
    origin = rows[0]
    signal = {"day": origin["day"], "payload": {"invalidation_adj": 0}}
    dates = [r["day"] for r in rows]
    outcome = signal_outcome(signal, rows[:4] + rows[5:], dates)
    assert outcome["horizons"]["5"]["status"] == "missing"
    assert outcome["horizons"]["20"]["status"] == "missing"


def test_tracking_adjusts_split_and_keeps_unmatured_observations_pending():
    rows = history()[:7]
    for row in rows[1:]:
        row["close"] = rows[0]["close"] / 2
        row["low"] = rows[0]["close"] / 2 * 0.99
        row["adj"] = 2
    signal = {"day": rows[0]["day"], "payload": {"invalidation_adj": 0}}
    outcome = signal_outcome(signal, rows, [r["day"] for r in rows])
    assert outcome["horizons"]["5"]["return"] == pytest.approx(0)
    assert outcome["horizons"]["10"]["status"] == "pending"
