"""Recorded research comparisons and rolling selection using past data only."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from sqlalchemy import select

from .backtest import simulate, simulation_profile
from .config import now
from .db import sessions, signals, trading_days
from .providers import DataError, parse_day

PROFILES = ("baseline", "guarded", "adaptive")


def validate_day(day):
    if day:
        try:
            parse_day(day)
        except DataError as error:
            raise ValueError(str(error)) from None


@contextmanager
def research_snapshot(db):
    with db.engine.connect() as connection:
        if connection.dialect.name != "sqlite":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            # sqlite3 legacy transaction mode does not BEGIN on SELECT by itself.
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
            else:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield ResearchSnapshot(connection)


class ResearchSnapshot:
    """One read transaction, cached queries, and a digest of consumed rows."""

    def __init__(self, connection):
        self.connection = connection
        self.cache = {}

    def rows(self, query):
        compiled = query.compile(compile_kwargs={"literal_binds": True})
        key = str(compiled)
        if key not in self.cache:
            self.cache[key] = [dict(row) for row in self.connection.execute(query).mappings()]
        return self.cache[key]

    def fingerprint(self):
        digest = hashlib.sha256()
        for key, rows in sorted(self.cache.items()):
            digest.update(key.encode())
            for row in sorted(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) for r in rows):
                digest.update(row.encode())
        return digest.hexdigest()


def compact(result):
    return {key: result.get(key) for key in ("status", "stop", "parameters", "summary", "performance", "skipped")}


def _dates(view, source, as_of, start_day=""):
    calendar = view.rows(select(trading_days.c.day).where(
        trading_days.c.source == source, trading_days.c.is_open == 1,
        trading_days.c.day <= as_of, trading_days.c.day >= start_day if start_day else True)
        .order_by(trading_days.c.day))
    first = view.rows(select(signals.c.day).where(signals.c.source == source, signals.c.day <= as_of)
                      .order_by(signals.c.day).limit(1))
    if not first:
        return []
    return [r["day"] for r in calendar if r["day"] >= first[0]["day"]]


def _cutoff(view, source, as_of):
    latest = view.rows(select(sessions.c.day).where(sessions.c.source == source,
        sessions.c.day <= as_of if as_of else True).order_by(sessions.c.day.desc()).limit(1))
    return latest[0]["day"] if latest else None


def _manifest(view, settings, cutoff, parameters):
    code = hashlib.sha256()
    for name in ("backtest.py", "execution.py", "fills.py", "paper_trading.py", "performance.py", "validation.py"):
        code.update(Path(__file__).with_name(name).read_bytes())
    return {"version": "research-v1", "execution_version": "cash-v2", "source": settings.source,
            "as_of": cutoff, "created_at": now().isoformat(), "data_fingerprint": view.fingerprint(),
            "code_fingerprint": code.hexdigest(),
            "research_parameters": parameters, "auto_promote": False,
            "limitations": ["历史行业、名称、风险状态及行情修订不完全具有时点性。",
                "历史滚动回放不是当时预先登记的前向实验；之后的独立模拟才能验证新方案。",
                "日线不能证明成交；期末未平仓按收盘估值，未虚构平仓或扣除尚未发生的卖出费用。"]}


def compare_profiles(db, settings, as_of=None, start_day=""):
    """All alternatives and stresses are reported; never promote a winner."""
    validate_day(as_of)
    validate_day(start_day)
    with research_snapshot(db) as view:
        cutoff = _cutoff(view, settings.source, as_of)
        if not cutoff:
            return {"status": "insufficient", "message": "尚无可用行情", "profiles": []}
        dates = _dates(view, settings.source, cutoff, start_day)
        start = start_day or (dates[0] if dates else cutoff)
        results = []
        for profile in PROFILES:
            config = simulation_profile(profile, start_day=start)
            main = simulate(view, settings, cutoff, config)
            stresses = []
            for label, change in [("滑点翻倍", {"slippage_bps": config.slippage_bps * 2}),
                                  ("滑点四倍", {"slippage_bps": config.slippage_bps * 4}),
                                  ("佣金翻倍", {"commission_bps": config.commission_bps * 2,
                                                  "min_commission": config.min_commission * 2}),
                                  ("持有期5日", {"holding_days": 5}),
                                  ("持有期15日", {"holding_days": 15})]:
                stresses.append({"label": label, **compact(simulate(view, settings, cutoff, replace(config, **change)))})
            results.append({"profile": profile, **compact(main), "stress_tests": stresses})
        result = _manifest(view, settings, cutoff, {"start_day": start, "profiles": list(PROFILES)})
        complete = all(item["status"] == "success" for r in results for item in [r, *r["stress_tests"]])
        enough = len(dates) >= 253 and all(r["summary"].get("closed_trades", 0) >= 30 for r in results)
        result.update(status="incomplete" if not complete else "evaluated" if enough else "insufficient",
                      session_count=len(dates), profiles=results,
                      message="全部方案与压力结果仅供研究；不足252个日收益或30笔平仓交易的方案不判定优势。")
        return result


@dataclass(frozen=True)
class WalkForward:
    train_sessions: int = 126
    test_sessions: int = 42
    min_closed_trades: int = 30
    max_folds: int = 12

    def validate(self):
        if not (60 <= self.train_sessions <= 1260 and 20 <= self.test_sessions <= 252
                and 10 <= self.min_closed_trades <= 1000 and 1 <= self.max_folds <= 24):
            raise ValueError("滚动验证窗口或最小交易样本无效")


def walk_forward(db, settings, as_of=None, specification=None):
    spec = specification or WalkForward()
    spec.validate()
    validate_day(as_of)
    with research_snapshot(db) as view:
        cutoff = _cutoff(view, settings.source, as_of)
        if not cutoff:
            return {"status": "insufficient", "message": "尚无可用行情", "folds": []}
        dates = _dates(view, settings.source, cutoff)
        configs = {p: simulation_profile(p) for p in PROFILES}
        purge = max(c.holding_days for c in configs.values()) + 1
        required = spec.train_sessions + purge + spec.test_sessions
        folds = []
        possible = max(0, (len(dates) - required) // spec.test_sessions + 1)
        # Keep the latest bounded set of full folds, preserving their original grid.
        for offset in range(max(0, possible - spec.max_folds), possible):
            begin = offset * spec.test_sessions
            train_start, train_end = dates[begin], dates[begin + spec.train_sessions - 1]
            test_index = begin + spec.train_sessions + purge
            test_start, test_end = dates[test_index], dates[test_index + spec.test_sessions - 1]
            trials = []
            for name, config in configs.items():
                result = simulate(view, settings, train_end, replace(config, start_day=train_start))
                summary = result["summary"]
                eligible = (result["status"] == "success" and summary.get("through") == train_end
                            and len(result["curve"]) == spec.train_sessions
                            and summary.get("closed_trades", 0) >= spec.min_closed_trades)
                score = (summary["return"] - summary["benchmark_return"] + .5 * summary["max_drawdown"]
                         if eligible and summary.get("benchmark_return") is not None else None)
                trials.append({"profile": name, "selection_score": score, **compact(result)})
            valid = [t for t in trials if t["selection_score"] is not None]
            fold = {"train_start": train_start, "train_end": train_end, "test_start": test_start,
                    "test_end": test_end, "purge_sessions": purge, "training_trials": trials,
                    "selected_profile": None, "status": "insufficient", "test_results": []}
            if valid:
                chosen = max(valid, key=lambda t: (t["selection_score"], -PROFILES.index(t["profile"])))
                fold["selected_profile"] = chosen["profile"]
                fold["frozen_parameters"] = asdict(configs[chosen["profile"]])
                # Compute the test only after the training-only selection is frozen.
                for name, config in configs.items():
                    tested = simulate(view, settings, test_end, replace(config, start_day=test_start))
                    fold["test_results"].append({"profile": name, **compact(tested)})
                fold["status"] = "evaluated" if all(t["status"] == "success"
                    and t["summary"].get("through") == test_end for t in fold["test_results"]) else "incomplete"
            folds.append(fold)
        evaluated = [f for f in folds if f["status"] == "evaluated"]
        test_trades = sum(next(t["summary"]["closed_trades"] for t in f["test_results"]
                              if t["profile"] == f["selected_profile"]) for f in evaluated)
        result = _manifest(view, settings, cutoff, {**asdict(spec), "profiles": list(PROFILES),
            "purge_sessions": purge, "selection_score": "训练净收益−同期基准收益+0.5×负值最大回撤"})
        incomplete = any(f["status"] == "incomplete" or any(t["status"] == "incomplete"
                         for t in f["training_trials"]) for f in folds)
        result.update(status="incomplete" if incomplete else "evaluated" if len(evaluated) >= 3 and test_trades >= 30
                      and len(evaluated) == len(folds) else "insufficient",
                      session_count=len(dates), required_sessions=required, folds=folds,
                      evaluated_folds=len(evaluated), selected_test_closed_trades=test_trades,
                      message="每折独立从现金开始，训练后留出持有期+1交易日隔离区；"
                              "只按训练分数选方案。所有测试方案均展示，不依据测试改选。"
                              "至少3个完整测试窗口及30笔测试平仓交易才标记完成研究评估；不自动替换账户。")
        return result
