from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from stock_daily.analysis import build_report
from stock_daily.backtest import Simulation, simulate
from stock_daily.config import SHANGHAI
from stock_daily.db import bars, news_items, reports, sessions, signals, trading_days, watch_evaluations
from stock_daily.evidence_ai import summarize_evidence
from stock_daily.html_export import daily_payload, export_html
from stock_daily.observations import evaluate, plan_views, save_plan
from stock_daily.operations import backup_database, health_view
from stock_daily.pipeline import run_daily
from stock_daily.providers import DemoProvider
from stock_daily.research import changes, evidence
from stock_daily.scorecard import cohorts, summarize
from stock_daily.web import create_app


def test_display_limit_never_truncates_signal_archive(settings, db, monkeypatch):
    import stock_daily.pipeline

    def many_candidates(*args, **kwargs):
        report = build_report(*args, **kwargs)
        candidate = report["candidates"][0]
        all_candidates = [{**candidate, "code": code} for code in ["600000.SH", "600001.SH", "600002.SH"]]
        report.update(candidates=all_candidates[:1], all_candidates=all_candidates, all_candidate_count=3)
        return report

    monkeypatch.setattr(stock_daily.pipeline, "build_report", many_candidates)
    run_daily(db, replace(settings, max_candidates=1), "20261009", provider=DemoProvider(settings))
    saved = db.rows(select(signals))
    assert len(saved) == 3
    assert sum(s["payload"]["priority"] for s in saved) == 1
    assert all(s["payload"]["published_at"] for s in saved)
    assert len(db.latest_report("demo")["candidates"]) == 1


def test_legacy_recovery_preserves_published_evidence_and_separates_rebuilt_cohort(populated, monkeypatch):
    import stock_daily.archive_recovery
    from stock_daily.service import tracking

    db, settings, _ = populated
    report = db.latest_report("demo")
    original = deepcopy(db.rows(select(signals)))
    candidate = report["candidates"][0]
    rebuilt = [candidate, {**candidate, "code": "600001.SH"}, {**candidate, "code": "600002.SH"}]
    report.update(all_candidate_count=3)
    report.pop("all_candidates")
    report.pop("candidate_storage")
    with db.engine.begin() as connection:
        db.upsert(connection, reports, {"source": "demo", "day": report["day"], "payload": report})
    monkeypatch.setattr(stock_daily.archive_recovery, "screen_candidates", lambda *args: rebuilt)
    result = stock_daily.archive_recovery.recover_archive(db, settings)
    assert result["recovered"][0]["reconstructed"] == 2
    saved = db.rows(select(signals))
    assert next(s for s in saved if s["code"] == original[0]["code"]) == original[0]
    assert sum(s["payload"].get("reconstructed", False) for s in saved) == 2
    assert stock_daily.archive_recovery.recover_archive(db, settings)["recovered"] == []
    groups = tracking(db, settings)["groups"]
    assert next(g for g in groups if g["cohort"] == "reconstructed")["count"] == 2
    assert next(g for g in groups if g["cohort"] == "all")["count"] == 1
    assert simulate(db, settings)["skipped"]["冻结指标重建候选，不是当时发布信号"] == 2


def test_legacy_recovery_refuses_mismatched_count(populated, monkeypatch):
    import stock_daily.archive_recovery

    db, settings, _ = populated
    report = db.latest_report("demo")
    report["all_candidate_count"] = 100
    report.pop("candidate_storage")
    with db.engine.begin() as connection:
        db.upsert(connection, reports, {"source": "demo", "day": report["day"], "payload": report})
    before = db.rows(select(signals))
    monkeypatch.setattr(stock_daily.archive_recovery, "screen_candidates", lambda *args: report["candidates"])
    assert stock_daily.archive_recovery.recover_archive(db, settings)["skipped"]
    assert db.rows(select(signals)) == before


