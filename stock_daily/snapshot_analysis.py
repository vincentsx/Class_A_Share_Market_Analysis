"""Explain intraday changes without extrapolating volume or publishing daily signals."""

import math
from collections import defaultdict
from statistics import mean

from sqlalchemy import select

from .db import bars, snapshots, trading_days

VERSION = "intraday-v2"
MIN_SLOT_SAMPLES = 5
MIN_SECTOR_STOCKS = 5


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def grouped(rows, classifications):
    groups = defaultdict(list)
    for row in rows:
        industry = classifications.get(row["code"], "未分类")
        if industry != "未分类":
            groups[industry].append(row)
    return groups


def active(pct, up_ratio, count, benchmark_pct):
    # These are descriptive thresholds, not a forecast or an entry rule.
    return count >= MIN_SECTOR_STOCKS and pct >= 1 and up_ratio >= 60 and pct > benchmark_pct


def sector_stats(group, benchmark_pct):
    pct = mean(r["pct"] for r in group)
    up_ratio = sum(r["pct"] > 0 for r in group) / len(group) * 100
    return {"count": len(group), "pct": pct, "up_ratio": up_ratio,
            "amount": sum(r["amount"] for r in group),
            "active": active(pct, up_ratio, len(group), benchmark_pct)}


def comparable(first, second):
    common = first.keys() & second.keys()
    return bool(common) and len(common) / max(len(first), len(second)) >= 0.98


