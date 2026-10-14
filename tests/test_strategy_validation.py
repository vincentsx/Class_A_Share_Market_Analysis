import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from stock_daily.backtest import Simulation, simulate, simulation_profile
from stock_daily.db import bars, reports, sessions, signals, trading_days
from stock_daily.execution import entry_size
from stock_daily.performance import performance
from stock_daily.trade_plan import build_trade_plan
from stock_daily.validation import WalkForward, compare_profiles, research_snapshot, walk_forward
from stock_daily.web import create_app


def seed(db, count=160, codes=("600000.SH", "600001.SH", "600002.SH"), start=date(2025, 1, 6)):
    days = []
    day = start
    while len(days) < count + 30:
        if day.weekday() < 5:
            days.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    with db.engine.begin() as c:
        for day in days:
            db.upsert(c, trading_days, {"source": "demo", "day": day, "is_open": 1})
        # Include weekends so next-session verification never guesses dates.
        current = start
        while current.strftime("%Y%m%d") <= days[-1]:
            if current.weekday() >= 5:
                db.upsert(c, trading_days, {"source": "demo", "day": current.strftime("%Y%m%d"), "is_open": 0})
            current += timedelta(days=1)
        for index, day in enumerate(days[:count]):
            stamp = f"{day[:4]}-{day[4:6]}-{day[6:]}T18:30:00+08:00"
            close = 10 + index * .01
            db.upsert(c, sessions, {"source": "demo", "day": day, "quality": {"suspended": []},
                "benchmark": {"close": 100 + index * .02},
                "universe": {code: {"name": code} for code in codes},
                "events": [], "event_status": "disabled", "acquired_at": stamp})
            db.upsert(c, reports, {"source": "demo", "day": day, "payload": {
                "day": day, "generated_at": stamp,
                "market": {"above_ma20_ratio": 80, "benchmark_return20": 5}}})
            for rank, code in enumerate(codes):
                db.upsert(c, bars, {"source": "demo", "day": day, "code": code,
                    "open": close, "close": close, "high": close + .5, "low": close - .5,
                    "volume": 1e7, "amount": 1e8, "pct": .1, "adj": 1})
                db.upsert(c, signals, {"source": "demo", "day": day, "code": code, "strategy": "breakout",
                    "payload": {"name": code, "industry": "行业A" if rank < 2 else "行业B",
                        "rule_version": "v1", "published_at": stamp, "relative20": 3 - rank,
                        "invalidation_adj": close - .5, "atr14": .3}})
    return days[:count]


def test_risk_sizing_includes_fees_and_atr_floor():
    config = simulation_profile("guarded", weight=.9)
    size = entry_size(config, cash=100000, equity=100000, price=10,
        previous={"adj": 1, "volume": 1e7}, payload={"invalidation_adj": 9.99, "atr14": 1}, lot=100)
    assert size["shares"] == 400  # 500 shares would exceed the budget once fees are included.
    assert size["risk_distance"] == 1
    assert size["estimated_risk"] <= 500
    assert size["shares"] * 10 <= size["budget"]
    assert entry_size(config, cash=100000, equity=100000, price=10,
        previous={"adj": 1, "volume": 1e7}, payload={"invalidation_adj": 9}, lot=100)["shares"] == 0


@pytest.mark.parametrize("opening,reason", [(10.4, "追高上限"), (9.5, "失效位置")])
def test_opening_guards_and_baseline_control(settings, db, opening, reason):
    days = seed(db, 3, ("600000.SH",))
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day == days[1]).values(open=opening))
    guarded = simulate(db, settings, days[1], simulation_profile("guarded"))
    assert not guarded["positions"]
    assert any(reason in key for key in guarded["skipped"])
    assert len(simulate(db, settings, days[1])["positions"]) == 1


def test_plan_and_execution_share_risk_sizing_and_ignore_future(settings, db):
    days = seed(db, 3)
    config = simulation_profile("guarded")
    report = db.latest_report("demo", days[0])
    planned = build_trade_plan(db, settings, report, config=config)
    # Match next-open reference so the plan and fill should size identically.
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day == days[1]).values(open=10))
    result = simulate(db, settings, days[1], config)
    for entry, position in zip(planned["entries"], sorted(result["positions"], key=lambda p: p["code"])):
        assert entry["reference_shares"] == position["shares"]
        assert entry["entry_risk"] == position["entry_risk"]
        assert position["entry_risk"]["estimated_risk"] <= position["entry_risk"]["risk_budget"]
    assert build_trade_plan(db, settings, report, config=config) == planned
    assert result["summary"]["profit"] == pytest.approx(sum(p["profit"] for p in result["positions"]))