def test_changes_distinguish_dropout_invalidation_and_unavailable(populated):
    db, settings, _ = populated
    previous = deepcopy(db.latest_report("demo"))
    candidate = previous["all_candidates"][0]
    old = [{**candidate, "code": f"60000{i}.SH", "invalidation_adj": 10} for i in range(4)]
    previous.update(day="20261008", all_candidates=old, candidates=old, all_candidate_count=4)
    with db.engine.begin() as connection:
        db.upsert(connection, reports, {"source": "demo", "day": "20261008", "payload": previous})
    current = deepcopy(db.latest_report("demo"))
    new = {**candidate, "code": "600004.SH"}
    current.update(all_candidates=[old[0], new], candidates=[old[0], new], all_candidate_count=2,
                   stocks=[{"code": "600001.SH"}, {"code": "600002.SH"}])
    result = changes(db, settings, current, [{"code": "600001.SH", "close": 11, "adj": 1},
                                           {"code": "600002.SH", "close": 9, "adj": 1}])
    assert result["new"][0]["code"] == "600004.SH"
    assert result["continuing"][0]["code"] == "600000.SH"
    assert result["left"][0]["code"] == "600001.SH"
    assert result["invalidated"][0]["code"] == "600002.SH"
    assert result["unavailable"][0]["code"] == "600003.SH"
    assert changes(db, settings, {**current, "day": "20261007"})["comparable"] is False


def test_legacy_candidate_absence_not_called_first_signal(populated):
    db, settings, _ = populated
    previous = deepcopy(db.latest_report("demo"))
    previous.update(day="20261008", all_candidate_count=99)
    previous.pop("all_candidates")
    previous.pop("candidate_storage")
    previous["candidates"] = []
    with db.engine.begin() as connection:
        db.upsert(connection, reports, {"source": "demo", "day": "20261008", "payload": previous})
    result = changes(db, settings, db.latest_report("demo"))
    assert result["new"][0]["change"] == "较已存名单新增"
    assert not result["baseline_complete"]


def outcome(day, value, strategy="breakout", code="600000.SH"):
    return {"day": day, "code": code, "strategy": strategy, "rule_version": "v1", "priority": True,
            "label": strategy, "invalidated_on": None,
            "horizons": {"5": {"status": "ready", "return": value, "max_adverse": -2,
                                "end_day": "20261009", "excess_return": value - 1},
                         "10": {"status": "pending"}, "20": {"status": "missing"}}}


def test_distributions_missing_counts_and_forward_cooldown():
    dates = [f"202608{i:02}" for i in range(1, 31)]
    items = [outcome(dates[0], -10), outcome(dates[1], 1), outcome(dates[20], 20),
             outcome(dates[1], 1, strategy="pullback")]
    s = summarize(items, 5)
    assert s["median_return"] == 1
    assert s["mean_excess"] == 2
    assert s["distribution"] == {"<-5%": 1, "-5%至0%": 0, "0%至5%": 2, "≥5%": 1}
    assert summarize(items, 10)["pending"] == 4
    assert summarize(items, 20)["missing"] == 4
    groups = cohorts(items, dates)
    assert next(g for g in groups if g["cohort"] == "cooldown20" and g["strategy"] == "breakout")["count"] == 2
    assert next(g for g in groups if g["cohort"] == "all" and g["strategy"] == "breakout")["count"] == 3


def test_plan_transitions_are_durable_and_changes_not_repeated(settings, db):
    save_plan(db, "600000.SH", {"reason": "检查", "target": 12, "checks": ["above_target"]})
    watched = [{"code": "600000.SH", "note": ""}]
    report = {"day": "20261008", "generated_at": "2099-01-01T20:30:00+08:00", "stocks": [
        {"code": "600000.SH", "close": 11, "ma20": 10, "pct": 1, "volume_ratio": 1}]}
    with db.engine.begin() as connection:
        first = plan_views(db, settings, report, watched, True, connection)[0]
    assert first["changes"] == []
    report["day"] = "20261009"
    report["stocks"][0]["close"] = 13
    with db.engine.begin() as connection:
        second = plan_views(db, settings, report, watched, True, connection)[0]
    assert second["changes"][0]["state"] == "triggered"
    assert plan_views(db, settings, report, watched)[0]["changes"] == second["changes"]
    report["day"] = "20261012"
    with db.engine.begin() as connection:
        third = plan_views(db, settings, report, watched, True, connection)[0]
    assert third["changes"] == []
    assert len(db.rows(select(watch_evaluations))) == 3
    assert evaluate({"checks": []}, report["stocks"][0])["active"] == []


