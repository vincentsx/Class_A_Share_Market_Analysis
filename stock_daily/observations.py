"""Personal plans with durable daily transitions, separate from market signals."""

import hashlib
import json

from sqlalchemy import select

from .config import now
from .db import watch_evaluations, watch_plans

DEFAULT_CHECKS = ["below_ma20", "down5", "volume_down"]
CHECKS = {"below_ma20": "收盘低于20日均线", "down5": "当日跌幅达到5%",
          "volume_down": "放量下跌，成交量达到20日均量2倍",
          "below_invalidation": "收盘低于计划失效价", "above_target": "收盘达到计划观察价"}


def plan_version(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evaluate(plan, metric):
    if not metric:
        return {"available": False, "active": []}
    conditions = {
        "below_ma20": metric["close"] < metric["ma20"], "down5": metric["pct"] <= -5,
        "volume_down": metric["volume_ratio"] >= 2 and metric["pct"] < 0,
        "below_invalidation": plan.get("invalidation") is not None and metric["close"] < plan["invalidation"],
        "above_target": plan.get("target") is not None and metric["close"] >= plan["target"],
    }
    return {"available": True, "active": [c for c in plan.get("checks", DEFAULT_CHECKS) if conditions[c]]}


def plan_views(db, settings, report, watched, persist=False, connection=None):
    plans = {r["code"]: r for r in db.rows(select(watch_plans))}
    if not report:
        return [{**w, "plan": plans.get(w["code"], {}).get("payload", {}), "changes": []} for w in watched]
    day = report["day"]
    metrics = {m["code"]: m for m in report["stocks"]}
    saved = {r["code"]: r["payload"] for r in db.rows(select(watch_evaluations).where(
        watch_evaluations.c.source == settings.source, watch_evaluations.c.day == day))}
    # Only compare to a real prior daily evaluation; retain the stated baseline date.
    previous = db.rows(select(watch_evaluations).where(
        watch_evaluations.c.source == settings.source, watch_evaluations.c.day < day)
        .order_by(watch_evaluations.c.day.desc()))
    prior = {}
    for row in previous:
        prior.setdefault(row["code"], row)
    views = []
    for watch in watched:
        code = watch["code"]
        entry = plans.get(code)
        plan = entry["payload"] if entry else {"reason": watch.get("note", ""), "checks": DEFAULT_CHECKS}
        version = plan_version(plan)
        # Never apply a newly edited plan retroactively to an old report.
        applicable = not entry or entry["updated_at"] <= report.get("generated_at", "")
        value = saved.get(code) if saved.get(code, {}).get("version") == version else None
        if value is None:
            value = {**evaluate(plan, metrics.get(code)), "version": version, "changes": [], "baseline_day": None}
            old = prior.get(code)
            same_plan = old and old["payload"].get("version") == version
            if applicable and same_plan and old["payload"].get("available") and value["available"]:
                old_active = set(old["payload"]["active"])
                value["baseline_day"] = old["day"]
                value["changes"] = ([{"check": c, "label": CHECKS[c], "state": "triggered"}
                                     for c in value["active"] if c not in old_active] +
                                    [{"check": c, "label": CHECKS[c], "state": "cleared"}
                                     for c in old_active if c not in value["active"]])
            if not applicable:
                value.update(available=False, active=[], changes=[], notice="观察计划修改晚于报告；等待下一份日线报告")
            elif not same_plan:
                value["notice"] = "首次评估或计划已修改，建立基线，不重复发送状态提醒"
            if persist and applicable:
                db.upsert(connection, watch_evaluations, {"source": settings.source, "day": day,
                                                         "code": code, "payload": value})
        views.append({**watch, "plan": plan, "evaluation": value, "changes": value["changes"]})
    return views


def save_plan(db, code, payload):
    with db.engine.begin() as connection:
        db.upsert(connection, watch_plans, {"code": code, "payload": payload, "updated_at": now().isoformat()})
