"""Durable default paper account: execute once on observed quotes, reconcile closes."""

import math
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import select

from .backtest import board, effective_config, fees, position_value, simulate, simulation_profile
from .config import SHANGHAI, now
from .db import bars, paper_accounts, reports, sessions, signals, trading_days
from .execution import entry_guard, entry_size, industry_of, market_gate
from .fills import make_fill
from .performance import performance


def _rows(db, source, day=None):
    return db.rows(select(paper_accounts).where(paper_accounts.c.source == source,
        paper_accounts.c.day <= day if day else True).order_by(paper_accounts.c.day.desc()).limit(1))


def _summarize(account, day):
    summary = account["summary"]
    positions = account["positions"]
    for p in positions:
        p.update(position_value(p, p["mark"]))
    market_value = sum(p["market_value"] for p in positions)
    equity = summary["available_cash"] + market_value
    summary.update(market_value=market_value, equity=equity, profit=equity - summary["initial_capital"],
        realized_profit=sum(t["profit"] for t in account["trades"]),
        unrealized_profit=sum(p["profit"] for p in positions),
        closed_trades=len(account["trades"]), open_positions=len(positions), through=day,
        fees=sum(f["fee"] for f in account["fills"]))
    summary["return"] = (equity / summary["initial_capital"] - 1) * 100
    account["as_of"] = day


def reconcile(db, settings, stored, cutoff):
    """Close prices may mark holdings and trigger tomorrow's exits, never rewrite fills."""
    account = deepcopy(stored)
    daily = account["paper_trading"]["daily_through"]
    if cutoff <= daily:
        return account
    dates = [r["day"] for r in db.rows(select(trading_days.c.day).where(
        trading_days.c.source == settings.source, trading_days.c.is_open == 1,
        trading_days.c.day > daily, trading_days.c.day <= cutoff).order_by(trading_days.c.day))]
    for day in dates:
        rows = db.rows(select(sessions).where(sessions.c.source == settings.source, sessions.c.day == day))
        if not rows:
            account["stop"] = {"day": day, "reason": "缺少完整交易日日线；保留已确认成交，暂停后续交易"}
            break
        session = rows[0]
        held = {p["code"]: p for p in account["positions"]}
        prices = {r["code"]: r for r in db.rows(select(bars).where(bars.c.source == settings.source,
            bars.c.day == day, bars.c.code.in_(held)))} if held else {}
        suspended = set(session["quality"].get("suspended", []))
        invalid = None
        for code, p in held.items():
            row, stock = prices.get(code), session["universe"].get(code, {})
            if (not row and code not in suspended or row and not math.isclose(row["adj"], p["adj"], rel_tol=1e-8)
                    or stock.get("is_st") or "ST" in stock.get("name", "").upper() or "退" in stock.get("name", "")):
                invalid = f"持仓{code}缺行情、发生复权变化或风险标记；保留已确认成交，暂停后续交易"
                break
        if invalid:
            account["stop"] = {"day": day, "reason": invalid}
            break
        for code, p in held.items():
            row = prices.get(code)
            if row:
                p["mark"] = row["close"]
                p.pop("quote_time", None)
            p["held_sessions"] += 1
            if not p["exit_due"] and row and row["close"] * row["adj"] < p["invalidation_adj"]:
                p["exit_due"] = "前一收盘失效，下一有效盘中报价退出"
            if not p["exit_due"] and p["held_sessions"] >= account["parameters"]["holding_days"]:
                p["exit_due"] = "持有期届满，下一有效盘中报价退出"
            if p["exit_due"] and not p["exit_trigger_day"]:
                p.update(exit_trigger_day=day, exit_trigger_close=row["close"] if row else None)
            if (p["exit_due"] and p["entry_day"] < day and p["exit_trigger_day"] < day
                    and not any(d["day"] == day for d in p["exit_delays"])):
                p["exit_delays"].append({"day": day, "reason": "当日未取得可模拟退出条件，保留持仓待后续报价"})
        _summarize(account, day)
        curve = account["curve"]
        origin = db.rows(select(sessions.c.benchmark).where(sessions.c.source == settings.source,
            sessions.c.day == curve[0]["day"])) if curve else []
        benchmark_return = (session["benchmark"]["close"] / origin[0]["benchmark"]["close"] - 1) * 100 if origin else None
        account["summary"]["benchmark_return"] = benchmark_return
        curve.append({"day": day, "equity": account["summary"]["equity"],
            "cash": account["summary"]["available_cash"], "positions": len(held),
            "return": account["summary"]["return"], "benchmark_return": benchmark_return})
        peak, drawdown = account["summary"]["initial_capital"], 0
        for point in curve:
            peak = max(peak, point["equity"])
            drawdown = min(drawdown, (point["equity"] / peak - 1) * 100)
        account["summary"]["max_drawdown"] = drawdown
        pause = effective_config(simulation_profile(), day).drawdown_pause
        if pause and account["summary"]["equity"] / peak - 1 <= -pause:
            account["execution_state"]["entry_paused"] = True
        account["performance"] = performance(curve, account["trades"], account["positions"], account["parameters"]["capital"])
        account["paper_trading"]["daily_through"] = day
        account["stop"] = None
    if account.get("stop"):
        account["status"] = "incomplete"
    return account


