"""Single worker with durable run status and bounded retries after daily cutoffs."""

import logging
import signal
import threading
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import SHANGHAI, now
from .db import runs
from .pipeline import refresh_events, run_daily

logger = logging.getLogger(__name__)


def due(db, settings, current, kind, cutoff):
    if current.strftime("%H:%M") < cutoff:
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


def worker(db, settings):
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    logger.info(
        "定时任务启动：Asia/Shanghai %s；公告补查 %s", settings.schedule_time, settings.announcement_time
    )
    while not stopped.is_set():
        current = now()
        for kind, cutoff, operation in (
            ("daily", settings.schedule_time, run_daily),
            ("announcements", settings.announcement_time, refresh_events),
        ):
            if kind == "announcements" and not settings.announcements and settings.source != "demo":
                continue
            if kind == "announcements" and not db.latest_report(settings.source, current.strftime("%Y%m%d")):
                continue
            if due(db, settings, current, kind, cutoff):
                try:
                    operation(db, settings, current.strftime("%Y%m%d"))
                except Exception:
                    logger.error("%s 任务失败，详细状态已写入数据库；15分钟后重试", kind)
        stopped.wait(30)
