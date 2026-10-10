from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from stock_daily.config import Settings
from stock_daily.free_provider import BaoStockProvider
from stock_daily.pipeline import run_daily, validate_batch
from stock_daily.providers import DataError, DemoProvider, parse_day
from stock_daily.sdk_client import SDKClient, read_result


class FakeSDK:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.calls = []

    def query(self, method, **params):
        self.calls.append((method, params))
        result = self.fixtures[method]
        if isinstance(result, Exception):
            raise result
        return result(params) if callable(result) else result

    def close(self):
        pass


def fixtures():
    def basic(code, kind="1", ipo="2000-01-01"):
        return dict(code=code, code_name="普通股票", type=kind, status="1", ipoDate=ipo, outDate="")

    def quote(code, status="1", is_st="0"):
        return dict(date="2026-10-09", code=code, open="10", high="11", low="9", close="10",
                    volume="12000", amount="120000", pctChg="0", turn="1", peTTM="", pbMRQ="2",
                    tradestatus=status, adjustflag="3", isST=is_st)

    return {
        "query_stock_basic": [basic("sh.600000"), basic("sz.000001"), basic("sz.300001", ipo="2026-10-12"),
                              basic("sh.000300", "2"), basic("sh.900901"), basic("sh.510300", "5")],
        "query_stock_industry": [{"code": "sh.600000", "industry": "银行"}, {"code": "sz.000001", "industry": "银行"}],
        "query_daily_history_k_AStock": [quote("sh.600000", is_st="1"), quote("sz.000001", status="0")],
        "query_all_stock": [{"code": "sz.000001", "tradeStatus": "0"}],
        "query_adjust_factor": [dict(code="sh.600000", dividOperateDate="2025-06-01", backAdjustFactor="2"),
                                dict(code="sh.600000", dividOperateDate="2026-10-09", backAdjustFactor="4")],
        "query_history_k_data_plus": [{"code": "sh.000300", "date": "2026-10-09", "close": "4000", "pctChg": "1"}],
        "stock_notice_report": [{"代码": "600000", "公告日期": date(2026, 10, 9), "公告标题": "股东减持公告",
                                 "网址": "https://data.eastmoney.com/notices/detail/600000/example.html"}],
    }


def test_free_units_scope_suspensions_st_and_events(db, settings):
    client = FakeSDK(fixtures())
    config = replace(settings, source="baostock", announcements=True)
    provider = BaoStockProvider(config, db, client)
    batch = provider.fetch_day("20261009")
    quality = validate_batch(batch, batch.universe, config)
    assert quality["coverage"] == 100
    assert quality["expected"] == 1
    assert set(batch.universe) == {"600000.SH", "000001.SZ", "300001.SZ"}
    assert batch.bars[0]["volume"] == 12000  # Already shares, unlike Tushare's lots.
    assert batch.bars[0]["amount"] == 120000  # Already yuan.
    assert batch.bars[0]["adj"] == 4
    assert batch.bars[0]["pe"] is None
    assert batch.universe["600000.SH"]["is_st"] is True
    assert provider.factor("600000.SH", "20261008") == 2
    assert batch.events[0]["published_at"] == ""  # Do not invent a publication time.
    assert batch.event_status == "available"


def test_factor_cache_survives_restart_and_bulk_events_extend_it(db, settings):
    provider = BaoStockProvider(settings, db, FakeSDK(fixtures()))
    provider.fetch_day("20261009")
    data = fixtures()
    data["query_daily_adjust_factor"] = [dict(code="sh.600000", dividOperateDate="2026-10-10", backAdjustFactor="8")]
    client = FakeSDK(data)
    second = BaoStockProvider(settings, db, client)
    second.universe()
    second.target = "20261010"
    assert second.factor("600000.SH", "20261010") == 8
    assert second.factor("600000.SH", "20261009") == 4
    assert not any(method == "query_adjust_factor" for method, _ in client.calls)
    assert sum(method == "query_daily_adjust_factor" for method, _ in client.calls) == 1


def test_missing_factor_response_does_not_become_default_one(settings):
    data = fixtures()
    data["query_adjust_factor"] = DataError("网络超时")
    with pytest.raises(DataError, match="网络超时"):
        BaoStockProvider(settings, client=FakeSDK(data)).fetch_day("20261009")


@pytest.mark.parametrize("field,value", [("date", "2026-10-08"), ("adjustflag", "2"), ("close", "nan")])
def test_reject_wrong_day_adjusted_data_and_nonfinite_prices(settings, field, value):
    data = fixtures()
    data["query_daily_history_k_AStock"][0][field] = value
    with pytest.raises(DataError):
        BaoStockProvider(settings, client=FakeSDK(data)).fetch_day("20261009")


def test_calendar_checks_holidays_and_completeness(settings):
    client = FakeSDK({"query_trade_dates": [
        {"calendar_date": "2026-10-08", "is_trading_day": "0"},
        {"calendar_date": "2026-10-09", "is_trading_day": "1"},
    ]})
    provider = BaoStockProvider(settings, client=client)
    assert provider.calendar("20261008", "20261009") == ["20261009"]
    with pytest.raises(DataError, match="日历"):
        provider.calendar("20261007", "20261009")


