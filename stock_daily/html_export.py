"""Standalone, read-only HTML snapshots with local assets and no credentials."""

import json
import os
import secrets
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from .config import now
from .db import bars
from .providers import DataError, parse_day
from .service import dashboard, tracking
from .static_report import daily_view, template_helpers

ASSETS = Path(__file__).parent


def script_json(value):
    # JSON is placed inside a script data block; HTML's parser recognizes </script>
    # even inside a quoted JSON string, so escape all HTML-significant characters.
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    for character, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                               ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        result = result.replace(character, escaped)
    return result


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = file.name
            file.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def daily_payload(db, settings, day=None, public=False, portfolio=False):
    if day:
        parse_day(day)
    data = dashboard(db, settings, day)
    report = data["report"]
    if day and report is None:
        raise DataError(f"{settings.source} 没有 {day} 的已发布报告；未替换输出文件")
    # Export one report date. Tracking is a separate snapshot through its stated cutoff.
    data["dates"] = [report["day"]] if report else []
    if public:
        data["watchlist"] = []
        data["runs"] = []
        data.pop("email", None)
        data.pop("health", None)
        data.pop("ai_configured", None)
    histories = {}
    if report:
        codes = {c["code"] for c in report.get("all_candidates", report["candidates"])}
        for code in sorted(codes):
            rows = db.rows(
                select(bars.c.day, bars.c.close, bars.c.adj)
                .where(bars.c.source == settings.source, bars.c.code == code, bars.c.day <= report["day"])
                .order_by(bars.c.day.desc()).limit(65)
            )
            histories[code] = list(reversed(rows))
    observations = tracking(db, settings, as_of=report["day"] if report else None)
    if report:
        observations["method"] += f" 观察数据截止于 {report['day']}。"
    from .backtest import simulate

    simulation = simulate(db, settings, report["day"]) if report else None
    if portfolio and simulation:
        from .paper_trading import annotate_plan, current_account
        from .portfolio import value_portfolio

        simulation = current_account(db, settings, report["day"]) or simulation
        from .decision_ai import configured_policy, latest_analysis

        simulation['market_analysis'] = latest_analysis(db, settings, now())
        simulation['decision_policy'] = configured_policy(settings)
        if "valuation" not in simulation:
            simulation["valuation"] = value_portfolio(db, settings, simulation)
        data["report"]["trade_plan"] = annotate_plan(data["report"].get("trade_plan"), simulation)
    from .attribution import build_attribution
    from .forward_study import study_view
    from .live_publish import live_url

    if simulation:
        simulation["attribution"] = build_attribution(simulation)
        simulation["forward_study"] = study_view(db, settings, None if portfolio else report["day"])

    return {"dashboard": data, "tracking": observations, "histories": histories,
            "backtest": simulation,
            "exported_at": now().isoformat(), "public": public, "portfolio": portfolio,
            "live_url": live_url(settings) if public and portfolio else ""}


def render_daily(payload, settings):
    environment = Environment(loader=FileSystemLoader(ASSETS / "templates"), autoescape=select_autoescape(["html"]))
    template_helpers(environment)
    text = environment.get_template("index.html").render(
        source=settings.source,
        public_report=payload.get("public", False),
        portfolio_report=payload.get("portfolio", False),
        live_feed=bool(payload.get("live_url")),
        offline=True,
        nonce=secrets.token_urlsafe(24),
        exported_at=now().strftime("%Y-%m-%d %H:%M（北京时间）"),
        css=(ASSETS / "static/style.css").read_text() + (ASSETS / "static/mail_report.css").read_text(),
        static_report=daily_view(payload),
        javascript=(ASSETS / "static/app.js").read_text(),
        appearance_css=(ASSETS / "static/appearance.css").read_text(),
        appearance_javascript=(ASSETS / "static/appearance.js").read_text(),
        snapshot=script_json(payload),
    )
    return text


def export_html(db, settings, day=None, output=None):
    payload = daily_payload(db, settings, day)
    text = render_daily(payload, settings)
    report = payload["dashboard"]["report"]
    destination = Path(output or Path(settings.html_report_dir) / f"{settings.source}-latest.html").resolve()
    if destination.suffix.lower() != ".html":
        raise DataError("HTML 输出文件必须以 .html 结尾")
    atomic_write(destination, text)
    return {"status": "success", "source": settings.source, "day": report["day"] if report else None,
            "report_ready": report is not None, "path": str(destination)}


def export_published(db, settings, day):
    """Publish a dated copy and a latest copy; callers handle I/O independently of DB success."""
    folder = Path(settings.html_report_dir)
    result = export_html(db, settings, day, folder / f"{settings.source}-{day}.html")
    # Reuse identical bytes so both copies represent the same snapshot.
    if settings.export_png:
        from .png_export import export_png

        result["png"] = export_png(result["path"])
        publish_latest_png(result["png"]["path"], folder / f"{settings.source}-latest.png")
    atomic_write((folder / f"{settings.source}-latest.html").resolve(), Path(result["path"]).read_text())
    return result


def publish_latest_png(png, destination):
    destination = Path(destination).resolve()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as file:
            temporary = file.name
            file.write(Path(png).read_bytes())
        os.replace(temporary, destination)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
