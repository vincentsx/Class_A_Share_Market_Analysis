from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from stock_daily.config import SHANGHAI
from stock_daily.db import runs
from stock_daily.scheduler import due
from stock_daily.web import create_app


def test_web_report_endpoints_and_write_auth(populated):
    from dataclasses import replace

    db, settings, _ = populated
    settings = replace(settings, api_key="test-key")
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/dashboard").json()["report"]["day"] == "20261009"
        assert client.get("/api/report/20261008").status_code == 404
        assert client.get("/api/report/20261009/download").headers["content-disposition"].endswith('.json"')
        assert len(client.get("/api/stocks/600000.SH/history?day=20261009").json()) == 65
        assert client.get("/api/stocks/bad/history").status_code == 422
        assert client.post("/api/watchlist", json={"code": "600000.SH"}).status_code == 401
        headers = {"Authorization": "Bearer test-key"}
        assert client.post("/api/watchlist", json={"code": "bad"}, headers=headers).status_code == 422
        assert (
            client.post(
                "/api/watchlist", json={"code": "600000.SH", "note": "关注"}, headers=headers
            ).status_code
            == 200
        )
        assert client.get("/api/dashboard").json()["watchlist"][0]["note"] == "关注"
        assert client.delete("/api/watchlist/600000.SH", headers=headers).status_code == 200
        assert client.get("/api/watchlist").json() == []
        assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_missing_report_is_empty_not_substituted(populated):
    db, settings, _ = populated
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/dashboard?day=20261008").json()["report"] is None


def test_scheduler_cutoff_persistent_completion_and_retry_bound(settings, db):
    current = datetime(2026, 10, 9, 17, 29, tzinfo=SHANGHAI)
    assert not due(db, settings, current, "daily", "17:30")
    current += timedelta(minutes=1)
    assert due(db, settings, current, "daily", "17:30")
    for i in range(4):
        with db.engine.begin() as connection:
            connection.execute(
                runs.insert().values(
                    id=str(i),
                    source="demo",
                    day="20261009",
                    kind="daily",
                    status="failed",
                    message="test",
                    started_at=(current - timedelta(minutes=30 + i)).isoformat(),
                    completed_days=0,
                )
            )
    assert not due(db, settings, current, "daily", "17:30")


def test_successful_task_not_repeated(settings, db):
    identifier = db.start_run("demo", "20261009")
    db.finish_run(identifier, "success", "完成")
    assert not due(db, settings, datetime(2026, 10, 9, 20, 0, tzinfo=SHANGHAI), "daily", "17:30")


def test_three_slots_complete_independently_and_no_late_midday_capture(settings, db):
    identifier = db.start_run('demo', '20261009', 'midday')
    db.finish_run(identifier, 'success', '午间完成')
    current = datetime(2026, 10, 9, 15, 5, tzinfo=SHANGHAI)
    assert due(db, settings, current, 'close', '15:05', '18:30')
    assert not due(db, settings, current, 'midday', '11:35', '13:00')
    identifier = db.start_run('demo', '20261009', 'close')
    db.finish_run(identifier, 'success', '收盘完成')
    current = current.replace(hour=18, minute=30)
    assert due(db, settings, current, 'evening', '18:30')
    assert not due(db, settings, current, 'close', '15:05', '18:30')


def test_artifact_route_is_source_scoped_and_preserves_nonce_csp(populated, tmp_path):
    from dataclasses import replace

    from stock_daily.html_export import export_html

    db, settings, _ = populated
    settings = replace(settings, html_report_dir=str(tmp_path))
    export_html(db, settings)
    with TestClient(create_app(settings, db)) as client:
        response = client.get('/artifacts/demo-latest.html')
        assert response.status_code == 200
        assert "script-src 'nonce-" in response.headers['content-security-policy']
        assert client.get('/artifacts/baostock-latest.html').status_code == 404
        assert client.get('/artifacts/.env').status_code == 404
        assert client.get('/artifacts/demo-20261009-1135.png').status_code == 404


def test_evening_reuses_published_report_and_retries_artifact_export(populated, tmp_path, monkeypatch):
    from dataclasses import replace

    from stock_daily.scheduler import run_evening

    db, settings, _ = populated
    settings = replace(settings, export_html=True, html_report_dir=str(tmp_path))
    def unexpected_daily(*args, **kwargs):
        raise AssertionError('已成功的日线不应重新采集')
    monkeypatch.setattr('stock_daily.scheduler.run_daily', unexpected_daily)
    result = run_evening(db, settings, '20261009')
    assert result['status'] == 'success'
    assert (tmp_path / 'demo-20261009-1830.html').read_bytes() == (tmp_path / 'demo-latest.html').read_bytes()
    assert db.rows(select(runs).where(runs.c.kind == 'evening'))[-1]['status'] == 'success'


def test_evening_failure_is_durable_and_not_treated_as_completed(settings, db, monkeypatch):
    from stock_daily.providers import DataError
    from stock_daily.scheduler import run_evening

    def unavailable(*args, **kwargs):
        raise DataError('日线数据未更新')
    monkeypatch.setattr('stock_daily.scheduler.run_daily', unavailable)
    with pytest.raises(DataError, match='未更新'):
        run_evening(db, settings, '20261009')
    assert db.rows(select(runs).where(runs.c.kind == 'evening'))[-1]['status'] == 'failed'