def test_new_plan_not_applied_to_historical_report(populated):
    db, settings, _ = populated
    save_plan(db, "600000.SH", {"target": 1, "checks": ["above_target"]})
    report = db.latest_report("demo")
    report["generated_at"] = "2026-10-09T20:30:00+08:00"
    result = plan_views(db, settings, report, [{"code": "600000.SH"}])[0]
    assert not result["evaluation"]["available"]
    assert not result["changes"]


def test_plan_auth_validation_and_history_tracking_cutoff(populated):
    db, settings, _ = populated
    config = replace(settings, api_key="local-key")
    db.save_watch("600000.SH")
    with TestClient(create_app(config, db)) as client:
        assert client.put("/api/watchlist/600000.SH/plan", json={}).status_code == 401
        headers = {"Authorization": "Bearer local-key"}
        assert client.put("/api/watchlist/600000.SH/plan", json={"checks": ["fake"]}, headers=headers).status_code == 422
        assert client.put("/api/watchlist/600000.SH/plan", json={"checks": ["above_target"]}, headers=headers).status_code == 422
        assert client.put("/api/watchlist/600000.SH/plan", json={"reason": "test", "checks": []}, headers=headers).status_code == 200
        assert client.get("/api/tracking?day=20261008").json()["items"] == []
        assert client.get("/api/backtest?weight=nan").status_code == 422
        assert client.get("/api/backtest?holding_days=0").status_code == 422


def test_evidence_enforces_publication_and_first_seen_cutoffs(populated):
    db, settings, _ = populated
    config = replace(settings, source="baostock", news_enabled=True)
    report = deepcopy(db.latest_report("demo"))
    report.update(events=[], generated_at="2026-10-09T20:30:00+08:00")
    code, name = report["stocks"][0]["code"], report["stocks"][0]["name"]
    records = [{"provider": "cls", "id": str(i), "published_at": published, "first_seen_at": seen,
                "title": name + "人工智能公告", "content": code, "url": "https://www.cls.cn/detail/123",
                "important": 0} for i, (published, seen) in enumerate([
                    ("2026-10-09T14:00:00+08:00", "2026-10-09T14:01:00+08:00"),
                    ("2026-10-09T14:00:00+08:00", "2026-10-10T14:01:00+08:00"),
                    ("2026-10-09T21:00:00+08:00", "2026-10-09T21:01:00+08:00")])]
    with db.engine.begin() as connection:
        connection.execute(news_items.insert(), records)
    result = evidence(db, config, report)
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == "0"
    assert not evidence(db, settings, report)["items"]  # Demo never mixes real news.


def test_heartbeat_staleness_and_verified_next_task(settings, db):
    current = datetime(2026, 10, 13, 10, tzinfo=SHANGHAI)
    db.save_state("demo", "worker", {"at": current.isoformat(), "status": "running"})
    with db.engine.begin() as connection:
        db.upsert(connection, trading_days, {"source": "demo", "day": "20261013", "is_open": 1})
    assert health_view(db, settings, current)["worker"]["status"] == "running"
    assert health_view(db, settings, current + timedelta(seconds=91))["worker"]["status"] == "stale"
    assert health_view(db, settings, current)["next_tasks"][0]["calendar_verified"]


def test_sqlite_backup_is_consistent_private_and_idempotent(populated, tmp_path):
    import sqlite3

    db, settings, _ = populated
    config = replace(settings, backup_dir=str(tmp_path / "backups"))
    record = backup_database(db, config)
    path = Path(config.backup_dir) / record["file"]
    assert path.stat().st_mode & 0o077 == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("select count(*) from reports").fetchone()[0] == 1
    assert backup_database(db, config)["file"] == record["file"]


def simulation_fixture(db):
    dates = ["20261005", "20261006", "20261007", "20261008", "20261009"]
    with db.engine.begin() as connection:
        for i, day in enumerate(dates):
            db.upsert(connection, trading_days, {"source": "demo", "day": day, "is_open": 1})
            db.upsert(connection, sessions, {"source": "demo", "day": day, "quality": {"suspended": []},
                      "benchmark": {"close": 100 + i}, "universe": {"600000.SH": {"name": "观察公司"}},
                      "events": [], "event_status": "disabled", "acquired_at": day})
            db.upsert(connection, bars, {"source": "demo", "day": day, "code": "600000.SH", "open": 10 + i * .1,
                      "close": 10.1 + i * .1, "high": 10.5 + i * .1, "low": 9.8 + i * .1,
                      "adj": 1, "volume": 1e7, "amount": 1e8, "pct": 1})
        for day in dates[:2]:
            db.upsert(connection, signals, {"source": "demo", "day": day, "code": "600000.SH", "strategy": "breakout",
                      "payload": {"name": "观察公司", "rule_version": "v1", "invalidation_adj": 1,
                                  "relative20": 1, "published_at": day[:4] + "-" + day[4:6] + "-" + day[6:] + "T20:30:00+08:00"}})
    return dates