def recorded_account(db, settings, as_of=None):
    latest = db.rows(select(sessions.c.day).where(sessions.c.source == settings.source,
        sessions.c.day <= as_of if as_of else True).order_by(sessions.c.day.desc()).limit(1))
    if not latest:
        return None
    saved = _rows(db, settings.source, latest[0]["day"])
    return reconcile(db, settings, saved[0]["payload"]["account"], latest[0]["day"]) if saved else None


def _quote_error(quote, previous, day, current):
    if not quote:
        return "缺少盘中报价或停牌待核验"
    if "ST" in quote.get("name", "").upper() or "退" in quote.get("name", ""):
        return "盘中名称出现风险标记，暂停核验"
    try:
        stamp = datetime.fromisoformat(quote["quote_time"])
        if stamp.tzinfo is None:
            raise ValueError
        stamp = stamp.astimezone(SHANGHAI)
        if (stamp.strftime("%Y%m%d") != day or stamp > current + timedelta(seconds=5)
                or current - stamp > timedelta(minutes=2)
                or not ("09:30" <= stamp.strftime("%H:%M") <= "11:30"
                        or "13:00" <= stamp.strftime("%H:%M") <= "15:00")):
            return "报价过期、在未来或不在连续交易时段"
        if not all(isinstance(quote[k], (float, int)) and math.isfinite(quote[k]) and quote[k] > 0
                   for k in ("price", "open", "previous", "low", "high", "volume")):
            return "盘中报价字段无效"
        if quote["low"] > min(quote["price"], quote["open"]) or quote["high"] < max(quote["price"], quote["open"]):
            return "盘中高低价无效"
        if not previous or not math.isclose(quote["previous"], previous["close"], abs_tol=.0051, rel_tol=0):
            return "昨收尺度不一致，暂停核验除权或行情修订"
    except (ValueError, TypeError, KeyError):
        return "盘中报价字段或时间无效"
    return None