def analyze_snapshot(db, settings, payload, stocks):
    day, slot = payload["day"], payload["slot"]
    rows = payload["quotes"]
    by_code = {r["code"]: r for r in rows}
    classifications = {r["code"]: r["industry"] for r in rows}
    dates = [r["day"] for r in db.rows(
        select(trading_days.c.day).where(trading_days.c.source == settings.source,
                                       trading_days.c.day < day, trading_days.c.is_open == 1)
        .order_by(trading_days.c.day.desc()).limit(20)
    )]
    historical = db.rows(select(snapshots.c.day, snapshots.c.payload).where(
        snapshots.c.source == settings.source, snapshots.c.slot == slot,
        snapshots.c.day.in_(dates))) if dates else []
    past = {r["day"]: r["payload"] for r in historical}
    past_quotes = {d: {r["code"]: r for r in p["quotes"]} for d, p in past.items()}
    histories = defaultdict(dict)
    if dates:
        for row in db.rows(select(bars).where(bars.c.source == settings.source, bars.c.day.in_(dates))):
            histories[row["code"]][row["day"]] = row

    morning = []
    if slot == "close":
        morning = db.rows(select(snapshots.c.payload).where(
            snapshots.c.source == settings.source, snapshots.c.day == day, snapshots.c.slot == "midday"))
    morning_quotes = {r["code"]: r for r in morning[0]["payload"]["quotes"]} if morning else {}
    afternoon = {}
    for code in by_code.keys() & morning_quotes.keys():
        close, noon = by_code[code], morning_quotes[code]
        # A vendor correction must not become negative afternoon trading volume.
        if (close["amount"] >= noon["amount"] and close["volume"] >= noon["volume"]
                and positive(noon["price"])):
            afternoon[code] = {"code": code, "return": (close["price"] / noon["price"] - 1) * 100,
                               "amount": close["amount"] - noon["amount"],
                               "volume": close["volume"] - noon["volume"]}

    for row in rows:
        code = row["code"]
        history = histories[code]
        daily = [history[d] for d in dates if d in history]
        complete = len(dates) == 20 and len(daily) == 20 and all(
            all(positive(r[k]) for k in ("close", "high", "volume", "adj")) for r in daily)
        adjusted = complete and len({r["adj"] for r in daily}) != 1
        price_consistent = bool(complete and abs(row["previous"] / daily[0]["close"] - 1) <= 0.01)
        row.update(volume_ratio=None, volume_baseline=None, volume_samples=0,
                   daily_volume_ratio=None, ma20=None, prior_high20=None,
                   distance_ma20=None, distance_high20=None, tags=[], risks=[], afternoon=None)
        if complete and not adjusted and price_consistent:
            row["daily_volume_ratio"] = row["volume"] / mean(r["volume"] for r in daily)
            row["ma20"] = mean(r["close"] for r in daily)
            row["prior_high20"] = max(r["high"] for r in daily)
            row["distance_ma20"] = (row["price"] / row["ma20"] - 1) * 100
            row["distance_high20"] = (row["price"] / row["prior_high20"] - 1) * 100
        else:
            row["risks"].append("历史不足或近期复权/昨收不一致，20日量价参考不可用")
        if slot == "close" and row["daily_volume_ratio"] is not None:
            row.update(volume_ratio=row["daily_volume_ratio"], volume_baseline="此前20日全天均量",
                       volume_samples=20)
        elif slot == "midday" and not adjusted and price_consistent:
            volumes = []
            for d in dates:
                old = past_quotes.get(d, {}).get(code)
                if not old or not positive(old.get("volume")):
                    break
                volumes.append(old["volume"])
            row["volume_samples"] = len(volumes)
            if len(volumes) >= MIN_SLOT_SAMPLES:
                row.update(volume_ratio=row["volume"] / mean(volumes),
                           volume_baseline=f"此前{len(volumes)}个连续交易日午间均量")
        if slot == "midday" and row["volume_ratio"] is None:
            row["risks"].append(f"午间同时间样本不足（至少{MIN_SLOT_SAMPLES}个连续交易日），不判断缩量")

        ratio = row["volume_ratio"]
        if ratio is not None:
            if ratio >= 1.5:
                row["tags"].append("放量横盘" if abs(row["pct"]) < 1 else
                                   "放量上涨" if row["pct"] > 0 else "放量下跌")
            elif ratio <= 0.7:
                row["tags"].append("缩量回调" if row["pct"] < 0 else "缩量上涨" if row["pct"] > 0 else "缩量横盘")
        elif slot == "midday" and row["daily_volume_ratio"] is not None and row["daily_volume_ratio"] >= 1.5:
            row["tags"].append("上午量已超全天均量1.5倍")
        row["fade_pp"] = (row["high"] - row["price"]) / row["previous"] * 100
        if row["fade_pp"] >= 3:
            row["tags"].append("冲高回落")
        if row["ma20"] is not None:
            if row["price"] > row["prior_high20"]:
                row["tags"].append("突破20日高点参考")
            if row["price"] < row["ma20"] <= row["previous"]:
                row["tags"].append("跌破20日均线参考")
            if row["distance_ma20"] > 10:
                row["risks"].append("价格高于20日均线超过10%，关注偏离风险")
        stock = stocks.get(code, {})
        row["eligible"] = (row["amount"] >= settings.min_amount and not stock.get("is_st", False)
                           and "ST" not in row["name"].upper() and "退" not in row["name"])
        if not row["eligible"]:
            row["risks"].append("风险标记或成交额不足，仅保留报价，不进入异动清单")
        if code in afternoon:
            row["afternoon"] = afternoon[code]
        elif code in morning_quotes:
            row["risks"].append("累计量额回退或午间价格无效，午后比较不可用")

    current_groups = grouped(rows, classifications)
    past_groups = {d: grouped(p["quotes"], classifications) for d, p in past.items()}
    prior_day = dates[0] if dates else None
    previous = past.get(prior_day)
    previous_rows = past_quotes.get(prior_day, {})
    common = by_code.keys() & previous_rows.keys()
    market_comparable = comparable(by_code, previous_rows)
    common_total = sum(by_code[c]["amount"] for c in common)
    old_common_total = sum(previous_rows[c]["amount"] for c in common)
    afternoon_total = sum(r["amount"] for r in afternoon.values())
    industries = []
    for name, group in current_groups.items():
        stats = sector_stats(group, payload["market"]["benchmark"]["pct"])
        current = {r["code"]: r for r in group}
        stats.update(name=name, amount_share=stats["amount"] / payload["market"]["amount"] * 100,
                     status="活跃" if stats["active"] else "分化" if stats["pct"] > 0 else "偏弱",
                     prior_day=prior_day if previous else None, pct_change_pp=None,
                     breadth_change_pp=None, share_change_pp=None, active_streak=1 if stats["active"] else 0,
                     history_compared=0, afternoon=None, leaders=[])
        stats["volume_count"] = sum(r["volume_ratio"] is not None for r in group)
        stats["volume_up_count"] = sum(r["volume_ratio"] is not None and r["volume_ratio"] >= 1.5
                                       for r in group)
        stats["volume_down_count"] = sum(r["volume_ratio"] is not None and r["volume_ratio"] <= 0.7
                                         for r in group)
        old = {r["code"]: r for r in past_groups.get(prior_day, {}).get(name, [])}
        if previous and comparable(current, old):
            cohort = current.keys() & old.keys()
            current_pct = mean(current[c]["pct"] for c in cohort)
            old_pct = mean(old[c]["pct"] for c in cohort)
            stats["pct_change_pp"] = current_pct - old_pct
            stats["breadth_change_pp"] = (sum(current[c]["pct"] > 0 for c in cohort)
                                          - sum(old[c]["pct"] > 0 for c in cohort)) / len(cohort) * 100
            was_active = active(old_pct, sum(old[c]["pct"] > 0 for c in cohort) / len(cohort) * 100,
                                len(cohort), previous["market"]["benchmark"]["pct"])
            stats["status"] = ("持续活跃" if was_active else "新近活跃") if stats["active"] else (
                "热度回落" if was_active else stats["status"])
            if market_comparable and common_total > 0 and old_common_total > 0:
                stats["share_change_pp"] = (sum(current[c]["amount"] for c in cohort) / common_total
                                            - sum(old[c]["amount"] for c in cohort) / old_common_total) * 100
            if stats["active"]:
                for d in dates:
                    old_group = past_groups.get(d, {}).get(name, [])
                    if not comparable(current, {r["code"]: r for r in old_group}):
                        break
                    cohort_rows = [r for r in old_group if r["code"] in current]
                    old_stats = sector_stats(cohort_rows, past[d]["market"]["benchmark"]["pct"])
                    stats["history_compared"] += 1
                    if not old_stats["active"]:
                        break
                    stats["active_streak"] += 1
        cohort = [afternoon[c] for c in current if c in afternoon]
        if morning and len(cohort) / len(current) >= 0.98:
            stats["afternoon"] = {
                "return": mean(r["return"] for r in cohort),
                "up_ratio": sum(r["return"] > 0 for r in cohort) / len(cohort) * 100,
                "amount": sum(r["amount"] for r in cohort),
                "amount_share": sum(r["amount"] for r in cohort) / afternoon_total * 100
                if afternoon_total else None,
                "count": len(cohort),
            }
        # Leaders illustrate participation; they are not recommendations.
        stats["leaders"] = [{"code": r["code"], "name": r["name"], "pct": r["pct"],
                             "volume_ratio": r["volume_ratio"]}
                            for r in sorted(group, key=lambda r: r["pct"], reverse=True)[:3]]
        industries.append(stats)
    industries.sort(key=lambda s: (s["active"], s["pct"]), reverse=True)
    payload["industries"] = industries
    alerts = [r for r in rows if r["tags"] and r["eligible"]]
    risk_tags = {"放量下跌", "冲高回落", "跌破20日均线参考"}
    risks = [r for r in alerts if risk_tags.intersection(r["tags"])]
    volume_up = [r for r in alerts if any(t.startswith("放量") or t.startswith("上午量") for t in r["tags"])]
    volume_down = [r for r in alerts if any(t.startswith("缩量") for t in r["tags"])]
    volume_up.sort(key=lambda r: (r["volume_ratio"] or r["daily_volume_ratio"] or 0, r["amount"]), reverse=True)
    volume_down.sort(key=lambda r: (r["volume_ratio"], -r["amount"]))
    risks.sort(key=lambda r: (len(risk_tags.intersection(r["tags"])), r["fade_pp"], r["amount"]), reverse=True)
    hot = [s for s in industries if s["active"]]
    summaries = []
    if hot:
        lead = hot[0]
        summaries.append(f"{lead['name']}行业样本活跃：平均涨跌{lead['pct']:+.2f}%，"
                         f"上涨家数占{lead['up_ratio']:.0f}%，成交额占{lead['amount_share']:.2f}%。")
    else:
        summaries.append("当前没有同时满足涨幅、上涨广度、相对基准和样本家数条件的活跃行业。")
    volume_description = f"放量{len(volume_up)}只、缩量{len(volume_down)}只"
    if slot == "midday":
        lower_bound = sum("上午量已超全天均量1.5倍" in r["tags"] for r in volume_up)
        volume_description = (f"同时间放量{len(volume_up) - lower_bound}只、缩量{len(volume_down)}只，"
                              f"另有{lower_bound}只上午量已达历史全天均量1.5倍")
    summaries.append(f"可比量能覆盖{sum(r['volume_ratio'] is not None for r in rows)}/{len(rows)}只；"
                     f"异动清单中{volume_description}，风险异动{len(risks)}只。")
    if slot == "close" and morning:
        summaries.append(f"收盘与午间有{len(afternoon)}只有效可比报价，午后新增成交额"
                         f"{afternoon_total / 1e8:.2f}亿元；累计量额回退的报价不参与比较。")
        afternoon_sectors = [s for s in industries if s["afternoon"] and s["count"] >= MIN_SECTOR_STOCKS]
        strengthening = sorted([s for s in afternoon_sectors if s["afternoon"]["return"] >= 1],
                               key=lambda s: s["afternoon"]["return"], reverse=True)[:3]
        weakening = sorted([s for s in afternoon_sectors if s["afternoon"]["return"] <= -1],
                            key=lambda s: s["afternoon"]["return"])[:3]
        for label, sectors in (("午后走强", strengthening), ("午后降温", weakening)):
            if sectors:
                summaries.append(label + "：" + "、".join(
                    f"{s['name']}（相对午间{s['afternoon']['return']:+.2f}%）" for s in sectors) + "。")
    elif slot == "close":
        summaries.append("未保存当天午间快照，无法判断午后走强、降温或新增成交额。")
    else:
        summaries.append("午间量能仅比较历史午间累计量；样本不足时只提示上午量已显著超过历史全天均量，不推算全天量。")
    payload["analysis"] = {
        "version": VERSION, "summaries": summaries, "hot_sectors": hot[:8],
        "volume_up": volume_up[:20], "volume_down": volume_down[:20], "risk_alerts": risks[:20],
        "counts": {"volume_up": len(volume_up), "volume_down": len(volume_down), "risk": len(risks)},
        "prior_slot_day": prior_day if previous else None,
        "afternoon_count": len(afternoon), "afternoon_amount": afternoon_total if morning else None,
        "volume_coverage": sum(r["volume_ratio"] is not None for r in rows),
        "notes": [
            "热点按缓存行业分类统计，不含概念题材识别；行业平均涨跌为等权样本，不是官方行业指数。",
            "活跃行业：至少5只样本、平均涨幅≥1%、上涨家数≥60%，且平均涨幅高于沪深300。连续活跃仅计算连续已保存的同时间快照。",
            "放量≥1.5倍、缩量≤0.7倍。收盘比较此前20个交易日全天均量；午间比较此前5～20个连续交易日午间均量。",
            "20日均线与高点均截至前一交易日；近期复权变化、历史缺失或昨收不一致时不提供量价参考。",
            "行业昨日比较要求两日样本重合≥98%；成交额占比变化还要求市场样本重合≥98%。午后成交额占比基于有效可比股票。",
            "冲高回落为最高价至当前价回落≥昨收的3%；异动清单排除风险标记及成交额不足的股票。",
            "实时量价异动用于观察，晚间另行核验日线与公告；不写入正式信号及收益跟踪。",
        ],
    }
    return payload
