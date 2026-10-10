import logging
import math
from collections import defaultdict
from datetime import timedelta

import httpx
from sqlalchemy import delete, select, update

from .analysis import build_report
from .config import now
from .db import bars, reports, runs, sessions, signals, trading_days
from .providers import DataError, make_provider, parse_day

logger = logging.getLogger(__name__)


def validate_batch(batch, universe, settings):
    codes = [r["code"] for r in batch.bars]
    if not codes or len(set(codes)) != len(codes):
        raise DataError("行情为空或有重复股票")
    for row in batch.bars:
        if row["day"] != batch.day or row["code"] not in universe:
            raise DataError("行情日期或股票代码无法核对")
        for key in ("open", "high", "low", "close", "volume", "amount", "adj", "pct"):
            if not isinstance(row.get(key), (int, float)) or not math.isfinite(row[key]):
                raise DataError(f"{row['code']} 的 {key} 非有效数值")
        if min(row[k] for k in ("open", "high", "low", "close", "volume", "amount", "adj")) <= 0:
            raise DataError(f"{row['code']} 价格、成交量或复权因子无效")
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
            raise DataError(f"{row['code']} OHLC不一致")
        for key in ("turnover", "pe", "pb"):
            if row.get(key) is not None and (
                not isinstance(row[key], (int, float)) or not math.isfinite(row[key])
            ):
                raise DataError(f"{row['code']} 的 {key} 无效")
    expected = {
        code
        for code, stock in universe.items()
        if stock["list_date"]
        and stock["list_date"] <= batch.day
        and (not stock["delist_date"] or stock["delist_date"] > batch.day)
    } - batch.suspended
    missing = sorted(expected - set(codes))
    covered = len(expected & set(codes)) / len(expected) if expected else 0
    if covered < settings.min_coverage:
        raise DataError(
            f"{batch.day} 行情覆盖率 {covered:.1%}，低于阈值 {settings.min_coverage:.1%}；缺失{len(missing)}只"
        )
    benchmark = batch.benchmark
    if (
        benchmark.get("trade_date") != batch.day
        or not isinstance(benchmark.get("close"), (int, float))
        or not math.isfinite(benchmark["close"])
        or benchmark["close"] <= 0
        or not isinstance(benchmark.get("pct_chg"), (int, float))
        or not math.isfinite(benchmark["pct_chg"])
    ):
        raise DataError("基准行情无效")
    return {
        "expected": len(expected),
        "collected": len(codes),
        "coverage": covered * 100,
        "missing": missing,
        "suspended": sorted(batch.suspended),
        "status": "complete" if not missing else "partial",
        "min_coverage": settings.min_coverage * 100,
    }