def execute_quotes(db, settings, payload, current=None):
    """Only the quote worker writes. A lock and persisted order keys prevent duplicates."""
    current = (current or now()).astimezone(SHANGHAI)
    day = current.strftime("%Y%m%d")
    try:
        captured = datetime.fromisoformat(payload["captured_at"])
        if (captured.tzinfo is None or captured > current + timedelta(seconds=5)
                or current - captured > timedelta(minutes=2)):
            return {"status": "waiting", "message": "采集批次过期或时间无效，不确认成交"}
    except (ValueError, TypeError, KeyError):
        return {"status": "waiting", "message": "采集批次时间无效，不确认成交"}
    if payload.get("day") != day or not ("09:30" <= current.strftime("%H:%M") <= "11:30"
            or "13:00" <= current.strftime("%H:%M") <= "15:00"):
        return {"status": "skipped", "message": "非当前连续交易时段，不模拟成交"}
    with db.lock("paper:" + settings.source):
        prior_reports = db.rows(select(reports).where(reports.c.source == settings.source,
            reports.c.day < day).order_by(reports.c.day.desc()).limit(1))
        if not prior_reports:
            return {"status": "waiting", "message": "缺少已发布前一交易日报告"}
        report = prior_reports[0]["payload"]
        basis = prior_reports[0]["day"]
        opened = [r["day"] for r in db.rows(select(trading_days.c.day).where(
            trading_days.c.source == settings.source, trading_days.c.is_open == 1,
            trading_days.c.day > basis, trading_days.c.day <= day).order_by(trading_days.c.day))]
        if opened != [day]:
            return {"status": "waiting", "message": "交易日历或前一交易日报告不完整，暂停成交"}
        saved = _rows(db, settings.source, day)
        if saved and saved[0]["day"] == day:
            record = deepcopy(saved[0]["payload"])
            if payload["captured_at"] <= record["processed_at"]:
                return {"status": "unchanged", "message": "该批报价已处理"}
        else:
            account = simulate(db, settings, basis)
            if account.get("stop") or account["summary"].get("through") != basis:
                return {"status": "waiting", "message": "前一交易日账户未完整核验，暂停成交"}
            record = {"day": day, "basis_day": basis, "started_at": current.isoformat(),
                "processed_at": "", "attempts": {}, "account": account}
            account["paper_trading"] = {"mode": "quote", "daily_through": basis, "day": day,
                "confirmed_at": current.isoformat(), "quote_at": None, "message": "等待有效报价"}
            account["parameters"] = asdict(simulation_profile())
            account["execution_version"] = "paper-quote-v1"
            for p in account["positions"]:
                if p.get("exit_due"):
                    p["exit_due"] = p["exit_due"].replace("下一可模拟开盘", "下一有效盘中报价")
        account = record["account"]
        config = effective_config(simulation_profile(), day)
        prior = {r["code"]: r for r in db.rows(select(bars).where(bars.c.source == settings.source, bars.c.day == basis))}
        prior_session = db.rows(select(sessions).where(sessions.c.source == settings.source, sessions.c.day == basis))
        universe = prior_session[0]["universe"] if prior_session else {}
        quotes = payload["quotes"]
        errors = {p["code"]: error for p in account["positions"]
            if (error := _quote_error(quotes.get(p["code"]), prior.get(p["code"]), day, current))}
        count_before = len(account["fills"])
        decisions = {}
        if errors:
            decisions.update(errors)
        else:
            cash = account["summary"]["available_cash"]
            positions = {p["code"]: p for p in account["positions"]}
            for code, p in list(positions.items()):
                q = quotes[code]
                p.update(mark=q["price"], quote_time=q["quote_time"])
                if not p["exit_due"] or p["entry_day"] >= day:
                    continue
                limit, _ = board(code)
                price = q["price"] * (1 - config.slippage_bps / 10000)
                if (limit is None or q["price"] <= prior[code]["close"] * (1 - limit + .001)
                        or price < q["low"] or p["shares"] > prior[code]["volume"] * config.participation):
                    decisions[code] = "退出等待：跌停、滑点超出已观察范围或前日量能不足"
                    continue
                gross = p["shares"] * price
                cost = fees(gross, config, sell=True)
                cash += gross - cost
                trade = {**p, "exit_day": day, "exit_price": price, "exit_open": q["open"],
                    "exit_reference_price": q["price"], "exit_time": q["quote_time"],
                    "exit_confirmed_at": current.isoformat(), "exit_basis": "quote",
                    "exit_fee": cost, "fees": p["entry_fee"] + cost, "proceeds": gross - cost,
                    "profit": gross - cost - p["spent"], "exit_reason": p["exit_due"],
                    "net_return": ((gross - cost) / p["spent"] - 1) * 100}
                account["trades"].append(trade)
                account["fills"].insert(0, make_fill(trade, settings.source, "sell",
                    sequence=len(account["fills"]) + 1, cash_after=cash))
                del positions[code]
            candidates = db.rows(select(signals).where(signals.c.source == settings.source, signals.c.day == basis))
            for signal in sorted(candidates, key=lambda s: (-s["payload"].get("relative20", 0), s["code"], s["strategy"])):
                code, item = signal["code"], signal["payload"]
                key = f"{basis}|{code}|{signal['strategy']}"
                if key in record["attempts"]:
                    continue
                try:
                    stamp = datetime.fromisoformat(item.get("published_at") or report.get("generated_at"))
                    timely = stamp.tzinfo is not None and basis <= stamp.astimezone(SHANGHAI).strftime("%Y%m%d") <= day
                    timely = timely and stamp < current.replace(hour=9, minute=30, second=0, microsecond=0)
                except (ValueError, TypeError):
                    timely = False
                if not timely or item.get("reconstructed"):
                    record["attempts"][key] = "排除事后发布、时间不明或重建信号"
                    continue
                if code in positions or len(positions) >= config.max_positions:
                    decisions[code] = "已有持仓或达到持仓上限，等待后续刷新"
                    continue
                stock = universe.get(code, {})
                limit, lot = board(code)
                if limit is None or stock.get("is_st") or "ST" in stock.get("name", "").upper() or "退" in stock.get("name", ""):
                    record["attempts"][key] = "不支持板块或风险标记"
                    continue
                q, previous = quotes.get(code), prior.get(code)
                error = _quote_error(q, previous, day, current)
                if error:
                    decisions[code] = error
                    continue
                gate = market_gate(report, config) or entry_guard(config, q["open"], previous, item)
                if gate:
                    record["attempts"][key] = gate
                    continue
                gate = entry_guard(config, q["price"], previous, item)
                if account["execution_state"].get("entry_paused") or gate:
                    decisions[code] = gate or "组合回撤暂停新增仓位"
                    continue
                price = q["price"] * (1 + config.slippage_bps / 10000)
                if q["price"] >= previous["close"] * (1 + limit - .001) or price > q["high"]:
                    decisions[code] = "入场等待：接近涨停或滑点超出已观察范围"
                    continue
                equity = cash + sum(p["shares"] * p["mark"] for p in positions.values())
                industry = industry_of(item)
                exposure = sum(p["shares"] * p["mark"] for p in positions.values() if p["industry"] == industry)
                sizing = entry_size(config, cash=cash, equity=equity, price=price, previous=previous,
                    payload=item, lot=lot, industry_value=exposure)
                if sizing["reason"]:
                    decisions[code] = sizing["reason"]
                    continue
                shares = sizing["shares"]
                cost = fees(shares * price, config)
                spent = shares * price + cost
                cash -= spent
                p = {"code": code, "name": item["name"], "strategy": signal["strategy"],
                    "shares": shares, "entry_day": day, "entry_index": len(account["curve"]),
                    "entry_price": price, "entry_fee": cost, "spent": spent, "adj": previous["adj"],
                    "mark": q["price"], "quote_time": q["quote_time"], "entry_open": q["open"],
                    "entry_reference_price": q["price"], "entry_time": q["quote_time"],
                    "entry_confirmed_at": current.isoformat(), "entry_basis": "quote",
                    "published_at": stamp.isoformat(), "label": item.get("label", signal["strategy"]),
                    "rule_version": item.get("rule_version", ""), "entry_reasons": item.get("reasons", []),
                    "entry_risk": sizing, "industry": industry, "signal_day": basis,
                    "invalidation_price": item["invalidation_adj"] / previous["adj"],
                    "invalidation_adj": item["invalidation_adj"], "exit_due": None, "exit_trigger_day": None,
                    "exit_trigger_close": None, "exit_delays": [], "held_sessions": 0}
                positions[code] = p
                account["fills"].insert(0, make_fill(p, settings.source, "buy", sequence=len(account["fills"]) + 1, cash_after=cash))
                record["attempts"][key] = "filled"
            account["positions"] = list(positions.values())
            account["summary"]["available_cash"] = cash
            _summarize(account, day)
            benchmark = quotes.get("000300.SH")
            origin = db.rows(select(sessions.c.benchmark).where(sessions.c.source == settings.source,
                sessions.c.day == account["curve"][0]["day"])) if account["curve"] else []
            if benchmark and origin:
                previous_benchmark = {"close": prior_session[0]["benchmark"]["close"]}
                if not _quote_error(benchmark, previous_benchmark, day, current):
                    account["summary"]["benchmark_return"] = (benchmark["price"] / origin[0]["benchmark"]["close"] - 1) * 100
                else:
                    account["summary"]["benchmark_return"] = None
            account["paper_trading"]["quote_at"] = payload["captured_at"]
        record["processed_at"] = payload["captured_at"]
        decisions = {**{key.split("|")[1]: reason for key, reason in record["attempts"].items()
                        if reason != "filled"}, **decisions}
        account["paper_trading"].update(confirmed_at=current.isoformat(), decisions=decisions,
            status="waiting" if errors else "success",
            message="部分持仓报价待核验，本批暂停交易并保留已确认成交。" if errors else
                    "每10分钟按有效报价确认模拟买卖；已确认成交保留，收盘后仅核验日线退出条件。")
        account["status"] = "live"
        account["method"] = ("启用盘中执行后的默认模拟账户，每10分钟按当时有效报价加减滑点处理先卖后买，"
            "保存报价及确认时间；不回填错过的开盘成交。买入后最早下一交易日卖出，"
            "失效和持有期仍按收盘判断；已确认买卖不会被后续报价或收盘回放撤销。")
        with db.engine.begin() as connection:
            db.upsert(connection, paper_accounts, {"source": settings.source, "day": day, "payload": record})
        return {"status": "waiting" if errors else "success", "new_fills": len(account["fills"]) - count_before,
                "message": account["paper_trading"]["message"], "decisions": decisions}


