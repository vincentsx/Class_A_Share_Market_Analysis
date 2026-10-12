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
from .db import Database, bars, watchlist
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

    app = FastAPI(title="Stock Daily · A股盘后分析", version="0.1.0", lifespan=lifespan)
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
            request=request, name="index.html", context={"source": settings.source}
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
    def get_tracking():
        return tracking(database, settings)

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
        return {"status": "deleted"}

    return app
