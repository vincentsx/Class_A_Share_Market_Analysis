import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(SHANGHAI)


@dataclass(frozen=True)
class Settings:
    source: str = "demo"
    database_url: str = "sqlite:///./data/stock_daily.db"
    token: str = ""
    tushare_url: str = "https://api.tushare.pro"
    request_interval: float = 0.7
    announcements: bool = False
    api_key: str = ""
    schedule_time: str = "17:30"
    announcement_time: str = "21:30"
    history_sessions: int = 100
    min_amount: float = 50_000_000
    min_coverage: float = 0.98
    max_candidates: int = 20
    alert_webhook: str = ""

    @classmethod
    def from_env(cls):
        load_dotenv()
        config = cls(
            source=os.getenv("DATA_SOURCE", "demo"),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            token=os.getenv("TUSHARE_TOKEN", ""),
            tushare_url=os.getenv("TUSHARE_URL", cls.tushare_url),
            request_interval=float(os.getenv("TUSHARE_REQUEST_INTERVAL", "0.7")),
            announcements=os.getenv("ENABLE_ANNOUNCEMENTS", "false").lower() == "true",
            api_key=os.getenv("APP_API_KEY", ""),
            schedule_time=os.getenv("SCHEDULE_TIME", "17:30"),
            announcement_time=os.getenv("ANNOUNCEMENT_TIME", "21:30"),
            history_sessions=int(os.getenv("HISTORY_SESSIONS", "100")),
            min_amount=float(os.getenv("MIN_AMOUNT_YUAN", "50000000")),
            min_coverage=float(os.getenv("MIN_COVERAGE", "0.98")),
            max_candidates=int(os.getenv("MAX_CANDIDATES", "20")),
            alert_webhook=os.getenv("ALERT_WEBHOOK", ""),
        )
        if config.source not in {"demo", "tushare"}:
            raise ValueError("DATA_SOURCE 必须是 demo 或 tushare")
        if config.history_sessions < 65 or not 0 < config.min_coverage <= 1:
            raise ValueError("HISTORY_SESSIONS 至少为65，MIN_COVERAGE 范围为 (0,1]")
        if config.max_candidates < 1 or config.min_amount < 0 or config.request_interval < 0:
            raise ValueError("候选数量、成交额或请求间隔配置无效")
        for value in (config.schedule_time, config.announcement_time):
            datetime.strptime(value, "%H:%M")
        if config.database_url.startswith("sqlite:///./"):
            Path(config.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        return config
