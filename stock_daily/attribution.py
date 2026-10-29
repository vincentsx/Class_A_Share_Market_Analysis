"""Reconcile strategy contributions against the account at the displayed cutoff."""

from collections import defaultdict
from statistics import mean

LABELS = {"breakout": "日线放量突破", "pullback": "日线趋势回调转强",
          "intraday_breakout": "盘中突破", "intraday_reclaim": "盘中均线收复"}


def entry_version(trade):
    return (trade.get("entry_confirmation") or {}).get("version") or "legacy-unrecorded"


def summarize(closed, opened):
    gains = [t["profit"] for t in closed if t["profit"] > 0]
    losses = [t["profit"] for t in closed if t["profit"] < 0]
    held = [t["held_sessions"] for t in closed if t.get("held_sessions") is not None]
    realized = sum(t["profit"] for t in closed)
    unrealized = sum(t["profit"] for t in opened)
    fees = sum(t.get("entry_fee", 0) + t.get("exit_fee", 0) for t in closed)
    fees += sum(t.get("entry_fee", 0) for t in opened)
    return {"closed": len(closed), "open": len(opened), "wins": len(gains), "losses": len(losses),
            "realized": realized, "unrealized": unrealized, "profit": realized + unrealized, "fees": fees,
            "win_rate": len(gains) / len(closed) * 100 if closed else None,
            "profit_factor": sum(gains) / -sum(losses) if losses else None,
            "average_holding_sessions": mean(held) if held else None,
            "small_sample": len(closed) < 30}


def build_attribution(account):
    valuation = account.get("valuation") or {}
    summary = valuation.get("summary") or account.get("summary") or {}
    closed = account.get("trades", [])
    opened = valuation.get("positions", account.get("positions", []))

    def groups(key):
        buckets = defaultdict(lambda: ([], []))
        for index, items in enumerate((closed, opened)):
            for t in items:
                buckets[key(t)][index].append(t)
        return [{"key": k, "label": LABELS.get(k, k), **summarize(*values)}
                for k, values in sorted(buckets.items())]

    total = summarize(closed, opened)
    expected = summary.get("profit")
    gap = total["profit"] - expected if expected is not None else None
    return {"version": "account-attribution-v1", "day": summary.get("through"),
            "as_of": valuation.get("captured_at") or (account.get("paper_trading") or {}).get("quote_at"),
            "daily_through": (account.get("paper_trading") or {}).get("daily_through", (account.get("summary") or {}).get("through")),
            "total": total, "reconciliation_gap": gap, "reconciled": gap is not None and abs(gap) < .000001,
            "by_strategy": groups(lambda t: t["strategy"]),
            "by_version": groups(entry_version),
            "by_strategy_version": groups(lambda t: " / ".join((LABELS.get(t["strategy"], t["strategy"]),
                t.get("rule_version") or "规则版本未记录", entry_version(t)))),
            "by_exit": [{"key": reason, **summarize([t for t in closed if (t.get("exit_reason") or "原因未记录") == reason], [])}
                        for reason in sorted({t.get("exit_reason") or "原因未记录" for t in closed})],
            "method": "按实际成交和当前估值归因，含未平仓损益；费用已计入盈亏，不重复扣除。"
                      "胜率仅统计已平仓交易；少于30笔标记样本不足，达到30笔也不代表已验证优势。"
                      "legacy-unrecorded表示未记录盘中入场版本；不同版本的市场时期与资金敞口不同，不能直接判优。"}
