"""Publish a small public portfolio feed without uploading report images."""

import base64
import json
import re
import tempfile
from pathlib import Path

from .backtest import simulate
from .config import now
from .operations import state
from .pages import digest, gh
from .portfolio import value_portfolio
from .providers import DataError

BRANCH = "pages-live"
FILENAME = "portfolio.json"
VERSION = "portfolio-live-v1"


def live_url(settings):
    repository = settings.pages_repository
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        return ""
    return f"https://raw.githubusercontent.com/{repository}/{BRANCH}/{FILENAME}"


def public_portfolio(db, settings):
    report = db.latest_report(settings.source)
    if not report:
        raise DataError("尚无已发布日线账户，不发布空持仓数据")
    simulation = simulate(db, settings, report["day"])
    simulation["valuation"] = value_portfolio(db, settings, simulation)
    valuation = simulation["valuation"]
    from .trade_plan import build_trade_plan

    plan = report.get("trade_plan") or build_trade_plan(db, settings, report)
    return {"version": VERSION, "source": settings.source, "report_day": report["day"],
            "data_at": valuation.get("captured_at", report.get("generated_at", "")),
            "backtest": simulation, "trade_plan": plan}


def publish_portfolio(db, settings):
    if not settings.pages_enabled:
        return {"status": "disabled"}
    repository = settings.pages_repository
    if not live_url(settings):
        raise DataError("公开持仓同步需要有效的PAGES_REPOSITORY")
    payload = public_portfolio(db, settings)
    content = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
    fingerprint = digest(content)
    previous = state(db, settings, "portfolio_publish")
    if (previous.get("status") == "success" and previous.get("repository") == repository
            and previous.get("fingerprint") == fingerprint):
        return {**previous, "status": "unchanged"}
    timestamp = now().isoformat()
    record = {"at": timestamp, "repository": repository, "quote_at": payload["data_at"]}
    try:
        ref = gh("api", f"repos/{repository}/git/ref/heads/{BRANCH}", check=False)
        if ref.returncode:
            main = json.loads(gh("api", f"repos/{repository}/git/ref/heads/main").stdout)
            gh("api", "--method", "POST", f"repos/{repository}/git/refs",
               "-f", f"ref=refs/heads/{BRANCH}", "-f", f"sha={main['object']['sha']}")
        existing = gh("api", f"repos/{repository}/contents/{FILENAME}?ref={BRANCH}", check=False)
        payload["published_at"] = timestamp
        request = {"branch": BRANCH, "message": f"更新公开模拟持仓 {payload['data_at']}",
                   "content": base64.b64encode(json.dumps(payload, ensure_ascii=False,
                                                          allow_nan=False).encode()).decode()}
        if not existing.returncode:
            request["sha"] = json.loads(existing.stdout)["sha"]
        with tempfile.TemporaryDirectory(prefix="portfolio-publish-") as folder:
            path = Path(folder) / "request.json"
            path.write_text(json.dumps(request))
            gh("api", "--method", "PUT", f"repos/{repository}/contents/{FILENAME}", "--input", str(path))
        record.update(status="success", fingerprint=fingerprint, bytes=len(content), url=live_url(settings))
        db.save_state(settings.source, "portfolio_publish", record)
        return record
    except Exception:
        db.save_state(settings.source, "portfolio_publish", {**record, "status": "failed",
                      "message": "轻量持仓同步失败，网页保留已保存数据并显示时间"})
        raise
