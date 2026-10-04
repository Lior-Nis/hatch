"""Words and times for people: the dashboard's plain-language layer.

Internal state names stay in the database and in ``title`` attributes; these
maps are only what a teammate who has not read the code sees.
"""

from datetime import UTC, datetime

from markupsafe import Markup, escape

# label, tone (css class on the tag)
_STATUS: dict[str, tuple[str, str]] = {
    "proposed": ("Idea", ""),
    "scripted": ("Writing", ""),
    "storyboarded": ("Planning shots", ""),
    "generating": ("Making video", ""),
    "generated": ("Video made", ""),
    "qa_pending": ("Being checked", ""),
    "approval_pending": ("Needs your OK", "flag"),
    "ready": ("Approved", "good"),
    "scheduled": ("Scheduled", "good"),
    "published": ("Posted", "good"),
    "observing": ("Collecting results", "good"),
    "evaluated": ("Results in", "good"),
    "generation_failed": ("Failed to make", "fail"),
    "qa_rejected": ("Failed safety check", "fail"),
    "human_rejected": ("Rejected", "fail"),
    "publish_failed": ("Failed to post", "fail"),
    "aborted_budget": ("Stopped by budget", "fail"),
    "planned": ("Planned", ""),
    "failed": ("Failed", "fail"),
    "cancelled": ("Cancelled", ""),
    "supported": ("Worked again", "good"),
    "partially_supported": ("Partly worked", ""),
    "not_supported": ("Did not repeat", "fail"),
    "inconclusive": ("Unclear", ""),
    "anomalous": ("Odd result", "flag"),
    "experimental": ("Testing", ""),
    "validated": ("Proven", "good"),
}

RELATION_LABELS = {
    "exploit": "Repeat of a winner",
    "mutation": "Tweak",
    "recombination": "Mix",
    "replication": "Retest",
    "resurrection": "Revival",
}

REJECT_REASONS: dict[str, str] = {
    "scary": "Scary or unsafe",
    "copy": "Copies another character or brand",
    "quality": "Poor quality",
    "nonsense": "Doesn't make sense",
    "other": "Other",
}


def _raw(value: object) -> str:
    return str(getattr(value, "value", value))


def status_label(value: object) -> str:
    raw = _raw(value)
    return _STATUS.get(raw, (raw.replace("_", " ").capitalize(), ""))[0]


def status_tag(value: object) -> Markup:
    raw = _raw(value)
    label, tone = _STATUS.get(raw, (raw.replace("_", " ").capitalize(), ""))
    return Markup('<span class="tag {}" title="{}">{}</span>').format(tone, raw, label)


def relation_label(value: object) -> str:
    raw = _raw(value)
    return RELATION_LABELS.get(raw, raw.capitalize())


def when(moment: datetime | None) -> Markup:
    """A time the browser rewrites into the viewer's own time zone.

    The fallback text is UTC, which is what is shown if scripts are off or the
    browser cannot report a zone."""
    if moment is None:
        return Markup("")
    stamp = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    stamp = stamp.astimezone(UTC)
    return Markup('<time class="t" datetime="{}">{} UTC</time>').format(
        stamp.isoformat(), escape(stamp.strftime("%b %d, %H:%M"))
    )