def test_notice_failures_and_bad_dates_stay_unverified(settings):
    config = replace(settings, announcements=True)
    data = fixtures()
    data["stock_notice_report"] = DataError("公告超时")
    assert BaoStockProvider(config, client=FakeSDK(data)).events("20261009") == ([], "unavailable")
    data = fixtures()
    data["stock_notice_report"][0]["公告日期"] = date(2026, 10, 8)
    assert BaoStockProvider(config, client=FakeSDK(data)).events("20261009") == ([], "unavailable")
    disabled_client = FakeSDK({})
    assert BaoStockProvider(settings, client=disabled_client).events("20261009") == ([], "disabled")
    assert not disabled_client.calls


def test_sdk_page_failure_cannot_publish_a_truncated_result():
    result = SimpleNamespace(error_code="0", fields=["code"], data=[[str(i)] for i in range(2000)],
                             cur_page_num="1", cur_row_num=0, next=lambda: False)
    with pytest.raises(DataError, match="下一页"):
        read_result(result)


def unresponsive_worker(pipe, timeout):
    pipe.recv()
    pipe.recv()  # Wait forever for another request instead of replying.


def test_sdk_timeout_terminates_and_reconnects_without_hanging(monkeypatch):
    monkeypatch.setattr("stock_daily.sdk_client.time.sleep", lambda _: None)
    client = SDKClient(Settings(free_timeout=0.1, free_request_interval=0), worker_target=unresponsive_worker)
    with pytest.raises(DataError, match="已重连3次"):
        client.query("query_trade_dates")
    assert client.process is None
    assert client.pipe is None


def test_free_adapter_publishes_full_pipeline_and_daily_st_flag_excludes_signals(db, settings):
    config = replace(settings, source="baostock")
    synthetic = DemoProvider(settings)
    synthetic.fetch_day("20261009")
    stock_map = synthetic.universe()
    st_code = None

    def vendor(code):
        number_code, exchange = code.split(".")
        return f"{exchange.lower()}.{number_code}"

    def quotes(params):
        day = params["date"].replace("-", "")
        return [{
            "date": params["date"], "code": vendor(r["code"]),
            **{k: r[k] for k in ("open", "high", "low", "close", "volume", "amount")},
            "pctChg": r["pct"], "turn": r["turnover"], "peTTM": r["pe"], "pbMRQ": r["pb"],
            "tradestatus": "1", "adjustflag": "3", "isST": "1" if r["code"] == st_code else "0",
        } for r in synthetic.fetch_day(day).bars]

    def factors(params):
        records, previous = [], None
        for day, batch in sorted(synthetic.cache.items()):
            for row in batch.bars:
                if vendor(row["code"]) == params["code"] and row["adj"] != previous:
                    records.append({"code": params["code"], "dividOperateDate": parse_day(day).isoformat(), "backAdjustFactor": row["adj"]})
                    previous = row["adj"]
        return records

    client = FakeSDK({
        "query_stock_basic": [{"code": vendor(code), "code_name": stock["name"], "type": "1", "status": "1",
                               "ipoDate": "2020-01-01", "outDate": ""} for code, stock in stock_map.items()],
        "query_stock_industry": [{"code": vendor(code), "industry": stock["industry"]} for code, stock in stock_map.items()],
        "query_trade_dates": lambda p: [{"calendar_date": parse_day(day).isoformat(), "is_trading_day": "1"}
                                        for day in synthetic.calendar(p["start_date"].replace("-", ""), p["end_date"].replace("-", ""))],
        "query_daily_history_k_AStock": quotes,
        "query_adjust_factor": factors,
        "query_all_stock": lambda p: [{"code": vendor(code), "tradeStatus": "0"} for code in synthetic.fetch_day(p["day"].replace("-", "")).suspended],
        "query_history_k_data_plus": lambda p: [{"code": "sh.000300", "date": p["start_date"],
                                                "close": synthetic.fetch_day(p["start_date"].replace("-", "")).benchmark["close"],
                                                "pctChg": synthetic.fetch_day(p["start_date"].replace("-", "")).benchmark["pct_chg"]}],
    })
    # The synthetic calendar fixture supplies open days; this test exercises adapter/pipeline,
    # while the separate calendar test requires vendor rows for every calendar day.
    provider = BaoStockProvider(config, db, client)
    provider.calendar = lambda start, end: (setattr(provider, "target", end) or synthetic.calendar(start, end))
    assert run_daily(db, config, "20261009", provider=provider)["status"] == "success"
    first = db.latest_report("baostock")
    assert first["quality"]["coverage"] == 100
    assert first["candidates"]
    assert db.latest_report("demo") is None
    st_code = first["candidates"][0]["code"]
    run_daily(db, config, "20261009", force=True, provider=provider)
    second = db.latest_report("baostock")
    assert all(candidate["code"] != st_code for candidate in second["candidates"])
    current = next(stock for stock in second["stocks"] if stock["code"] == st_code)
    assert current["is_st"] is True
    assert current["name"] == stock_map[st_code]["name"]
