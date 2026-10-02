"""Shared fixtures. Database tests run against real Postgres (``make db-up``)."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import Base, alembic_config, registry

assert registry

_SDK_CREDENTIAL_VARIABLES = ("HF_KEY", "HF_API_KEY", "HF_API_SECRET")


@pytest.fixture(autouse=True)
def isolate_from_env_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never read the developer's .env / .env.local or SDK credentials:
    a real key there must not leak into a test, let alone pay for a call."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _SDK_CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


TEST_DATABASE_URL = os.environ.get(
    "HATCH_TEST_DATABASE_URL", "postgresql+psycopg://hatch:hatch@localhost:54329/hatch_test"
)


def _recreate_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{target.database}"'))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.exit(
            f"Postgres is not reachable at {target.host}:{target.port} — run `make db-up`. ({exc})"
        )
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """An empty database migrated to head by Alembic — never by ``create_all``."""
    _recreate_database(TEST_DATABASE_URL)
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def session(connection: Connection) -> Iterator[Session]:
    """A session whose work is rolled back after each test."""
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        yield session


@pytest.fixture
def committed_db(engine: Engine) -> Iterator[Engine]:
    """For tests that need real commits (several connections, CLI processes):
    yields the engine and empties every table afterwards."""
    yield engine
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))