def test_announcement_first_seen_is_stable_and_followup_crosses_report_dates(settings, db):
    from stock_daily.db import announcement_evidence
    from stock_daily.research import record_announcements

    simulation_fixture(db)
    event = {"code": "600000.SH", "day": "20261005", "title": "观察公司公告",
             "published_at": "2026-10-05T14:00:00+08:00", "url": "https://example.com/announcement"}
    with db.engine.begin() as connection:
        record_announcements(db, connection, "demo", [event], "2026-10-05T14:01:00+08:00")
        record_announcements(db, connection, "demo", [event], "2026-10-06T20:30:00+08:00")
    assert db.rows(select(announcement_evidence))[0]["first_seen_at"] == "2026-10-05T14:01:00+08:00"
    report = {"day": "20261009", "generated_at": "2026-10-09T20:30:00+08:00", "events": [],
              "stocks": [{"code": "600000.SH", "name": "观察公司"}], "candidates": []}
    cards = evidence(db, settings, report)["items"]
    assert len(cards) == 1
    reaction = cards[0]["reactions"][0]
    assert reaction["anchor_day"] == "20261005" and reaction["end_day"] == "20261006"
    assert reaction["return"] == pytest.approx((10.2 / 10.1 - 1) * 100)
    report.update(day="20261005", generated_at="2026-10-05T14:00:30+08:00")
    assert evidence(db, settings, report)["items"] == []


def test_simulation_next_open_t_plus_one_costs_and_overlap(settings, db):
    simulation_fixture(db)
    result = simulate(db, settings, config=Simulation(holding_days=1))
    assert result["trades"][0]["entry_day"] == "20261006"
    assert result["trades"][0]["exit_day"] == "20261007"
    assert result["trades"][0]["fees"] > 0
    assert result["trades"][0]["shares"] % 100 == 0
    assert all(p["cash"] >= 0 for p in result["curve"])
    account = result["summary"]
    assert account["initial_capital"] == 100000
    assert account["market_value"] == 0
    realized = sum(t["shares"] * (t["exit_price"] - t["entry_price"]) - t["fees"] for t in result["trades"])
    assert account["profit"] == pytest.approx(realized)
    assert account["available_cash"] == pytest.approx(100000 + realized)
    longer = simulate(db, settings, config=Simulation(holding_days=3))
    assert longer["skipped"]["已有持仓，合并重叠信号"] == 1
    assert longer["summary"]["closed_trades"] == 1


def test_open_position_account_values_include_costs_once(settings, db):
    simulation_fixture(db)
    result = simulate(db, settings, config=Simulation(holding_days=10))
    position = result["positions"][0]
    account = result["summary"]
    assert account["market_value"] == pytest.approx(position["shares"] * 10.5)
    assert account["available_cash"] == pytest.approx(100000 - position["spent"])
    assert account["equity"] == pytest.approx(account["market_value"] + account["available_cash"])
    assert account["profit"] == pytest.approx(position["shares"] * 10.5 - position["spent"])
    assert account["profit"] / account["initial_capital"] * 100 == pytest.approx(account["return"])


def test_empty_simulation_does_not_invent_current_account_values(settings, db):
    result = simulate(db, settings, config=Simulation(capital=50000))
    assert result["summary"]["initial_capital"] == 50000
    assert result["summary"]["available_cash"] is None
    assert result["summary"]["market_value"] is None
    assert result["summary"]["profit"] is None


