"""Single worker with durable run status and bounded retries after daily cutoffs."""

import logging
import os
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
        email = {"status": "disabled"}
        if settings.export_html or settings.export_png:
            export_id = db.start_run(settings.source, day, "export")
            artifacts = export_published(db, settings, day)
            path = Path(artifacts["path"])
            slot_path = path.with_name(f"{settings.source}-{day}-{settings.announcement_time.replace(':', '')}.html")
            atomic_write(slot_path, path.read_text())
            if settings.export_png:
                publish_latest_png(path.with_suffix(".png"), slot_path.with_suffix(".png"))
            db.finish_run(export_id, "success", "晚间HTML/PNG已按配置导出")
            export_id = None
            from .mailer import enqueue_safely

            email = enqueue_safely(db, settings, day, "evening", slot_path, slot_path.with_suffix(".png"),
                                   db.latest_report(settings.source, day))
        db.finish_run(identifier, "success", "晚间日线分析已完成；公告按配置核验；HTML/PNG按配置导出")
        return {"status": "success", "day": day, "artifacts": artifacts, "email": email}
    except (Exception, KeyboardInterrupt):
        if locals().get("export_id"):
            db.finish_run(export_id, "failed", "晚间HTML/PNG导出未完成，已保存日线保留")
        db.finish_run(identifier, "failed", "晚间分析、公告或导出未完成；已保存数据保留，请查看对应任务及日志")
        raise


def worker(db, settings):
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    activity = {"phase": "启动检查"}

    def pulse():
        while not stopped.is_set():
            try:
                db.save_state(settings.source, "worker", {"at": now().isoformat(), "status": "running",
                                                          "phase": activity["phase"], "pid": os.getpid()})
            except Exception:
                logger.warning("worker心跳写入失败", exc_info=True)
            stopped.wait(20)

    heartbeat = threading.Thread(target=pulse, daemon=True)
    quotes = threading.Thread(target=_portfolio_loop, args=(db, settings, stopped), daemon=True)
    alerts = threading.Thread(target=_trade_mail_loop, args=(db, settings, stopped), daemon=True)
    wecom = threading.Thread(target=_wecom_loop, args=(db, settings, stopped), daemon=True)
    # The single-process worker lock is released by the OS even after a crash.
    if db.engine.dialect.name == "sqlite":
        import fcntl

        path = Path(db.engine.url.database).resolve().with_suffix(".worker.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        guard = path.open("a")
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            guard.close()
            raise RuntimeError("该数据库已有worker运行，请勿重复启动") from None
    else:
        import hashlib

        from sqlalchemy import text

        guard = db.engine.connect()
        lock_id = int.from_bytes(hashlib.sha256(b"stock-daily-worker").digest()[:8], "big", signed=True)
        if not guard.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}).scalar():
            guard.close()
            raise RuntimeError("该数据库已有worker运行，请勿重复启动")
        guard.commit()  # Session advisory lock survives; avoid an idle transaction for the worker lifetime.
    try:
        heartbeat.start()
        quotes.start()
        alerts.start()
        wecom.start()
        _worker_loop(db, settings, stopped, activity)
    finally:
        stopped.set()
        heartbeat.join(timeout=5)
        quotes.join(timeout=5)
        alerts.join(timeout=5)
        wecom.join(timeout=5)
        try:
            db.save_state(settings.source, "worker", {"at": now().isoformat(), "status": "stopped",
                                                      "phase": "已停止", "pid": os.getpid()})
            if db.engine.dialect.name != "sqlite":
                guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
        finally:
            guard.close()


def _wecom_loop(db, settings, stopped):
    from .wecom import run

    run(db, settings, stopped)


def _trade_mail_loop(db, settings, stopped):
    """SMTP waits cannot block quote collection or report scheduling."""
    from .mailer import flush_outbox
    from .trade_mail import queue_trade_alerts

    while not stopped.is_set():
        if settings.email_enabled and settings.trade_email_enabled:
            try:
                queue_trade_alerts(db, settings)
                flush_outbox(db, settings, limit=10, slots=("trade",))
            except Exception:
                logger.warning("模拟成交提醒未完成；成交已保留，邮件将重试", exc_info=True)
        stopped.wait(30)


