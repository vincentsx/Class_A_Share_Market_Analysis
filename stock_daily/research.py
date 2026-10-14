"""Point-in-time daily changes and evidence. No network calls or invented impact scores."""

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import SHANGHAI
from .db import announcement_evidence, bars, news_items, reports, snapshots, trading_days
from .news import PROVIDERS, kind_of, local_time, mentioned_codes, name_index
from .static_report import safe_link


def candidates(report):
    return report.get("all_candidates", report.get("candidates", []))


def announcement_id(event):
    text = "\0".join(str(event.get(k, "")) for k in ("code", "day", "title", "url", "published_at"))
    return hashlib.sha256(text.encode()).hexdigest()


def record_announcements(db, connection, source, events, captured_at):
    for event in events:
        identifier = announcement_id(event)
        existing = connection.execute(select(announcement_evidence.c.id).where(
            announcement_evidence.c.source == source, announcement_evidence.c.id == identifier)).first()
        if not existing:
            connection.execute(announcement_evidence.insert().values(
                source=source, id=identifier, day=event["day"], first_seen_at=captured_at, payload=event))


def changes(db, settings, report, today_rows=None):
    day = report["day"]
    previous_days = db.rows(select(trading_days.c.day).where(
        trading_days.c.source == settings.source, trading_days.c.day < day, trading_days.c.is_open == 1)
        .order_by(trading_days.c.day.desc()).limit(1))
    previous_day = previous_days[0]["day"] if previous_days else None
    previous = db.latest_report(settings.source, previous_day) if previous_day else None
    result = {"previous_day": previous_day, "comparable": bool(previous), "new": [], "continuing": [],
              "left": [], "invalidated": [], "unavailable": [], "summaries": [], "intraday": []}
    current = {(c["code"], c["strategy"]): c for c in candidates(report)}
    if previous:
        old = {(c["code"], c["strategy"]): c for c in candidates(previous)}
        # A legacy top-N report cannot prove absence from its undisplayed candidates.
        old_complete = previous.get("candidate_storage") == "complete" or (
            previous.get("all_candidate_count", len(old)) == len(old))
        reconstructed = previous.get("candidate_storage") == "reconstructed"
        result["baseline_complete"] = old_complete
        result["new"] = [dict(c, change="较重建名单新增" if reconstructed else
                              "新出现" if old_complete else "较已存名单新增")
                         for key, c in current.items() if key not in old]
        result["continuing"] = [dict(c, change="持续满足") for key, c in current.items() if key in old]
        metrics = {r["code"]: r for r in report["stocks"]}
        current_bars = {r["code"]: r for r in (today_rows if today_rows is not None else db.rows(select(bars).where(
            bars.c.source == settings.source, bars.c.day == day)))}
        for key, c in old.items():
            if key in current:
                continue
            metric = metrics.get(c["code"])
            row = current_bars.get(c["code"])
            if row is None or metric is None:
                result["unavailable"].append(dict(c, change="当日行情或连续历史不足，无法判定"))
            elif row["close"] * row["adj"] < c["invalidation_adj"]:
                result["invalidated"].append(dict(c, change="收盘触发原信号失效位置"))
            else:
                result["left"].append(dict(c, change="不再满足全部入选条件，未触发原失效位置"))
        result["summaries"].append(
            f"较{previous_day}已存名单：新增{len(result['new'])}条，持续{len(result['continuing'])}条，"
            f"不再入选{len(result['left'])}条，触发失效{len(result['invalidated'])}条，"
            f"无法判定{len(result['unavailable'])}条。")
        if not old_complete:
            result["summaries"].append("上一交易日仅存优先展示名单，新增不代表首次满足规则。")
        if reconstructed:
            result["summaries"].append("上一交易日包含冻结指标重建候选；该部分不是当时已发布信号。")
    else:
        result["summaries"].append("缺少上一交易日已发布报告，暂不判断新增、持续或退出。")
    for record in db.rows(select(snapshots).where(snapshots.c.source == settings.source,
                                                 snapshots.c.day == day).order_by(snapshots.c.slot.desc())):
        payload = record["payload"]
        if payload.get("captured_at", "") > report.get("generated_at", ""):
            continue
        result["intraday"].append({"slot": record["slot"], "captured_at": payload["captured_at"],
                                   "summaries": payload.get("analysis", {}).get("summaries", [])})
    result["summaries"].insert(0, f"规则命中{report['all_candidate_count']}条；"
                                f"优先展示{len(report['candidates'])}条；"
                                f"存档{len(candidates(report))}条。")
    if report.get("reconstructed_count"):
        result["summaries"].append(f"其中{report['reconstructed_count']}条由冻结指标重建，"
                                   "不是当时已发布信号；效果单独统计，模拟成交排除。")
    return result


