import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text

from .config import Settings
from .db import Database, bars, watch_evaluations, watch_plans, watchlist
from .service import dashboard, tracking

ROOT = Path(__file__).parent


def validate_code(code):
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        raise ValueError("股票代码应为 600000.SH、000001.SZ 或 920xxx.BJ 等格式")
    return code


def create_app(settings=None, database=None):
    settings = settings or Settings.from_env()
    database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        yield
        database.engine.dispose()

    app = FastAPI(title="Stock Daily · A股盘后分析", version="0.2.0", lifespan=lifespan)
    app.state.settings, app.state.db = settings, database
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        ))
        return response

    def authorize(authorization: str = Header(default="")):
        if not settings.api_key:
            raise HTTPException(503, "自选股网页写入未启用；设置 APP_API_KEY 或使用 CLI")
        if not secrets.compare_digest(authorization, f"Bearer {settings.api_key}"):
            raise HTTPException(401, "需要有效的管理密钥")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request=request, name="index.html", context={"source": settings.source,
                "midday_time": settings.midday_time, "schedule_time": settings.schedule_time,
                "announcement_time": settings.announcement_time,
                "portfolio_interval_minutes": settings.portfolio_interval_minutes}
        )

    @app.get("/health")
    def health():
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "source": settings.source}

    @app.get("/artifacts/{filename}")
    def artifact(filename: str):
        pattern = rf"{re.escape(settings.source)}-(?:latest|\d{{8}}(?:-\d{{4}})?)\.(?:html|png)"
        if not re.fullmatch(pattern, filename):
            raise HTTPException(404, "报告文件不存在")
        folder = Path(settings.html_report_dir).resolve()
        path = (folder / filename).resolve()
        if path.parent != folder or not path.is_file():
            raise HTTPException(404, "报告尚未生成")
        policy = "default-src 'none'; img-src data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        if path.suffix == ".html":
            match = re.search(r'nonce="([A-Za-z0-9_-]{20,80})"', path.read_text())
            if match:
                policy += f"; script-src 'nonce-{match[1]}'; style-src 'nonce-{match[1]}'"
        return FileResponse(path, headers={"Content-Security-Policy": policy, "Cache-Control": "no-store"})

    @app.get("/api/dashboard")
    def get_dashboard(day: str | None = Query(default=None, pattern=r"^\d{8}$")):
        return dashboard(database, settings, day)

    @app.get("/api/tracking")
    def get_tracking(day: str | None = Query(default=None, pattern=r"^\d{8}$")):
        return tracking(database, settings, as_of=day)

    @app.get("/api/operations")
    def operations():
        from .operations import health_view

        return health_view(database, settings)

    @app.get("/api/backtest")
    def backtest(day: str | None = Query(default=None, pattern=r"^\d{8}$"),
                 strategy: str = "all", cohort: str = "all", rule_version: str = "",
                 capital: float = 100000, weight: float = .1, max_positions: int = 10,
                 holding_days: int = 10, commission_bps: float = 3, min_commission: float = 5,
                 sell_tax_bps: float = 5, transfer_bps: float = .1, slippage_bps: float = 5,
                 participation: float = .01, split_day: str = "", live: bool = False):
        from .backtest import Simulation, simulate

        try:
            config = Simulation(capital, weight, max_positions, holding_days, commission_bps, min_commission,
                                sell_tax_bps, transfer_bps, slippage_bps, participation, strategy,
                                rule_version, cohort, split_day)
            result = simulate(database, settings, day, config)
            if live:
                from .portfolio import value_portfolio

                result["valuation"] = value_portfolio(database, settings, result)
            return result
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/api/evidence/{day}/summary", dependencies=[Depends(authorize)])
    def evidence_summary(day: str):
        from .evidence_ai import summarize_evidence

        get_report(day)
        try:
            return summarize_evidence(database, settings, dashboard(database, settings, day)["report"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/report/{day}")
    def get_report(day: str):
        if not re.fullmatch(r"\d{8}", day):
            raise HTTPException(422, "日期应为 YYYYMMDD")
        report = database.latest_report(settings.source, day)
        if not report:
            raise HTTPException(404, "该日期没有报告")
        return report

    @app.get("/api/report/{day}/download")
    def download(day: str):
        import json

        report = get_report(day)
        return Response(
            json.dumps(report, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="stock-daily-{day}.json"'},
        )

    @app.get("/api/watchlist")
    def get_watchlist():
        return database.rows(select(watchlist).order_by(watchlist.c.code))

    @app.get("/api/stocks/{code}/history")
    def stock_history(code: str, day: str | None = Query(default=None, pattern=r"^\d{8}$")):
        try:
            validate_code(code)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        query = select(bars).where(bars.c.source == settings.source, bars.c.code == code)
        if day:
            query = query.where(bars.c.day <= day)
        rows = database.rows(query.order_by(bars.c.day.desc()).limit(65))
        return list(reversed(rows))

    class WatchInput(BaseModel):
        code: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
        note: str = Field(default="", max_length=500)

    @app.post("/api/watchlist", dependencies=[Depends(authorize)])
    def add_watch(value: WatchInput):
        database.save_watch(value.code, value.note)
        return {"status": "saved", "code": value.code}

    @app.delete("/api/watchlist/{code}", dependencies=[Depends(authorize)])
    def remove_watch(code: str):
        try:
            validate_code(code)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        with database.engine.begin() as connection:
            connection.execute(delete(watchlist).where(watchlist.c.code == code))
            connection.execute(delete(watch_plans).where(watch_plans.c.code == code))
            connection.execute(delete(watch_evaluations).where(watch_evaluations.c.code == code))
        return {"status": "deleted"}

    class PlanInput(BaseModel):
        reason: str = Field(default="", max_length=1000)
        question: str = Field(default="", max_length=1000)
        target: float | None = Field(default=None, gt=0, allow_inf_nan=False)
        invalidation: float | None = Field(default=None, gt=0, allow_inf_nan=False)
        checks: list[str] = Field(default=["below_ma20", "down5", "volume_down"], max_length=5)

    @app.put("/api/watchlist/{code}/plan", dependencies=[Depends(authorize)])
    def update_plan(code: str, value: PlanInput):
        from .observations import CHECKS, save_plan

        if not database.rows(select(watchlist.c.code).where(watchlist.c.code == code)):
            raise HTTPException(404, "请先添加到自选股")
        if any(c not in CHECKS for c in value.checks) or len(set(value.checks)) != len(value.checks):
            raise HTTPException(422, "提醒条件无效或重复")
        if "above_target" in value.checks and value.target is None or (
                "below_invalidation" in value.checks and value.invalidation is None):
            raise HTTPException(422, "价格提醒需要对应的观察价或失效价")
        save_plan(database, code, value.model_dump())
        return {"status": "saved", "code": code}

    return app