def alert(settings, message):
    if not settings.alert_webhook:
        return
    try:
        httpx.post(
            settings.alert_webhook,
            json={
                "system": "stock-daily",
                "source": settings.source,
                "message": message,
                "time": now().isoformat(),
            },
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError:
        logger.error("失败提醒发送失败，请检查 webhook 配置")


def save_session(db, connection, source, snapshot, rows):
    connection.execute(delete(bars).where(bars.c.source == source, bars.c.day == snapshot["day"]))
    for offset in range(0, len(rows), 500):
        connection.execute(bars.insert(), [{**row, "source": source} for row in rows[offset : offset + 500]])
    db.upsert(connection, sessions, snapshot)


def run_daily(db, settings, day=None, force=False, provider=None):
    day = day or now().strftime("%Y%m%d")
    parse_day(day)
    if day > now().strftime("%Y%m%d"):
        raise DataError("不能分析未来日期")
    identifier = db.start_run(settings.source, day)
    source = settings.source
    own_provider = provider is None
    active_provider = provider
    try:
        with db.lock(source):
            active_provider = active_provider or make_provider(settings)
            start = (parse_day(day) - timedelta(days=max(220, settings.history_sessions * 2))).strftime(
                "%Y%m%d"
            )
            calendar = active_provider.calendar(start, day)
            with db.engine.begin() as connection:
                for calendar_day in calendar:
                    db.upsert(connection, trading_days, {"source": source, "day": calendar_day, "is_open": 1})
            if day not in calendar:
                db.finish_run(identifier, "skipped", "非交易日，未生成新报告")
                return {"status": "skipped", "day": day, "message": "非交易日"}
            existing = db.latest_report(source, day)
            if existing and not force:
                db.finish_run(identifier, "skipped", "已有报告，使用 --force 可重新采集当天数据")
                return {"status": "exists", "day": day}
            calendar = calendar[-settings.history_sessions :]
            if len(calendar) < 65:
                raise DataError("交易日历历史不足65日")
            universe = active_provider.universe()
            stored = {
                r["day"]
                for r in db.rows(
                    select(sessions.c.day).where(
                        sessions.c.source == source,
                        sessions.c.day.in_(calendar),
                    )
                )
            }
            # Fetch latest first: fail fast if today's data is not ready, and warm synthetic data once.
            needed = [day] + [d for d in calendar if d not in stored and d != day]
            today_rows, today = None, None
            for completed, target in enumerate(needed, 1):
                batch = active_provider.fetch_day(target)
                quality = validate_batch(batch, universe, settings)
                snapshot = {
                    "source": source,
                    "day": target,
                    "quality": quality,
                    "benchmark": batch.benchmark,
                    "universe": universe,
                    "events": batch.events,
                    "event_status": batch.event_status,
                    "acquired_at": now().isoformat(),
                }
                if target == day:
                    today_rows, today = batch.bars, snapshot
                with db.engine.begin() as connection:
                    if target != day:
                        save_session(db, connection, source, snapshot, batch.bars)
                    connection.execute(
                        update(runs)
                        .where(runs.c.id == identifier)
                        .values(
                            completed_days=completed,
                            message=f"已采集 {completed}/{len(needed)} 个交易日，最近完成 {target}",
                        )
                    )
                logger.info("%s %s 已采集 %s 只股票", source, target, len(batch.bars))
            all_sessions = {
                r["day"]: r
                for r in db.rows(
                    select(sessions).where(
                        sessions.c.source == source,
                        sessions.c.day.in_(calendar),
                    )
                )
            }
            history = defaultdict(list)
            for row in db.rows(
                select(bars)
                .where(bars.c.source == source, bars.c.day.in_(calendar), bars.c.day != day)
                .order_by(bars.c.day)
            ):
                history[row["code"]].append(row)
            all_sessions[day] = today
            for row in today_rows:
                history[row["code"]].append({**row, "source": source})
            report = build_report(
                settings,
                day,
                history,
                all_sessions,
                today["universe"],
                today["quality"],
                today["events"],
                today["event_status"],
            )
            report["generated_at"] = now().isoformat()
            report["acquired_at"] = today["acquired_at"]
            with db.engine.begin() as connection:
                # Publish today's raw data, metadata, report and signals atomically.
                save_session(db, connection, source, today, today_rows)
                db.upsert(connection, reports, {"source": source, "day": day, "payload": report})
                # Keep report and signal snapshot in the same transaction, including forced reruns.
                connection.execute(delete(signals).where(signals.c.source == source, signals.c.day == day))
                for candidate in report["candidates"]:
                    connection.execute(
                        signals.insert().values(
                            source=source,
                            day=day,
                            code=candidate["code"],
                            strategy=candidate["strategy"],
                            payload=candidate,
                        )
                    )
            db.finish_run(identifier, "success", f"报告已生成，{len(report['candidates'])} 个观察信号")
            return {"status": "success", "day": day, "candidates": len(report["candidates"])}
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, (DataError, ValueError, RuntimeError))
            else "任务异常，请检查服务日志"
        )
        db.finish_run(identifier, "failed", message)
        alert(settings, f"{day} 分析失败：{message}")
        raise
    finally:
        if own_provider and active_provider:
            active_provider.close()


def refresh_events(db, settings, day=None, provider=None):
    day = day or now().strftime("%Y%m%d")
    parse_day(day)
    identifier = db.start_run(settings.source, day, "announcements")
    active_provider = provider
    try:
        with db.lock(settings.source):
            saved = db.rows(
                select(sessions).where(sessions.c.source == settings.source, sessions.c.day == day)
            )
            if not saved:
                db.finish_run(identifier, "skipped", "当天无行情快照，未补查公告")
                return {"status": "skipped"}
            active_provider = active_provider or make_provider(settings)
            events, status = active_provider.events(day)
            # Events are displayed dynamically, preserving the original signal evidence at creation time.
            if status == "unavailable":
                raise DataError("公告接口不可用，已保留原有公告数据")
            with db.engine.begin() as connection:
                connection.execute(
                    update(sessions)
                    .where(sessions.c.source == settings.source, sessions.c.day == day)
                    .values(
                        events=events,
                        event_status=status,
                    )
                )
            db.finish_run(identifier, "success", f"公告状态 {status}，共 {len(events)} 条")
            return {"status": "success", "count": len(events)}
    except Exception as error:
        db.finish_run(identifier, "failed", str(error) if isinstance(error, DataError) else "公告补查失败")
        alert(settings, f"{day} 公告补查失败")
        raise
    finally:
        if provider is None and active_provider:
            active_provider.close()
