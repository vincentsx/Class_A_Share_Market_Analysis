import pytest

from stock_daily.config import Settings
from stock_daily.db import Database
from stock_daily.pipeline import run_daily
from stock_daily.providers import DemoProvider


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", history_sessions=65)


@pytest.fixture
def db(settings):
    database = Database(settings.database_url)
    yield database
    database.engine.dispose()


@pytest.fixture
def populated(settings, db):
    provider = DemoProvider(settings)
    run_daily(db, settings, "20261009", provider=provider)
    return db, settings, provider
