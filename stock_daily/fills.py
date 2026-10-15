"""One record per executed model buy/sell; quotes and intentions are excluded."""


def make_fill(position, source, side, *, sequence=None, cash_after=None):
    sell = side == "sell"
    prefix = "exit" if sell else "entry"
    price, shares = position.get(f"{prefix}_price"), position.get("shares")
    fee = position.get(f"{prefix}_fee")
    notional = shares * price if shares is not None and price is not None else None
    cash_flow = position.get("proceeds") if sell else position.get("spent")
    if cash_flow is None and notional is not None and fee is not None:
        cash_flow = notional - fee if sell else notional + fee
    if cash_flow is not None and not sell:
        cash_flow = -cash_flow
    opening = position.get(f"{prefix}_open")
    reference = position.get(f"{prefix}_reference_price", opening)
    slippage = shares * ((reference - price) if sell else (price - reference)) if all(
        v is not None for v in (shares, price, reference)) else None
    round_trip = f"{source}|{position['code']}|{position['strategy']}|{position['entry_day']}"
    return {"id": f"{round_trip}|{side}", "round_trip_id": round_trip, "source": source,
            "sequence": sequence, "day": position.get(f"{prefix}_day"), "side": side,
            "code": position["code"], "name": position.get("name"), "strategy": position["strategy"],
            "label": position.get("label"), "rule_version": position.get("rule_version"),
            "shares": shares, "price": price, "open_price": opening, "notional": notional,
            "fee": fee, "cash_flow": cash_flow, "cash_after": cash_after, "slippage_cost": slippage,
            "reference_price": reference, "execution_time": position.get(f"{prefix}_time"),
            "confirmed_at": position.get(f"{prefix}_confirmed_at"),
            "period": "盘中报价模拟" if position.get(f"{prefix}_basis") == "quote" else "开盘模拟",
            "signal_day": position.get("signal_day"), "published_at": position.get("published_at"),
            "entry_day": position["entry_day"], "entry_price": position.get("entry_price"),
            "entry_fee": position.get("entry_fee"), "invalidation_price": position.get("invalidation_price"),
            "entry_reasons": position.get("entry_reasons", []), "entry_risk": position.get("entry_risk"),
            "entry_confirmation": position.get("entry_confirmation"),
            "realized_profit": position.get("profit") if sell else None,
            "reason": position.get("exit_reason") if sell else (
                "盘中连续报价、量额与相对强度确认通过，按有效报价模拟入场" if position.get("entry_confirmation") else
                "已发布信号，按有效盘中报价模拟入场" if position.get("entry_basis") == "quote"
                else "已发布信号，下一交易日开盘模拟入场"),
            "exit_trigger_day": position.get("exit_trigger_day") if sell else None,
            "exit_trigger_close": position.get("exit_trigger_close") if sell else None,
            "exit_trigger_kind": position.get("exit_trigger_kind", "daily") if sell else None,
            "exit_trigger_time": position.get("exit_trigger_time") if sell else None,
            "exit_trigger_price": position.get("exit_trigger_price") if sell else None,
            "exit_policy": dict(position["exit_policy"]) if position.get("exit_policy") else None,
            "exit_delays": position.get("exit_delays", []) if sell else [],
            "origin": "model_execution" if sequence is not None else "legacy_derived"}


def history_fills(simulation, source):
    if not simulation:
        return []
    if simulation.get("fills") is not None:
        return simulation["fills"]
    # Old exports can recover buy/sell amounts, but not their intra-session
    # execution order or cash balance. Leave those unavailable, never fabricate.
    fills = []
    for trade in simulation.get("trades", []):
        fills.extend(make_fill(trade, source, side) for side in ("buy", "sell"))
    fills.extend(make_fill(p, source, "buy") for p in simulation.get("positions", []))
    return sorted(fills, key=lambda f: (f["day"] or "", f["id"]), reverse=True)
