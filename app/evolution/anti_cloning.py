"""Anti-cloning: evolve mechanisms, never copy surfaces.

A descendant may keep (or deliberately change) its parent's *mechanism* genes,
but it must tell a new story. This module measures how close a candidate's
surface — its topic and creative specification — is to the IP's existing
catalogue and blocks near-duplicates. It also flags resemblance to well-known
external properties.

Similarity is lexical (content-word overlap), which is deterministic and free.
``TextSimilarity`` is the seam for an embedding-based measure later.
"""

import re
import uuid
from collections.abc import Callable, Collection

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.creative.genome import Genes
from app.experiments.models import Experiment
from app.ips.models import IP

TextSimilarity = Callable[[str, str], float]

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "then",
        "there",
        "they",
        "this",
        "to",
        "toward",
        "towards",
        "up",
        "with",
        "while",
        "when",
        "who",
    ]
)

# Well-known children's properties a candidate must not resemble. Matched as
# whole words, case-insensitively. Deliberately a blunt first line of defence:
# the IP/brand QA gate inspects the generated video itself.
_EXTERNAL_PROPERTIES = (
    "Peppa Pig", "Bluey", "Paw Patrol", "Cocomelon", "Baby Shark", "Pinkfong", "Blippi",
    "Mickey Mouse", "Minnie Mouse", "Disney", "Pixar", "Frozen", "Elsa", "Moana", "Encanto",
    "Winnie the Pooh", "Pokémon", "Pokemon", "Pikachu", "Super Mario", "Nintendo", "Sonic",
    "SpongeBob", "Minions", "Hello Kitty", "Sesame Street", "Elmo", "Barbie", "LEGO", "Duplo",
    "Marvel", "Spider-Man", "Spiderman", "Batman", "Superman", "Thomas the Tank Engine",
    "Dora the Explorer", "Teletubbies", "Totoro", "Shrek", "Pingu", "Miffy", "Masha and the Bear",
    "Octonauts", "PJ Masks", "My Little Pony", "Transformers", "Hot Wheels", "Roblox", "Minecraft",
    "Fortnite", "Star Wars", "Harry Potter", "Gruffalo", "Hungry Caterpillar", "Curious George",
    "Paddington", "Peter Rabbit", "Snoopy", "Garfield", "Tom and Jerry", "Looney Tunes",
    "Scooby-Doo", "Ben and Holly", "Hey Duggee", "Numberblocks", "Ms Rachel", "Gabby's Dollhouse",
)  # fmt: skip
_EXTERNAL_PATTERNS = tuple(
    (name, re.compile(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])"))
    for name in _EXTERNAL_PROPERTIES
)


def _content_words(text: str) -> frozenset[str]:
    words = (word.rstrip("s") if len(word) > 3 else word for word in _WORD.findall(text.lower()))
    return frozenset(word for word in words if word not in _STOPWORDS)


def lexical_similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words: 1.0 identical, 0.0 nothing shared."""
    words_a, words_b = _content_words(a), _content_words(b)
    if not words_a and not words_b:
        return 1.0
    return len(words_a & words_b) / len(words_a | words_b)


def external_ip_matches(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(name for name, pattern in _EXTERNAL_PATTERNS if pattern.search(lowered))


def surface_text(genes: Genes, creative_spec: str) -> str:
    """What a video is *about*: topic, lesson, cast, and the story itself."""
    parts = [genes.topic, genes.educational_goal or "", *genes.supporting_characters, creative_spec]
    return " ".join(part for part in parts if part)


class AntiCloningPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_surface_similarity: float = 0.6
    """A candidate at or above this similarity to any catalogue entry is a clone."""
    catalog_size: int = 100
    """How many of the IP's most recent experiments to compare against."""


class CloneVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    blocked: bool
    reasons: tuple[str, ...]
    nearest_experiment_id: uuid.UUID | None
    nearest_similarity: float
    external_ip_matches: tuple[str, ...]


def check_clone(
    session: Session,
    *,
    ip: IP,
    genes: Genes,
    creative_spec: str,
    policy: AntiCloningPolicy | None = None,
    similarity: TextSimilarity = lexical_similarity,
    exclude: Collection[uuid.UUID] = (),
) -> CloneVerdict:
    """Compare a candidate's surface with the IP's recent catalogue (which
    includes its siblings and parent) and with known external properties."""
    policy = policy or AntiCloningPolicy()
    candidate = surface_text(genes, creative_spec)
    catalogue = session.scalars(
        select(Experiment)
        .where(Experiment.ip_id == ip.id, Experiment.id.not_in(list(exclude)))
        .options(selectinload(Experiment.genome))
        .order_by(Experiment.created_at.desc())
        .limit(policy.catalog_size)
    )

    nearest_id: uuid.UUID | None = None
    nearest = 0.0
    for other in catalogue:
        other_genes = other.genome.genes
        other_text = " ".join(
            str(part)
            for part in (
                other_genes.get("topic", ""),
                other_genes.get("educational_goal") or "",
                *other_genes.get("supporting_characters", []),
                other.genome.creative_spec,
            )
            if part
        )
        score = similarity(candidate, other_text)
        if nearest_id is None or score > nearest:
            nearest_id, nearest = other.id, score

    reasons = []
    if nearest_id is not None and nearest >= policy.max_surface_similarity:
        reasons.append(
            f"too similar to experiment {nearest_id} (similarity {nearest:.2f} ≥ "
            f"{policy.max_surface_similarity:.2f}): tell a different story — new topic, new "
            "events — while keeping the mechanism"
        )
    external = external_ip_matches(candidate + " " + genes.primary_character)
    if external:
        reasons.append(
            f"resembles existing properties ({', '.join(external)}): Hatch IP must be original"
        )
    return CloneVerdict(
        blocked=bool(reasons),
        reasons=tuple(reasons),
        nearest_experiment_id=nearest_id,
        nearest_similarity=nearest,
        external_ip_matches=external,
    )
