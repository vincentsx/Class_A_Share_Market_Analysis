"""Worker liveness, stage status and consistent private database backups."""

import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from .config import SHANGHAI, now
from .db import reports, runs, snapshots, system_state, trading_days


def state(db, settings, key):
    records = db.rows(select(system_state.c.payload).where(system_state.c.source == settings.source,
                                                          system_state.c.key == key))
    return records[0]["payload"] if records else {}


def backup_database(db, settings, force=False):
    if not settings.backup_enabled and not force:
        return {"status": "disabled"}
    current = now()
    prior = state(db, settings, "backup")
    if not force and prior.get("status") == "success" and prior.get("day") == current.strftime("%Y%m%d"):
        return prior
    folder = Path(settings.backup_dir).resolve()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = db.engine.url
    extension = ".sqlite3" if url.get_backend_name() == "sqlite" else ".dump"
    destination = folder / f"stock-daily-{current.strftime('%Y%m%d-%H%M%S-%f')}{extension}"
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=folder)
        os.close(fd)
        if url.get_backend_name() == "sqlite":
            if not url.database or url.database == ":memory:":
                raise ValueError("内存数据库不能作为持久备份")
            with sqlite3.connect(f"file:{Path(url.database).resolve()}?mode=ro", uri=True) as source:
                with sqlite3.connect(temporary) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份完整性校验失败")
        elif url.get_backend_name() == "postgresql":
            # Credentials are passed only in the child environment; never logged or written to metadata.
            env = os.environ.copy()
            for key, value in {"PGHOST": url.host, "PGPORT": url.port, "PGUSER": url.username,
                               "PGPASSWORD": url.password, "PGDATABASE": url.database}.items():
                if value is not None:
                    env[key] = str(value)
            result = subprocess.run(["pg_dump", "--format=custom", "--file", temporary],
                                    env=env, capture_output=True, timeout=300)
            if result.returncode:
                raise RuntimeError("pg_dump备份失败，请检查客户端、连接及权限")
        else:
            raise ValueError("仅支持SQLite和PostgreSQL备份")
        if Path(temporary).stat().st_size == 0:
            raise RuntimeError("备份文件为空")
        os.replace(temporary, destination)
        record = {"status": "success", "day": current.strftime("%Y%m%d"), "at": current.isoformat(),
                  "file": destination.name, "bytes": destination.stat().st_size}
        db.save_state(settings.source, "backup", record)
        cutoff = (current - timedelta(days=settings.backup_retain_days)).timestamp()
        for file in folder.glob("stock-daily-*" + extension):
            if file != destination and file.is_file() and not file.is_symlink() and file.stat().st_mtime < cutoff:
                file.unlink()
        return record
    except Exception:
        db.save_state(settings.source, "backup", {"status": "failed", "at": current.isoformat(),
                                                  "message": "数据库备份失败，请查看日志及备份配置"})
        raise
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def health_view(db, settings, current=None):
    current = current or now()
    heartbeat = state(db, settings, "worker")
    age = (current - datetime.fromisoformat(heartbeat["at"])).total_seconds() if heartbeat.get("at") else None
    worker_status = "running" if age is not None and 0 <= age <= 90 and heartbeat.get("status") == "running" else (
        "stopped" if heartbeat.get("status") == "stopped" else "stale" if age is not None else "unknown")
    stages = []
    for kind, label in [("daily", "日线采集"), ("midday", "午间快照"), ("close", "收盘快照"),
                        ("evening", "晚间分析与导出"), ("export", "HTML / PNG导出"),
                        ("email", "邮件发送"), ("pages", "公开网站发布")]:
        records = db.rows(select(runs).where(runs.c.source == settings.source, runs.c.kind == kind)
                          .order_by(runs.c.started_at.desc()).limit(1))
        stages.append({"kind": kind, "label": label, **(records[0] if records else {"status": "not_run"})})
    latest_daily = db.rows(select(reports.c.day).where(reports.c.source == settings.source)
                           .order_by(reports.c.day.desc()).limit(1))
    latest_snapshot = db.rows(select(snapshots.c.day, snapshots.c.slot).where(snapshots.c.source == settings.source)
                              .order_by(snapshots.c.day.desc(), snapshots.c.slot).limit(1))
    next_tasks = []
    for kind, cutoff in [("midday", settings.midday_time), ("close", settings.schedule_time),
                         ("evening", settings.announcement_time)]:
        # Use the actual saved calendar; if not warmed, report a check time rather than a promised trading task.
        target = current.replace(hour=int(cutoff[:2]), minute=int(cutoff[3:]), second=0, microsecond=0)
        known = db.rows(select(trading_days.c.day).where(trading_days.c.source == settings.source,
                                                        trading_days.c.is_open == 1,
                                                        trading_days.c.day >= current.strftime("%Y%m%d"))
                        .order_by(trading_days.c.day))
        eligible = [r["day"] for r in known if r["day"] > current.strftime("%Y%m%d") or target > current]
        if eligible:
            target = datetime.strptime(eligible[0] + cutoff, "%Y%m%d%H:%M").replace(tzinfo=SHANGHAI)
        elif target <= current:
            target += timedelta(days=1)
        next_tasks.append({"kind": kind, "at": target.isoformat(), "calendar_verified": bool(eligible)})
    pages = state(db, settings, "pages")
    if settings.pages_enabled:
        file = Path(settings.html_report_dir).absolute().parent / ".pages-published.json"
        try:
            dispatch = json.loads(file.read_text())
            pages["dispatched_at"] = dispatch.get("dispatched_at")
        except (OSError, ValueError):
            pass
    return {"checked_at": current.isoformat(), "worker": {**heartbeat, "status": worker_status,
                                                           "age_seconds": age},
            "latest_daily": latest_daily[0]["day"] if latest_daily else None,
            "latest_snapshot": latest_snapshot[0] if latest_snapshot else None,
            "next_tasks": next_tasks, "stages": stages, "backup": state(db, settings, "backup"),
            "backup_enabled": settings.backup_enabled, "pages": pages}
