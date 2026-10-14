import json
import re
from dataclasses import replace

import pytest
from sqlalchemy import select

from stock_daily.db import reports, runs, signals
from stock_daily.html_export import export_html, script_json
from stock_daily.pipeline import run_daily
from stock_daily.providers import DataError
from stock_daily.service import tracking


def snapshot(html):
    return json.loads(re.search(r'<script id="offline-snapshot" type="application/json">(.*?)</script>', html, re.S).group(1))


def test_export_is_self_contained_read_only_and_does_not_embed_credentials(populated, tmp_path):
    db, settings, _ = populated
    settings = replace(settings, token="private-token-sentinel", api_key="private-management-key")
    attack = '</script><script>window.UNSAFE=1</script>'
    report = db.latest_report(settings.source)
    report["candidates"][0]["name"] = attack
    with db.engine.begin() as connection:
        db.upsert(connection, reports, {"source": settings.source, "day": report["day"], "payload": report})
    path = tmp_path / "local.html"
    result = export_html(db, settings, output=path)
    content = path.read_text()
    assert result["report_ready"] is True
    assert 'href="/static/' not in content
    assert 'src="/static/' not in content
    assert settings.token not in content and settings.api_key not in content
    assert '<form id="watch-form"' not in content
    assert attack not in content
    data = snapshot(content)
    assert data["dashboard"]["report"]["candidates"][0]["name"] == attack
    assert all(len(rows) == 65 for rows in data["histories"].values())
    assert data["tracking"]["items"]
    assert "connect-src 'none'" in content


def test_json_escapes_html_parser_boundaries_without_changing_data():
    value = {"text": "</script>&\u2028\u2029"}
    encoded = script_json(value)
    assert "<" not in encoded and "&" not in encoded
    assert json.loads(encoded) == value


def test_historical_observation_cutoff_excludes_later_signals_and_price_outcomes(populated):
    db, settings, _ = populated
    payload = db.latest_report("demo")["candidates"][0]
    with db.engine.begin() as connection:
        connection.execute(signals.insert().values(source="demo", day="20261001", code=payload["code"],
                                                  strategy=payload["strategy"], payload=payload))
    historical = tracking(db, settings, as_of="20261002")
    assert historical["as_of"] == "20261002"
    assert [item["day"] for item in historical["items"]] == ["20261001"]
    assert historical["items"][0]["horizons"]["5"]["status"] == "pending"
    assert next(item for item in tracking(db, settings)["items"] if item["day"] == "20261001")["horizons"]["5"]["status"] == "ready"


def test_requested_missing_day_preserves_existing_output(populated, tmp_path):
    db, settings, _ = populated
    path = tmp_path / "local.html"
    path.write_text("previous verified report")
    with pytest.raises(DataError, match="没有"):
        export_html(db, settings, "20261008", path)
    assert path.read_text() == "previous verified report"


def test_empty_real_source_is_an_explicit_status_snapshot_not_demo(populated, tmp_path):
    db, settings, _ = populated
    config = replace(settings, source="baostock")
    path = tmp_path / "local.html"
    result = export_html(db, config, output=path)
    data = snapshot(path.read_text())
    assert not result["report_ready"]
    assert data["dashboard"]["report"] is None
    assert data["dashboard"]["source"] == "baostock"
    assert data["histories"] == {}


def test_failed_initial_acquisition_updates_latest_html_with_failure(db, settings, tmp_path):
    config = replace(settings, source="baostock", export_html=True, html_report_dir=str(tmp_path))
    path = tmp_path / "baostock-latest.html"
    export_html(db, config, output=path)

    class OfflineProvider:
        def calendar(self, start, end):
            raise DataError("网络接收错误测试")

    with pytest.raises(DataError, match="网络接收错误测试"):
        run_daily(db, config, "20261009", provider=OfflineProvider())
    data = snapshot(path.read_text())
    assert data["dashboard"]["report"] is None
    assert data["histories"] == {}
    assert data["dashboard"]["runs"][0]["status"] == "failed"
    assert data["dashboard"]["runs"][0]["message"] == "网络接收错误测试"


def test_failed_rerun_preserves_report_in_latest_html(populated, tmp_path, monkeypatch):
    db, settings, provider = populated
    config = replace(settings, export_html=True, html_report_dir=str(tmp_path))
    original_report = db.latest_report(settings.source)
    path = tmp_path / "demo-latest.html"
    export_html(db, config, output=path)

    def fail(day):
        raise DataError("重新采集失败测试")

    monkeypatch.setattr(provider, "fetch_day", fail)
    with pytest.raises(DataError, match="重新采集失败测试"):
        run_daily(db, config, "20261009", force=True, provider=provider)
    data = snapshot(path.read_text())
    assert data["dashboard"]["report"]["day"] == original_report["day"]
    assert data["dashboard"]["report"]["candidates"] == original_report["candidates"]
    assert data["dashboard"]["runs"][0]["status"] == "failed"


def test_daily_success_exports_dated_and_latest_html(populated, tmp_path):
    db, settings, provider = populated
    config = replace(settings, export_html=True, html_report_dir=str(tmp_path))
    result = run_daily(db, config, "20261009", force=True, provider=provider)
    assert result["html"]["status"] == "success"
    dated = tmp_path / "demo-20261009.html"
    latest = tmp_path / "demo-latest.html"
    assert latest.read_bytes() == dated.read_bytes()
    data = snapshot(dated.read_text())
    assert data["dashboard"]["report"]["day"] == "20261009"
    assert data["tracking"]["as_of"] == "20261009"
    assert all(item["day"] <= "20261009" for item in data["tracking"]["items"])


def test_html_io_failure_does_not_invalidate_published_database_report(populated, tmp_path, monkeypatch):
    db, settings, provider = populated
    config = replace(settings, export_html=True, html_report_dir=str(tmp_path))
    previous = tmp_path / "demo-latest.html"
    previous.write_text("previous verified HTML")

    def fail(*args):
        raise OSError("disk full test")

    monkeypatch.setattr("stock_daily.html_export.atomic_write", fail)
    result = run_daily(db, config, "20261009", force=True, provider=provider)
    assert result["status"] == "success"
    assert result["html"]["status"] == "failed"
    assert db.latest_report("demo", "20261009") is not None
    assert previous.read_text() == "previous verified HTML"
    task = db.rows(select(runs).where(runs.c.kind == "daily").order_by(runs.c.started_at.desc()).limit(1))[0]
    assert task["status"] == "success" and "HTML导出失败" in task["message"]
    export = db.rows(select(runs).where(runs.c.kind == "export").order_by(runs.c.started_at.desc()).limit(1))[0]
    assert export["status"] == "failed"
