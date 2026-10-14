"""Free intraday observations kept separate from validated daily bars and signals."""

import logging
import math
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from .config import SHANGHAI, now
from .db import reports, sessions, snapshots, trading_days, watchlist
from .html_export import ASSETS, atomic_write, publish_latest_png, script_json
from .providers import DataError, make_provider, parse_day
from .snapshot_analysis import analyze_snapshot
from .static_report import snapshot_view, template_helpers

logger = logging.getLogger(__name__)
LABELS = {"midday": "午间观察", "close": "收盘快照"}


def parse_quotes(text, expected, day, slot, captured):
    """Sina quote volume is shares, amount is yuan; never evaluate the JS response."""
    received, missing = {}, {}
    seen = set()
    for vendor, content in re.findall(r'var\s+hq_str_((?:sh|sz)\d{6})="([^"\r\n]*)";', text):
        code = vendor[2:] + "." + vendor[:2].upper()
        if code not in expected:
            continue
        if code in seen:
            raise DataError("实时行情返回重复代码")
        seen.add(code)
        values = content.split(",")
        try:
            if len(values) < 33:
                raise ValueError("响应为空或字段不完整")
            timestamp = datetime.fromisoformat(values[30] + "T" + values[31]).replace(tzinfo=SHANGHAI)
            if timestamp.strftime("%Y%m%d") != day:
                raise ValueError("日期不符或旧行情")
            if timestamp > captured + timedelta(seconds=5):
                raise ValueError("行情时间在未来")
            # Stock quotes can publish a few minutes after the last trade. Preserve
            # each vendor timestamp rather than pretending every symbol traded at 11:30.
            minimum = "11:25" if slot == "midday" else "14:55"
            if timestamp.strftime("%H:%M") < minimum or (slot == "midday" and timestamp.hour >= 13):
                raise ValueError("行情时间不属于所选时段")
            numeric = {key: float(values[index]) for key, index in
                       (("open", 1), ("previous", 2), ("price", 3), ("high", 4), ("low", 5),
                        ("volume", 8), ("amount", 9))}
            if not all(math.isfinite(v) and v >= 0 for v in numeric.values()):
                raise ValueError("价格或成交数据无效")
            if numeric["price"] == 0 or numeric["open"] == 0 or numeric["volume"] == 0:
                raise ValueError("无成交或停牌待核对")
            if min(numeric[k] for k in ("previous", "low", "high", "amount")) <= 0:
                raise ValueError("价格或成交数据无效")
            if numeric["low"] > min(numeric["open"], numeric["price"]) or numeric["high"] < max(numeric["open"], numeric["price"]):
                raise ValueError("最高最低价不一致")
            received[code] = {"code": code, "name": values[0], **numeric,
                              "pct": (numeric["price"] / numeric["previous"] - 1) * 100,
                              "quote_time": timestamp.isoformat()}
        except (ValueError, IndexError) as error:
            missing[code] = str(error)
    for code in expected - seen:
        missing[code] = "接口未返回该股票"
    return received, missing


class SinaQuotes:
    def __init__(self, settings):
        self.settings = settings

    def fetch(self, codes, day, slot):
        all_rows, missing = {}, {}
        requested = sorted(set(codes) | {"000300.SH"})
        with httpx.Client(timeout=min(self.settings.free_timeout, 30),
                          headers={"Referer": "https://finance.sina.com.cn/"}) as client:
            for start in range(0, len(requested), 180):
                batch = requested[start:start + 180]
                symbols = ",".join(c[-2:].lower() + c[:6] for c in batch)
                for attempt in range(3):
                    try:
                        response = client.get("https://hq.sinajs.cn/list=" + symbols)
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise DataError("新浪实时接口请求失败，已保留上一次快照") from None
                        time.sleep(max(1, self.settings.free_request_interval))
                rows, absent = parse_quotes(response.content.decode("gbk"), set(batch), day, slot, now())
                all_rows.update(rows)
                missing.update(absent)
                if start // 180 % 5 == 0 or start + 180 >= len(requested):
                    logger.info("实时快照已请求 %s/%s 个代码", min(start + 180, len(requested)), len(requested))
                if start + 180 < len(requested):
                    time.sleep(max(0.3, self.settings.free_request_interval))
        return all_rows, missing


