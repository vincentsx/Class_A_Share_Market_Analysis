"""Net account metrics; short histories do not receive annual return estimates."""

import math
from collections import defaultdict
from statistics import mean, stdev


def performance(curve, trades, positions, capital):
    daily = [b["equity"] / a["equity"] - 1 for a, b in zip(curve, curve[1:])]
    n = len(daily)
    volatility = stdev(daily) if n >= 2 else 0
    gains = [t["profit"] for t in trades if t["profit"] > 0]
    losses = [-t["profit"] for t in trades if t["profit"] < 0]
    annual = ((curve[-1]["equity"] / capital) ** (252 / n) - 1) * 100 if n >= 252 else None
    peak, longest, underwater = capital, 0, 0
    for point in curve:
        if point["equity"] >= peak:
            peak, underwater = point["equity"], 0
        else:
            underwater += 1
            longest = max(longest, underwater)
    turnover = sum(t["shares"] * (t["entry_price"] + t["exit_price"]) for t in trades)
    turnover += sum(p["shares"] * p["entry_price"] for p in positions)
    slippage = sum(t["shares"] * (t["entry_price"] - t["entry_open"] + t["exit_open"] - t["exit_price"])
                   for t in trades)
    slippage += sum(p["shares"] * (p["entry_price"] - p["entry_open"]) for p in positions)
    contributions = defaultdict(lambda: {"realized_profit": 0, "unrealized_profit": 0, "closed_trades": 0})
    for trade in trades:
        item = contributions[trade["strategy"]]
        item["realized_profit"] += trade["profit"]
        item["closed_trades"] += 1
    for position in positions:
        contributions[position["strategy"]]["unrealized_profit"] += position["profit"]
    for item in contributions.values():
        item["profit"] = item["realized_profit"] + item["unrealized_profit"]
    return {"return_observations": n, "annual_return": annual,
            "annual_volatility": volatility * math.sqrt(252) * 100 if n >= 30 else None,
            "sharpe": mean(daily) / volatility * math.sqrt(252) if n >= 30 and volatility > 1e-12 else None,
            "win_rate": len(gains) / len(trades) * 100 if trades else None,
            "profit_factor": sum(gains) / sum(losses) if losses else None,
            "payoff_ratio": mean(gains) / mean(losses) if gains and losses else None,
            "longest_underwater_sessions": longest, "current_underwater_sessions": underwater,
            "turnover": turnover / mean(p["equity"] for p in curve) if curve else None,
            "slippage_cost": slippage, "strategy_contributions": dict(contributions),
            "small_sample": n < 252 or len(trades) < 30,
            "method": "按扣费日线账户计算；夏普无风险利率假设为0，至少30个日收益；"
                      "年化收益至少252个日收益。胜率及盈亏比仅统计已平仓交易；"
                      "换手为双边成交额/平均净值，未年化。连续水下交易日按日线观察；"
                      "策略贡献包含已实现与持仓浮盈亏。统计不足不证明策略优势。"}