def test_simulation_excludes_after_the_fact_reports(settings, db):
    simulation_fixture(db)
    with db.engine.begin() as connection:
        connection.execute(update(signals).values(payload={"name": "观察公司", "invalidation_adj": 1,
                                                           "published_at": "2026-10-10T20:30:00+08:00"}))
    result = simulate(db, settings)
    assert result["skipped"]["事后生成或发布时间缺失"] == 2
    assert not result["positions"] and not result["trades"]


@pytest.mark.parametrize("reason", ["missing", "corporate_action"])
def test_simulation_never_fills_unknown_held_valuation(settings, db, reason):
    simulation_fixture(db)
    with db.engine.begin() as connection:
        query = (bars.c.day == "20261007") & (bars.c.code == "600000.SH")
        if reason == "missing":
            connection.execute(delete(bars).where(query))
        else:
            connection.execute(update(bars).where(query).values(adj=2))
    result = simulate(db, settings)
    assert result["status"] == "incomplete"
    assert result["summary"]["through"] == "20261006"
    baseline = simulate(db, settings, as_of="20261006")["summary"]
    for field in ("initial_capital", "market_value", "available_cash", "equity", "profit"):
        assert result["summary"][field] == pytest.approx(baseline[field])


def test_simulation_price_limits_block_entry_and_delay_exit(settings, db):
    simulation_fixture(db)
    with db.engine.begin() as connection:
        connection.execute(update(bars).where(bars.c.day == "20261006").values(open=11.11, high=11.2, close=11.11))
    result = simulate(db, settings)
    assert result["skipped"]["开盘接近涨停，保守不成交"] == 1


def test_public_payload_omits_personal_plans_and_runtime_metadata(populated):
    db, settings, _ = populated
    db.save_watch("600000.SH", "私人备注")
    save_plan(db, "600000.SH", {"reason": "私人关注理由", "question": "私人计划", "checks": []})
    payload = daily_payload(db, settings, public=True)
    assert not payload["dashboard"]["watchlist"]
    assert "health" not in payload["dashboard"]
    assert "私人计划" not in str(payload)
    assert "backtest" in payload


def test_ai_summary_checks_citations_and_caches_without_second_request(populated, monkeypatch):
    db, settings, _ = populated
    config = replace(settings, ai_api_key="test-secret", ai_model="configured-model")
    report = db.latest_report("demo")
    report["evidence"] = {"as_of": "2026-10-09T20:30:00+08:00", "items": [{"id": "fact-1", "title": "原文"}]}
    calls = []

    def post(self, url, **kwargs):
        import json
        calls.append(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"status": "completed", "output": [
            {"content": [{"type": "output_text", "text": json.dumps({"facts": [
                {"text": "原文事实", "evidence_ids": ["fact-1"]}], "questions": []})}]}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    assert summarize_evidence(db, config, report)["facts"][0]["evidence_ids"] == ["fact-1"]
    summarize_evidence(db, config, report)
    assert len(calls) == 1 and calls[0]["json"]["store"] is False
    report["evidence"]["items"][0]["id"] = "another-id"
    with pytest.raises(ValueError, match="引用"):
        summarize_evidence(db, config, report)


def test_compatible_chat_summary_routes_correctly_and_provider_cache_is_separate(populated, monkeypatch):
    import json

    db, settings, _ = populated
    config = replace(settings, ai_api_key="test-secret", ai_model="deepseek-v4-pro",
                     ai_api_base_url="https://compatible.example/v1", ai_api_style="chat_completions")
    report = db.latest_report("demo")
    report["evidence"] = {"as_of": "2026-10-09T20:30:00+08:00", "items": [{"id": "fact-1", "title": "原文"}]}
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs["json"]))
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps({"facts": [
                {"text": "原文事实", "evidence_ids": ["fact-1"]}], "questions": []})}}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    result = summarize_evidence(db, config, report)
    assert result["model"] == "deepseek-v4-pro"
    assert calls[0][0] == "https://compatible.example/v1/chat/completions"
    assert calls[0][1]["response_format"] == {"type": "json_object"}
    assert len(calls[0][1]["messages"]) == 2
    assert "test-secret" not in repr(config)
    summarize_evidence(db, config, report)
    assert len(calls) == 1
    summarize_evidence(db, replace(config, ai_api_base_url="https://another.example/v1"), report)
    assert len(calls) == 2


