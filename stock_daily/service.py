from collections import defaultdict

from sqlalchemy import func, select

from .analysis import signal_outcome, tag_events
from .config import now
from .db import bars, provider_cache, reports, runs, sessions, signals, trading_days, watchlist


def dashboard(db, settings, day=None):
    report = db.latest_report(settings.source, day)
    dates = db.rows(
        select(reports.c.day)
        .where(reports.c.source == settings.source)
        .order_by(reports.c.day.desc())
        .limit(120)
    )
    tasks = db.rows(
        select(runs).where(runs.c.source == settings.source).order_by(runs.c.started_at.desc()).limit(8)
    )
    watched = db.rows(select(watchlist).order_by(watchlist.c.added_at.desc()))
    data = {
        "source": settings.source,
        "report": report,
        "dates": [r["day"] for r in dates],
        "runs": tasks,
        "watchlist": [],
        "today": now().strftime("%Y%m%d"),
        "midday_time": settings.midday_time,
        "schedule_time": settings.schedule_time,
        "announcement_time": settings.announcement_time,
    }
    from .mailer import outbox_status

    data['email'] = outbox_status(db, settings)
    from .operations import health_view

    data['health'] = health_view(db, settings)
    if settings.source == "baostock":
        data["factor_histories"] = db.rows(
            select(func.count().label("count")).select_from(provider_cache).where(provider_cache.c.source == "baostock")
        )[0]["count"]
    if report is None:
        data["watchlist"] = [{**w, "available": False, "alerts": ["尚无分析报告"]} for w in watched]
        return data
    report = dict(report)
    frozen_report = dict(report)
    stored_session = db.rows(
        select(sessions).where(sessions.c.source == settings.source, sessions.c.day == report["day"])
    )[0]
    report["events"] = tag_events(stored_session["events"])
    report["event_status"] = stored_session["event_status"]
    data["report"] = report
    from .observations import plan_views
    from .research import changes, evidence

    if "changes" not in report:
        report["changes"] = changes(db, settings, report)
    if "evidence" not in report:
        report["evidence"] = evidence(db, settings, frozen_report)
    from .evidence_ai import cached_summary

    data["ai_configured"] = bool(settings.ai_api_key and settings.ai_model)
    data["evidence_summary"] = cached_summary(db, settings, report["day"], report["evidence"])
    stock_map = {m["code"]: m for m in report["stocks"]}
    last_bars = {
        r["code"]: r
        for r in db.rows(
            select(bars).where(
                bars.c.source == settings.source,
                bars.c.day == report["day"],
            )
        )
    }
    suspended = set(report["quality"]["suspended"])
    for watch in watched:
        metric = stock_map.get(watch["code"])
        alerts = []
        if metric is None:
            alerts.append(
                "停牌，暂无当日行情"
                if watch["code"] in suspended
                else "当日行情或连续65日历史不足，无法完成趋势检查"
            )
        else:
            if metric["close"] < metric["ma20"]:
                alerts.append("收盘低于20日均线")
            if metric["pct"] <= -5:
                alerts.append("当日跌幅超过或等于5%")
            if metric["volume_ratio"] >= 2 and metric["pct"] < 0:
                alerts.append("放量下跌：成交量达到此前20日均量的2倍")
        events = [e for e in report["events"] if e["code"] == watch["code"]]
        alerts.extend(f"公告待核对：{e['title']}" for e in events if e["tags"])
        if report["event_status"] in {"disabled", "unavailable"}:
            alerts.append("公告未核验，不能据此认定无公告风险")
        data["watchlist"].append(
            {
                **(metric or last_bars.get(watch["code"], {})),
                **watch,
                "available": metric is not None,
                "alerts": alerts,
                "events": events,
            }
        )
    data["watchlist"] = plan_views(db, settings, report, data["watchlist"])
    return data


