"""Cached intraday marks for confirmed simulated holdings, separate from executions."""

import math
from datetime import datetime, timedelta

from sqlalchemy import select

from .backtest import position_value
from .config import SHANGHAI, now
from .db import bars, paper_accounts, sessions, signals, trading_days
from .operations import state
from .performance import account_pnl
from .providers import DataError, make_provider


def market_window(current, interval=10):
    """Aligned exchange-session buckets, plus one final post-close collection."""
    current = current.astimezone(SHANGHAI)
    minute = current.hour * 60 + current.minute
    for start, end in [(570, 690), (780, 900)]:
        if start <= minute <= end:
            bucket = start + (minute - start) // interval * interval
            return current.strftime("%Y%m%d") + f"-{bucket // 60:02d}{bucket % 60:02d}"
    if 905 <= minute < 915:
        return current.strftime("%Y%m%d") + "-close"
    return None


def portfolio_due(db, settings, current):
    if settings.source == "demo" or current.weekday() >= 5:
        return False
    window = market_window(current, settings.portfolio_interval_minutes)
    calendar = db.rows(select(trading_days.c.is_open).where(trading_days.c.source == settings.source,
                                                           trading_days.c.day == current.strftime("%Y%m%d")))
    if calendar and not calendar[0]["is_open"]:
        return False
    return bool(window and state(db, settings, "portfolio_refresh").get("window") != window)


def refresh_portfolio(db, settings, feed=None, provider=None, current=None):
    from .intraday import SinaQuotes

    current = current or now()
    day = current.strftime("%Y%m%d")
    window = market_window(current, settings.portfolio_interval_minutes)
    if not window or settings.source == "demo":
        return {"status": "skipped"}
    attempt = {"at": current.isoformat(), "window": window, "status": "running"}
    db.save_state(settings.source, "portfolio_refresh", attempt)
    identifier = db.start_run(settings.source, day, "valuation")
    active_provider = provider
    try:
        calendar = db.rows(select(trading_days).where(trading_days.c.source == settings.source,
                                                     trading_days.c.day == day))
        if not calendar:
            active_provider = active_provider or make_provider(settings, db)
            start = (current - timedelta(days=14)).strftime("%Y%m%d")
            opened = set(active_provider.calendar(start, day))
            with db.engine.begin() as connection:
                for offset in range(15):
                    date = (current - timedelta(days=offset)).strftime("%Y%m%d")
                    db.upsert(connection, trading_days, {"source": settings.source, "day": date,
                                                         "is_open": int(date in opened)})
            is_open = day in opened
        else:
            is_open = bool(calendar[0]["is_open"])
        if not is_open:
            attempt["status"] = "skipped"
            db.finish_run(identifier, "skipped", "非交易日，不更新模拟持仓估值")
            return attempt
        latest = db.rows(select(sessions.c.day).where(sessions.c.source == settings.source)
                         .order_by(sessions.c.day.desc()).limit(1))
        if not latest:
            raise DataError("请先初始化日线行情和模拟持仓")
        codes = {r["code"] for r in db.rows(select(signals.c.code).where(
            signals.c.source == settings.source, signals.c.day <= latest[0]["day"]).distinct())}
        from .candidate_pool import active_candidates

        codes.update(r['code'] for r in active_candidates(db, settings, day, latest[0]['day'], current))
        accounts = db.rows(select(paper_accounts.c.payload).where(paper_accounts.c.source == settings.source,
            paper_accounts.c.day <= day).order_by(paper_accounts.c.day.desc()).limit(1))
        if accounts:
            codes.update(p['code'] for p in accounts[0]['payload']['account']['positions'])
        quotes, missing = (feed or SinaQuotes(settings)).fetch(codes, day, "portfolio")
        if "000300.SH" not in quotes:
            raise DataError("盘中沪深300报价无法核验，保留上次估值")
        payload = {"day": day, "captured_at": current.isoformat(), "quotes": quotes,
                   "missing": missing, "provider": "新浪免费实时行情"}
        db.save_state(settings.source, "portfolio_quotes", payload)
        from .paper_trading import execute_quotes

        execution = execute_quotes(db, settings, payload, current)
        attempt["status"] = "success"
        db.finish_run(identifier, "success", f"盘中报价已更新：{len(quotes)}个代码；"
                      f"新增{execution.get('new_fills', 0)}笔模拟成交；{execution['status']}")
        return {**attempt, "count": len(quotes), "execution": execution}
    except Exception:
        attempt["status"] = "failed"
        db.finish_run(identifier, "failed", "盘中报价更新失败，保留上次行情；未改写日线或模拟成交")
        raise
    finally:
        db.save_state(settings.source, "portfolio_refresh", attempt)
        if provider is None and active_provider:
            active_provider.close()