@pytest.mark.parametrize("case", ["truncated", "unknown_citation", "unauthorized", "empty_choices"])
def test_compatible_chat_errors_do_not_cache_invalid_summaries(populated, monkeypatch, case):
    import json

    from stock_daily.db import evidence_summaries

    db, settings, _ = populated
    config = replace(settings, ai_api_key="test-secret", ai_model="deepseek-v4-pro",
                     ai_api_base_url="https://compatible.example/v1", ai_api_style="chat_completions")
    report = db.latest_report("demo")
    report["evidence"] = {"as_of": "2026-10-09T20:30:00+08:00", "items": [{"id": "fact-1", "title": "原文"}]}

    def post(self, url, **kwargs):
        body = {"choices": [{"finish_reason": "length" if case == "truncated" else "stop",
                             "message": {"content": json.dumps({"facts": [{"text": "转述",
                                 "evidence_ids": ["unknown" if case == "unknown_citation" else "fact-1"]}],
                                 "questions": []})}}]}
        if case == "empty_choices":
            body["choices"] = []
        return httpx.Response(401 if case == "unauthorized" else 200,
                              request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx.Client, "post", post)
    with pytest.raises(ValueError) as error:
        summarize_evidence(db, config, report)
    assert "test-secret" not in str(error.value)
    assert not db.rows(select(evidence_summaries))


def test_offline_browser_all_candidates_and_new_views(populated, tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    db, settings, _ = populated
    path = tmp_path / "workbench.html"
    export_html(db, settings, output=path)
    with api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium未安装")
        browser = playwright.chromium.launch()
        with browser.new_context(viewport={"width": 390, "height": 844}) as context:
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            page.wait_for_function("() => window.REPORT_READY === true")
            assert page.locator("#daily-changes").is_visible()
            page.locator("#candidate-scope").select_option("all")
            assert page.locator("#candidate-table tr").count() >= 1
            page.locator('[data-view="tracking"]').click()
            page.wait_for_function("() => document.getElementById('tracking-method').textContent.length > 0")
            assert page.locator("#scorecard-table").is_visible()
            page.locator('[data-view="backtest"]').click()
            page.wait_for_function("() => document.getElementById('backtest-output').textContent.length > 0")
            assert page.locator("#simulation-account .tracking-card").count() == 4
            for label in ("初始资金", "当前持仓市值", "可用资金", "盈亏金额"):
                assert label in page.locator("#simulation-account").inner_text()
            assert page.locator("#backtest-form button").is_disabled()
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not errors
        browser.close()


def test_live_browser_follows_new_reports_and_preserves_historical_selection(populated):
    import json
    from urllib.parse import urlsplit

    api = pytest.importorskip("playwright.sync_api")
    db, settings, provider = populated
    config = replace(settings, api_key="test-admin")
    db.save_watch("600000.SH")
    with TestClient(create_app(config, db)) as client, api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium未安装")
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            parsed = urlsplit(route.request.url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            response = client.request(route.request.method, path,
                                      headers={"Authorization": route.request.headers.get("authorization", "")},
                                      json=json.loads(route.request.post_data) if route.request.post_data else None)
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get("content-type", "text/plain"))

        page.route("http://workbench.test/**", route_request)
        page.clock.install()
        page.goto("http://workbench.test/")
        page.wait_for_function("() => window.REPORT_READY === true")
        assert page.locator("#date-select").input_value() == ""
        run_daily(db, settings, "20261012", provider=provider)
        page.clock.fast_forward(30000)
        page.wait_for_function("() => document.getElementById('market-date').textContent.includes('2026.10.12')")
        page.locator("#date-select").select_option("20261009")
        page.wait_for_function("() => document.getElementById('market-date').textContent.includes('2026.10.09')")
        page.clock.fast_forward(30000)
        assert page.locator("#date-select").input_value() == "20261009"
        page.locator('[data-view="watch"]').click()
        page.locator('.edit-plan[data-code="600000.SH"]').click()
        page.locator('#plan-form [name="reason"]').fill("验证计划界面")
        page.locator('#plan-form [name="key"]').fill("test-admin")
        page.locator('#plan-form button[type="submit"]').click()
        page.wait_for_function("() => !document.getElementById('plan-dialog').open")
        api.expect(page.locator("#watch-table")).to_contain_text("验证计划界面")
        assert not errors
        browser.close()
