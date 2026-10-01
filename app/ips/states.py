from enum import StrEnum

from app.lifecycle import Lifecycle


class IPStatus(StrEnum):
    IDEA = "idea"
    EXPERIMENTAL = "experimental"
    PROMISING = "promising"
    VALIDATED = "validated"
    SCALED = "scaled"
    ARCHIVED = "archived"


IP_LIFECYCLE = Lifecycle(
    "IP",
    initial=IPStatus.IDEA,
    transitions={
        IPStatus.IDEA: {IPStatus.EXPERIMENTAL, IPStatus.ARCHIVED},
        IPStatus.EXPERIMENTAL: {IPStatus.PROMISING, IPStatus.ARCHIVED},
        IPStatus.PROMISING: {IPStatus.VALIDATED, IPStatus.ARCHIVED},
        IPStatus.VALIDATED: {IPStatus.SCALED, IPStatus.ARCHIVED},
        IPStatus.SCALED: {IPStatus.ARCHIVED},
        # Resurrection: an archived IP re-enters as an experiment, never higher.
        IPStatus.ARCHIVED: {IPStatus.EXPERIMENTAL},
    },
)