def current_account(db, settings, report_day, current=None):
    """Read-only view; GET/export never creates trades or rewrites report archives."""
    current = current or now()
    rows = _rows(db, settings.source, current.strftime("%Y%m%d"))
    if not rows or rows[0]["day"] < report_day:
        return None
    account = reconcile(db, settings, rows[0]["payload"]["account"], report_day)
    if account["paper_trading"]["daily_through"] >= account["summary"]["through"]:
        account["valuation"] = {"status": "daily", "interval_minutes": settings.portfolio_interval_minutes,
            "message": "收盘日线已核验；盘中确认的成交、股数和资金保留。"}
        if account["paper_trading"]["day"] > account["paper_trading"]["daily_through"]:
            account["valuation"].update(status="unavailable", message=account["paper_trading"]["message"])
        return account
    account["valuation"] = {"status": "available", "interval_minutes": settings.portfolio_interval_minutes,
        "day": account["summary"]["through"], "captured_at": account["paper_trading"].get("quote_at"),
        "quote_start": min((p.get("quote_time", "") for p in account["positions"]), default=account["paper_trading"].get("quote_at") or ""),
        "quote_end": max((p.get("quote_time", "") for p in account["positions"]), default=account["paper_trading"].get("quote_at") or ""),
        "positions": deepcopy(account["positions"]), "summary": deepcopy(account["summary"]),
        "message": account["paper_trading"]["message"] + " 日线绩效截至"
                   + account["paper_trading"]["daily_through"] + "。"}
    at = account["paper_trading"].get("quote_at")
    freshness_at = current
    if "11:30" < current.strftime("%H:%M") < "13:00":
        freshness_at = current.replace(hour=11, minute=30, second=0, microsecond=0)
    elif current.strftime("%H:%M") > "15:00":
        freshness_at = current.replace(hour=15, minute=0, second=0, microsecond=0)
    if not at or freshness_at - datetime.fromisoformat(at) > timedelta(minutes=settings.portfolio_interval_minutes + 5):
        account["valuation"].update(status="stale", message="行情已过期，保留已确认成交与最近一次估值。")
    if account.get("stop"):
        account["valuation"].update(status="unavailable", message=account["stop"]["reason"])
    return account