def build_snapshot(db, settings, day, slot, quotes, missing, stocks, metadata_at):
    benchmark = quotes.get("000300.SH")
    if not benchmark:
        raise DataError("沪深300实时行情日期或时间无法核验")
    rows = [{**quote, "industry": stocks[code].get("industry", "未分类")}
            for code, quote in quotes.items() if code in stocks]
    coverage = len(rows) / len(stocks) if stocks else 0
    if coverage < settings.min_coverage:
        raise DataError(f"实时有效报价覆盖缓存股票范围 {coverage:.1%}，不足 {settings.min_coverage:.1%}；未发布快照")
    if slot == "midday" and now().strftime("%Y%m%d") == day and now().hour >= 13:
        raise DataError("已进入下午交易时段，不能补采午间快照")
    # Reference candidates must predate the snapshot (no look-ahead when backfilling).
    saved_reports = db.rows(select(reports.c.payload).where(
        reports.c.source == settings.source, reports.c.day < day)
        .order_by(reports.c.day.desc()).limit(1))
    report = saved_reports[0]["payload"] if saved_reports else None
    watched = {r["code"] for r in db.rows(select(watchlist))}
    reference = {r["code"] for r in report.get("all_candidates", report["candidates"])} if report else set()
    timestamps = [r["quote_time"] for r in rows]
    payload = {"day": day, "slot": slot, "label": LABELS[slot], "source": settings.source,
            "scheduled_time": settings.midday_time if slot == "midday" else settings.schedule_time,
            "quote_provider": "新浪免费实时行情", "captured_at": now().isoformat(),
            "quote_start": min(timestamps), "quote_end": max(timestamps), "metadata_at": metadata_at,
            "expected": len(stocks), "coverage": coverage * 100, "missing": missing,
            "market": {"count": len(rows), "up": sum(r["pct"] > 0 for r in rows),
                       "down": sum(r["pct"] < 0 for r in rows), "flat": sum(r["pct"] == 0 for r in rows),
                       "amount": sum(r["amount"] for r in rows), "benchmark": benchmark},
            "reference_day": report["day"] if report else None,
            "quotes": sorted(rows, key=lambda r: r["pct"], reverse=True)}
    analyze_snapshot(db, settings, payload, stocks)
    payload["focus"] = [{**r, "reason": "自选股" if r["code"] in watched else "上一份日线观察信号"}
                        for r in payload["quotes"] if r["code"] in watched | reference]
    from .news import build_news

    payload["news"] = build_news(db, settings, payload)
    return payload


def render_snapshot(payload, settings):
    cutoff = payload.get("scheduled_time") or (settings.midday_time if payload["slot"] == "midday" else settings.schedule_time)
    env = Environment(loader=FileSystemLoader(ASSETS / "templates"), autoescape=select_autoescape(["html"]))
    template_helpers(env)
    return env.get_template("snapshot.html").render(
        data=payload, snapshot=script_json(payload), nonce=secrets.token_urlsafe(24), cutoff=cutoff,
        public_report=payload.get("public", False),
        static_report=snapshot_view(payload),
        css=(ASSETS / "static/snapshot.css").read_text() + (ASSETS / "static/mail_report.css").read_text(),
        javascript=(ASSETS / "static/snapshot.js").read_text(),
    )


def export_snapshot(payload, settings):
    folder = Path(settings.html_report_dir)
    cutoff = payload.get("scheduled_time") or (settings.midday_time if payload["slot"] == "midday" else settings.schedule_time)
    path = folder / f"{settings.source}-{payload['day']}-{cutoff.replace(':', '')}.html"
    content = render_snapshot(payload, settings)
    atomic_write(path.resolve(), content)
    result = {"html": str(path.resolve())}
    if settings.export_png:
        from .png_export import export_png

        result["png"] = export_png(path)["path"]
        publish_latest_png(result["png"], folder / f"{settings.source}-latest.png")
    atomic_write((folder / f"{settings.source}-latest.html").resolve(), content)
    return result


def export_saved_snapshot(db, settings, day=None, slot=None):
    query = select(snapshots.c.payload).where(snapshots.c.source == settings.source)
    if day:
        parse_day(day)
        query = query.where(snapshots.c.day == day)
    if slot:
        query = query.where(snapshots.c.slot == slot)
    saved = db.rows(query.order_by(snapshots.c.day.desc(), snapshots.c.slot.asc()).limit(1))
    if not saved:
        raise DataError("没有符合日期和时段的已保存实时快照")
    payload = saved[0]["payload"]
    if not payload.get("analysis"):
        # Upgrade an old saved quote snapshot using only earlier price windows.
        # Preserve the original quotes, capture time and observation list.
        saved_metadata = db.rows(select(sessions.c.universe).where(
            sessions.c.source == settings.source, sessions.c.day <= payload["day"])
            .order_by(sessions.c.day.desc()).limit(1))
        stocks = saved_metadata[0]["universe"] if saved_metadata else {}
        reasons = {r["code"]: r["reason"] for r in payload["focus"]}
        analyze_snapshot(db, settings, payload, stocks)
        payload["focus"] = [{**r, "reason": reasons[r["code"]]} for r in payload["quotes"]
                            if r["code"] in reasons]
    if not payload.get("news"):
        from .news import build_news

        # Ordinary re-export never fetches or adds knowledge observed after capture.
        payload["news"] = build_news(db, settings, payload)
    return {"status": "success", **export_snapshot(payload, settings)}


