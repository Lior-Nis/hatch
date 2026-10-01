from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, Identified, JSONDict, enum_column
from app.ips.states import IPStatus


class IP(Identified, Base):
    """An original intellectual property: one world/brand identity."""

    __tablename__ = "ips"

    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(60))
    status: Mapped[IPStatus] = mapped_column(enum_column(IPStatus), default=IPStatus.IDEA)
    spec: Mapped[JSONDict]
    """World rules, age target, visual identity, safety constraints."""
