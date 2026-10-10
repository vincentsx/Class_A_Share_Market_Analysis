from datetime import datetime, timedelta

from fastapi.testclient import TestClient

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