def value_portfolio(db, settings, simulation, current=None):
    """All-or-nothing valuation. Historical summaries, cash and trades remain immutable."""
    current = current or now()
    summary = simulation["summary"]
    result = {"status": "unavailable", "interval_minutes": settings.portfolio_interval_minutes,
              "basis_day": summary.get("through"),
              "message": "尚无可用盘中估值，显示最近已核验的日线账户。"}
    if simulation.get("stop") or not summary.get("through"):
        result["message"] = "模拟缺少完整行情，盘中估值暂停；显示最后有效日线账户。"
        return result
    saved = state(db, settings, "portfolio_quotes")
    if not saved:
        return result
    day = saved["day"]
    if day <= summary["through"]:
        result.update(status="daily", message="该日模拟账户已用完整日线核验。")
        return result
    # Do not bridge missing trading sessions or silently apply current quotes to an old archive.
    opened = db.rows(select(trading_days.c.day).where(trading_days.c.source == settings.source,
                  trading_days.c.is_open == 1, trading_days.c.day > summary["through"], trading_days.c.day <= day)
                  .order_by(trading_days.c.day))
    if [r["day"] for r in opened] != [day]:
        result["message"] = "日线账户与报价之间缺少完整交易日数据，盘中估值暂停。"
        return result
    quotes = saved["quotes"]
    positions = []
    timestamps = []
    for position in simulation["positions"]:
        code = position["code"]
        quote = quotes.get(code)
        prior = db.rows(select(bars.c.close).where(bars.c.source == settings.source, bars.c.code == code,
                                                  bars.c.day == summary["through"]))
        if not quote or not prior:
            result["message"] = f"持仓{code}缺少有效报价，暂保留完整的日线估值。"
            return result
        # Changed previous-close scale can indicate ex-rights; shares/dividends cannot be inferred.
        if not math.isclose(quote["previous"], prior[0]["close"], abs_tol=.0051, rel_tol=0):
            result["message"] = f"持仓{code}昨收与已核验日线不一致，需核验除权或数据修订。"
            return result
        positions.append({**position, "mark": quote["price"], "quote_time": quote["quote_time"],
                          **position_value(position, quote["price"])})
        timestamps.append(quote["quote_time"])
    benchmark = quotes.get("000300.SH")
    if not benchmark:
        return result
    timestamps.append(benchmark["quote_time"])
    try:
        stamps = [datetime.fromisoformat(s).astimezone(SHANGHAI) for s in timestamps]
        if any(t.strftime("%Y%m%d") != day or t > current + timedelta(seconds=5) for t in stamps):
            return result
    except (ValueError, TypeError):
        return result
    freshness_at = current
    clock = current.strftime("%H:%M")
    if "11:30" < clock < "13:00":
        freshness_at = current.replace(hour=11, minute=30, second=0, microsecond=0)
    elif clock > "15:00":
        freshness_at = current.replace(hour=15, minute=0, second=0, microsecond=0)
    stale = day != current.strftime("%Y%m%d") or (
        (freshness_at - min(stamps)).total_seconds() > (settings.portfolio_interval_minutes + 5) * 60)
    market_value = sum(p["shares"] * p["mark"] for p in positions)
    equity = summary["available_cash"] + market_value
    origin = db.rows(select(sessions.c.benchmark).where(sessions.c.source == settings.source,
                                                       sessions.c.day == simulation["curve"][0]["day"]))
    benchmark_return = (benchmark["price"] / origin[0]["benchmark"]["close"] - 1) * 100 if origin else None
    result.update(status="stale" if stale else "available", day=day, captured_at=saved["captured_at"],
                  quote_start=min(timestamps), quote_end=max(timestamps), positions=positions,
                  summary={**summary, "market_value": market_value, "equity": equity,
                           "profit": equity - summary["initial_capital"],
                           "unrealized_profit": sum(p["profit"] for p in positions),
                           "return": (equity / summary["initial_capital"] - 1) * 100,
                           "benchmark_return": benchmark_return, "through": day},
                  message=("行情已过期，显示最后保存的盘中估值。" if stale else "")
                  + f"已确认持仓基于{summary['through']}；盘中仅更新估值，现金与成交仍按日线核验。")
    result["summary"].update(account_pnl(result["summary"], simulation["curve"]))
    return result
