"""Next-session intentions from the report day's data, never future executions."""

from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import select

from .backtest import board, effective_config, fees, simulate, simulation_profile
from .config import SHANGHAI, now
from .db import bars, sessions, signals, trading_days
from .execution import entry_guard, entry_size, industry_of, market_gate


class TransactionView:
    """Read the unpublished report/session/signals inside their publishing transaction."""

    def __init__(self, connection):
        self.connection = connection

    def rows(self, query):
        return [dict(row) for row in self.connection.execute(query).mappings()]


def next_session(db, source, day):
    end = datetime.strptime(day, "%Y%m%d") + timedelta(days=30)
    calendar = {r["day"]: r["is_open"] for r in db.rows(select(trading_days).where(
        trading_days.c.source == source, trading_days.c.day > day,
        trading_days.c.day <= end.strftime("%Y%m%d")))}
    current = datetime.strptime(day, "%Y%m%d") + timedelta(days=1)
    while current <= end:
        date = current.strftime("%Y%m%d")
        if date not in calendar:
            return None
        if calendar[date]:
            return date
        current += timedelta(days=1)
    return None


def build_trade_plan(db, settings, report, *, connection=None, frozen=False, config=None):
    view = TransactionView(connection) if connection is not None else db
    day, source = report["day"], settings.source
    config = config or simulation_profile()
    simulation = simulate(view, settings, day, config)
    target = next_session(view, source, day)
    created = now().isoformat() if frozen else None
    deadline = datetime.strptime(target + "0930", "%Y%m%d%H%M").replace(tzinfo=SHANGHAI) if target else None
    result = {"version": "next-session-v2", "basis_day": day, "target_day": target,
              "calendar_verified": bool(target), "origin": "published" if frozen else "derived",
              "created_at": created, "report_generated_at": report.get("generated_at"),
              "parameters": asdict(config), "account": simulation["summary"],
              "status": "ready" if target else "calendar_pending", "entries": [], "exits": [], "holds": [],
              "method": "按当日日线与默认模拟参数制定，买卖顺序为先卖后买。参考买卖价按当日收盘价加减滑点，"
                        "参考股数、支出和回款不是委托或成交。下一交易日按真实开盘与完整日线重新核验，"
                        "跳空、停牌、涨跌停、复权变化、资金和量能可能改变股数或导致不成交。"
                        "盘中估值不改写本计划；修改模拟页参数不改写默认参数的日线计划。"}
    result["policy"] = {"effective_from": config.policy_start_day or None}
    config = effective_config(config, target or day)
    if frozen and deadline and datetime.fromisoformat(created) >= deadline:
        result["status"] = "late"
    if simulation.get("stop") or simulation["summary"].get("through") != day:
        result.update(status="blocked", message="模拟账户未完整核验到本日，暂停制定买卖计划。",
                      stop=simulation.get("stop"))
        return result
    position_map = {p["code"]: p for p in simulation["positions"]}
    exiting = {code for code, p in position_map.items() if p["exit_due"]}
    cash = simulation["summary"]["available_cash"]
    sale_cash = 0
    for code, p in position_map.items():
        item = {"code": code, "name": p["name"], "shares": p["shares"], "entry_day": p["entry_day"],
                "entry_price": p["entry_price"], "reference_close": p["mark"],
                "held_sessions": p["held_sessions"], "invalidation_price": p["invalidation_price"],
                "reason": p["exit_due"], "exit_trigger_day": p["exit_trigger_day"],
                "exit_delays": p["exit_delays"]}
        if code in exiting:
            price = p["mark"] * (1 - config.slippage_bps / 10000)
            gross = p["shares"] * price
            net = gross - fees(gross, config, sell=True)
            item.update(reference_price=price, reference_proceeds=net,
                        conditions=["下一可模拟开盘退出；买入后最早下一交易日卖出",
                                    "停牌、开盘接近跌停、滑点价超出日内范围或量能不足时延期",
                                    "退出触发价不是保证成交的卖出价"])
            sale_cash += net
            result["exits"].append(item)
        else:
            item["reason"] = "尚未触发收盘失效或持有期届满，继续按原规则观察"
            result["holds"].append(item)
    retained = set(position_map) - exiting
    retained_value = sum(position_map[c]["shares"] * position_map[c]["mark"] for c in retained)
    scenario_cash = cash + sale_cash
    scenario_equity = scenario_cash + retained_value
    result["scenario"] = {"cash_before_sales": cash, "reference_sale_proceeds": sale_cash,
                          "reference_cash_after_sales": scenario_cash,
                          "positions_before_sales": len(position_map), "positions_after_sales": len(retained),
                          "note": "按全部待卖出持仓成功退出、价格等于收盘参考价测算；实际未卖出不能使用该回款或空位。"}
    latest = {r["code"]: r for r in view.rows(select(bars).where(bars.c.source == source, bars.c.day == day))}
    stored = view.rows(select(sessions.c.universe, sessions.c.quality).where(
        sessions.c.source == source, sessions.c.day == day))[0]
    suspended = set(stored["quality"].get("suspended", []))
    candidates = sorted(view.rows(select(signals).where(signals.c.source == source, signals.c.day == day)),
                        key=lambda s: (-s["payload"].get("relative20", 0), s["code"], s["strategy"]))
    reserved = set(retained)
    industry_values = {}
    for code in retained:
        p = position_map[code]
        industry = p.get("industry", "未分类")
        industry_values[industry] = industry_values.get(industry, 0) + p["shares"] * p["mark"]
    independent_cash = cash
    independent_slots = max(0, config.max_positions - len(position_map))
    for rank, signal in enumerate(candidates, 1):
        code, payload = signal["code"], signal["payload"]
        if (config.strategy != "all" and signal["strategy"] != config.strategy
                or config.rule_version and payload.get("rule_version") != config.rule_version
                or config.cohort == "priority" and not payload.get("priority", True)):
            continue
        row, (limit, lot) = latest.get(code), board(code)
        stock = stored["universe"].get(code, {})
        published = payload.get("published_at") or report.get("generated_at")
        item = {"rank": rank, "code": code, "name": payload["name"], "strategy": signal["strategy"],
                "label": payload.get("label", signal["strategy"]), "rule_version": payload.get("rule_version"),
                "signal_day": day, "published_at": published, "reasons": payload.get("reasons", []),
                "risks": payload.get("risks", []), "status": "watch", "reference_shares": None,
                "reference_price": None, "reference_spent": None,
                "invalidation_price": payload.get("invalidation"), "depends_on_sales": False}
        result["entries"].append(item)
        if payload.get("reconstructed"):
            item["reason"] = "重建候选，不是当时发布信号，排除模拟买入"
            continue
        try:
            stamp = datetime.fromisoformat(published)
            if stamp.tzinfo is None or stamp.astimezone(SHANGHAI).strftime("%Y%m%d") < day:
                raise ValueError("发布时间无效")
            if deadline and stamp >= deadline:
                raise ValueError("事后发布")
        except (TypeError, ValueError):
            item["reason"] = "发布时间缺失、时区不明或晚于目标开盘，排除模拟买入"
            continue
        if limit is None or stock.get("is_st") or "ST" in stock.get("name", "").upper() or "退" in stock.get("name", ""):
            item["reason"] = "不支持的板块或风险标记，排除模拟买入"
            continue
        if not row or code in suspended:
            item["reason"] = "当日参考行情缺失或停牌，暂不制定买入数量"
            continue
        price = row["close"] * (1 + config.slippage_bps / 10000)
        item.update(reference_close=row["close"], reference_price=price,
                    invalidation_price=payload["invalidation_adj"] / row["adj"],
                    upper_open_exclusive=row["close"] * (1 + limit - .001),
                    conditions=["仅下一交易日开盘尝试，实际股数按开盘价、费用、资金与前一日量能重新计算",
                                "开盘接近涨停、停牌、复权变化、滑点价超出日内范围等情况不模拟成交",
                                "买入后按收盘失效位置与持有期核验退出，不是盘中触价立即止损"])
        if config.max_entry_gap:
            item["max_open_inclusive"] = row["close"] * (1 + config.max_entry_gap)
            item["conditions"].append(f"开盘涨幅超过{config.max_entry_gap * 100:g}%时取消入场")
        if config.require_valid_entry:
            item["lower_open_exclusive"] = item["invalidation_price"]
            item["conditions"].append("开盘达到或低于失效位置时取消入场")
        reason = ("组合回撤达到阈值，暂停新增仓位" if simulation.get("execution_state", {}).get("entry_paused")
                  else market_gate(report, config))
        reason = reason or entry_guard(config, row["close"], row, payload)
        if reason:
            item["reason"] = reason
            continue
        if code in reserved:
            item["reason"] = "已有持仓或更高优先级同股信号，不重复买入"
            continue
        if len(reserved) >= config.max_positions:
            item["reason"] = "预计仍达到持仓上限，仅保留观察"
            continue
        industry = industry_of(payload)
        sizing = entry_size(config, cash=scenario_cash, equity=scenario_equity, price=price,
                            previous=row, payload=payload, lot=lot, industry_value=industry_values.get(industry, 0))
        shares, budget = sizing["shares"], sizing.get("budget")
        item["entry_risk"] = sizing
        if sizing["reason"]:
            item["reason"] = sizing["reason"]
            continue
        spent = shares * price + fees(shares * price, config)
        depends = code in exiting or independent_slots < 1 or spent > independent_cash
        item.update(status="conditional" if depends else "planned", reference_shares=shares,
                    reference_spent=spent, reference_budget=budget, depends_on_sales=depends,
                    reason="需先成功卖出，释放资金或仓位，之后才尝试买入" if depends else "按规则优先级尝试下一开盘买入")
        if not depends:
            independent_cash -= spent
            independent_slots -= 1
        scenario_cash -= spent
        # Match the simulator's sequential fees and valuation at its assumed opening reference.
        scenario_equity -= fees(shares * price, config)
        reserved.add(code)
        industry_values[industry] = industry_values.get(industry, 0) + shares * price
    result["counts"] = {"planned": sum(i["status"] == "planned" for i in result["entries"]),
                        "conditional": sum(i["status"] == "conditional" for i in result["entries"]),
                        "watch": sum(i["status"] == "watch" for i in result["entries"]),
                        "exits": len(result["exits"]), "holds": len(result["holds"])}
    return result
