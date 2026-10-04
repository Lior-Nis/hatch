"""The account checklist: what a human still has to create, per IP and platform.

Hatch never creates social accounts. This page tells the owner exactly which of
the 12 (3 IPs x 4 platforms) exist in Hatch's records and what to type for the
ones that do not. Whether an account exists on the platform itself, or is
connected in Buffer, is not something Hatch can see until it is mapped.
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ips.models import IP
from app.platforms import Platform
from app.publishing.models import PlatformAccount

PLATFORM_NAMES = {
    Platform.YOUTUBE_SHORTS: "YouTube",
    Platform.TIKTOK: "TikTok",
    Platform.INSTAGRAM_REELS: "Instagram",
    Platform.FACEBOOK_REELS: "Facebook",
}

BIO_LIMITS = {
    Platform.YOUTUBE_SHORTS: 1000,
    Platform.TIKTOK: 80,
    Platform.INSTAGRAM_REELS: 150,
    Platform.FACEBOOK_REELS: 255,
}

# Setup tips. Platform settings change: treat these as a starting point and
# check the screen you are on.
TIPS = {
    Platform.YOUTUBE_SHORTS: (
        "Create a brand channel. In YouTube Studio, Settings, Channel, Advanced settings, "
        "choose 'Yes, set this channel as made for kids'. "
        "Connect it in Buffer as the channel owner."
    ),
    Platform.TIKTOK: (
        "Create the account, then connect it in Buffer. TikTok has no made-for-kids switch "
        "for creators, which is why the platform policy decision is open in Todoist."
    ),
    Platform.INSTAGRAM_REELS: (
        "Switch to a professional account and link it to the brand's Facebook Page. "
        "Buffer needs that link to publish Reels."
    ),
    Platform.FACEBOOK_REELS: (
        "Create a Facebook Page for the brand, not a personal profile, then connect it in Buffer."
    ),
}


def suggested_handles(ip: IP) -> list[str]:
    ideas = [str(handle) for handle in ip.spec.get("suggested_handles", [])]
    return ideas or [re.sub(r"[^a-z0-9]", "", ip.name.lower())]


def suggested_bio(premise: str, platform: Platform) -> str:
    suffix = " Made for kids."
    limit = BIO_LIMITS[platform] - len(suffix)
    sentence = premise.strip().split(". ")[0].rstrip(".") + "."
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip(" ,;") + "…"
    return sentence + suffix


def checklist(session: Session) -> list[dict[str, Any]]:
    mapped = {
        (account.ip_id, account.platform): account
        for account in session.scalars(select(PlatformAccount))
    }
    rows = []
    for ip in session.scalars(select(IP).order_by(IP.slug)):
        accounts: list[dict[str, Any]] = []
        for platform in Platform:
            account = mapped.get((ip.id, platform))
            accounts.append(
                {
                    "platform": PLATFORM_NAMES[platform],
                    "mapped": account is not None,
                    "handle": account.handle if account else None,
                    "name": ip.name,
                    "handle_ideas": suggested_handles(ip),
                    "bio": suggested_bio(str(ip.spec["premise"]), platform),
                    "tip": TIPS[platform],
                }
            )
        rows.append(
            {
                "ip": ip,
                "accounts": accounts,
                "done": sum(1 for a in accounts if a["mapped"]),
            }
        )
    return rows
