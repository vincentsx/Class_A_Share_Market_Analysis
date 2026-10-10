from collections import defaultdict
from statistics import mean

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
        "schedule_time": settings.schedule_time,
        "announcement_time": settings.announcement_time,
    }
    if settings.source == "baostock":
        data["factor_histories"] = db.rows(
            select(func.count().label("count")).select_from(provider_cache).where(provider_cache.c.source == "baostock")
        )[0]["count"]
    if report is None:
        data["watchlist"] = [{**w, "available": False, "alerts": ["尚无分析报告"]} for w in watched]
        return data
    report = dict(report)
    stored_session = db.rows(
        select(sessions).where(sessions.c.source == settings.source, sessions.c.day == report["day"])
    )[0]
    report["events"] = tag_events(stored_session["events"])
    report["event_status"] = stored_session["event_status"]
    data["report"] = report
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
    return data


def tracking(db, settings, limit=200, as_of=None):
    saved = db.rows(
        select(signals)
        .where(signals.c.source == settings.source, signals.c.day <= as_of if as_of else True)
        .order_by(signals.c.day.desc(), signals.c.code)
        .limit(limit)
    )
    if not saved:
        return {"items": [], "summary": [], "method": "收盘信号观察统计，未扣费、不模拟成交"}
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
            **signal_outcome(s, histories[s["code"]], dates),
        }
        for s in saved
    ]
    summary = []
    for horizon in ("5", "10", "20"):
        observations = [
            item["horizons"][horizon]
            for item in items
            if item["horizons"].get(horizon, {}).get("status") == "ready"
        ]
        summary.append(
            {
                "horizon": int(horizon),
                "count": len(observations),
                "mean_return": mean(v["return"] for v in observations) if observations else None,
                "positive_ratio": sum(v["return"] > 0 for v in observations) / len(observations) * 100
                if observations
                else None,
                "mean_adverse": mean(v["max_adverse"] for v in observations) if observations else None,
            }
        )
    return {
        "items": items,
        "summary": summary,
        "method": f"最近最多{limit}个已发布信号；收盘到收盘、未扣费，不模拟成交。重叠信号不视为独立样本。",
        "as_of": latest,
    }
