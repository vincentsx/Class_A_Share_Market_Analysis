import time
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_free_provider import FakeSDK, fixtures

from stock_daily.db import sessions
from stock_daily.free_provider import BaoStockProvider
from stock_daily.history_recovery import HistoryRecoveryClient, make_recovery_provider
from stock_daily.pipeline import validate_batch
from stock_daily.providers import DataError


def recovery_fixtures():
    data = fixtures()
    quotes = data["query_daily_history_k_AStock"]
    benchmark = data["query_history_k_data_plus"]

    def history(params):
        return benchmark if params["code"] == "sh.000300" else [r for r in quotes if r["code"] == params["code"]]

    data["query_history_k_data_plus"] = history
    return data


def test_recovery_prices_units_status_and_restart_checkpoints(db, settings, tmp_path):
    config = replace(settings, source="baostock")
    sdk = FakeSDK(recovery_fixtures())
    provider = BaoStockProvider(config, db, sdk)
    stocks = provider.universe()
    client = HistoryRecoveryClient(config, db, stocks, ["20261009"], tmp_path, sdk)
    provider.client = client
    sdk.calls.clear()
    batch = provider.fetch_day("20261009")
    assert validate_batch(batch, stocks, config)["coverage"] == 100
    assert batch.bars[0]["volume"] == 12000
    assert batch.bars[0]["amount"] == 120000
    assert batch.bars[0]["adj"] == 4
    assert batch.suspended == {"000001.SZ"}
    assert batch.universe["600000.SH"]["is_st"] is True
    assert not any(method in {"query_stock_basic", "query_daily_history_k_AStock", "query_all_stock"}
                   for method, _ in sdk.calls)
    assert len(list(tmp_path.rglob("*.json"))) == 2
    sdk.calls.clear()
    restarted = HistoryRecoveryClient(config, db, stocks, ["20261009"], tmp_path, sdk)
    assert restarted.query("query_daily_history_k_AStock", date="2026-10-09") == client.rows["20261009"]
    assert not any(params.get("code") != "sh.000300" for method, params in sdk.calls)


def test_failed_stock_keeps_previous_checkpoint_for_resume(db, settings, tmp_path):
    data = recovery_fixtures()
    sdk = FakeSDK(data)
    stocks = BaoStockProvider(settings, db, sdk).universe()
    original = data["query_history_k_data_plus"]

    def fail_second(params):
        if params["code"] == "sh.600000":
            raise DataError("中断测试")
        return original(params)

    data["query_history_k_data_plus"] = fail_second
    first = HistoryRecoveryClient(settings, db, stocks, ["20261009"], tmp_path, sdk)
    with pytest.raises(DataError, match="中断测试"):
        first.load()
    assert first.rows is None
    assert len(list(tmp_path.rglob("*.json"))) == 1
    data["query_history_k_data_plus"] = original
    sdk.calls.clear()
    resumed = HistoryRecoveryClient(settings, db, stocks, ["20261009"], tmp_path, sdk)
    resumed.load()
    assert not any(params.get("code") == "sz.000001" for method, params in sdk.calls)


def test_two_connections_obey_a_shared_interval_and_close_extra_worker(db, settings, tmp_path):
    starts = []

    class RecordingSDK(FakeSDK):
        closed = False

        def query(self, method, **params):
            if method == "query_history_k_data_plus" and params["code"] != "sh.000300":
                starts.append(time.monotonic())
                time.sleep(0.06)
            return super().query(method, **params)

        def close(self):
            self.closed = True

    first, second = RecordingSDK(recovery_fixtures()), RecordingSDK(recovery_fixtures())
    stocks = BaoStockProvider(settings, db, first).universe()
    client = HistoryRecoveryClient(replace(settings, free_request_interval=0.03), db, stocks,
                                   ["20261009"], tmp_path, first, worker_factory=lambda: second)
    client.load()
    assert len(client.clients) == 2
    assert len(starts) == 2 and max(starts) - min(starts) >= 0.025
    assert second.closed and not first.closed
    assert len(client.rows["20261009"]) == 2


@pytest.mark.parametrize("field,value", [("code", "sh.600001"), ("date", "2026-10-08"),
                                        ("adjustflag", "2"), ("close", "nan")])
def test_invalid_history_never_becomes_a_checkpoint(db, settings, tmp_path, field, value):
    data = recovery_fixtures()
    row = data["query_daily_history_k_AStock"][0]
    row[field] = value
    original = data["query_history_k_data_plus"]
    data["query_history_k_data_plus"] = lambda p: [row] if p["code"] == "sh.600000" else original(p)
    sdk = FakeSDK(data)
    stocks = BaoStockProvider(settings, db, sdk).universe()
    client = HistoryRecoveryClient(settings, db, stocks, ["20261009"], tmp_path, sdk)
    with pytest.raises(DataError):
        client.stock_rows("600000.SH")
    assert not list(tmp_path.rglob("*.json"))


def test_recovery_rejects_stale_metadata(populated):
    db, settings, _ = populated
    session = db.rows(select(sessions).limit(1))[0]
    session.update(source="baostock", acquired_at="2026-10-07T18:00:00+08:00")
    with db.engine.begin() as connection:
        db.upsert(connection, sessions, session)
    with pytest.raises(DataError, match="基础信息早于报告日期"):
        make_recovery_provider(db, replace(settings, source="baostock"), "20261009")
