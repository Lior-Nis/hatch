import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Evidence, Identified, JSONDict
from app.experiments.models import Experiment
from app.ips.models import IP


class Character(Identified, Base):
    """An original character belonging to one IP."""

    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("ip_id", "name"),)

    ip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ips.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(60))

    ip: Mapped[IP] = relationship()
    versions: Mapped[list["CharacterVersion"]] = relationship(
        back_populates="character", order_by="CharacterVersion.version"
    )


class CharacterVersion(Evidence, Identified, Base):
    """An immutable definition of how a character looks and behaves. Changing
    a character means adding a version, so old experiments stay reproducible."""

    __tablename__ = "character_versions"
    __table_args__ = (UniqueConstraint("character_id", "version"),)

    character_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("characters.id"), index=True)
    version: Mapped[int]
    description: Mapped[str] = mapped_column(Text)
    visual_spec: Mapped[JSONDict]
    """Appearance prompt fragments, reference asset ids, style notes."""

    character: Mapped[Character] = relationship(back_populates="versions")


class ExperimentCharacter(Evidence, Base):
    """Which character versions an experiment used, and in what role."""

    __tablename__ = "experiment_characters"

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), primary_key=True)
    character_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("character_versions.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(40))

    experiment: Mapped[Experiment] = relationship(back_populates="characters")
    character_version: Mapped[CharacterVersion] = relationship()
