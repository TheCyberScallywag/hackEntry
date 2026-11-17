"""Showcase profiles for organisations: a logo, a hero image and a short description.

They come from data/orgs/profiles.json, keyed by organisation key, with the images beside it
(served at /org-assets). The logos and photographs belong to the organisations they depict (see
data/orgs/README.md), so a profile is tied to the organisation's name as well as its key:
renaming a demo organisation with ORG_NAMES removes its branding too. An organisation without a
profile is shown with its initials.
"""

import json
from functools import cache
from typing import Any

from app.config import ORG_ASSETS_DIR

ASSETS_URL = "/org-assets"


@cache
def _profiles() -> dict[str, dict[str, Any]]:
    path = ORG_ASSETS_DIR / "profiles.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _asset(key: str, filename: str | None) -> str | None:
    """The URL of an organisation's image, or None when the profile names none or it is missing."""
    if not filename or "/" in filename or not (ORG_ASSETS_DIR / key / filename).is_file():
        return None
    return f"{ASSETS_URL}/{key}/{filename}"


def initials(name: str) -> str:
    return "".join(word[0] for word in name.split()[:2]).upper() or "?"


def profile_for(org: dict) -> dict[str, Any]:
    """What the pages show for one organisation row (`key`, `name`, `description`)."""
    raw = _profiles().get(org["key"])
    if raw is None or raw.get("name") != org["name"]:
        raw = {}
    return {
        "tagline": raw.get("tagline", ""),
        "about": raw.get("about") or org.get("description", ""),
        "highlights": raw.get("highlights", []),
        "website": raw.get("website", ""),
        "credit": raw.get("credit", ""),
        "logo": _asset(org["key"], raw.get("logo")),
        "logo_shape": raw.get("logo_shape", "square"),
        "hero": _asset(org["key"], raw.get("hero")),
        "hero_alt": raw.get("hero_alt", ""),
        "initials": initials(org["name"]),
    }
