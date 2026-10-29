"""Two isolated cash accounts, executed atomically on future observed quote batches."""

import hashlib
import uuid
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from .attribution import build_attribution
from .backtest import Simulation, simulation_profile
from .config import SHANGHAI, now
from .db import forward_accounts, forward_quotes, forward_studies
from .entry_policy import VERSION as ENTRY_VERSION
from .performance import performance

ARMS = {"control": "对照组 · 现有入场", "filtered": "过滤组 · 弱市暂停新增"}
METHOD = ("两组各以10万元现金从启用后的新报价起步，不继承默认账户持仓、不回填成交。"
          "唯一参数差异为市场过滤：前一已发布报告站上MA20占比至少40%，且沪深300近20日涨幅为正才新增买入。"
          "其余风控、候选和已保存AI意见相同；过滤只限制新增，退出继续执行。"
          "复用默认账户的AI观察范围，未覆盖的研究持仓仅执行固定退出保护。"
          "这是前向观察，未配平持仓敞口，结果不等于已证明的因果收益；不自动替换默认账户。")


def fingerprint():
    digest = hashlib.sha256()
    for name in ("paper_trading.py", "backtest.py", "entry_policy.py", "exit_policy.py", "execution.py",
                 "decision_ai.py", "candidate_pool.py", "forward_study.py", "fills.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def inputs(settings):
    return {"ai_enabled": settings.ai_trading_enabled, "model": settings.ai_model,
            "interval_minutes": settings.portfolio_interval_minutes,
            "ai_interval_minutes": settings.realtime_interval_minutes}


def latest(db, source, as_of=None):
    query = select(forward_studies).where(forward_studies.c.source == source)
    if as_of:
        query = query.where(forward_studies.c.activated_at <= f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T23:59:59+08:00")
    rows = db.rows(query.order_by(forward_studies.c.activated_at.desc()).limit(1))
    return rows[0] if rows else None


def start(db, settings, current=None):
    current = (current or now()).astimezone(SHANGHAI)
    with db.lock("forward:" + settings.source):
        existing = latest(db, settings.source)
        if existing:
            return {"status": "exists", "id": existing["id"], "activated_at": existing["activated_at"]}
        config = simulation_profile("guarded")
        record = {"source": settings.source, "id": uuid.uuid4().hex,
                  "activated_at": current.isoformat(), "payload": {
                      "version": "market-filter-v1", "status": "waiting", "code_fingerprint": fingerprint(),
                      "inputs": inputs(settings), "parameters": asdict(config), "batches": 0,
                      "last_batch": None, "blocked": [], "message": "已启用，等待下一批有效交易时段报价"}}
        with db.engine.begin() as connection:
            db.upsert(connection, forward_studies, record)
        return {"status": "started", "id": record["id"], "activated_at": record["activated_at"]}


class Scope:
    def __init__(self, study, arm):
        self.study, self.arm = study, arm
        self.identity = f"study:{study['id']}:{arm}"
        self.config = Simulation(**{**study["payload"]["parameters"], "market_filter": arm == "filtered"})

    def where(self, table, source):
        return (table.c.source == source, table.c.study_id == self.study["id"], table.c.arm == self.arm)

    def records(self, db, source, day):
        return db.rows(select(forward_accounts).where(*self.where(forward_accounts, source),
            forward_accounts.c.day <= day).order_by(forward_accounts.c.day.desc()).limit(1))

    def quotes(self, db, source, day, captured):
        return db.rows(select(forward_quotes).where(*self.where(forward_quotes, source),
            forward_quotes.c.day == day, forward_quotes.c.captured_at < captured)
            .order_by(forward_quotes.c.captured_at.desc()).limit(1))

    def account(self, db, settings, saved, basis):
        from .paper_trading import reconcile

        if saved:
            return reconcile(db, settings, saved[0]["payload"]["account"], basis, config=self.config)
        capital = self.config.capital
        return {"status": "success", "stop": None, "as_of": basis, "positions": [], "trades": [], "fills": [],
                "curve": [], "periods": [], "skipped": {}, "execution_state": {"entry_paused": False},
                "parameters": asdict(self.config), "performance": performance([], [], [], capital),
                "summary": {"initial_capital": capital, "available_cash": capital, "equity": capital,
                            "market_value": 0, "profit": 0, "return": 0, "max_drawdown": 0, "through": basis}}

    def save(self, db, connection, source, record, quotes):
        keys = {"source": source, "study_id": self.study["id"], "arm": self.arm, "day": record["day"]}
        account = record["account"]
        account["method"] = METHOD
        stats = account.setdefault("study_observations", {"count": 0, "exposure_sum": 0, "max_drawdown": 0})
        if account["paper_trading"].get("status") != "partial":
            equity = account["summary"]["equity"]
            stats["count"] += 1
            stats["exposure_sum"] += account["summary"]["market_value"] / equity if equity > 0 else 0
            peak = account["execution_state"]["quote_peak_equity"]
            stats["max_drawdown"] = min(stats["max_drawdown"], (equity / peak - 1) * 100)
        db.upsert(connection, forward_accounts, {**keys, "payload": record})
        db.upsert(connection, forward_quotes, {**keys, "captured_at": record["processed_at"],
            "payload": {"quotes": quotes, "version": ENTRY_VERSION}})


class TransactionDB:
    """Reuse one transaction for both executions; outer study lock owns serialization."""
    def __init__(self, db, connection):
        self.connection = connection
        self.engine = SimpleNamespace(begin=lambda: nullcontext(connection), dialect=connection.dialect)
        self.upsert = db.upsert

    def rows(self, query):
        return [dict(r) for r in self.connection.execute(query).mappings()]

    def lock(self, *_args, **_kwargs):
        return nullcontext()


def process(db, settings, payload, current=None):
    """A failed pair retains both previous accounts and exposes a retryable status."""
    try:
        return _process(db, settings, payload, current)
    except Exception:
        with db.lock("forward:" + settings.source):
            study = latest(db, settings.source)
            if study:
                study["payload"].update(status="waiting", message="本批对照未完成，两组均保留原记录，等待后续有效报价")
                with db.engine.begin() as connection:
                    db.upsert(connection, forward_studies, study)
        raise


def _process(db, settings, payload, current=None):
    from .decision_ai import decision_context
    from .paper_trading import execute_quotes

    current = (current or now()).astimezone(SHANGHAI)
    with db.lock("forward:" + settings.source):
        study = latest(db, settings.source)
        if not study:
            return {"status": "disabled"}
        state = study["payload"]
        captured = datetime.fromisoformat(payload["captured_at"])
        if captured.tzinfo is None or captured <= datetime.fromisoformat(study["activated_at"]):
            return {"status": "skipped", "message": "启用前报价不参与前向对照"}
        if state["last_batch"] and captured <= datetime.fromisoformat(state["last_batch"]):
            return {"status": "unchanged"}
        if not ("09:30" <= current.strftime("%H:%M") <= "11:30"
                or "13:00" <= current.strftime("%H:%M") <= "15:00"):
            return {"status": "skipped"}
        if state["code_fingerprint"] != fingerprint() or state["inputs"] != inputs(settings):
            state.update(status="paused", message="执行代码或AI/采集设置已变化，对照暂停；不可混合版本判优")
            with db.engine.begin() as connection:
                db.upsert(connection, forward_studies, study)
            return {"status": "paused"}
        with db.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
            else:
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            view = TransactionDB(db, connection)
            saved_ai = decision_context(view, settings, current)
            results, accounts = {}, {}
            for arm in ARMS:
                scope = Scope(study, arm)
                results[arm] = execute_quotes(view, settings, payload, current, scope=scope, saved_ai=saved_ai)
                if results[arm]["status"] not in {"success", "partial"}:
                    # Raising rolls back the first arm too; do not create unmatched observations.
                    raise ValueError("前向对照等待完整输入：" + results[arm].get("message", "行情未就绪"))
                accounts[arm] = scope.records(view, settings.source, payload["day"])[0]["payload"]["account"]
            control, filtered = accounts["control"], accounts["filtered"]
            new_buys = [f for f in control["fills"][:results["control"]["new_fills"]] if f["side"] == "buy"]
            for fill in new_buys:
                reason = filtered["paper_trading"]["decisions"].get(fill["code"], "")
                if reason in {"市场趋势或广度偏弱，暂停新增仓位", "缺少当时市场状态，暂停新增仓位"}:
                    state["blocked"].append({"id": fill["round_trip_id"], "code": fill["code"],
                        "name": fill["name"], "entry_day": fill["entry_day"], "strategy": fill["strategy"],
                        "at": payload["captured_at"], "reason": reason})
            state.update(status="running", message="两组已处理同一批有效报价", last_batch=payload["captured_at"],
                         batches=state["batches"] + 1)
            db.upsert(connection, forward_studies, study)
        return {"status": "success", "arms": results}


def held_codes(db, settings, day):
    study = latest(db, settings.source, day)
    if not study:
        return set()
    return {p["code"] for arm in ARMS for r in Scope(study, arm).records(db, settings.source, day)
            for p in r["payload"]["account"]["positions"]}


def study_view(db, settings, as_of=None):
    from .fills import make_fill
    from .paper_trading import reconcile

    study = latest(db, settings.source, as_of)
    if not study:
        return {"status": "not_started", "method": METHOD, "arms": [], "message": "尚未启用独立前向对照"}
    state = study["payload"]
    day = as_of or now().strftime("%Y%m%d")
    from .db import sessions

    daily = db.rows(select(sessions.c.day).where(sessions.c.source == settings.source, sessions.c.day <= day)
                    .order_by(sessions.c.day.desc()).limit(1))
    result = {"id": study["id"], "version": state["version"], "status": state["status"],
              "activated_at": study["activated_at"], "last_batch": state["last_batch"],
              "batches": state["batches"], "message": state["message"], "method": METHOD, "arms": []}
    control = None
    for arm, label in ARMS.items():
        scope = Scope(study, arm)
        rows = scope.records(db, settings.source, day)
        if not rows:
            result["arms"].append({"arm": arm, "label": label, "summary": {
                "initial_capital": scope.config.capital, "equity": scope.config.capital,
                "profit": 0, "return": 0, "available_cash": scope.config.capital, "market_value": 0,
                "closed_trades": 0, "open_positions": 0, "fees": 0}, "processed_at": None,
                "parameters": asdict(scope.config), "status": "waiting", "stop": None, "curve": []})
            continue
        record = rows[0]["payload"]
        account = reconcile(db, settings, record["account"], daily[0]["day"], config=scope.config) if daily else record["account"]
        attribution = build_attribution(account)
        result["arms"].append({"arm": arm, "label": label, "summary": account["summary"],
            "parameters": account["parameters"], "attribution": attribution, "processed_at": record["processed_at"],
            "stop": account.get("stop"), "status": account["paper_trading"].get("status"),
            "curve": account["curve"], "observations": account.get("study_observations")})
        if arm == "control":
            control = account
    blocked = []
    for item in state["blocked"]:
        if item["entry_day"] > day:
            continue
        matches = [(t, "closed") for t in (control or {}).get("trades", [])]
        matches += [(t, "open") for t in (control or {}).get("positions", [])]
        match = next(((t, status) for t, status in matches
                      if make_fill(t, Scope(study, "control").identity, "buy")["round_trip_id"] == item["id"]), None)
        blocked.append({**item, "status": match[1] if match else "unavailable",
                        "profit": match[0]["profit"] if match else None})
    result["blocked"] = {"count": len(blocked), "items": blocked[-100:],
        "realized": sum(r["profit"] for r in blocked if r["status"] == "closed"),
        "unrealized": sum(r["profit"] for r in blocked if r["status"] == "open"),
        "missing": sum(r["status"] == "unavailable" for r in blocked)}
    result["comparable"] = (len(result["arms"]) == 2 and all(
        r["processed_at"] and not r["stop"] and r["status"] != "partial" for r in result["arms"])
        and result["arms"][0]["processed_at"] == result["arms"][1]["processed_at"])
    result["difference"] = (result["arms"][1]["summary"]["equity"] - result["arms"][0]["summary"]["equity"]
                            if result["comparable"] else None)
    if as_of:
        stamps = [r["processed_at"] for r in result["arms"] if r["processed_at"]]
        count = db.rows(select(forward_quotes.c.captured_at).where(forward_quotes.c.source == settings.source,
            forward_quotes.c.study_id == study["id"], forward_quotes.c.arm == "control", forward_quotes.c.day <= day))
        result.update(status="snapshot", message="历史前向对照快照，不含之后的报价与成交",
                      last_batch=max(stamps) if stamps else None, batches=len(count))
    return result
