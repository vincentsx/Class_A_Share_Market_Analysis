"""Conservative next-open cash simulation, kept separate from signal observations.

Daily OHLC cannot prove execution. Missing held prices or corporate actions stop
the curve instead of fabricating a valuation. Settings are explicit assumptions.
"""

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select

from .config import SHANGHAI
from .db import bars, reports, sessions, signals, trading_days


@dataclass(frozen=True)
class Simulation:
    capital: float = 100000
    weight: float = .1
    max_positions: int = 10
    holding_days: int = 10
    commission_bps: float = 3
    min_commission: float = 5
    sell_tax_bps: float = 5
    transfer_bps: float = .1
    slippage_bps: float = 5
    participation: float = .01
    strategy: str = "all"
    rule_version: str = ""
    cohort: str = "all"
    split_day: str = ""

    def validate(self):
        values = [self.capital, self.weight, self.commission_bps, self.min_commission, self.sell_tax_bps,
                  self.transfer_bps, self.slippage_bps, self.participation]
        if not all(math.isfinite(v) for v in values):
            raise ValueError("模拟参数必须为有限数值")
        if not 1000 <= self.capital <= 1e9 or not 0 < self.weight <= 1 or not 1 <= self.max_positions <= 50:
            raise ValueError("资金、单只权重或持仓上限无效")
        if not 1 <= self.holding_days <= 60 or not 0 < self.participation <= .1:
            raise ValueError("持有天数需为1～60，成交参与率需为(0,10%]")
        if min(values[2:7]) < 0 or max(values[2:7]) > 1000:
            raise ValueError("费用与滑点参数需为0～1000")
        if self.strategy not in {"all", "breakout", "pullback"} or self.cohort not in {"all", "priority"}:
            raise ValueError("模拟策略或样本范围无效")
        if self.split_day:
            datetime.strptime(self.split_day, "%Y%m%d")


def board(code):
    if code.startswith(("688", "689")) and code.endswith(".SH"):
        return .2, 200
    if code.startswith(("300", "301")) and code.endswith(".SZ"):
        return .2, 100
    if code.startswith("60") and code.endswith(".SH") or code.startswith(("000", "001", "002", "003")) and code.endswith(".SZ"):
        return .1, 100
    return None, None


def fees(notional, config, sell=False):
    return max(config.min_commission, notional * config.commission_bps / 10000) + notional * (
        config.transfer_bps + (config.sell_tax_bps if sell else 0)) / 10000


def position_value(position, mark):
    """Unrealized P/L includes entry costs only; no sale is assumed."""
    value = position["shares"] * mark
    return {"market_value": value, "profit": value - position["spent"],
            "net_return": (value / position["spent"] - 1) * 100}


