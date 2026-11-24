"""A volunteer's own profile: display name, skills and cause stances."""

from dataclasses import dataclass, field

from app.catalogue import get_catalogue
from app.services.errors import Conflict, Invalid, NotFound
from app.sqlstore import Database, enqueue

# State -> label. The one definition: the web templates, the API and the mobile app (which gets
# the labels from GET /api/v1/profile) all show these.
SKILL_STATES = {
    "none": "Not set",
    "has": "Have (not offering)",
    "offers": "Have and offer",
    "exclude": "Exclude from matching",
}
HAVE = ("has", "offers")  # the states in which a level means something
CAUSE_STANCES = {"neutral": "No preference", "prefer": "Prefer", "exclude": "Exclude"}


def skill_flags(state: str) -> tuple[int, int, int]:
    """A skill state as the `profile_skills` columns (has, offers, excluded)."""
    return int(state in ("has", "offers")), int(state == "offers"), int(state == "exclude")


def skill_state(has: int, offers: int, excluded: int) -> str:
    """The inverse of `skill_flags`. A stored row always has at least one flag set."""
    return "exclude" if excluded else "offers" if offers else "has" if has else "none"


@dataclass
class Profile:
    display_name: str
    version: int
    skills: dict[str, str] = field(default_factory=dict)  # skill_id -> SKILL_STATES
    causes: dict[str, str] = field(default_factory=dict)  # cause_id -> CAUSE_STANCES
    # skill_id -> 1..4 (app.services.engagements.LEVELS): how far they say they can go with a
    # skill they have. Only skills with a level appear.
    levels: dict[str, int] = field(default_factory=dict)
    learning: list[str] = field(default_factory=list)  # skill IDs they want to get better at


def get_profile(db: Database, user_id: str) -> Profile:
    with db.read() as conn:
        user = conn.execute(
            "SELECT display_name, profile_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise NotFound("user")
        profile = Profile(user["display_name"], user["profile_version"])
        for r in conn.execute(
            "SELECT skill_id, has, offers, excluded, level FROM profile_skills WHERE user_id = ?",
            (user_id,),
        ):
            profile.skills[r["skill_id"]] = skill_state(r["has"], r["offers"], r["excluded"])
            if r["level"]:
                profile.levels[r["skill_id"]] = r["level"]
        for r in conn.execute(
            "SELECT cause_id, stance FROM profile_causes WHERE user_id = ?", (user_id,)
        ):
            profile.causes[r["cause_id"]] = r["stance"]
        profile.learning = [
            r[0]
            for r in conn.execute(
                "SELECT skill_id FROM profile_learning WHERE user_id = ? ORDER BY skill_id",
                (user_id,),
            )
        ]
    return profile


def update_profile(
    db: Database,
    user_id: str,
    *,
    display_name: str,
    skills: dict[str, str],
    causes: dict[str, str],
    expected_version: int,
    levels: dict[str, int] | None = None,
    learning: list[str] | None = None,
) -> None:
    """`levels` or `learning` None leaves them as they are (a client that does not know about
    them changes nothing). A level is kept only for a skill the volunteer has; a skill they
    exclude cannot also be one they want to learn."""
    catalogue = get_catalogue()
    display_name = display_name.strip()
    if not 1 <= len(display_name) <= 60:
        raise Invalid("Display name must be 1–60 characters.")
    if set(skills) - catalogue.skill_ids or set(skills.values()) - set(SKILL_STATES):
        raise Invalid("Unknown skill or skill state.")
    if set(causes) - catalogue.cause_ids or set(causes.values()) - set(CAUSE_STANCES):
        raise Invalid("Unknown cause or cause preference.")
    if levels is not None and (
        set(levels) - catalogue.skill_ids or any(v not in (1, 2, 3, 4) for v in levels.values())
    ):
        raise Invalid("Unknown skill, or a level that is not between 1 and 4.")
    if learning is not None and set(learning) - catalogue.skill_ids:
        raise Invalid("Unknown skill to learn.")
    with db.tx() as conn:
        if learning is None:
            learning = [
                r[0]
                for r in conn.execute(
                    "SELECT skill_id FROM profile_learning WHERE user_id = ?", (user_id,)
                )
            ]
        if levels is None:
            levels = {
                r["skill_id"]: r["level"]
                for r in conn.execute(
                    "SELECT skill_id, level FROM profile_skills WHERE user_id = ? "
                    "AND level IS NOT NULL",
                    (user_id,),
                )
            }
        updated = conn.execute(
            "UPDATE users SET display_name = ?, profile_version = profile_version + 1 "
            "WHERE id = ? AND profile_version = ?",
            (display_name, user_id, expected_version),
        ).rowcount
        if not updated:
            raise Conflict("Your profile changed in another tab. Reload and try again.")
        conn.execute("DELETE FROM profile_skills WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO profile_skills (user_id, skill_id, has, offers, excluded, level) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (user_id, sid, *skill_flags(st), levels.get(sid) if st in HAVE else None)
                for sid, st in sorted(skills.items())
                if st != "none"
            ],
        )
        conn.execute("DELETE FROM profile_learning WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO profile_learning (user_id, skill_id) VALUES (?, ?)",
            [(user_id, sid) for sid in sorted(set(learning)) if skills.get(sid) != "exclude"],
        )
        conn.execute("DELETE FROM profile_causes WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO profile_causes (user_id, cause_id, stance) VALUES (?, ?, ?)",
            [(user_id, cid, st) for cid, st in sorted(causes.items()) if st != "neutral"],
        )
        enqueue(conn, "volunteer", user_id)
