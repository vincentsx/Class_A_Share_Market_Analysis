"""Recover truncated legacy candidates from frozen report metrics, with provenance.

Reconstructed rows are never passed off as originally published signals.
Count, rank and old published candidate payloads must match before writing.
"""

import math
from dataclasses import replace

from sqlalchemy import select

from .analysis import RULE_VERSION, screen_candidates
from .config import now
from .db import bars, reports, signals


def recover_archive(db, settings, day=None):
    records = db.rows(select(reports).where(reports.c.source == settings.source,
                                            reports.c.day == day if day else True).order_by(reports.c.day))
    recovered, skipped = [], []
    for record in records:
        report = record["payload"]
        if report.get("candidate_storage") in {"complete", "reconstructed"}:
            continue
        if report.get("all_candidate_count", len(report["candidates"])) <= len(report["candidates"]):
            continue
        if report.get("rule_version") != RULE_VERSION:
            skipped.append({"day": record["day"], "reason": "规则版本不匹配，未重建"})
            continue
        stored = {r["code"]: r for r in db.rows(select(bars).where(
            bars.c.source == settings.source, bars.c.day == record["day"]))}
        if any(m["code"] not in stored or not math.isclose(m["close"], stored[m["code"]]["close"], rel_tol=1e-8)
               for m in report["stocks"]):
            skipped.append({"day": record["day"], "reason": "行情与冻结指标不一致，未重建"})
            continue
        # Legacy v1 published parameters defaulted to 50m; do not borrow a later .env threshold.
        threshold = report.get("rule_parameters", {}).get("min_amount", 50_000_000)
        matched = screen_candidates(replace(settings, min_amount=threshold), report["stocks"], report["industries"],
                                    report["events"], report["event_status"],
                                    {code: r["adj"] for code, r in stored.items()})
        original = report["candidates"]
        if len(matched) != report["all_candidate_count"] or matched[:len(original)] != original:
            skipped.append({"day": record["day"], "reason": "命中总数、排序或原始依据不匹配，未重建"})
            continue
        published = {(r["code"], r["strategy"]): r for r in db.rows(select(signals).where(
            signals.c.source == settings.source, signals.c.day == record["day"]))}
        timestamp = now().isoformat()
        full = []
        for candidate in matched:
            key = candidate["code"], candidate["strategy"]
            if key in published:
                full.append(candidate)
            else:
                full.append({**candidate, "reconstructed": True, "reconstructed_at": timestamp})
        report.update(all_candidates=full, candidate_storage="reconstructed", reconstructed_at=timestamp,
                      reconstructed_count=sum(bool(c.get("reconstructed")) for c in full))
        report["notes"] = [*report["notes"], "补充候选根据原报告冻结指标重建；重建记录不是当时已发布信号，"
                           "效果单独列组，不作为可交易历史信号。"]
        # Recompute the new comparison view after restoring the baseline, but keep signal evidence unchanged.
        report.pop("changes", None)
        with db.engine.begin() as connection:
            for candidate in full:
                if not candidate.get("reconstructed"):
                    continue
                connection.execute(signals.insert().values(source=settings.source, day=record["day"],
                    code=candidate["code"], strategy=candidate["strategy"],
                    payload={**candidate, "published_at": timestamp, "priority": False}))
            db.upsert(connection, reports, {"source": settings.source, "day": record["day"], "payload": report})
        recovered.append({"day": record["day"], "original": len(published),
                          "reconstructed": report["reconstructed_count"], "total": len(full)})
    return {"status": "success", "recovered": recovered, "skipped": skipped}
