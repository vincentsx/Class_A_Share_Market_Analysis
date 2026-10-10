"""Free Shanghai/Shenzhen A-share daily data and optional Eastmoney notices."""

import logging
import math
import re
from bisect import bisect_right
from datetime import timedelta

from sqlalchemy import select

from .db import provider_cache
from .providers import Batch, DataError, parse_day
from .sdk_client import SDKClient

logger = logging.getLogger(__name__)


def iso(day):
    return parse_day(day).isoformat()


def compact(value):
    text = str(value or "")
    return parse_day(text.replace("-", "")).strftime("%Y%m%d") if text else ""


def canonical(code):
    if not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\.\d{6}", code):
        raise DataError("BaoStock 股票代码格式异常")
    exchange, number = code.split(".")
    return f"{number}.{exchange.upper()}"


def a_share(code):
    # Exclude B shares, indices, funds and bonds even if their vendor type is 'stock'.
    return bool(re.fullmatch(r"(?:sh\.6\d{5}|sz\.(?:00|30)\d{4})", str(code)))


def number(value, optional=False):
    if optional and value in (None, "", "None", "nan", "NaN"):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise DataError("BaoStock 数值字段无效") from None
    if not math.isfinite(result):
        raise DataError("BaoStock 数值字段非有限数")
    return result


def factor_records(rows, code, end):
    records = []
    for row in rows:
        day = compact(row["dividOperateDate"])
        factor = number(row["backAdjustFactor"])
        if row["code"] != code or not day or day > end or factor <= 0:
            raise DataError("BaoStock 复权因子日期、代码或数值异常")
        records.append({"day": day, "factor": factor})
    records.sort(key=lambda item: item["day"])
    if len({item["day"] for item in records}) != len(records):
        raise DataError("BaoStock 复权记录重复")
    return records