def run_snapshot(db, settings, day=None, slot="midday", feed=None, provider=None, news_feed=None):
    day = day or now().strftime("%Y%m%d")
    parse_day(day)
    if slot not in LABELS:
        raise DataError("快照时段必须是 midday 或 close")
    identifier = db.start_run(settings.source, day, slot)
    active_provider = provider
    try:
        with db.lock(settings.source):
            if settings.source == "demo":
                db.finish_run(identifier, "skipped", "合成示例不混入真实实时行情；20:30生成示例日线报告")
                return {"status": "skipped", "day": day}
            active_provider = active_provider or make_provider(settings, db)
            calendar_start = (parse_day(day) - timedelta(days=60)).strftime("%Y%m%d")
            calendar = active_provider.calendar(calendar_start, day)
            if day not in calendar:
                db.finish_run(identifier, "skipped", "非交易日，不生成实时快照")
                return {"status": "skipped", "day": day}
            if day != now().strftime("%Y%m%d"):
                raise DataError("实时快照只能采集北京时间当天；历史时段不能用当前行情补造")
            if slot == "midday" and now().hour >= 13:
                raise DataError("已进入下午交易时段，不能补采午间快照；已有快照可用 export-snapshot 重导出")
            # Refresh the actual exchange sessions so a missing evening run
            # cannot silently make an older bar or snapshot become "yesterday".
            with db.engine.begin() as connection:
                for calendar_day in calendar:
                    db.upsert(connection, trading_days, {"source": settings.source,
                                                        "day": calendar_day, "is_open": 1})
            saved = db.rows(select(sessions.c.universe, sessions.c.acquired_at)
                            .where(sessions.c.source == settings.source)
                            .order_by(sessions.c.acquired_at.desc()).limit(1))
            if not saved:
                raise DataError("请先执行 run 初始化股票基础信息和历史数据")
            metadata_at = saved[0]["acquired_at"]
            if (now() - datetime.fromisoformat(metadata_at)).days > 7:
                raise DataError("缓存股票基础信息超过7天，请先更新日线数据")
            stocks = {code: stock for code, stock in saved[0]["universe"].items()
                      if stock["list_date"] and stock["list_date"] <= day
                      and (not stock["delist_date"] or stock["delist_date"] > day)}
            if settings.news_enabled:
                from .news import collect_news

                try:
                    collect_news(db, settings, feed=news_feed)
                except Exception:
                    logger.warning("新闻采集未完成；继续生成行情快照", exc_info=True)
            quotes, missing = (feed or SinaQuotes(settings)).fetch(stocks, day, slot)
            payload = build_snapshot(db, settings, day, slot, quotes, missing, stocks, metadata_at)
            # Separate storage: provisional intraday prices never become daily bars,
            # published close signals or 5/10/20-session outcome observations.
            with db.engine.begin() as connection:
                db.upsert(connection, snapshots, {"source": settings.source, "day": day,
                                                  "slot": slot, "payload": payload})
            artifacts = {}
            if settings.export_html or settings.export_png:
                export_id = db.start_run(settings.source, day, "export")
                try:
                    artifacts = export_snapshot(payload, settings)
                    db.finish_run(export_id, "success", f"{LABELS[slot]}HTML/PNG已按配置导出")
                except (Exception, KeyboardInterrupt):
                    db.finish_run(export_id, "failed", "实时HTML/PNG导出失败，已保存报价保留")
                    raise
            from .mailer import enqueue_safely

            email = enqueue_safely(db, settings, day, slot, artifacts.get('html'), artifacts.get('png'), payload)
            db.finish_run(identifier, "success", f"{LABELS[slot]}已生成：{len(payload['quotes'])}只有效报价；HTML/PNG按配置导出")
            return {"status": "success", "day": day, "slot": slot, "count": len(payload["quotes"]), "email": email, **artifacts}
    except (Exception, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, (DataError, ValueError)) else "实时快照采集或导出失败，请查看日志"
        db.finish_run(identifier, "failed", message)
        logger.exception("实时快照任务未完成")
        raise
    finally:
        if provider is None and active_provider:
            active_provider.close()
