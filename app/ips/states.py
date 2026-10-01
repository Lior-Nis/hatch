from enum import StrEnum


class IPStatus(StrEnum):
    IDEA = "idea"
    EXPERIMENTAL = "experimental"
    PROMISING = "promising"
    VALIDATED = "validated"
    SCALED = "scaled"
    ARCHIVED = "archived"