def annotate_plan(plan, account):
    """Overlay confirmations for today's view without changing the frozen plan."""
    if not plan or not account.get("paper_trading") or plan.get("target_day") != account["paper_trading"]["day"]:
        return plan
    plan = deepcopy(plan)
    fills = [f for f in account["fills"] if f["day"] == plan["target_day"]]
    decisions = account["paper_trading"].get("decisions", {})
    for item in plan.get("exits", []):
        if item["code"] in decisions:
            item["reason"] += "；本次刷新：" + decisions[item["code"]]
        fill = next((f for f in fills if f["side"] == "sell" and f["code"] == item["code"]
                     and f["entry_day"] == item["entry_day"]), None)
        if fill:
            item["execution"] = fill
    for item in plan.get("entries", []):
        if item["code"] in decisions:
            item["reason"] = "本次刷新：" + decisions[item["code"]]
            if any(word in decisions[item["code"]] for word in ("取消", "排除", "不支持")):
                item["status"] = "watch"
        fill = next((f for f in fills if f["side"] == "buy" and f["code"] == item["code"]
                     and f["strategy"] == item["strategy"] and f["signal_day"] == item["signal_day"]), None)
        if fill:
            item.update(status="filled", execution=fill, reason="已按有效盘中报价确认模拟买入")
    plan["counts"] = {**plan.get("counts", {}),
        **{status: sum(i["status"] == status for i in plan.get("entries", []))
           for status in ("planned", "conditional", "watch")},
        "exits": sum(not i.get("execution") for i in plan.get("exits", []))}
    plan["execution"] = {"confirmed_at": account["paper_trading"]["confirmed_at"],
        "buys": sum(f["side"] == "buy" for f in fills), "sells": sum(f["side"] == "sell" for f in fills)}
    return plan