def test_industry_cap_and_prior_market_state(settings, db):
    days = seed(db, 3)
    config = simulation_profile("guarded", weight=.5, risk_fraction=0, max_industry_weight=.15)
    result = simulate(db, settings, days[1], config)
    assert {p["industry"] for p in result["positions"]} == {"行业A", "行业B"}
    assert len(result["positions"]) == 2  # second stock in A cannot bypass the industry cap.
    with db.engine.begin() as c:
        report = db.latest_report("demo", days[0])
        report["market"]["above_ma20_ratio"] = 20
        c.execute(update(reports).where(reports.c.day == days[0]).values(payload=report))
    adaptive = simulate(db, settings, days[1], simulation_profile("adaptive"))
    assert not adaptive["positions"]  # strong entry-day breadth must not override yesterday's frozen state.
    assert adaptive["skipped"]["市场趋势或广度偏弱，暂停新增仓位"] == 3


def test_drawdown_pause_starts_after_the_close_and_keeps_exits(settings, db):
    days = seed(db, 5, ("600000.SH", "600001.SH"))
    config = Simulation(weight=.4, drawdown_pause=.01, holding_days=1)
    with db.engine.begin() as c:
        c.execute(delete(signals).where(signals.c.day == days[0], signals.c.code == "600001.SH"))
        c.execute(update(bars).where(bars.c.day == days[1], bars.c.code == "600000.SH")
                  .values(close=9, low=8.5))
        c.execute(update(bars).where(bars.c.day == days[2], bars.c.code == "600000.SH")
                  .values(open=9, low=8.5))
    first = simulate(db, settings, days[1], config)
    assert len(first["positions"]) == 1  # today's falling close cannot pre-empt today's opening buy.
    assert first["execution_state"]["entry_paused"]
    full = simulate(db, settings, days[3], config)
    assert full["trades"] and full["trades"][0]["exit_day"] == days[2]
    assert not full["positions"]
    assert full["skipped"]["组合回撤达到阈值，暂停新增仓位"] > 0


def test_independent_window_excludes_old_signals_and_positions(settings, db):
    days = seed(db, 5)
    result = simulate(db, settings, days[4], Simulation(start_day=days[2]))
    assert result["curve"][0]["day"] == days[2]
    assert result["curve"][0]["equity"] == 100000
    assert all(p["entry_day"] == days[3] for p in result["positions"])
    with pytest.raises(ValueError, match="起始日期"):
        simulate(db, settings, days[1], Simulation(start_day=days[2]))


def test_net_performance_short_sample_and_contributions():
    curve = [{"equity": value} for value in (1000, 900, 950, 1000, 1070)]
    trades = [{"strategy": "breakout", "profit": value, "shares": 10, "entry_price": 10,
               "exit_price": 11, "entry_open": 9.9, "exit_open": 11.1} for value in (100, -50)]
    positions = [{"strategy": "pullback", "profit": 20, "shares": 10, "entry_price": 10, "entry_open": 9.9}]
    metrics = performance(curve, trades, positions, 1000)
    assert metrics["annual_return"] is None and metrics["sharpe"] is None
    assert metrics["win_rate"] == 50 and metrics["profit_factor"] == 2
    assert metrics["payoff_ratio"] == 2
    assert metrics["longest_underwater_sessions"] == 2
    assert metrics["current_underwater_sessions"] == 0
    assert sum(v["profit"] for v in metrics["strategy_contributions"].values()) == 70
    assert metrics["slippage_cost"] == pytest.approx(5)
    json.dumps(metrics, allow_nan=False)


def test_comparison_is_read_only_records_all_alternatives_and_stresses(settings, db):
    days = seed(db, 5)
    before = db.rows(select(signals))
    result = compare_profiles(db, settings, days[-1])
    assert result["status"] == "insufficient" and result["auto_promote"] is False
    assert [p["profile"] for p in result["profiles"]] == ["baseline", "guarded", "adaptive"]
    assert all(len(p["stress_tests"]) == 5 for p in result["profiles"])
    assert before == db.rows(select(signals))
    assert result["data_fingerprint"] == compare_profiles(db, settings, days[-1])["data_fingerprint"]
    assert len(result["code_fingerprint"]) == 64


def test_walk_forward_freezes_training_selection_before_future_returns(settings, db):
    days = seed(db, 240)
    spec = WalkForward(60, 42, 10, 24)
    original = walk_forward(db, settings, days[-1], spec)
    first = original["folds"][0]
    assert first["selected_profile"] is not None
    assert first["train_end"] == days[59] and first["test_start"] == days[71]
    assert len(first["test_results"]) == 3
    assert original["status"] == "evaluated"
    with db.engine.begin() as c:
        c.execute(update(bars).where(bars.c.day >= first["test_start"]).values(open=30, high=31, close=30))
    changed = walk_forward(db, settings, days[-1], spec)
    assert changed["folds"][0]["training_trials"] == first["training_trials"]
    assert changed["folds"][0]["selected_profile"] == first["selected_profile"]
    assert changed["folds"][0]["frozen_parameters"] == first["frozen_parameters"]
    assert changed["folds"][0]["test_results"] != first["test_results"]
    assert changed["data_fingerprint"] != original["data_fingerprint"]


