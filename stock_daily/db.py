import uuid
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from .config import now

metadata = MetaData()
announcement_evidence = Table(
    "announcement_evidence", metadata,
    Column("source", String(20), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("day", String(8), nullable=False, index=True),
    Column("first_seen_at", String(40), nullable=False),
    Column("payload", JSON, nullable=False),
)
evidence_summaries = Table(
    "evidence_summaries", metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("fingerprint", String(64), primary_key=True),
    Column("payload", JSON, nullable=False),
)
watch_plans = Table(
    "watch_plans", metadata,
    Column("code", String(20), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("updated_at", String(40), nullable=False),
)
watch_evaluations = Table(
    "watch_evaluations", metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("code", String(20), primary_key=True),
    Column("payload", JSON, nullable=False),
)
system_state = Table(
    "system_state", metadata,
    Column("source", String(20), primary_key=True),
    Column("key", String(40), primary_key=True),
    Column("payload", JSON, nullable=False),
)
paper_accounts = Table(
    "paper_accounts", metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("payload", JSON, nullable=False),
)
news_items = Table(
    "news_items", metadata,
    Column("provider", String(20), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("published_at", String(40), nullable=False, index=True),
    Column("first_seen_at", String(40), nullable=False),
    Column("title", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("important", Integer, nullable=False),
)
news_fetches = Table(
    "news_fetches", metadata,
    Column("provider", String(20), primary_key=True),
    Column("fetched_at", String(40), primary_key=True),
    Column("status", String(20), nullable=False),
    Column("count", Integer, nullable=False),
    Column("invalid_count", Integer, nullable=False),
    Column("error", Text, nullable=False),
)
mail_outbox = Table(
    "mail_outbox", metadata,
    Column("id", String(64), primary_key=True),
    Column("source", String(20), nullable=False),
    Column("day", String(8), nullable=False),
    Column("slot", String(20), nullable=False),
    Column("recipient", String(320), nullable=False),
    Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("message", LargeBinary, nullable=False),
    Column("message_id", String(150), nullable=False),
    Column("subject", String(300), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("next_attempt_at", String(40), nullable=False),
    Column("last_attempt_at", String(40)),
    Column("sent_at", String(40)),
    Column("last_error", Text, nullable=False),
)
trade_alerts = Table(
    "trade_alerts", metadata,
    Column("id", String(64), primary_key=True),
    Column("source", String(20), nullable=False),
    Column("day", String(8), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)
snapshots = Table(
    "snapshots", metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("slot", String(20), primary_key=True),
    Column("payload", JSON, nullable=False),
)
provider_cache = Table(
    "provider_cache",
    metadata,
    Column("source", String(20), primary_key=True),
    Column("key", String(60), primary_key=True),
    Column("payload", JSON, nullable=False),
)
trading_days = Table(
    "trading_days",
    metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("is_open", Integer, nullable=False, default=1),
)
bars = Table(
    "bars",
    metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("code", String(20), primary_key=True),
    *[
        Column(key, Float, nullable=False)
        for key in ("open", "high", "low", "close", "volume", "amount", "adj", "pct")
    ],
    Column("turnover", Float),
    Column("pe", Float),
    Column("pb", Float),
)
sessions = Table(
    "sessions",
    metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("quality", JSON, nullable=False),
    Column("benchmark", JSON, nullable=False),
    Column("universe", JSON, nullable=False),
    Column("events", JSON, nullable=False),
    Column("event_status", String(60), nullable=False),
    Column("acquired_at", String(40), nullable=False),
)
reports = Table(
    "reports",
    metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("payload", JSON, nullable=False),
)
signals = Table(
    "signals",
    metadata,
    Column("source", String(20), primary_key=True),
    Column("day", String(8), primary_key=True),
    Column("code", String(20), primary_key=True),
    Column("strategy", String(30), primary_key=True),
    Column("payload", JSON, nullable=False),
)
watchlist = Table(
    "watchlist",
    metadata,
    Column("code", String(20), primary_key=True),
    Column("note", Text, nullable=False),
    Column("added_at", String(40), nullable=False),
)
runs = Table(
    "runs",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("source", String(20), nullable=False),
    Column("day", String(8), nullable=False),
    Column("kind", String(30), nullable=False),
    Column("status", String(30), nullable=False),
    Column("message", Text, nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("ended_at", String(40)),
    Column("completed_days", Integer, nullable=False, default=0),
)
locks = Table(
    "locks",
    metadata,
    Column("key", String(40), primary_key=True),
    Column("owner", String(40), nullable=False),
    Column("expires", String(40), nullable=False),
)


class BusyError(RuntimeError):
    pass


class Database:
    def __init__(self, url):
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {},
        )
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_options(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=30000")

        metadata.create_all(self.engine)

    def rows(self, query):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def upsert(self, connection, table, values):
        if not values:
            return
        constructor = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        statement = constructor(table).values(values)
        primary = [column.name for column in table.primary_key]
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=primary,
                set_={
                    c.name: getattr(statement.excluded, c.name)
                    for c in table.columns
                    if c.name not in primary
                },
            )
        )

    @contextmanager
    def lock(self, source):
        owner = uuid.uuid4().hex
        key = f"pipeline:{source}"
        acquired = False
        with self.engine.begin() as connection:
            connection.execute(delete(locks).where(locks.c.key == key, locks.c.expires < now().isoformat()))
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    locks.insert().values(
                        key=key,
                        owner=owner,
                        expires=(now() + timedelta(hours=6)).isoformat(),
                    )
                )
            acquired = True
        except IntegrityError:
            raise BusyError("已有采集任务运行中；异常退出的任务锁最长6小时后过期") from None
        try:
            yield
        finally:
            if acquired:
                with self.engine.begin() as connection:
                    connection.execute(delete(locks).where(locks.c.key == key, locks.c.owner == owner))

    def start_run(self, source, day, kind="daily"):
        identifier = uuid.uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(
                runs.insert().values(
                    id=identifier,
                    source=source,
                    day=day,
                    kind=kind,
                    status="running",
                    message="正在检查数据",
                    started_at=now().isoformat(),
                    completed_days=0,
                )
            )
        return identifier

    def finish_run(self, identifier, status, message):
        with self.engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(runs.c.id == identifier)
                .values(
                    status=status,
                    message=message,
                    ended_at=now().isoformat(),
                )
            )

    def latest_report(self, source, day=None):
        query = select(reports.c.payload).where(reports.c.source == source)
        if day:
            query = query.where(reports.c.day == day)
        result = self.rows(query.order_by(reports.c.day.desc()).limit(1))
        return result[0]["payload"] if result else None

    def save_watch(self, code, note=""):
        with self.engine.begin() as connection:
            self.upsert(
                connection,
                watchlist,
                {
                    "code": code,
                    "note": note,
                    "added_at": now().isoformat(),
                },
            )

    def save_state(self, source, key, payload):
        with self.engine.begin() as connection:
            self.upsert(connection, system_state, {"source": source, "key": key, "payload": payload})