class BaoStockProvider:
    def __init__(self, settings, db=None, client=None):
        self.settings = settings
        self.db = db
        self.client = client or SDKClient(settings)
        self.stocks = None
        self.target = None
        self.factor_cache = {}
        self.factors_ready = False

    def close(self):
        self.client.close()

    def calendar(self, start, end):
        rows = self.client.query("query_trade_dates", start_date=iso(start), end_date=iso(end))
        days = [compact(row["calendar_date"]) for row in rows]
        expected = (parse_day(end) - parse_day(start)).days + 1
        if len(days) != expected or len(set(days)) != expected or any(d < start or d > end for d in days):
            raise DataError("BaoStock 交易日历缺失或重复")
        if any(str(row["is_trading_day"]) not in {"0", "1"} for row in rows):
            raise DataError("BaoStock 交易日状态无效")
        self.target = end
        return sorted(compact(r["calendar_date"]) for r in rows if str(r["is_trading_day"]) == "1")

    def universe(self):
        if self.stocks is not None:
            return self.stocks
        basics = self.client.query("query_stock_basic")
        industries = self.client.query("query_stock_industry")
        sector_map = {r["code"]: r["industry"] for r in industries}
        result = {}
        for row in basics:
            vendor_code = row["code"]
            if str(row["type"]) != "1" or not a_share(vendor_code):
                continue
            code = canonical(vendor_code)
            if code in result:
                raise DataError("BaoStock 基础信息代码重复")
            result[code] = {
                "code": code,
                "name": row["code_name"],
                "industry": sector_map.get(vendor_code) or "未分类",
                "market": "科创板" if code.startswith("688") else "创业板" if code.startswith(("300", "301")) else "主板",
                "list_date": compact(row["ipoDate"]),
                "delist_date": compact(row["outDate"]),
                "list_status": "L" if str(row["status"]) == "1" else "D",
            }
        if not result or not any(r["industry"] != "未分类" for r in result.values()):
            raise DataError("BaoStock 股票基础信息或行业分类为空")
        self.stocks = result
        return result

    def save_factors(self, values):
        if not self.db or not values:
            return
        with self.db.engine.begin() as connection:
            for code, payload in values.items():
                self.db.upsert(connection, provider_cache, {"source": "baostock", "key": code, "payload": payload})

    def prepare_factors(self):
        if self.factors_ready:
            return
        if self.db:
            self.factor_cache = {
                row["key"]: row["payload"]
                for row in self.db.rows(select(provider_cache).where(provider_cache.c.source == "baostock"))
            }
        # Bulk daily factors contain only corporate actions, not every stock's daily factor.
        # Update existing complete histories through each missing date, including non-trading days.
        stale = {code: payload for code, payload in self.factor_cache.items() if payload["through"] < self.target}
        if stale:
            first = min(payload["through"] for payload in stale.values())
            cursor = parse_day(first) + timedelta(days=1)
            while cursor <= parse_day(self.target):
                day = cursor.strftime("%Y%m%d")
                rows = self.client.query("query_daily_adjust_factor", date=cursor.isoformat())
                seen = set()
                for row in rows:
                    code = canonical(row["code"])
                    if code in seen or compact(row["dividOperateDate"]) != day:
                        raise DataError("BaoStock 批量复权因子日期异常或记录重复")
                    seen.add(code)
                    if code in stale and stale[code]["through"] < day:
                        records = factor_records([row], row["code"], self.target)
                        stale[code]["records"].extend(records)
                cursor += timedelta(days=1)
            for payload in stale.values():
                payload["through"] = self.target
            self.save_factors(stale)
        self.factors_ready = True

    def factor(self, code, day):
        self.prepare_factors()
        if code not in self.factor_cache:
            number_code, exchange = code.split(".")
            vendor = f"{exchange.lower()}.{number_code}"
            # Starting at IPO preserves the factor effective before the analysis window.
            start = self.stocks[code]["list_date"] or "19901219"
            rows = self.client.query("query_adjust_factor", code=vendor, start_date=iso(start), end_date=iso(self.target))
            payload = {"through": self.target, "records": factor_records(rows, vendor, self.target)}
            self.factor_cache[code] = payload
            self.save_factors({code: payload})
            if len(self.factor_cache) % 100 == 0:
                logger.info("BaoStock 已初始化 %s 只股票的复权历史", len(self.factor_cache))
        records = self.factor_cache[code]["records"]
        index = bisect_right([item["day"] for item in records], day) - 1
        # A successful, complete empty history means no corporate action since IPO.
        return records[index]["factor"] if index >= 0 else 1.0

    def events(self, day):
        if not self.settings.announcements:
            return [], "disabled"
        try:
            rows = self.client.query("stock_notice_report", symbol="全部", date=day)
            events, seen = [], set()
            for row in rows:
                raw = str(row["代码"]).zfill(6)
                vendor = f"{'sh' if raw.startswith('6') else 'sz'}.{raw}"
                if not a_share(vendor):
                    continue
                code = canonical(vendor)
                if self.stocks is not None and code not in self.stocks:
                    continue
                if compact(row["公告日期"]) != day:
                    raise DataError("AKShare 公告日期与请求不符")
                title = str(row["公告标题"] or "").strip()
                url = str(row["网址"] or "")
                if not title or not url.startswith("https://"):
                    raise DataError("AKShare 公告标题或链接异常")
                key = (code, title, url)
                if key in seen:
                    continue
                seen.add(key)
                events.append({"code": code, "day": day, "title": title, "url": url, "published_at": ""})
            return events, "available"
        except (DataError, KeyError, ValueError, TypeError):
            return [], "unavailable"

    def fetch_day(self, day):
        stocks = self.universe()
        self.target = self.target or day
        raw = self.client.query("query_daily_history_k_AStock", date=iso(day))
        if not raw:
            raise DataError(f"BaoStock {day} 行情尚未发布或接口为空")
        rows, suspended, seen = [], set(), set()
        snapshot = {code: dict(info) for code, info in stocks.items()}
        for r in raw:
            if compact(r["date"]) != day:
                raise DataError("BaoStock 行情日期与请求不符")
            if not a_share(r["code"]):
                continue
            code = canonical(r["code"])
            if code not in stocks or code in seen:
                raise DataError("BaoStock 行情股票未知或重复")
            seen.add(code)
            status = str(r["tradestatus"])
            if status == "0":
                suspended.add(code)
                continue
            if status != "1" or str(r["adjustflag"]) != "3" or str(r["isST"]) not in {"0", "1"}:
                raise DataError("BaoStock 必须返回未复权行情与有效交易/ST状态")
            snapshot[code]["is_st"] = str(r["isST"]) == "1"
            rows.append({
                "day": day, "code": code,
                **{k: number(r[k]) for k in ("open", "high", "low", "close")},
                "volume": number(r["volume"]), "amount": number(r["amount"]),
                "adj": self.factor(code, day), "pct": number(r["pctChg"]),
                "turnover": number(r.get("turn"), optional=True),
                "pe": number(r.get("peTTM"), optional=True), "pb": number(r.get("pbMRQ"), optional=True),
            })
        # Verify suspensions against a separate day-specific securities list.
        listed = self.client.query("query_all_stock", day=iso(day))
        for row in listed:
            if a_share(row["code"]) and str(row["tradeStatus"]) == "0":
                suspended.add(canonical(row["code"]))
        benchmark = self.client.query(
            "query_history_k_data_plus", code="sh.000300", fields="date,code,close,pctChg",
            start_date=iso(day), end_date=iso(day), frequency="d", adjustflag="3",
        )
        if len(benchmark) != 1 or benchmark[0]["code"] != "sh.000300" or compact(benchmark[0]["date"]) != day:
            raise DataError("BaoStock 沪深300基准缺失或日期不符")
        events, status = self.events(day) if day == self.target else ([], "not_requested")
        return Batch(day, rows, {
            "trade_date": day, "close": number(benchmark[0]["close"]), "pct_chg": number(benchmark[0]["pctChg"]),
        }, suspended, events, status, snapshot)
