"""Single worker with durable run status and bounded retries after daily cutoffs."""

import logging
import signal
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from .config import SHANGHAI, now
from .db import runs
from .pipeline import refresh_events, run_daily

logger = logging.getLogger(__name__)


def due(db, settings, current, kind, cutoff, deadline=None):
    if current.strftime("%H:%M") < cutoff:
        return False
    if deadline and current.strftime("%H:%M") >= deadline:
        return False
    attempts = db.rows(
        select(runs)
        .where(
            runs.c.source == settings.source, runs.c.day == current.strftime("%Y%m%d"), runs.c.kind == kind
        )
        .order_by(runs.c.started_at.desc())
    )
    if any(a["status"] in {"success", "skipped"} for a in attempts):
        return False
    # A terminated process can leave 'running'; retry only after the 6-hour lease expires.
    if attempts:
        latest = attempts[0]
        timestamp = datetime.fromisoformat(latest["started_at"]).astimezone(SHANGHAI)
        wait = timedelta(hours=6) if latest["status"] == "running" else timedelta(minutes=15)
        if current - timestamp < wait:
            return False
    return len(attempts) < 4


def run_evening(db, settings, day):
    """One durable slot; export failures retry without recomputing successful daily data."""
    from .html_export import atomic_write, export_published, publish_latest_png

    identifier = db.start_run(settings.source, day, "evening")
    try:
        config = replace(settings, export_html=False, export_png=False)
        existing = db.latest_report(settings.source, day)
        if not existing and settings.source == "baostock":
            from .db import sessions
            from .history_recovery import make_recovery_provider

            saved = db.rows(select(sessions.c.acquired_at).where(sessions.c.source == settings.source)
                            .order_by(sessions.c.acquired_at.desc()).limit(1))
            fresh = saved and datetime.fromisoformat(saved[0]["acquired_at"]).astimezone(SHANGHAI).strftime("%Y%m%d") >= day
            if fresh:
                # Reuse the established recovery path only when its existing
                # same-day metadata guard permits it; never promote stale scope.
                provider = make_recovery_provider(db, config, day)
                try:
                    result = run_daily(db, config, day, provider=provider)
                finally:
                    provider.close()
            else:
                result = run_daily(db, config, day)
        else:
            result = run_daily(db, config, day) if not existing else {"status": "exists", "day": day}
        if result["status"] == "skipped":
            db.finish_run(identifier, "skipped", "非交易日，未生成晚间报告")
            return result
        if settings.announcements and existing:
            # A fresh daily run already queried today's announcements; an existing
            # report needs its evening refresh, preserving original signal evidence.
            refresh_events(db, config, day)
        artifacts = None
        if settings.export_html or settings.export_png:
            artifacts = export_published(db, settings, day)
            path = Path(artifacts["path"])
            slot_path = path.with_name(f"{settings.source}-{day}-{settings.announcement_time.replace(':', '')}.html")
            atomic_write(slot_path, path.read_text())
            if settings.export_png:
                publish_latest_png(path.with_suffix(".png"), slot_path.with_suffix(".png"))
        db.finish_run(identifier, "success", "晚间日线分析已完成；公告按配置核验；HTML/PNG按配置导出")
        return {"status": "success", "day": day, "artifacts": artifacts}
    except (Exception, KeyboardInterrupt):
        db.finish_run(identifier, "failed", "晚间分析、公告或导出未完成；已保存数据保留，请查看对应任务及日志")
        raise


def worker(db, settings):
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    from .intraday import run_snapshot

    logger.info("定时任务启动：Asia/Shanghai 午间 %s；收盘 %s；晚间 %s（HTML/PNG）",
                settings.midday_time, settings.schedule_time, settings.announcement_time)
    while not stopped.is_set():
        current = now()
        for kind, cutoff, deadline in (
            ("midday", settings.midday_time, "13:00"),
            ("close", settings.schedule_time, settings.announcement_time),
            ("evening", settings.announcement_time, None),
        ):
            if due(db, settings, current, kind, cutoff, deadline):
                try:
                    day = current.strftime("%Y%m%d")
                    if kind == "evening":
                        run_evening(db, settings, day)
                    else:
                        run_snapshot(db, settings, day, kind)
                except Exception:
                    logger.exception("%s 任务失败；时段内15分钟后重试，最多4次", kind)
        stopped.wait(30)