def _portfolio_loop(db, settings, stopped):
    """Keep quote cadence independent of lengthy report exports and site uploads."""
    from .live_publish import publish_portfolio
    from .portfolio import portfolio_due, refresh_portfolio

    publish_checked_at = None

    while not stopped.is_set():
        try:
            if portfolio_due(db, settings, now()):
                refresh_portfolio(db, settings)
        except Exception:
            logger.warning("盘中估值未完成，保留已保存行情并标注时效", exc_info=True)
        if settings.pages_enabled and (publish_checked_at is None or now() - publish_checked_at >= timedelta(minutes=1)):
            publish_checked_at = now()
            try:
                publish_portfolio(db, settings)
            except Exception:
                logger.warning("公开持仓轻量同步未完成，将重试；盘中采集继续", exc_info=True)
        stopped.wait(30)


def _worker_loop(db, settings, stopped, activity):
    pages_checked_at = None
    backup_checked_at = None
    from .intraday import run_snapshot

    logger.info("定时任务启动：Asia/Shanghai 午间 %s；收盘 %s；晚间 %s（HTML/PNG）",
                settings.midday_time, settings.schedule_time, settings.announcement_time)
    while not stopped.is_set():
        current = now()
        if settings.backup_enabled and (backup_checked_at is None or current - backup_checked_at >= timedelta(minutes=15)):
            from .operations import backup_database

            backup_checked_at = current
            activity["phase"] = "数据库备份"
            try:
                backup_database(db, settings)
            except Exception:
                logger.warning("数据库备份未完成，行情任务继续", exc_info=True)
        if settings.news_enabled and settings.source != "demo" and "08:00" <= current.strftime("%H:%M") < "21:00":
            from .news import collect_news

            try:
                activity["phase"] = "快讯采集"
                collect_news(db, settings)
            except Exception:
                logger.warning("热点新闻采集未完成；行情任务继续运行", exc_info=True)
        for kind, cutoff, deadline in (
            ("midday", settings.midday_time, "13:00"),
            ("close", settings.schedule_time, settings.announcement_time),
            ("evening", settings.announcement_time, None),
        ):
            current = now()
            if due(db, settings, current, kind, cutoff, deadline):
                try:
                    activity["phase"] = {"midday": "午间采集与导出", "close": "收盘采集与导出",
                                         "evening": "晚间日线分析与导出"}[kind]
                    day = current.strftime("%Y%m%d")
                    if kind == "evening":
                        run_evening(db, settings, day)
                    else:
                        run_snapshot(db, settings, day, kind)
                except Exception:
                    logger.exception("%s 任务失败；时段内15分钟后重试，最多4次", kind)
        if settings.email_enabled:
            from .mailer import flush_outbox

            try:
                activity["phase"] = "邮件发送"
                flush_outbox(db, settings)
            except Exception:
                logger.warning("邮件处理未完成，请检查本地邮箱配置；分析结果已保留")
        if settings.pages_enabled and (pages_checked_at is None or now() - pages_checked_at >= timedelta(minutes=10)):
            from .pages import publish_pages

            pages_checked_at = now()
            identifier = db.start_run(settings.source, now().strftime("%Y%m%d"), "pages")
            try:
                activity["phase"] = "公开网站发布"
                result = publish_pages(db, settings, settings.pages_repository)
                db.save_state(settings.source, "pages", {"at": now().isoformat(), "status": result["status"],
                                                         "url": result.get("url", "")})
                db.finish_run(identifier, "success", f"公开网站处理：{result['status']}；上线以部署成功为准")
                logger.info("GitHub Pages：%s", result["status"])
            except Exception:
                db.finish_run(identifier, "failed", "公开网站发布未完成，将重试")
                db.save_state(settings.source, "pages", {"at": now().isoformat(), "status": "failed"})
                logger.warning("GitHub Pages发布未完成；报告和邮件已保留，10分钟后重试", exc_info=True)
        activity["phase"] = "等待下一次检查"
        stopped.wait(30)
