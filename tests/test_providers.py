import httpx
import pytest

from stock_daily.config import Settings
from stock_daily.providers import DataError, TushareProvider


def provider(responder):
    return TushareProvider(
        Settings(source="tushare", token="unit-test-token", request_interval=0),
        httpx.Client(transport=httpx.MockTransport(responder)),
    )


def reply(fields, items):
    return httpx.Response(200, json={"code": 0, "data": {"fields": fields, "items": items}})


def test_permission_error_is_redacted_and_not_retried():
    calls = []

    def denied(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 2002, "msg": "unit-test-token 权限不足"})

    client = provider(denied)
    with pytest.raises(DataError) as error:
        client.query("daily", "close", trade_date="20261009")
    assert "unit-test-token" not in str(error.value)
    assert len(calls) == 1


def test_pagination_continues_until_short_page():
    import json

    def page(request):
        offset = json.loads(request.content)["params"]["offset"]
        return reply(["ts_code"], [["a"], ["b"]] if offset == 0 else [["c"]])

    assert provider(page).paged("daily", "ts_code", limit=2) == [
        {"ts_code": "a"},
        {"ts_code": "b"},
        {"ts_code": "c"},
    ]


def test_server_ignoring_pagination_is_not_silently_accepted():
    with pytest.raises(DataError, match="未正确分页"):
        provider(lambda _: reply(["ts_code"], [["a"], ["b"]])).paged("daily", "ts_code", limit=2)


def test_live_units_are_normalized_before_storage():
    client = provider(lambda _: reply([], []))
    day = "20261009"
    fixtures = {
        "daily": [
            {
                "ts_code": "600000.SH",
                "trade_date": day,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "vol": 120,
                "amount": 120,
                "pct_chg": 0,
            }
        ],
        "adj_factor": [{"ts_code": "600000.SH", "trade_date": day, "adj_factor": 1}],
        "daily_basic": [
            {"ts_code": "600000.SH", "trade_date": day, "turnover_rate": 1, "pe_ttm": None, "pb": 2}
        ],
        "suspend_d": [],
    }
    client.paged = lambda api, fields, **params: fixtures[api]
    client.query = lambda *args, **kwargs: [{"trade_date": day, "close": 4000, "pct_chg": 0}]
    batch = client.fetch_day(day)
    assert batch.bars[0]["volume"] == 12000
    assert batch.bars[0]["amount"] == 120000
    assert batch.bars[0]["pe"] is None
    assert batch.event_status == "disabled"
