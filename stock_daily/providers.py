"""Providers return canonical prices (yuan), volume (shares) and amount (yuan)."""

import math
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from .config import Settings


class DataError(RuntimeError):
    pass


def parse_day(day: str) -> date:
    try:
        result = datetime.strptime(day, "%Y%m%d").date()
        if result.strftime("%Y%m%d") != day:
            raise ValueError
        return result
    except ValueError:
        raise DataError("日期应为8位 YYYYMMDD") from None


@dataclass
class Batch:
    day: str
    bars: list[dict]
    benchmark: dict
    suspended: set[str]
    events: list[dict]
    event_status: str
    universe: dict | None = None


class TushareProvider:
    def __init__(self, settings: Settings, client=None):
        if not settings.token:
            raise DataError("请在 .env 中配置 TUSHARE_TOKEN；示例模式请使用 DATA_SOURCE=demo")
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)
        self.last_request = 0.0

    def close(self):
        self.client.close()

    def query(self, api, fields, **params):
        """Retry transport/server/rate-limit errors; never retry permission failures."""
        for attempt in range(3):
            delay = self.settings.request_interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                response = self.client.post(
                    self.settings.tushare_url,
                    json={
                        "api_name": api,
                        "token": self.settings.token,
                        "params": params,
                        "fields": fields,
                    },
                )
                response.raise_for_status()
                result = response.json()
                if result.get("code") != 0:
                    # Do not include upstream messages: they may contain credentials.
                    if result.get("code") == 2002:
                        raise DataError(f"{api} 接口鉴权或权限不足，请检查 Token 和积分权限")
                    raise httpx.TransportError(f"{api} 返回异常状态")
                data = result.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
                    raise DataError(f"{api} 返回格式不正确")
                if any(len(item) != len(data["fields"]) for item in data.get("items", [])):
                    raise DataError(f"{api} 返回字段数量不正确")
                return [dict(zip(data["fields"], item)) for item in data.get("items", [])]
            except DataError:
                raise
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise DataError(f"{api} 请求失败，已重试3次；检查网络和接口限制") from None
                time.sleep(2**attempt)

    def paged(self, api, fields, limit=2000, **params):
        rows = []
        previous = None
        for offset in range(0, 100_000, limit):
            page = self.query(api, fields, limit=limit, offset=offset, **params)
            if page and page == previous:
                raise DataError(f"{api} 未正确分页，拒绝使用截断数据")
            rows.extend(page)
            if len(page) < limit:
                return rows
            previous = page
        raise DataError(f"{api} 超过分页保护上限")

    def calendar(self, start, end):
        rows = self.query("trade_cal", "cal_date,is_open", exchange="SSE", start_date=start, end_date=end)
        expected = (parse_day(end) - parse_day(start)).days + 1
        if len({r["cal_date"] for r in rows}) != expected:
            raise DataError("交易日历缺失，不能确定采集范围")
        return sorted({r["cal_date"] for r in rows if int(r["is_open"]) == 1})

    def universe(self):
        rows = []
        for status in ("L", "D", "P"):
            rows.extend(
                self.paged(
                    "stock_basic",
                    "ts_code,name,industry,market,list_date,delist_date,list_status",
                    limit=5000,
                    list_status=status,
                )
            )
        result = {
            r["ts_code"]: {
                "code": r["ts_code"],
                "name": r["name"],
                "industry": r.get("industry") or "未分类",
                "market": r.get("market") or "未知",
                "list_date": r.get("list_date") or "",
                "delist_date": r.get("delist_date") or "",
                "list_status": r.get("list_status"),
            }
            for r in rows
        }
        if not result:
            raise DataError("股票基础信息为空")
        return result

    def events(self, day):
        if not self.settings.announcements:
            return [], "disabled"
        try:
            rows = self.paged("anns_d", "ts_code,ann_date,title,url,rec_time", ann_date=day)
            return [
                {
                    "code": r["ts_code"],
                    "day": r["ann_date"],
                    "title": r["title"],
                    "url": r.get("url") or "",
                    "published_at": str(r.get("rec_time") or ""),
                }
                for r in rows
            ], "available"
        except DataError:
            return [], "unavailable"

    def fetch_day(self, day):
        raw = self.paged("daily", "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg", trade_date=day)
        factors = self.paged("adj_factor", "ts_code,trade_date,adj_factor", trade_date=day)
        basics = self.paged(
            "daily_basic",
            "ts_code,trade_date,turnover_rate,pe_ttm,pb",
            trade_date=day,
        )
        for dataset in (raw, factors, basics):
            if any(r.get("trade_date") != day for r in dataset):
                raise DataError("接口返回了其他日期的数据")
            if len({r["ts_code"] for r in dataset}) != len(dataset):
                raise DataError("接口返回重复股票记录")
        factors = {r["ts_code"]: r["adj_factor"] for r in factors}
        basics = {r["ts_code"]: r for r in basics}
        rows = []
        for r in raw:
            code = r["ts_code"]
            if code not in factors or code not in basics:
                raise DataError(f"{code} 缺少复权因子或每日指标")
            rows.append(
                {
                    "day": day,
                    "code": code,
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": float(r["vol"]) * 100,
                    "amount": float(r["amount"]) * 1000,
                    "adj": factors[code],
                    "pct": r["pct_chg"],
                    "turnover": basics[code].get("turnover_rate"),
                    "pe": basics[code].get("pe_ttm"),
                    "pb": basics[code].get("pb"),
                }
            )
        suspended = self.paged(
            "suspend_d", "ts_code,trade_date,suspend_timing,suspend_type", trade_date=day, suspend_type="S"
        )
        benchmark = self.query(
            "index_daily",
            "ts_code,trade_date,open,high,low,close,pct_chg",
            ts_code="000300.SH",
            trade_date=day,
        )
        if len(benchmark) != 1 or benchmark[0]["trade_date"] != day:
            raise DataError("缺少当日沪深300基准行情")
        events, status = self.events(day)
        return Batch(
            day,
            rows,
            benchmark[0],
            {r["ts_code"] for r in suspended if not r.get("suspend_timing")},
            events,
            status,
        )


