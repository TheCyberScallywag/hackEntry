"""Shared catalogue (causes and skills) and the demo fixture, loaded from data/fixtures."""

import json
import re
import uuid
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Any

from app.config import FIXTURES_DIR

# Opaque, stable identifiers. Seeded entities derive theirs from the fixture key so a reseed
# gives the same IDs; anything created at runtime gets a random one (app.sqlstore.new_id).
_NAMESPACE = uuid.UUID("6f1d0c43-8a3b-4f43-9d7e-2b3c5d8e9a10")


def stable_id(prefix: str, key: str) -> str:
    return f"{prefix}_{uuid.uuid5(_NAMESPACE, f'{prefix}:{key}').hex[:16]}"


SKILL_KINDS = ("technical", "practical", "interpersonal", "professional", "language")


@dataclass(frozen=True)
class Theme:
    id: str
    name: str


@dataclass(frozen=True)
class Cause:
    id: str
    name: str
    theme: str  # Theme.id


@dataclass(frozen=True)
class Area:
    """A skill area. Top-level areas have no parent; skills sit in second-level areas."""

    id: str
    name: str
    parent: str | None = None


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    area: str  # Area.id, always a second-level area
    kind: str  # one of SKILL_KINDS
    category: str  # the area's name: what the profile screens group by
    alt_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalogue:
    causes: tuple[Cause, ...]
    skills: tuple[Skill, ...]
    themes: tuple[Theme, ...] = ()
    areas: tuple[Area, ...] = ()
    related: tuple[tuple[str, str], ...] = ()  # adjacent skills, each pair once, (a, b) with a < b

    # Looked up once per row on every match, coverage and placement screen, so the lookups are
    # built once. (cached_property writes to __dict__, which a frozen dataclass still has.)
    @cached_property
    def _cause_names(self) -> dict[str, str]:
        return {c.id: c.name for c in self.causes}

    @cached_property
    def _skill_names(self) -> dict[str, str]:
        return {s.id: s.name for s in self.skills}

    @cached_property
    def _skills(self) -> dict[str, Skill]:
        return {s.id: s for s in self.skills}

    @cached_property
    def cause_ids(self) -> frozenset[str]:
        return frozenset(self._cause_names)

    @cached_property
    def skill_ids(self) -> frozenset[str]:
        return frozenset(self._skill_names)

    @cached_property
    def _related(self) -> dict[str, tuple[str, ...]]:
        found: dict[str, list[str]] = {}
        for a, b in self.related:
            found.setdefault(a, []).append(b)
            found.setdefault(b, []).append(a)
        return {skill: tuple(sorted(others)) for skill, others in found.items()}

    def cause_name(self, cause_id: str) -> str:
        return self._cause_names.get(cause_id, cause_id)

    def skill_name(self, skill_id: str) -> str:
        return self._skill_names.get(skill_id, skill_id)

    def skill(self, skill_id: str) -> Skill:
        return self._skills[skill_id]

    def related_to(self, skill_id: str) -> tuple[str, ...]:
        """The skills adjacent to this one, whichever way round the pair was written."""
        return self._related.get(skill_id, ())


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"catalogue: {message}")


def load_catalogue(path: Path | None = None) -> Catalogue:
    """Read and check the catalogue. A broken reference is refused here, at start-up, and not
    discovered later as a skill with no area in the graph."""
    raw = json.loads((path or FIXTURES_DIR / "catalogue.json").read_text(encoding="utf-8"))
    themes = tuple(Theme(**t) for t in raw["themes"])
    areas = tuple(Area(**a) for a in raw["areas"])
    by_area = {a.id: a for a in areas}
    for area in areas:
        if area.parent is not None:
            parent = by_area.get(area.parent)
            _check(parent is not None, f"area {area.id} has unknown parent {area.parent}")
            _check(parent.parent is None, f"area {area.id} is more than two levels deep")
    causes = tuple(Cause(**c) for c in raw["causes"])
    for cause in causes:
        _check(cause.theme in {t.id for t in themes}, f"cause {cause.id} has unknown theme")
    skills = []
    for s in raw["skills"]:
        area = by_area.get(s["area"])
        _check(area is not None, f"skill {s['id']} has unknown area {s['area']}")
        _check(area.parent is not None, f"skill {s['id']} must sit in a second-level area")
        _check(s["kind"] in SKILL_KINDS, f"skill {s['id']} has unknown kind {s['kind']}")
        skills.append(
            Skill(s["id"], s["name"], s["area"], s["kind"], area.name, tuple(s["alt_labels"]))
        )
    skill_ids = {s.id for s in skills}
    related = tuple((a, b) for a, b in raw["related"])
    for a, b in related:
        _check(a in skill_ids and b in skill_ids, f"related pair {a}, {b} names an unknown skill")
        _check(a < b, f"related pair {a}, {b} must be in alphabetical order")
    _check(len(set(related)) == len(related), "a related pair is listed twice")
    for ids, what in (
        (skill_ids, skills),
        ({a.id for a in areas}, areas),
        ({c.id for c in causes}, causes),
    ):
        _check(len(ids) == len(what), "an id is used twice")
    return Catalogue(
        causes=causes, skills=tuple(skills), themes=themes, areas=areas, related=related
    )


def text_query(text: str) -> str:
    """Plain words from free text, as a full-text query that matches any of them. Everything
    that is not a letter or a digit is dropped, so nothing typed can be read as query syntax."""
    words = re.findall(r"[^\W_]+", text.lower())
    kept = list(dict.fromkeys(w for w in words if w not in ("and", "or", "not")))
    return " OR ".join(kept[:12])


@cache
def get_catalogue() -> Catalogue:
    return load_catalogue()


def load_demo(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or FIXTURES_DIR / "demo.json").read_text(encoding="utf-8"))