def simulate(db, settings, as_of=None, config=None):
    config = config or Simulation()
    config.validate()
    if as_of:
        datetime.strptime(as_of, "%Y%m%d")
    saved_reports = db.rows(select(reports).where(reports.c.source == settings.source,
                                                   reports.c.day <= as_of if as_of else True)
                            .order_by(reports.c.day))
    report_map = {r["day"]: r["payload"] for r in saved_reports}
    session_rows = db.rows(select(sessions).where(sessions.c.source == settings.source,
                                                 sessions.c.day <= as_of if as_of else True).order_by(sessions.c.day))
    if not session_rows:
        return {"status": "empty", "parameters": asdict(config), "curve": [], "trades": [], "skipped": {},
                "method": "尚无行情，无法模拟", "summary": {"initial_capital": config.capital,
                "market_value": None, "available_cash": None, "equity": None, "profit": None,
                "through": None}, "periods": [], "positions": []}
    latest = session_rows[-1]["day"]
    saved = db.rows(select(signals).where(signals.c.source == settings.source, signals.c.day <= latest,
                                         signals.c.strategy == config.strategy if config.strategy != "all" else True)
                    .order_by(signals.c.day, signals.c.code, signals.c.strategy))
    saved = [s for s in saved if (not config.rule_version or s["payload"].get("rule_version") == config.rule_version)
             and (config.cohort != "priority" or s["payload"].get("priority", True))]
    if len(saved) > 20000:
        raise ValueError("模拟信号超过20000条，请选择策略、版本或较早截止日期缩小范围")
    earliest = min((s["day"] for s in saved), default=latest)
    dates = [r["day"] for r in db.rows(select(trading_days.c.day).where(
        trading_days.c.source == settings.source, trading_days.c.day >= earliest,
        trading_days.c.day <= latest, trading_days.c.is_open == 1).order_by(trading_days.c.day))]
    session_map = {r["day"]: r for r in session_rows}
    codes = {s["code"] for s in saved}
    history = {(r["code"], r["day"]): r for r in db.rows(select(bars).where(
        bars.c.source == settings.source, bars.c.day >= earliest, bars.c.day <= latest,
        bars.c.code.in_(codes)))} if codes else {}
    entries = defaultdict(list)
    skipped = Counter()
    indices = {d: i for i, d in enumerate(dates)}
    for signal in saved:
        if signal["payload"].get("reconstructed"):
            skipped["冻结指标重建候选，不是当时发布信号"] += 1
            continue
        index = indices.get(signal["day"])
        if index is None or index + 1 >= len(dates):
            skipped["等待下一交易日"] += 1
            continue
        entry_day = dates[index + 1]
        published = signal["payload"].get("published_at") or report_map.get(signal["day"], {}).get("generated_at")
        try:
            timestamp = datetime.fromisoformat(published)
            if timestamp.tzinfo is None:
                raise ValueError("发布时间缺少时区")
            timestamp = timestamp.astimezone(SHANGHAI)
            deadline = datetime.strptime(entry_day + "0930", "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
            timely = timestamp < deadline and timestamp.strftime("%Y%m%d") >= signal["day"]
        except (TypeError, ValueError):
            timely = False
        if not timely:
            skipped["事后生成或发布时间缺失"] += 1
            continue
        entries[entry_day].append({**signal, "published_at": timestamp.isoformat()})
    cash, peak, drawdown = config.capital, config.capital, 0
    positions, trades, curve = {}, [], []
    stop = None
    benchmark_origin = session_map.get(dates[0], {}).get("benchmark", {}).get("close") if dates else None
    for index, day in enumerate(dates):
        session = session_map.get(day)
        if session is None:
            stop = {"day": day, "reason": "交易日行情快照缺失，停止计算，未填补净值"}
            break
        suspended = set(session["quality"].get("suspended", []))
        invalid = None
        for code, position in positions.items():
            row = history.get((code, day))
            stock = session["universe"].get(code, {})
            if stock.get("is_st") or "ST" in stock.get("name", "").upper() or "退" in stock.get("name", ""):
                invalid = f"持仓{code}出现风险标记，缺少可靠历史交易限制，停止计算"
                break
            if row is None and code not in suspended:
                invalid = f"持仓{code}行情缺失，停止计算"
                break
            if row and not math.isclose(row["adj"], position["adj"], rel_tol=1e-8):
                invalid = f"持仓{code}发生复权变化；缺少分红送转明细，停止计算"
                break
        if invalid:
            stop = {"day": day, "reason": invalid}
            break
        for code in list(positions):
            position = positions[code]
            if index <= position["entry_index"] or not position["exit_due"]:
                continue
            row = history.get((code, day))
            previous = history.get((code, dates[index - 1])) if index else None
            limit, _ = board(code)
            if not row or not previous or row["open"] <= previous["close"] * (1 - limit + .001):
                skipped["退出延期：停牌、缺少昨收或开盘接近跌停"] += 1
                position["exit_delays"].append({"day": day, "reason": "停牌、缺少昨收或开盘接近跌停"})
                continue
            sell_price = row["open"] * (1 - config.slippage_bps / 10000)
            if sell_price < row["low"]:
                skipped["退出延期：滑点价格超出日内范围"] += 1
                position["exit_delays"].append({"day": day, "reason": "滑点价格超出日内范围"})
                continue
            if position["shares"] > previous["volume"] * config.participation:
                skipped["退出延期：超过此前交易日成交量参与上限"] += 1
                position["exit_delays"].append({"day": day, "reason": "超过此前交易日成交量参与上限"})
                continue
            gross = position["shares"] * sell_price
            cost = fees(gross, config, sell=True)
            cash += gross - cost
            trades.append({"code": code, "name": position["name"], "strategy": position["strategy"],
                           "signal_day": position["signal_day"], "entry_day": position["entry_day"],
                           "exit_day": day, "shares": position["shares"], "entry_price": position["entry_price"],
                           "exit_price": sell_price, "fees": cost + position["entry_fee"],
                           "entry_open": position["entry_open"], "exit_open": row["open"],
                           "entry_fee": position["entry_fee"], "exit_fee": cost,
                           "spent": position["spent"], "proceeds": gross - cost,
                           "profit": gross - cost - position["spent"],
                           "held_sessions": index - position["entry_index"],
                           "published_at": position["published_at"], "label": position["label"],
                           "rule_version": position["rule_version"], "entry_reasons": position["entry_reasons"],
                           "invalidation_price": position["invalidation_price"],
                           "exit_trigger_day": position["exit_trigger_day"],
                           "exit_trigger_close": position["exit_trigger_close"],
                           "exit_delays": position["exit_delays"],
                           "net_return": ((gross - cost) / position["spent"] - 1) * 100,
                           "exit_reason": position["exit_due"]})
            del positions[code]
        for signal in sorted(entries[day], key=lambda s: (-s["payload"].get("relative20", 0), s["code"], s["strategy"])):
            code, payload = signal["code"], signal["payload"]
            if code in positions:
                skipped["已有持仓，合并重叠信号"] += 1
                continue
            if len(positions) >= config.max_positions:
                skipped["达到持仓上限"] += 1
                continue
            limit, lot = board(code)
            stock = session["universe"].get(code, {})
            if limit is None or stock.get("is_st") or "ST" in stock.get("name", "").upper() or "退" in stock.get("name", ""):
                skipped["不支持的板块或风险标记"] += 1
                continue
            row, previous = history.get((code, day)), history.get((code, signal["day"]))
            if not row or not previous or code in suspended:
                skipped["入场缺行情或停牌"] += 1
                continue
            if day < "20230410" or not math.isclose(row["adj"], previous["adj"], rel_tol=1e-8):
                skipped["不支持的历史制度或除权入场"] += 1
                continue
            if row["open"] >= previous["close"] * (1 + limit - .001):
                skipped["开盘接近涨停，保守不成交"] += 1
                continue
            price = row["open"] * (1 + config.slippage_bps / 10000)
            if price > row["high"]:
                skipped["入场滑点价格超出日内范围"] += 1
                continue
            # Use previously known volume and valuation; today's full volume is future information at open.
            equity = cash + sum(p["shares"] * p["mark"] for p in positions.values())
            budget = min(cash, equity * config.weight)
            shares = math.floor(min(budget / price, previous["volume"] * config.participation) / lot) * lot
            while shares >= lot and shares * price + fees(shares * price, config) > budget:
                shares -= lot
            if shares < lot:
                skipped["资金或流动性不足最小申报数量"] += 1
                continue
            gross = shares * price
            cost = fees(gross, config)
            cash -= gross + cost
            positions[code] = {"shares": shares, "entry_day": day, "entry_index": index, "entry_price": price,
                               "entry_fee": cost, "spent": gross + cost, "adj": row["adj"], "mark": price,
                               "entry_open": row["open"], "published_at": signal["published_at"],
                               "label": payload.get("label", signal["strategy"]),
                               "rule_version": payload.get("rule_version", ""),
                               "entry_reasons": payload.get("reasons", []),
                               "invalidation_price": payload["invalidation_adj"] / row["adj"],
                               "exit_trigger_day": None, "exit_trigger_close": None, "exit_delays": [],
                               "signal_day": signal["day"], "name": payload["name"], "strategy": signal["strategy"],
                               "invalidation_adj": payload["invalidation_adj"], "exit_due": None}
        for code, position in positions.items():
            position["held_sessions"] = index - position["entry_index"] + 1
            row = history.get((code, day))
            if row:
                position["mark"] = row["close"]
                if not position["exit_due"] and row["close"] * row["adj"] < position["invalidation_adj"]:
                    position["exit_due"] = "前一收盘失效，下一可模拟开盘退出"
            if not position["exit_due"] and index - position["entry_index"] + 1 >= config.holding_days:
                position["exit_due"] = "持有期届满，下一可模拟开盘退出"
            if position["exit_due"] and not position["exit_trigger_day"]:
                position["exit_trigger_day"] = day
                position["exit_trigger_close"] = row["close"] if row else None
        equity = cash + sum(p["shares"] * p["mark"] for p in positions.values())
        peak = max(peak, equity)
        drawdown = min(drawdown, (equity / peak - 1) * 100)
        benchmark = session["benchmark"]["close"]
        curve.append({"day": day, "equity": equity, "cash": cash, "positions": len(positions),
                      "return": (equity / config.capital - 1) * 100,
                      "benchmark_return": (benchmark / benchmark_origin - 1) * 100 if benchmark_origin else None})
    final = curve[-1] if curve else {"return": None, "benchmark_return": None}
    periods = []
    if config.split_day:
        if not dates or not dates[0] < config.split_day <= dates[-1]:
            raise ValueError("前后段分界日期必须位于模拟日期范围内")
        for label, points in [("前段", [p for p in curve if p["day"] < config.split_day]),
                              ("后段", [p for p in curve if p["day"] >= config.split_day])]:
            if points:
                start_equity = config.capital if label == "前段" else next(
                    (p["equity"] for p in reversed(curve) if p["day"] < config.split_day), config.capital)
                periods.append({"label": label, "from": points[0]["day"], "to": points[-1]["day"],
                                "return": (points[-1]["equity"] / start_equity - 1) * 100})
    return {"status": "incomplete" if stop else "success", "as_of": latest, "stop": stop,
            "parameters": asdict(config), "curve": curve, "trades": trades, "skipped": dict(skipped),
            "positions": [{"code": c, **p, **position_value(p, p["mark"])} for c, p in positions.items()],
            "periods": periods,
            "summary": {"return": final["return"], "benchmark_return": final["benchmark_return"],
                        "initial_capital": config.capital,
                        "market_value": final["equity"] - final["cash"] if curve else None,
                        "available_cash": final["cash"] if curve else None,
                        "equity": final["equity"] if curve else None,
                        "profit": final["equity"] - config.capital if curve else None,
                        "realized_profit": sum(t["profit"] for t in trades) if curve else None,
                        "unrealized_profit": sum(position_value(p, p["mark"])["profit"]
                                                 for p in positions.values()) if curve else None,
                        "max_drawdown": drawdown, "closed_trades": len(trades), "open_positions": len(positions),
                        "fees": sum(t["fees"] for t in trades) + sum(p["entry_fee"] for p in positions.values()),
                        "through": curve[-1]["day"] if curve else None},
            "method": "下一交易日开盘入场，买入后最早下一交易日卖出；失效或持有期到期后的下一开盘退出。"
                      "买卖时间仅表示交易日期和开盘模拟时段，不是实际成交时间；模拟价格为开盘价加减滑点。"
                      "失效按收盘价判断，不是盘中触价立即止损。已卖出盈亏扣除双边费用，未卖出浮盈亏仅扣买入费用。"
                      "按现金、最小申报数量、费用、滑点和前一日量能限制模拟；开盘接近涨跌停保守不成交。"
                      "缺失持仓行情或分红送转细节时停止净值。日线不能证明真实成交；费用是可修改假设。"
                      "历史名称/行业/风险状态与数据修订不完全具备时点性，仍有历史偏差；事后生成的信号排除。"
                      "前后段对比不是预先留出的样本外检验，不自动调参、不证明交易优势。"}
