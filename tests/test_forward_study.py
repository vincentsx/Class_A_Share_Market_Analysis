from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_entry_policy import batch
from test_mail_preview import browser as browser
from test_paper_trading import CURRENT, close_day, setup

from stock_daily.attribution import build_attribution
from stock_daily.db import (
    Database,
    forward_accounts,
    forward_quotes,
    forward_studies,
    mail_outbox,
    paper_accounts,
    paper_quote_batches,
    reports,
    trade_alerts,
    wecom_outbox,
)
from stock_daily.forward_study import Scope, held_codes, process, start, study_view


def activate(db, settings, weak=False):
    setup(db)
    if weak:
        row = db.rows(select(reports))[0]
        row["payload"]["market"].update(above_ma20_ratio=20, benchmark_return20=-5)
        with db.engine.begin() as c:
            db.upsert(c, reports, row)
    return start(db, settings, CURRENT - timedelta(minutes=1))


def observe_pair(db, settings):
    first = process(db, settings, batch(), CURRENT)
    assert first["status"] == "success"
    assert all(r["new_fills"] == 0 for r in first["arms"].values())
    later = CURRENT + timedelta(minutes=10)
    return process(db, settings, batch(later, 10.31), later)


def test_study_only_uses_post_activation_quotes_and_never_touches_default_or_notifications(db, settings):
    created = activate(db, settings)
    tables = (paper_accounts, paper_quote_batches, trade_alerts, mail_outbox, wecom_outbox)
    before = {t.name: db.rows(select(t)) for t in tables}
    assert start(db, settings, CURRENT)["id"] == created["id"]
    early = CURRENT - timedelta(minutes=2)
    assert process(db, settings, batch(early), early)["status"] == "skipped"
    result = observe_pair(db, settings)
    assert [r["new_fills"] for r in result["arms"].values()] == [1, 1]
    rows = db.rows(select(forward_accounts).order_by(forward_accounts.c.arm))
    accounts = [r["payload"]["account"] for r in rows]
    assert accounts[0]["summary"] == accounts[1]["summary"]
    assert accounts[0]["fills"][0]["id"] != accounts[1]["fills"][0]["id"]
    for a in accounts:
        assert a["summary"]["initial_capital"] == 100000
        assert a["summary"]["available_cash"] == pytest.approx(100000 + sum(f["cash_flow"] for f in a["fills"]))
        assert len(a["trades"]) == 0 and len(a["fills"]) == 1
    assert before == {t.name: db.rows(select(t)) for t in tables}
    restarted = Database(settings.database_url)
    try:
        at = CURRENT + timedelta(minutes=10)
        assert process(restarted, settings, batch(at), at)["status"] == "unchanged"
        assert restarted.rows(select(forward_accounts).order_by(forward_accounts.c.arm)) == rows
        assert held_codes(restarted, settings, "20261015") == {"600001.SH"}
    finally:
        restarted.engine.dispose()


def test_weak_market_blocks_only_filtered_arm_and_tracks_control_outcome(db, settings):
    activate(db, settings, weak=True)
    result = observe_pair(db, settings)
    assert result["arms"]["control"]["new_fills"] == 1
    assert result["arms"]["filtered"]["new_fills"] == 0
    result = study_view(db, settings, "20261015")
    assert result["batches"] == 2 and result["blocked"]["count"] == 1
    blocked = result["blocked"]["items"][0]
    assert blocked["status"] == "open"
    assert blocked["profit"] == pytest.approx(result["arms"][0]["summary"]["profit"])
    assert result["difference"] == pytest.approx(-blocked["profit"])
    assert result["arms"][1]["summary"]["available_cash"] == 100000


def test_second_arm_failure_rolls_back_both_accounts_quotes_and_progress(db, settings, monkeypatch):
    activate(db, settings)
    original = Scope.save
    def fail(self, *args):
        if self.arm == "filtered":
            raise RuntimeError("研究事务失败")
        return original(self, *args)
    monkeypatch.setattr(Scope, "save", fail)
    with pytest.raises(RuntimeError):
        process(db, settings, batch(), CURRENT)
    assert not db.rows(select(forward_accounts)) and not db.rows(select(forward_quotes))
    assert db.rows(select(forward_studies))[0]["payload"]["batches"] == 0
    monkeypatch.setattr(Scope, "save", original)
    assert process(db, settings, batch(), CURRENT)["status"] == "success"


def test_settings_or_implementation_changes_pause_without_mixing_versions(db, settings, monkeypatch):
    activate(db, settings)
    process(db, settings, batch(), CURRENT)
    before = db.rows(select(forward_accounts))
    at = CURRENT + timedelta(minutes=10)
    assert process(db, replace(settings, ai_trading_enabled=True), batch(at), at)["status"] == "paused"
    monkeypatch.setattr("stock_daily.forward_study.fingerprint", lambda: "changed")
    assert process(db, settings, batch(at), at)["status"] == "paused"
    assert db.rows(select(forward_accounts)) == before


