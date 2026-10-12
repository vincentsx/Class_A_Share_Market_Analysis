"""Resume BaoStock initialization through the individual-stock history endpoint.

Prices and trading states come from the SDK. Previously validated stock metadata
must have been acquired on or after the requested report date. Checkpoints hold
raw responses, not published reports, and all pipeline validations still apply.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from .config import SHANGHAI, now
from .db import runs, sessions
from .free_provider import BaoStockProvider, canonical, compact, iso, number
from .html_export import atomic_write
from .providers import DataError, parse_day
from .sdk_client import SDKClient

logger = logging.getLogger(__name__)
FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,isST"


class HistoryRecoveryClient:
    def __init__(self, settings, db, stocks, days, cache_dir, client=None, force=False, worker_factory=None):
        self.client = client or SDKClient(settings)
        self.worker_factory = worker_factory or (lambda: SDKClient(settings))
        self.workers = 2 if isinstance(self.client, SDKClient) or worker_factory else 1
        self.interval = settings.free_request_interval
        self.rate_lock = threading.Lock()
        self.last_start = 0
        self.local = threading.local()
        self.clients = []
        self.db, self.stocks, self.days = db, stocks, sorted(days)
        self.start, self.end = self.days[0], self.days[-1]
        self.cache_dir = Path(cache_dir) / f"{self.start}-{self.end}"
        self.force = force
        self.rows = None
        self.benchmarks = {}

    def close(self):
        self.client.close()

    def validate_rows(self, rows, vendor):
        seen = set()
        for row in rows:
            day = compact(row["date"])
            if row["code"] != vendor or not self.start <= day <= self.end or day in seen:
                raise DataError("个股历史响应的日期、代码或重复记录异常")
            seen.add(day)
            if str(row["adjustflag"]) != "3" or str(row["tradestatus"]) not in {"0", "1"}:
                raise DataError("个股历史必须为未复权行情与有效交易状态")
            if str(row["isST"]) not in {"0", "1"}:
                raise DataError("个股历史风险警示标记无效")
            if str(row["tradestatus"]) == "1":
                values = {key: number(row[key]) for key in ("open", "high", "low", "close", "volume", "amount")}
                if min(values.values()) <= 0:
                    raise DataError("个股历史价格或成交量额无效")
                if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
                    raise DataError("个股历史OHLC不一致")
                number(row["pctChg"])
                for key in ("turn", "peTTM", "pbMRQ"):
                    number(row.get(key), optional=True)

    def stock_rows(self, code, client=None):
        vendor = f"{code[-2:].lower()}.{code[:6]}"
        if canonical(vendor) != code:
            raise DataError("缓存基础信息的股票代码无效")
        path = self.cache_dir / f"{code}.json"
        if path.exists() and not self.force:
            try:
                payload = json.loads(path.read_text())
                if (payload["code"], payload["start"], payload["end"]) != (code, self.start, self.end):
                    raise ValueError
                rows = payload["rows"]
                self.validate_rows(rows, vendor)
                return rows
            except (ValueError, KeyError, TypeError, DataError):
                logger.warning("%s 的历史断点缓存无效，将重新采集", code)
        # Keep the configured interval across both connections, not per thread.
        with self.rate_lock:
            delay = self.interval - (time.monotonic() - self.last_start)
            if delay > 0:
                time.sleep(delay)
            self.last_start = time.monotonic()
        rows = (client or self.client).query("query_history_k_data_plus", code=vendor, fields=FIELDS,
                                           start_date=iso(self.start), end_date=iso(self.end),
                                           frequency="d", adjustflag="3")
        self.validate_rows(rows, vendor)
        atomic_write(path, json.dumps({"code": code, "start": self.start, "end": self.end,
                                      "acquired_at": now().isoformat(), "rows": rows}, ensure_ascii=False))
        return rows

    def worker_rows(self, code):
        if not hasattr(self.local, "client"):
            with self.rate_lock:
                self.local.client = self.client if not self.clients else self.worker_factory()
                self.clients.append(self.local.client)
        return self.stock_rows(code, self.local.client)

    def stock_results(self, stocks):
        if self.workers == 1:
            for code in stocks:
                yield self.stock_rows(code)
            return
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = [pool.submit(self.worker_rows, code) for code in stocks]
            for future in as_completed(futures):
                yield future.result()
        finally:
            # A failed stock must not leave thousands of queued acquisitions running.
            pool.shutdown(wait=True, cancel_futures=True)
            for client in self.clients[1:]:
                client.close()

    def load(self):
        if self.rows is not None:
            return
        rows = {day: [] for day in self.days}
        stocks = [code for code, stock in self.stocks.items()
                  if stock["list_date"] and stock["list_date"] <= self.end
                  and (not stock["delist_date"] or stock["delist_date"] > self.start)]
        task = self.db.rows(select(runs.c.id).where(runs.c.source == "baostock", runs.c.day == self.end,
                                                   runs.c.status == "running", runs.c.kind == "daily")
                            .order_by(runs.c.started_at.desc()).limit(1))
        for completed, stock_rows in enumerate(self.stock_results(sorted(stocks)), 1):
            for row in stock_rows:
                day = compact(row["date"])
                if day in rows:
                    rows[day].append(row)
            if completed % 100 == 0 or completed == len(stocks):
                message = f"个股历史恢复：已完成 {completed}/{len(stocks)} 只股票，尚未发布报告"
                logger.info(message)
                if task:
                    with self.db.engine.begin() as connection:
                        connection.execute(update(runs).where(runs.c.id == task[0]["id"]).values(message=message))
        benchmark = self.client.query("query_history_k_data_plus", code="sh.000300", fields="date,code,close,pctChg",
                                      start_date=iso(self.start), end_date=iso(self.end), frequency="d", adjustflag="3")
        for row in benchmark:
            day = compact(row["date"])
            if row["code"] != "sh.000300" or not self.start <= day <= self.end or day in self.benchmarks:
                raise DataError("恢复模式的沪深300基准日期或代码异常")
            self.benchmarks[day] = row
        if set(rows) - set(self.benchmarks):
            raise DataError("恢复模式的沪深300基准缺失")
        self.rows = rows

    def query(self, method, **params):
        if method == "query_daily_history_k_AStock":
            self.load()
            return self.rows[compact(params["date"])]
        if method == "query_all_stock":
            # In recovery mode, suspensions come from the official per-stock
            # tradestatus field; this is not an independent securities-list query.
            return [{"code": row["code"], "tradeStatus": row["tradestatus"]}
                    for row in self.rows[compact(params["day"])]]
        if method == "query_history_k_data_plus" and params.get("code") == "sh.000300":
            return [self.benchmarks[compact(params["start_date"])]]
        return self.client.query(method, **params)


def make_recovery_provider(db, settings, day, force=False, cache_dir="data/history_cache"):
    if settings.source != "baostock":
        raise DataError("--stock-history 仅支持 BaoStock 数据源")
    day = day or now().strftime("%Y%m%d")
    parse_day(day)
    saved = db.rows(select(sessions.c.universe, sessions.c.acquired_at)
                    .where(sessions.c.source == "baostock").order_by(sessions.c.acquired_at.desc()).limit(1))
    if not saved:
        raise DataError("恢复模式需要此前成功采集的基础信息；请先运行常规 run")
    acquired_day = datetime.fromisoformat(saved[0]["acquired_at"]).astimezone(SHANGHAI).strftime("%Y%m%d")
    if acquired_day < day:
        raise DataError("已缓存基础信息早于报告日期；不能用旧股票范围生成新的全市场报告")
    sdk = SDKClient(settings)
    try:
        start = (parse_day(day) - timedelta(days=max(220, settings.history_sessions * 2))).strftime("%Y%m%d")
        provider = BaoStockProvider(settings, db, sdk)
        calendar = provider.calendar(start, day)[-settings.history_sessions:]
        if day not in calendar:
            return provider  # The pipeline records the closed day normally.
        stored = {r["day"] for r in db.rows(select(sessions.c.day).where(sessions.c.source == "baostock"))}
        needed = sorted({day} | (set(calendar) - stored))
        provider.stocks = saved[0]["universe"]
        provider.client = HistoryRecoveryClient(settings, db, provider.stocks, needed, cache_dir, sdk, force)
        provider.data_notes = [f"个股历史恢复模式：基础信息采集于 {iso(acquired_day)}；停牌按个股日线交易状态核对。"]
        return provider
    except BaseException:
        sdk.close()
        raise