def test_missing_sessions_and_insufficient_training_cannot_select_winner(settings, db):
    days = seed(db, 95)
    spec = WalkForward(60, 20, 100, 1)
    result = walk_forward(db, settings, days[-1], spec)
    assert result["status"] == "insufficient"
    assert result["folds"][0]["selected_profile"] is None
    assert result["folds"][0]["test_results"] == []
    with db.engine.begin() as c:
        c.execute(delete(sessions).where(sessions.c.day == days[10]))
    result = walk_forward(db, settings, days[-1], replace(spec, min_closed_trades=10))
    assert result["status"] == "incomplete"
    assert all(t["status"] == "incomplete" for t in result["folds"][0]["training_trials"])
    assert result["folds"][0]["selected_profile"] is None


def test_research_uses_a_consistent_database_snapshot(settings, db):
    days = seed(db, 3)
    with research_snapshot(db) as view:
        view.rows(select(signals))  # establish snapshot before a concurrent writer
        with db.engine.begin() as c:
            c.execute(update(bars).where(bars.c.day == days[1]).values(close=99))
        assert all(r["close"] != 99 for r in view.rows(select(bars)))
    assert any(r["close"] == 99 for r in db.rows(select(bars)))


def test_api_profiles_validation_and_dates(settings, db):
    days = seed(db, 3)
    with TestClient(create_app(settings, db)) as client:
        result = client.get(f"/api/backtest?day={days[-1]}&profile=guarded").json()
        assert result["parameters"]["risk_fraction"] == .005
        assert result["parameters"]["require_valid_entry"] is True
        assert client.get("/api/backtest?profile=unknown").status_code == 422
        assert client.get("/api/backtest?risk_fraction=nan").status_code == 422
        assert client.get("/api/backtest?max_entry_gap=-1").status_code == 422
        assert client.get("/api/research/walk-forward?train_sessions=1").status_code == 422
        assert client.get("/api/research/compare?day=20251399").status_code == 422
        research = client.get(f"/api/research/walk-forward?day={days[-1]}").json()
        assert research["status"] == "insufficient" and research["required_sessions"] == 179


def test_yearly_metrics_do_not_create_infinite_sharpe():
    curve = [{"equity": 1000 * 1.001 ** i} for i in range(253)]
    metrics = performance(curve, [], [], 1000)
    assert metrics["annual_return"] == pytest.approx((1.001 ** 252 - 1) * 100)
    assert metrics["sharpe"] is None
    json.dumps(metrics, allow_nan=False)


def test_cli_profile_and_research_record(settings, db, monkeypatch, capsys, tmp_path):
    from stock_daily.cli import main

    seed(db, 3)
    monkeypatch.setattr("stock_daily.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr("sys.argv", ["stock-daily", "backtest", "--profile", "guarded", "--risk-fraction", ".002"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["parameters"]["risk_fraction"] == .002
    assert result["parameters"]["max_entry_gap"] == .03
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["stock-daily", "validate-strategies", "--mode", "walk-forward"])
    main()
    result = json.loads(capsys.readouterr().out)
    records = list((tmp_path / "data/research").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text()) == result
    assert result["status"] == "insufficient"


def test_deployed_strategy_switches_forward_without_rewriting_holdings(settings, db):
    days = seed(db, 4, start=date(2026, 10, 13))
    with db.engine.begin() as c:
        c.execute(delete(signals).where(signals.c.day == days[0], signals.c.code != "600000.SH"))
        for signal in db.rows(select(signals).where(signals.c.day == days[1])):
            payload = {**signal["payload"], "atr14": 2}
            c.execute(update(signals).where(signals.c.day == days[1], signals.c.code == signal["code"])
                      .values(payload=payload))
    old = simulate(db, settings, days[1], simulation_profile("baseline"))
    deployed = simulate(db, settings, days[1])
    assert deployed["parameters"]["policy_start_day"] == "20261015"
    assert deployed["parameters"]["risk_fraction"] == .005
    assert deployed["summary"] == old["summary"]
    assert deployed["positions"] == old["positions"]
    plan = build_trade_plan(db, settings, db.latest_report("demo", days[1]))
    assert plan["target_day"] == "20261015"
    assert all(i["entry_risk"]["risk_budget"] > 0 for i in plan["entries"] if i["status"] == "planned")
    switched = simulate(db, settings, days[2])
    existing = next(p for p in switched["positions"] if p["code"] == "600000.SH")
    assert existing["entry_day"] == days[1] and existing["shares"] == old["positions"][0]["shares"]
    assert existing["entry_risk"]["risk_budget"] is None
    additions = [p for p in switched["positions"] if p["code"] != "600000.SH"]
    assert len(additions) == 2 and all(p["shares"] == 200 for p in additions)
    assert all(p["entry_risk"]["estimated_risk"] <= p["entry_risk"]["risk_budget"] for p in additions)
    with TestClient(create_app(settings, db)) as client:
        api = client.get("/api/backtest?day=20261015").json()
        assert api["parameters"] == switched["parameters"] and api["summary"] == switched["summary"]