def test_historical_view_has_no_future_study_or_quotes_and_read_never_writes(db, settings):
    activate(db, settings)
    observe_pair(db, settings)
    before = db.rows(select(forward_accounts))
    assert study_view(db, settings, "20261014")["status"] == "not_started"
    old = study_view(db, settings, "20261015")
    row = db.rows(select(forward_studies))[0]
    row["payload"].update(last_batch="2026-10-16T10:00:00+08:00", batches=99, status="paused", message="未来状态")
    with db.engine.begin() as c:
        db.upsert(c, forward_studies, row)
    assert study_view(db, settings, "20261015") == old
    assert db.rows(select(forward_accounts)) == before


def test_attribution_includes_valued_open_positions_and_does_not_double_subtract_fees():
    closed = {"strategy": "pullback", "rule_version": "v1", "profit": -20,
              "entry_fee": 5, "exit_fee": 5, "held_sessions": 1, "exit_reason": "止损"}
    open_trade = {"strategy": "intraday_reclaim", "rule_version": "v2", "profit": 5,
                  "entry_fee": 5, "entry_confirmation": {"version": "entry-v5"}}
    account = {"trades": [closed], "positions": [open_trade], "summary": {"profit": -15, "through": "20261014"},
               "valuation": {"summary": {"profit": 10, "through": "20261015"},
                             "positions": [{**open_trade, "profit": 30}], "captured_at": CURRENT.isoformat()}}
    before = deepcopy(account)
    a = build_attribution(account)
    assert a["reconciled"] and a["total"]["profit"] == 10 and a["total"]["fees"] == 15
    assert sum(r["profit"] for r in a["by_strategy"]) == sum(r["profit"] for r in a["by_version"]) == 10
    assert a["day"] == "20261015" and a["daily_through"] == "20261014"
    assert account == before
    assert a["by_exit"][0]["realized"] == -20


def test_filtered_account_still_exits_and_obeys_t_plus_one(db, settings):
    activate(db, settings)
    observe_pair(db, settings)
    later = CURRENT + timedelta(minutes=20)
    payload = batch(later, 9.6)
    payload["quotes"]["600001.SH"]["low"] = 9.5
    process(db, settings, payload, later)
    assert all(r["payload"]["account"]["positions"][0]["exit_due"] for r in db.rows(select(forward_accounts)))
    close_day(db, price=10.2)
    row = db.rows(select(reports))[0]
    row["day"] = "20261015"
    row["payload"] = {**row["payload"], "day": "20261015", "generated_at": "2026-10-15T18:30:00+08:00",
                      "market": {"above_ma20_ratio": 10, "benchmark_return20": -5}}
    with db.engine.begin() as c:
        db.upsert(c, reports, row)
    tomorrow = CURRENT + timedelta(days=1)
    result = process(db, settings, batch(tomorrow), tomorrow)
    assert all(r["new_fills"] == 1 for r in result["arms"].values())
    view = study_view(db, settings, "20261016")
    assert all(r["summary"]["open_positions"] == 0 and r["summary"]["closed_trades"] == 1 for r in view["arms"])
    assert not db.rows(select(trade_alerts))


def test_pair_reads_ai_once_and_does_not_reuse_default_quote_confirmation(db, settings, monkeypatch):
    activate(db, settings)
    calls = []
    def analysis(*args):
        calls.append(1)
        return {"enabled": False, "status": "disabled", "opinions": {}}
    monkeypatch.setattr("stock_daily.decision_ai.decision_context", analysis)
    first = process(db, settings, batch(), CURRENT)
    assert len(calls) == 1
    assert all(r["new_fills"] == 0 for r in first["arms"].values())


def test_forward_pages_show_waiting_state_in_phone_and_scriptless_exports(browser, populated):
    from stock_daily.html_export import daily_payload, render_daily

    db, settings, _ = populated
    start(db, settings, CURRENT)
    payload = daily_payload(db, settings)
    payload["backtest"]["forward_study"] = study_view(db, settings)
    html = render_daily(payload, settings)
    for scripts in (True, False):
        with browser.new_context(viewport={"width": 390, "height": 844}, java_script_enabled=scripts) as ctx:
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://reports.example/**", lambda r: r.fulfill(content_type="text/html", body=html))
            page.goto("https://reports.example/report.html")
            if scripts:
                page.locator('[data-view="backtest"]').first.click()
                page.locator("#account-attribution").wait_for()
                page.locator("#forward-study").wait_for()
                assert "100000.00" in page.locator("#forward-study").inner_text()
            else:
                assert "策略归因 · 盈亏来自哪里" in page.locator("body").inner_text()
                assert "独立前向对照" in page.locator("body").inner_text()
                assert "100,000.00" in page.locator("body").inner_text()
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not errors
