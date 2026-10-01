"""Database foundation: declarative base, shared column types, sessions."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from alembic.config import Config
from sqlalchemy import DateTime, Engine, Enum, Numeric, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]

JSONDict = dict[str, Any]
Money = Annotated[Decimal, mapped_column(Numeric(12, 6))]
"""USD amounts. Six decimals so sub-cent provider prices are not rounded away."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_type: type[StrEnum]) -> Enum:
    """Store a StrEnum by value in a plain VARCHAR (no DB-level enum type), so
    adding a state is a code change validated by the state machines."""
    return Enum(
        enum_type,
        native_enum=False,
        length=40,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy declarative configuration
        uuid.UUID: PG_UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        JSONDict: JSONB,
        list[str]: JSONB,
    }


class Identified:
    """Mixin: UUID primary key and creation timestamp."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config