def tracking(db, settings, limit=10000, as_of=None):
    saved = db.rows(
        select(signals)
        .where(signals.c.source == settings.source, signals.c.day <= as_of if as_of else True)
        .order_by(signals.c.day.desc(), signals.c.code)
        .limit(limit)
    )
    total_count = db.rows(select(func.count().label("count")).select_from(signals).where(
        signals.c.source == settings.source, signals.c.day <= as_of if as_of else True))[0]["count"]
    if not saved:
        return {"items": [], "summary": [], "groups": [], "total_count": 0, "loaded_count": 0,
                "method": "收盘信号观察统计，未扣费、不模拟成交", "as_of": as_of}
    earliest = min(s["day"] for s in saved)
    latest = db.rows(
        select(sessions.c.day)
        .where(sessions.c.source == settings.source, sessions.c.day <= as_of if as_of else True)
        .order_by(sessions.c.day.desc())
        .limit(1)
    )[0]["day"]
    dates = [
        r["day"]
        for r in db.rows(
            select(trading_days.c.day)
            .where(
                trading_days.c.source == settings.source,
                trading_days.c.day >= earliest,
                trading_days.c.day <= latest,
                trading_days.c.is_open == 1,
            )
            .order_by(trading_days.c.day)
        )
    ]
    histories = defaultdict(list)
    for row in db.rows(
        select(bars)
        .where(
            bars.c.source == settings.source,
            bars.c.day >= earliest,
            bars.c.day <= latest,
            bars.c.code.in_({s["code"] for s in saved}),
        )
        .order_by(bars.c.day)
    ):
        histories[row["code"]].append(row)
    items = [
        {
            "day": s["day"],
            "code": s["code"],
            "name": s["payload"]["name"],
            "label": s["payload"]["label"],
            "strategy": s["strategy"],
            "rule_version": s["payload"].get("rule_version", "unknown"),
            "priority": s["payload"].get("priority", True),
            "reconstructed": s["payload"].get("reconstructed", False),
            **signal_outcome(s, histories[s["code"]], dates),
        }
        for s in saved
    ]
    benchmark = {r["day"]: r["benchmark"]["close"] for r in db.rows(select(sessions).where(
        sessions.c.source == settings.source, sessions.c.day >= earliest, sessions.c.day <= latest))}
    for item in items:
        for outcome in item["horizons"].values():
            start, end = benchmark.get(item["day"]), benchmark.get(outcome.get("end_day"))
            outcome["benchmark_return"] = (end / start - 1) * 100 if start and end else None
            outcome["excess_return"] = outcome["return"] - outcome["benchmark_return"] if (
                outcome.get("status") == "ready" and outcome["benchmark_return"] is not None) else None
    from .scorecard import cohorts, summarize

    coverage = db.rows(select(reports.c.day, reports.c.payload).where(
        reports.c.source == settings.source, reports.c.day >= earliest, reports.c.day <= latest))
    incomplete = [r["day"] for r in coverage if r["payload"].get("candidate_storage") != "complete"
                  and r["payload"].get("all_candidate_count", 0) > len(r["payload"]["candidates"])]
    published_items = [i for i in items if not i["reconstructed"]]
    return {
        "items": items[:200],
        "summary": [summarize(published_items, n) for n in (5, 10, 20)],
        "groups": cohorts(items, dates), "total_count": total_count, "loaded_count": len(items),
        "legacy_incomplete_days": incomplete,
        "reconstructed_count": len(items) - len(published_items),
        "method": f"统计最近最多{limit}条已存信号，当前{len(items)}条；明细展示前200条。"
                  "收盘到收盘、未扣费，不模拟成交。同股票/策略/版本按20交易日冷却另列统计；"
                  "冷却后的样本仍可能相关，不声称独立。小于30条标记样本不足。"
                  + (f" {len(incomplete)}个旧报告日未在当时完整发布，不能代表全部已发布规则命中。" if incomplete else "")
                  + (f" {len(items) - len(published_items)}条重建候选只在重建样本组统计，默认概览排除。"
                     if len(items) != len(published_items) else ""),
        "as_of": latest,
    }
