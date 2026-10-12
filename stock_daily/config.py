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
    free_request_interval: float = 0.3
    free_timeout: float = 90
    announcements: bool = False
    news_enabled: bool = False
    news_interval_minutes: int = 10
    news_timeout: float = 8
    api_key: str = ""
    midday_time: str = "11:35"
    schedule_time: str = "15:05"
    announcement_time: str = "20:30"
    history_sessions: int = 100
    min_amount: float = 50_000_000
    min_coverage: float = 0.98
    max_candidates: int = 20
    alert_webhook: str = ""
    export_html: bool = False
    export_png: bool = False
    html_report_dir: str = "./data/html"
    email_enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_security: str = "ssl"
    smtp_username: str = ""
    smtp_password: str = ""
    mail_to: str = ""
    smtp_timeout: float = 20
    mail_max_mb: int = 20

    @classmethod
    def from_env(cls):
        load_dotenv()
        config = cls(
            source=os.getenv("DATA_SOURCE", "baostock"),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            token=os.getenv("TUSHARE_TOKEN", ""),
            tushare_url=os.getenv("TUSHARE_URL", cls.tushare_url),
            request_interval=float(os.getenv("TUSHARE_REQUEST_INTERVAL", "0.7")),
            free_request_interval=float(os.getenv("FREE_REQUEST_INTERVAL", "0.3")),
            free_timeout=float(os.getenv("FREE_REQUEST_TIMEOUT", "90")),
            announcements=os.getenv("ENABLE_ANNOUNCEMENTS", "false").lower() == "true",
            news_enabled=os.getenv("ENABLE_NEWS", "true").lower() == "true",
            news_interval_minutes=int(os.getenv("NEWS_INTERVAL_MINUTES", "10")),
            news_timeout=float(os.getenv("NEWS_TIMEOUT", "8")),
            api_key=os.getenv("APP_API_KEY", ""),
            midday_time=os.getenv("MIDDAY_TIME", "11:35"),
            schedule_time=os.getenv("SCHEDULE_TIME", "15:05"),
            announcement_time=os.getenv("ANNOUNCEMENT_TIME", "20:30"),
            history_sessions=int(os.getenv("HISTORY_SESSIONS", "100")),
            min_amount=float(os.getenv("MIN_AMOUNT_YUAN", "50000000")),
            min_coverage=float(os.getenv("MIN_COVERAGE", "0.98")),
            max_candidates=int(os.getenv("MAX_CANDIDATES", "20")),
            alert_webhook=os.getenv("ALERT_WEBHOOK", ""),
            export_html=os.getenv("EXPORT_HTML", "true").lower() == "true",
            export_png=os.getenv("EXPORT_PNG", "true").lower() == "true",
            html_report_dir=os.getenv("HTML_REPORT_DIR", "./data/html"),
            email_enabled=os.getenv("EMAIL_ENABLED", "false").lower() == "true",
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=int(os.getenv("SMTP_PORT", "465")),
            smtp_security=os.getenv("SMTP_SECURITY", "ssl").lower(),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            mail_to=os.getenv("MAIL_TO", "").strip(),
            smtp_timeout=float(os.getenv("SMTP_TIMEOUT", "20")),
            mail_max_mb=int(os.getenv("MAIL_MAX_MB", "20")),
        )
        if config.source not in {"demo", "tushare", "baostock"}:
            raise ValueError("DATA_SOURCE 必须是 demo、baostock 或 tushare")
        if config.free_request_interval < 0 or not 1 <= config.free_timeout <= 300:
            raise ValueError("免费接口请求间隔需非负，超时需为1～300秒")
        if not 5 <= config.news_interval_minutes <= 60 or not 1 <= config.news_timeout <= 20:
            raise ValueError("NEWS_INTERVAL_MINUTES需为5～60分钟，NEWS_TIMEOUT需为1～20秒")
        if config.history_sessions < 65 or not 0 < config.min_coverage <= 1:
            raise ValueError("HISTORY_SESSIONS 至少为65，MIN_COVERAGE 范围为 (0,1]")
        if config.max_candidates < 1 or config.min_amount < 0 or config.request_interval < 0:
            raise ValueError("候选数量、成交额或请求间隔配置无效")
        for value in (config.midday_time, config.schedule_time, config.announcement_time):
            datetime.strptime(value, "%H:%M")
        if not "11:30" <= config.midday_time < "13:00":
            raise ValueError("MIDDAY_TIME 必须在11:30～12:59，避免把下午行情当成午间行情")
        if not "15:00" <= config.schedule_time < config.announcement_time:
            raise ValueError("SCHEDULE_TIME 必须在15:00之后且早于 ANNOUNCEMENT_TIME")
        if config.smtp_security not in {"ssl", "starttls"} or not 1 <= config.smtp_port <= 65535:
            raise ValueError("SMTP_SECURITY 必须为ssl或starttls，SMTP_PORT需为有效端口")
        if not 1 <= config.smtp_timeout <= 60 or not 1 <= config.mail_max_mb <= 50:
            raise ValueError("SMTP_TIMEOUT需为1～60秒，MAIL_MAX_MB需为1～50")
        if config.database_url.startswith("sqlite:///./"):
            Path(config.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        return config
