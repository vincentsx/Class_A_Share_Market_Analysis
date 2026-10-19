"""Forward-only confirmation of the pre-published daily candidate pool.

No daily volume projection, reconstructed minute bars, or use of today's high
as a previously observed price. Snapshots are actual validated worker quotes.
"""

import math
from datetime import datetime

from .config import SHANGHAI

VERSION = "entry-intraday-v3"
PULLBACK_TOLERANCE = .002
METHOD = ("盘后候选池每次刷新重评；至少两次有效观察，价格保持在开盘价与当日成交均价之上、"
          "相邻报价回落不超过0.2%且区间表现不弱于沪深300，区间量额须增加；"
          "指数低于开盘且继续走弱时暂停买入。按当日相对强度重排，成交前重算风险与资金。"
          "盘中触及日线失效参考价先暂停入场；连续两次有效报价严格低于参考价，"
          "或14:30起有效报价仍严格低于参考价时取消当日入场。等于参考价仅暂停；"
          "重新站回后须连续两次有效报价确认；"
          "开盘低于参考价不锁定全天取消，开盘追高超限及当日已卖出仍取消；不追补历史成交。")

# Only these v1 decisions become observable again when the current rule starts forward execution.
LEGACY_INVALIDATION_CANCELLATIONS = frozenset({
    "盘中已达到信号失效位置，取消当日入场",
    "开盘已达到失效位置，取消本次入场",
})


def vwap(quote):
    try:
        amount, volume = quote["amount"], quote["volume"]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (amount, volume)):
            return None
        price = amount / volume
        return price if quote["low"] - .0051 <= price <= quote["high"] + .0051 else None
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def continuous(current, prior, interval):
    """Compare actual same-session observations on the exchange's local clock."""
    if not prior:
        return False
    try:
        stamps = [datetime.fromisoformat(q["quote_time"]) for q in (current, prior)]
        if any(stamp.tzinfo is None for stamp in stamps):
            return False
        stamp, before = [value.astimezone(SHANGHAI) for value in stamps]
        elapsed = (stamp - before).total_seconds()
        return (stamp.date() == before.date() and (stamp.hour < 12) == (before.hour < 12)
                and max(60, (interval - 2) * 60) <= elapsed <= (2 * interval + 2) * 60
                and math.isclose(current["previous"], prior["previous"], abs_tol=.0051, rel_tol=0)
                and math.isclose(current["open"], prior["open"], abs_tol=.0051, rel_tol=0))
    except (KeyError, ValueError, TypeError):
        return False


def confirmation(quote, previous_quote, benchmark, previous_benchmark, *, stop, interval):
    result = {"version": VERSION, "status": "waiting", "reason": "等待连续有效盘中观察",
              "quote_time": quote["quote_time"], "vwap": vwap(quote), "invalidation_price": stop,
              "max_quote_pullback": PULLBACK_TOLERANCE}
    if quote["price"] <= stop:
        if quote["price"] < stop:
            stamp = datetime.fromisoformat(quote["quote_time"]).astimezone(SHANGHAI)
            if stamp.strftime("%H:%M") >= "14:30":
                return {**result, "status": "cancelled", "reason": "14:30起有效报价低于日线失效参考价，取消当日入场"}
            if (previous_quote and previous_quote["price"] < stop
                    and continuous(quote, previous_quote, interval)):
                return {**result, "status": "cancelled", "reason": "连续两次有效报价低于日线失效参考价，取消当日入场",
                        "previous_quote_time": previous_quote["quote_time"]}
        return {**result, "status": "paused",
                "reason": "报价触及日线失效参考价，暂停入场；重新站回后须连续两次有效报价确认"}
    if not benchmark:
        return {**result, "reason": "沪深300报价待核验，暂停新增买入"}
    result["relative_day"] = (quote["price"] / quote["previous"]
                              - benchmark["price"] / benchmark["previous"]) * 100
    if not previous_quote or not previous_benchmark:
        return result
    for current, prior in ((quote, previous_quote), (benchmark, previous_benchmark)):
        if not continuous(current, prior, interval):
            return {**result, "reason": "观察间隔、时段或价格尺度不连续，重新积累确认数据"}
    if previous_quote["price"] <= stop:
        return {**result, "reason": "报价已重新站回日线失效参考价，等待下一次有效报价继续确认"}
    if benchmark["price"] < benchmark["open"] and benchmark["price"] < previous_benchmark["price"]:
        return {**result, "reason": "沪深300低于开盘且继续走弱，暂停新增买入"}
    old_vwap = vwap(previous_quote)
    if result["vwap"] is None or old_vwap is None:
        return {**result, "reason": "成交量额或均价无法核验，等待后续刷新"}
    volume = quote["volume"] - previous_quote["volume"]
    amount = quote["amount"] - previous_quote["amount"]
    result.update(interval_volume=volume, interval_amount=amount,
                  relative_interval=(quote["price"] / previous_quote["price"]
                                     - benchmark["price"] / previous_benchmark["price"]) * 100,
                  previous_quote_time=previous_quote["quote_time"])
    if volume <= 0 or amount <= 0:
        return {**result, "reason": "区间成交量额未增加或发生回退，等待后续刷新"}
    interval_price = amount / volume
    if not quote["low"] - .0051 <= interval_price <= quote["high"] + .0051:
        return {**result, "reason": "区间量额尺度异常，等待后续刷新"}
    if (previous_quote["price"] < max(previous_quote["open"], old_vwap)
            or quote["price"] < max(quote["open"], result["vwap"], previous_quote["price"] * (1 - PULLBACK_TOLERANCE))):
        return {**result, "reason": "走势未连续站稳开盘价与成交均价，或报价回落超过0.2%，等待转强确认"}
    if result["relative_interval"] < -1e-9:
        return {**result, "reason": "区间表现弱于沪深300，等待转强确认"}
    return {**result, "status": "eligible", "reason": "连续报价、区间量额与相对强度确认通过"}


def rank_key(signal, evaluations):
    evaluation = evaluations.get(f"{signal['day']}|{signal['code']}|{signal['strategy']}", {})
    return (evaluation.get("status") != "eligible", -evaluation.get("relative_day", -1e9),
            -evaluation.get("relative_interval", -1e9), -signal["payload"].get("relative20", 0),
            signal["code"], signal["strategy"])
