"""Forward-only exit protection using observed quotes and completed prior bars."""

import math
from dataclasses import asdict, dataclass
from statistics import mean

from sqlalchemy import select

from .db import bars, signals, trading_days


@dataclass(frozen=True)
class ExitPolicy:
    version: str = "exit-v3"
    max_loss: float = .06
    initial_atr: float = 2
    min_initial_atr: float = 1
    trailing_atr: float = 2
    activate_r: float = 2
    trend_days: int = 2
    trend_period: int = 20


POLICY = ExitPolicy()
LEGACY_POLICY = ExitPolicy(version="exit-v2", min_initial_atr=0)


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def initial_protection(entry, structural, atr, *, policy=POLICY):
    """Freeze the stop and per-share risk before sizing a newly opened position.

    The loss cap takes precedence when one ATR would exceed it. Missing ATR
    preserves the structural/loss-cap fallback rather than inventing volatility.
    """
    initial = max(structural, entry * (1 - policy.max_loss))
    if positive(atr):
        structural_atr = max(structural, entry - policy.initial_atr * atr)
        if policy.min_initial_atr:
            structural_atr = min(structural_atr, entry - policy.min_initial_atr * atr)
        initial = max(entry * (1 - policy.max_loss), structural_atr)
    return {"initial_stop": initial, "risk_distance": max(0, entry - initial),
            "atr_missing": not positive(atr)}


def context(db, source, position, cutoff):
    """Never read today's high/low or a later close when making intraday decisions."""
    rows = list(reversed(db.rows(select(bars).where(bars.c.source == source,
        bars.c.code == position["code"], bars.c.day <= cutoff).order_by(bars.c.day.desc()).limit(21))))
    original = db.rows(select(signals.c.payload).where(signals.c.source == source,
        signals.c.code == position["code"], signals.c.day == position["signal_day"],
        signals.c.strategy == position["strategy"]))
    atr = original[0]["payload"].get("atr14") if original else None
    if not positive(atr) and len(rows) >= 15:
        ranges = [max(b["high"] * b["adj"] - b["low"] * b["adj"],
                      abs(b["high"] * b["adj"] - a["close"] * a["adj"]),
                      abs(b["low"] * b["adj"] - a["close"] * a["adj"]))
                  for a, b in zip(rows[-15:], rows[-14:])]
        atr = mean(ranges) / rows[-1]["adj"]
    result = {"atr": atr if positive(atr) else None, "basis_day": cutoff, "trend_exit": False}
    expected = list(reversed(db.rows(select(trading_days.c.day).where(trading_days.c.source == source,
        trading_days.c.is_open == 1, trading_days.c.day <= cutoff).order_by(trading_days.c.day.desc()).limit(21))))
    if (len(rows) >= POLICY.trend_period + POLICY.trend_days - 1 and rows[-1]["day"] == cutoff
            and [r["day"] for r in rows] == [r["day"] for r in expected]):
        closes = [r["close"] * r["adj"] for r in rows]
        result["ma20"] = mean(closes[-20:]) / rows[-1]["adj"]
        result["trend_exit"] = all(closes[end - 1] < mean(closes[end - 20:end])
            for end in range(len(closes) - POLICY.trend_days + 1, len(closes) + 1))
        result["close"] = rows[-1]["close"]
    return result


def break_even(position, config):
    """Exit quote needed to cover actual buy costs, projected sell fees and slippage."""
    shares = position["shares"]
    tax = (config.sell_tax_bps + config.transfer_bps) / 10000
    commission = config.commission_bps / 10000
    sell_price = max((position["spent"] + config.min_commission) / (shares * (1 - tax)),
                     position["spent"] / (shares * (1 - tax - commission)))
    return sell_price / (1 - config.slippage_bps / 10000)


def advance(position, quote, prior, config, *, policy=None):
    """Mutate only forward state; a triggered exit stays pending through T+1/limits."""
    entry = position["entry_price"]
    state = position.get("exit_policy")
    if not state:
        selected = policy or LEGACY_POLICY
        atr = prior.get("atr") if positive(prior.get("atr")) else None
        protection = initial_protection(entry, position["invalidation_price"], atr, policy=selected)
        initial = protection["initial_stop"]
        state = {**asdict(selected), "activated_at": quote["quote_time"], "atr": atr,
            "initial_stop": initial, "stop": initial, "peak": max(entry, quote["price"]),
            "risk_unit": max(protection["risk_distance"], atr or 0, entry * .0001),
            "atr_missing": protection["atr_missing"], "trailing_active": False,
            "break_even": break_even(position, config)}
        position["exit_policy"] = state
    state["peak"] = max(state["peak"], quote["price"])
    state["last_quote_time"] = quote["quote_time"]
    state["basis_day"] = prior["basis_day"]
    state["ma20"] = prior.get("ma20")
    if state["peak"] - entry >= state["activate_r"] * state["risk_unit"]:
        state["trailing_active"] = True
    if state["trailing_active"]:
        distance = state["trailing_atr"] * state["atr"] if state["atr"] else state["peak"] * state["max_loss"]
        state["stop"] = max(state["stop"], state["peak"] - distance, state["break_even"])
    if position.get("exit_due"):
        return
    if quote["price"] <= state["stop"]:
        reason = "盘中移动盈利保护触发" if state["trailing_active"] and state["stop"] > state["initial_stop"] else "盘中风险止损触发"
        position.update(exit_due=reason, exit_trigger_day=quote["quote_time"][:10].replace("-", ""),
            exit_trigger_kind="quote", exit_trigger_time=quote["quote_time"],
            exit_trigger_price=quote["price"], exit_trigger_close=None)
    elif prior.get("trend_exit"):
        position.update(exit_due="连续2个交易日收盘低于各自20日均线，趋势退出",
            exit_trigger_day=prior["basis_day"], exit_trigger_close=prior["close"], exit_trigger_kind="daily")
