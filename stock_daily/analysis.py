"""Explainable research rules, using only observations through the report date."""

from collections import defaultdict
from statistics import mean

RULE_VERSION = "swing-v1.0"
EVENT_WORDS = ("减持", "解禁", "立案", "诉讼", "风险", "亏损", "预亏", "处罚", "退市", "更正")


def calculate_metrics(history, benchmark_return20):
    if len(history) < 65:
        return None
    rows = history[-65:]
    closes = [r["close"] * r["adj"] for r in rows]
    highs = [r["high"] * r["adj"] for r in rows]
    lows = [r["low"] * r["adj"] for r in rows]
    volumes = [r["volume"] for r in rows]
    ma20, ma60 = mean(closes[-20:]), mean(closes[-60:])
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(rows))
    ]
    current = rows[-1]
    base = current["adj"]
    return20 = (closes[-1] / closes[-21] - 1) * 100
    return {
        "code": current["code"],
        "close": current["close"],
        "pct": current["pct"],
        "amount": current["amount"],
        "turnover": current["turnover"],
        "pe": current["pe"],
        "pb": current["pb"],
        "ma20": ma20 / base,
        "ma60": ma60 / base,
        "return5": (closes[-1] / closes[-6] - 1) * 100,
        "return20": return20,
        "relative20": return20 - benchmark_return20,
        "volume_ratio": volumes[-1] / mean(volumes[-21:-1]),
        "prior_high20": max(highs[-21:-1]) / base,
        "atr14": mean(true_ranges[-14:]) / base,
        "ma20_slope5": (ma20 / mean(closes[-25:-5]) - 1) * 100,
        "trend": "向上" if closes[-1] > ma20 > ma60 else "调整",
        "breakout": closes[-1] > max(highs[-21:-1]),
        "pullback": (
            abs(closes[-2] / mean(closes[-21:-1]) - 1) <= 0.03
            and mean(volumes[-4:-1]) < mean(volumes[-24:-4])
            and closes[-1] > closes[-2]
            and closes[-1] > ma20
        ),
        "corporate_action": len({r["adj"] for r in rows[-21:]}) > 1,
    }


def tag_events(events):
    return [{**e, "tags": [word for word in EVENT_WORDS if word in e["title"]]} for e in events]