class DemoProvider:
    """Deterministic synthetic data, including a split and a suspended symbol.

    Weekdays are a synthetic calendar, NOT the Chinese exchange calendar.
    """

    def __init__(self, settings):
        self.settings = settings
        self.anchor = date(2024, 1, 1)
        self.cache = {}

    def close(self):
        pass

    def calendar(self, start, end):
        current, finish = parse_day(start), parse_day(end)
        days = []
        while current <= finish:
            if current.weekday() < 5:
                days.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return days

    def universe(self):
        industries = ["半导体", "电力设备", "医药", "银行", "机械设备", "消费"]
        return {
            f"{600000 + i:06d}.SH": {
                "code": f"{600000 + i:06d}.SH",
                "name": f"示例{industries[i % 6]}{i + 1:02d}",
                "industry": industries[i % 6],
                "market": "主板",
                "list_date": "20200101",
                "delist_date": "",
                "list_status": "L",
            }
            for i in range(48)
        }

    def _series(self, end):
        if end in self.cache:
            return self.cache[end]
        finish = parse_day(end)
        if finish < self.anchor:
            raise DataError("示例数据支持2024年起的日期")
        dates = self.calendar(self.anchor.strftime("%Y%m%d"), end)
        rng = random.Random(20261010)
        prices = [10 + i * 0.65 for i in range(48)]
        index = 3500.0
        result = {}
        for n, day in enumerate(dates):
            market = 0.0002 + rng.gauss(0, 0.004)
            index *= 1 + market
            rows = []
            for i in range(48):
                # Sector drift, cyclical pullbacks, independently varying liquidity.
                drift = [0.0018, 0.001, 0.0001, 0.0003, 0.0008, -0.0003][i % 6]
                change = market + drift + 0.006 * math.sin(n / 7 + i) + rng.gauss(0, 0.007)
                previous = prices[i]
                price = previous * (1 + change)
                prices[i] = price
                adj = 2.0 if i == 5 and n >= 400 else 1.0
                close = price / adj
                opening = previous * (1 + rng.gauss(0, 0.002)) / adj
                volume = (4_000_000 + i * 150_000) * rng.uniform(0.65, 1.8) * adj
                if i == 47 and n % 23 == 0:
                    continue
                rows.append(
                    {
                        "day": day,
                        "code": f"{600000 + i:06d}.SH",
                        "open": round(opening, 4),
                        "high": round(max(opening, close) * 1.008, 4),
                        "low": round(min(opening, close) * 0.992, 4),
                        "close": round(close, 4),
                        "volume": round(volume),
                        "amount": round(volume * close),
                        "adj": adj,
                        "pct": round(change * 100, 4),
                        "turnover": round(volume / 8_000_000, 2),
                        "pe": 12 + i % 20,
                        "pb": round(1.1 + i % 10 * 0.3, 2),
                    }
                )
            result[day] = Batch(
                day,
                rows,
                {
                    "ts_code": "000300.SH",
                    "trade_date": day,
                    "close": round(index, 4),
                    "pct_chg": round(market * 100, 4),
                },
                {"600047.SH"} if n % 23 == 0 else set(),
                [
                    {
                        "code": "600000.SH",
                        "day": day,
                        "title": "示例：股东减持计划公告",
                        "url": "",
                        "published_at": f"{day} 16:00",
                    }
                ],
                "demo",
            )
        self.cache = result
        return result

    def fetch_day(self, day):
        self._series(day)
        if day not in self.cache:
            raise DataError("示例日历中当天不是交易日")
        return self.cache[day]

    def events(self, day):
        return self.fetch_day(day).events, "demo"


def make_provider(settings, db=None):
    if settings.source == "demo":
        return DemoProvider(settings)
    if settings.source == "baostock":
        from .free_provider import BaoStockProvider

        return BaoStockProvider(settings, db=db)
    return TushareProvider(settings)
