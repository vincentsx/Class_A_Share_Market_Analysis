"""Forward-only confirmation of the pre-published daily candidate pool.

No daily volume projection, reconstructed minute bars, or use of today's high
as a previously observed price. Snapshots are actual validated worker quotes.
"""

import math
from datetime import datetime

VERSION = "entry-intraday-v1"
METHOD = ("盘后候选池每次刷新重评；至少两次有效观察，价格保持在开盘价与当日成交均价之上、"
          "不弱于上次报价且区间表现不弱于沪深300，区间量额须增加；"
          "指数低于开盘且继续走弱时暂停买入。按当日相对强度重排，成交前重算风险与资金。"
          "观察到信号失效后当日取消；不追补历史成交。")


def vwap(quote):
    try:
        amount, volume = quote["amount"], quote["volume"]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (amount, volume)):
            return None
        price = amount / volume
        return price if quote["low"] - .0051 <= price <= quote["high"] + .0051 else None
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def confirmation(quote, previous_quote, benchmark, previous_benchmark, *, stop, interval):
    result = {"version": VERSION, "status": "waiting", "reason": "等待连续有效盘中观察",
              "quote_time": quote["quote_time"], "vwap": vwap(quote)}
    if quote["price"] <= stop:
        return {**result, "status": "cancelled", "reason": "盘中已达到信号失效位置，取消当日入场"}
    if not benchmark:
        return {**result, "reason": "沪深300报价待核验，暂停新增买入"}
    result["relative_day"] = (quote["price"] / quote["previous"]
                              - benchmark["price"] / benchmark["previous"]) * 100
    if not previous_quote or not previous_benchmark:
        return result
    for current, prior in ((quote, previous_quote), (benchmark, previous_benchmark)):
        stamp, before = datetime.fromisoformat(current["quote_time"]), datetime.fromisoformat(prior["quote_time"])
        elapsed = (stamp - before).total_seconds()
        if (stamp.date() != before.date() or (stamp.hour < 12) != (before.hour < 12)
                or not max(60, (interval - 2) * 60) <= elapsed <= (2 * interval + 2) * 60
                or not math.isclose(current["previous"], prior["previous"], abs_tol=.0051, rel_tol=0)
                or not math.isclose(current["open"], prior["open"], abs_tol=.0051, rel_tol=0)):
            return {**result, "reason": "观察间隔、时段或价格尺度不连续，重新积累确认数据"}
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
    if (previous_quote["price"] <= stop or previous_quote["price"] < max(previous_quote["open"], old_vwap)
            or quote["price"] < max(quote["open"], result["vwap"], previous_quote["price"])):
        return {**result, "reason": "走势未连续站稳开盘价与成交均价，等待转强确认"}
    if result["relative_interval"] < -1e-9:
        return {**result, "reason": "区间表现弱于沪深300，等待转强确认"}
    return {**result, "status": "eligible", "reason": "连续报价、区间量额与相对强度确认通过"}


def rank_key(signal, evaluations):
    evaluation = evaluations.get(f"{signal['day']}|{signal['code']}|{signal['strategy']}", {})
    return (evaluation.get("status") != "eligible", -evaluation.get("relative_day", -1e9),
            -evaluation.get("relative_interval", -1e9), -signal["payload"].get("relative20", 0),
            signal["code"], signal["strategy"])