def build_report(settings, day, histories, day_sessions, universe, quality, events, event_status):
    dates = sorted(day_sessions)
    if len(dates) < 65:
        raise ValueError("至少需要65个完整采集的交易日")
    benchmark = day_sessions[day]["benchmark"]
    benchmark20 = (benchmark["close"] / day_sessions[dates[-21]]["benchmark"]["close"] - 1) * 100
    expected_recent = dates[-65:]
    metrics = []
    today = [h[-1] for h in histories.values() if h and h[-1]["day"] == day]
    for code, history in histories.items():
        # Suspension/missing data must not silently change the meaning of a trading-day window.
        if [r["day"] for r in history[-65:]] != expected_recent:
            continue
        metric = calculate_metrics(history, benchmark20)
        stock = universe.get(code)
        if metric is not None and stock:
            metrics.append({**metric, **stock})
    sectors = defaultdict(list)
    for metric in metrics:
        if metric["industry"] != "未分类":
            sectors[metric["industry"]].append(metric)
    industries = sorted(
        [
            {
                "name": name,
                "count": len(group),
                "return5": mean(m["return5"] for m in group),
                "return20": mean(m["return20"] for m in group),
                "relative20": mean(m["relative20"] for m in group),
                "up_ratio": sum(m["pct"] > 0 for m in group) / len(group) * 100,
                "amount": sum(m["amount"] for m in group),
            }
            for name, group in sectors.items()
        ],
        key=lambda row: row["relative20"],
        reverse=True,
    )
    strong = {s["name"] for s in industries[: max(1, len(industries) // 3)] if s["relative20"] > 0}
    tagged = tag_events(events)
    by_code = defaultdict(list)
    for e in tagged:
        by_code[e["code"]].append(e)
    candidates = []
    for metric in metrics:
        if (
            metric["amount"] < settings.min_amount
            or "ST" in metric["name"].upper()
            or metric.get("is_st", False)
            or "退" in metric["name"]
            or metric["corporate_action"]
            or metric["trend"] != "向上"
            or metric["relative20"] <= 0
            or metric["industry"] not in strong
            or metric["market"] == "未知"
        ):
            continue
        strategies = []
        if metric["breakout"] and metric["volume_ratio"] >= 1.5:
            strategies.append(("breakout", "放量突破", metric["prior_high20"]))
        if metric["pullback"] and metric["ma20_slope5"] > 0:
            strategies.append(("pullback", "趋势回调转强", metric["ma20"]))
        for strategy, label, invalidation in strategies:
            risks = ["行业分类采用采集时的基础信息；信号尚未证明有交易优势"]
            if metric["return5"] > 10:
                risks.append("近5日涨幅超过10%，需关注追涨风险")
            if metric["atr14"] / metric["close"] > 0.05:
                risks.append("近14日波动较大")
            if event_status in {"disabled", "unavailable"}:
                risks.append("公告未完成核验")
            risks.extend(f"公告待核对：{e['title']}" for e in by_code[metric["code"]] if e["tags"])
            candidates.append(
                {
                    **metric,
                    "strategy": strategy,
                    "label": label,
                    "rule_version": RULE_VERSION,
                    "invalidation": invalidation,
                    "invalidation_adj": invalidation * histories[metric["code"]][-1]["adj"],
                    "observation": "观察未来5～10个交易日的延续性",
                    "reasons": [
                        f"行业位于相对强度前列（{metric['industry']}）",
                        f"近20日领先沪深300 {metric['relative20']:.2f} 个百分点",
                        f"收盘高于20日及60日均线；成交量为此前20日均量的 {metric['volume_ratio']:.2f} 倍",
                    ],
                    "risks": risks,
                    "events": by_code[metric["code"]],
                    "exit_condition": f"后续收盘低于 {invalidation:.2f} 元对应的复权位置，信号失效；不代表可按此价格成交",
                }
            )
    candidates.sort(key=lambda m: (m["relative20"], m["amount"]), reverse=True)
    # Market amount comparison uses the complete session totals, not only eligible candidates.
    totals = [sum(r["amount"] for r in histories_rows(histories, d)) for d in dates[-21:]]
    previous_total = mean(totals[:-1]) if len(totals) > 1 else 0
    amount = sum(r["amount"] for r in today)
    up = sum(r["pct"] > 0 for r in today)
    return {
        "day": day,
        "source": settings.source,
        "rule_version": RULE_VERSION,
        "scope": "BaoStock 覆盖的沪深A股（不含北交所）" if settings.source == "baostock" else "当前数据源覆盖范围",
        "quality": quality,
        "event_status": event_status,
        "market": {
            "count": len(today),
            "up": up,
            "down": sum(r["pct"] < 0 for r in today),
            "flat": sum(r["pct"] == 0 for r in today),
            "up_ratio": up / len(today) * 100,
            "amount": amount,
            "amount_ratio20": amount / previous_total if previous_total else None,
            "benchmark_pct": benchmark["pct_chg"],
            "benchmark_return20": benchmark20,
            "above_ma20_ratio": sum(m["close"] > m["ma20"] for m in metrics) / len(metrics) * 100
            if metrics
            else None,
            "eligible_history_count": len(metrics),
        },
        "industries": industries,
        "candidates": candidates[: settings.max_candidates],
        "all_candidate_count": len(candidates),
        "stocks": metrics,
        "events": tagged,
        "notes": [
            "行业收益为连续65日有行情样本的等权平均，并非官方行业指数。",
            "缺失或停牌历史不补成零成交；最近65日有缺口的股票暂不参与筛选。",
            "最近20日复权因子有变化的股票暂不参与量价筛选。",
            "信号跟踪是收盘到收盘的观察统计，不是模拟成交回测或收益承诺。",
        ],
    }


def histories_rows(histories, day):
    for rows in histories.values():
        for row in rows:
            if row["day"] == day:
                yield row


def signal_outcome(signal, history, session_dates):
    """Use exchange sessions as horizons; missing endpoint returns unavailable, never stale prices."""
    by_day = {row["day"]: row for row in history}
    start = by_day.get(signal["day"])
    if start is None:
        return {"status": "missing_origin", "horizons": {}, "invalidated_on": None}
    base = start["close"] * start["adj"]
    future_dates = [day for day in session_dates if day > signal["day"]]
    invalidated = next(
        (
            day
            for day in future_dates
            if day in by_day
            and by_day[day]["close"] * by_day[day]["adj"] < signal["payload"]["invalidation_adj"]
        ),
        None,
    )
    horizons = {}
    for horizon in (5, 10, 20):
        if len(future_dates) < horizon:
            horizons[str(horizon)] = {"status": "pending"}
            continue
        end = future_dates[horizon - 1]
        interval = future_dates[:horizon]
        if any(day not in by_day for day in interval):
            horizons[str(horizon)] = {"status": "missing", "end_day": end}
            continue
        end_value = by_day[end]["close"] * by_day[end]["adj"]
        path = [by_day[day]["low"] * by_day[day]["adj"] for day in interval]
        horizons[str(horizon)] = {
            "status": "ready",
            "end_day": end,
            "return": (end_value / base - 1) * 100,
            "max_adverse": min(0, (min(path) / base - 1) * 100),
        }
    return {"status": "observing", "horizons": horizons, "invalidated_on": invalidated}
