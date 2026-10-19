"""Shared, prior-information-only entry policy for simulation and intentions."""

import math


def market_gate(report, config):
    if not config.market_filter:
        return None
    market = (report or {}).get("market", {})
    breadth, trend = market.get("above_ma20_ratio"), market.get("benchmark_return20")
    if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (breadth, trend)):
        return "缺少当时市场状态，暂停新增仓位"
    if breadth < config.min_market_breadth or trend <= 0:
        return "市场趋势或广度偏弱，暂停新增仓位"
    return None


def entry_size(config, *, cash, equity, price, previous, payload, lot, industry_value=0,
               volume_capacity=None, risk_stop=None):
    """Size from known prices, frozen invalidation/ATR, and prior-day volume.

    The risk estimate includes estimated round-trip fees. Gaps and delayed exits
    can exceed this estimate; it is not a guaranteed maximum loss.
    A supplied live risk_stop already includes its frozen ATR floor and loss cap;
    the daily replay retains its original distance calculation when omitted.
    """
    from .backtest import fees

    stop = payload.get("invalidation_adj")
    if not isinstance(stop, (int, float)) or not math.isfinite(stop) or stop <= 0:
        return {"reason": "失效位置无效，排除模拟买入", "shares": 0}
    stop /= previous["adj"]
    if risk_stop is not None and (not isinstance(risk_stop, (int, float))
            or not math.isfinite(risk_stop) or not 0 < risk_stop < price):
        return {"reason": "初始保护价无效，排除模拟买入", "shares": 0}
    estimated_stop = stop if risk_stop is None else risk_stop
    budget = min(cash, equity * config.weight)
    if config.max_industry_weight:
        budget = min(budget, max(0, equity * config.max_industry_weight - industry_value))
    risk_budget = equity * config.risk_fraction if config.risk_fraction else None
    distance = price - estimated_stop
    if config.risk_fraction:
        if price <= stop or distance <= 0:
            return {"reason": "入场价不高于失效位置，排除模拟买入", "shares": 0}
        if config.atr_risk_floor:
            atr = payload.get("atr14")
            if not isinstance(atr, (float, int)) or not math.isfinite(atr) or atr <= 0:
                return {"reason": "缺少冻结ATR，无法核验风险仓位", "shares": 0}
            if risk_stop is None:
                distance = max(distance, atr * config.atr_risk_floor)
    capacity = min(max(0, budget) / price, previous["volume"] * config.participation)
    if volume_capacity is not None:
        capacity = min(capacity, max(0, volume_capacity))
    if risk_budget is not None:
        capacity = min(capacity, risk_budget / distance)
    shares = math.floor(capacity / lot) * lot
    while shares >= lot:
        entry_fee = fees(shares * price, config)
        estimated_risk = shares * max(0, distance) + entry_fee + fees(shares * estimated_stop, config, sell=True)
        if shares * price + entry_fee <= budget and (risk_budget is None or estimated_risk <= risk_budget):
            break
        shares -= lot
    reason = None
    if shares < lot:
        reason = "资金、风险预算、行业上限或流动性不足最小申报数量"
    result = {"shares": max(0, shares), "budget": budget, "invalidation_price": stop,
            "risk_budget": risk_budget, "risk_distance": max(0, distance),
            "estimated_risk": estimated_risk if shares >= lot else None, "reason": reason}
    if risk_stop is not None:
        result["risk_stop"] = risk_stop
    return result


def entry_guard(config, opening, previous, payload):
    if config.max_entry_gap and opening > previous["close"] * (1 + config.max_entry_gap):
        return "开盘涨幅超过追高上限，取消本次入场"
    if config.require_valid_entry:
        stop = payload.get("invalidation_adj")
        if not isinstance(stop, (int, float)) or not math.isfinite(stop) or stop <= 0:
            return "失效位置无效，排除模拟买入"
        if opening * previous["adj"] <= stop:
            return "开盘已达到失效位置，取消本次入场"
    return None


def industry_of(payload):
    return payload.get("industry") or "未分类"