def evidence(db, settings, report, today_rows=None):
    day = report["day"]
    day_end = datetime.strptime(day + "235959", "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
    cutoff = min(day_end, local_time(report.get("generated_at") or day_end.isoformat()))
    rows = {r["code"]: r for r in report["stocks"]}
    names = name_index(rows.values())
    selected = {r["code"] for r in candidates(report)}
    cards = []
    window = (cutoff - timedelta(days=7)).strftime("%Y%m%d")
    observations = [(e, report.get("acquired_at") or report.get("generated_at")) for e in report.get("events", [])]
    # Legacy immutable reports establish when their original titles were locally known.
    for prior in db.rows(select(reports.c.payload).where(reports.c.source == settings.source,
                                                         reports.c.day >= window, reports.c.day < day)):
        original = prior["payload"]
        observations.extend((e, original.get("acquired_at") or original.get("generated_at"))
                            for e in original.get("events", []))
    for item in db.rows(select(announcement_evidence).where(
            announcement_evidence.c.source == settings.source, announcement_evidence.c.day >= window,
            announcement_evidence.c.day <= day, announcement_evidence.c.first_seen_at <= cutoff.isoformat())):
        observations.append((item["payload"], item["first_seen_at"]))
    unique = {}
    for e, seen_at in observations:
        identifier = announcement_id(e)
        if not seen_at:
            continue
        if identifier not in unique or local_time(seen_at) < local_time(unique[identifier][1]):
            unique[identifier] = e, seen_at
    for identifier, (e, seen_at) in unique.items():
        if seen_at and local_time(seen_at) > cutoff:
            continue
        try:
            published = local_time(e["published_at"]).isoformat() if e.get("published_at") else None
        except (ValueError, TypeError):
            published = None
        if published and local_time(published) > cutoff:
            continue
        cards.append({"id": identifier,
                      "kind": "公告标题", "title": e["title"], "summary": e["title"],
                      "published_at": published,
                      "first_seen_at": seen_at, "codes": [e["code"]],
                      "sources": [{"name": "公告原文", "url": safe_link(e.get("url", ""))}],
                      "questions": ["请核对原文、事件发生日期及涉及金额；标题不足以判断影响。"],
                      "basis": "仅标题，未读取正文", "reactions": []})
    if settings.source != "demo" and settings.news_enabled:
        records = db.rows(select(news_items).where(
            news_items.c.published_at >= (cutoff - timedelta(days=7)).isoformat(),
            news_items.c.published_at <= cutoff.isoformat(), news_items.c.first_seen_at <= cutoff.isoformat())
            .order_by(news_items.c.published_at.desc(), news_items.c.first_seen_at.desc()).limit(2000))
        # Keep the newest known text version per source/title/day at this cutoff.
        seen = set()
        for r in records:
            key = (r["provider"], r["title"], r["published_at"][:10])
            if key in seen:
                continue
            seen.add(key)
            codes = mentioned_codes(r["title"] + " " + r["content"], rows, names)
            if not codes:
                continue
            cards.append({"id": r["id"], "kind": kind_of(r["title"] + " " + r["content"]),
                          "title": r["title"], "summary": r["content"][:240],
                          "published_at": r["published_at"], "first_seen_at": r["first_seen_at"],
                          "codes": sorted(codes), "sources": [{"name": PROVIDERS[r["provider"]],
                                                                 "url": safe_link(r["url"])}],
                          "questions": ["是否已有公司公告或独立来源证实？", "消息是否涉及主营业务与可量化金额？"],
                          "basis": "已留存快讯摘录，未推断利好利空", "reactions": []})
    total = len(cards)
    cards.sort(key=lambda c: (bool(set(c["codes"]) & selected), c["first_seen_at"] or ""), reverse=True)
    cards = cards[:60]
    dates = [r["day"] for r in db.rows(select(trading_days.c.day).where(
        trading_days.c.source == settings.source, trading_days.c.day <= day, trading_days.c.is_open == 1)
        .order_by(trading_days.c.day))]
    codes = {c for card in cards for c in card["codes"]}
    history = {(r["code"], r["day"]): r for r in db.rows(select(bars).where(
        bars.c.source == settings.source, bars.c.code.in_(codes), bars.c.day <= day,
        bars.c.day >= (cutoff - timedelta(days=20)).strftime("%Y%m%d")))} if codes else {}
    history.update({(r["code"], r["day"]): r for r in today_rows or []})
    for card in cards:
        known = max(local_time(t) for t in [card["published_at"], card["first_seen_at"]] if t)
        # Start at a close after local discovery; never label the earlier price as a reaction.
        anchors = [d for d in dates if d + "150000" > known.strftime("%Y%m%d%H%M%S")]
        anchor = anchors[0] if anchors else None
        for code in card["codes"]:
            start = history.get((code, anchor))
            future = [d for d in dates if anchor and d > anchor]
            for horizon in (1, 5):
                interval = future[:horizon]
                ready = bool(start and len(interval) == horizon and all((code, d) in history for d in interval))
                end = history.get((code, interval[-1])) if ready else None
                card["reactions"].append({"code": code, "horizon": horizon, "anchor_day": anchor,
                                          "end_day": interval[-1] if ready else None,
                                          "return": (end["close"] * end["adj"] / (start["close"] * start["adj"]) - 1)
                                          * 100 if ready else None})
    return {"as_of": cutoff.isoformat(), "items": cards, "count": total,
            "method": "仅使用发布与首次收录均不晚于截止时点的证据；价格同现及后续变化不能证明因果。"}
