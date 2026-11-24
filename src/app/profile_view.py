"""The profile screen's data, shared by the web page (/profile) and the mobile API
(GET/PUT /api/v1/profile) so both show the same structure and wording:

    display name
    Skills            one sub-section per skill category (type), each skill: SKILL_STATES
    Causes            each cause: CAUSE_STANCES
    Active projects   own suggestions in progress or published, plus published projects matched
    Past projects     own suggestions that were rejected or withdrawn

Active and past projects are the volunteer's suggestions and current matches. What they have
actually done on teams is their history (app.services.engagements.my_history), shown below the
profile form on the web and served at GET /api/v1/me/history. The labels are sent with the data,
so the mobile app does not keep a copy.
"""

from app import matching, services
from app.catalogue import get_catalogue
from app.graph.store import GraphStore
from app.sqlstore import Database

# The labels live with the rules they describe (app.services); re-exported for the templates.
SKILL_STATES = services.SKILL_STATES
CAUSE_STANCES = services.CAUSE_STANCES
PROJECT_STATES = services.PROJECT_STATES
# A draft is in neither list: it shows under "My suggestions" only, not among the projects.
ACTIVE_STATES = ("submitted", "changes_requested", "approved", "published")
PAST_STATES = ("rejected", "withdrawn")


def overview(db: Database, graph: GraphStore, user_id: str) -> dict:
    cat = get_catalogue()
    profile = services.get_profile(db, user_id)
    suggestions = services.my_submissions(db, user_id)
    volunteering = [o["id"] for o in services.directory(db, user_id) if o["opted_in"]]
    matches = matching.volunteer_matches(db, graph, user_id, volunteering)

    def suggestion(s: dict) -> dict:
        return {
            "id": s["id"], "title": s["title"], "organisation": s["org_name"],
            "org_id": s["org_id"], "relation": "suggested", "state": s["state"],
            "state_label": PROJECT_STATES[s["state"]], "detail": None,
        }  # fmt: skip

    active = [suggestion(s) for s in suggestions if s["state"] in ACTIVE_STATES]
    seen = {a["id"] for a in active}
    active += [
        {
            "id": m["project"],
            "title": m["title"],
            "organisation": m["org_name"],
            "org_id": m["org"],
            "relation": "matched",
            "state": "published",
            "state_label": PROJECT_STATES["published"],
            "detail": "You match: " + ", ".join(m["matched_names"]),
        }  # fmt: skip
        for m in matches.matches
        if m["project"] not in seen
    ]
    skills = [
        {
            "id": s.id,
            "name": s.name,
            "category": s.category,
            "state": profile.skills.get(s.id, "none"),
            "level": profile.levels.get(s.id),
        }
        for s in cat.skills
    ]
    by_category: dict[str, list[dict]] = {}  # catalogue order, grouped by category
    for skill in skills:
        by_category.setdefault(skill["category"], []).append(skill)
    groups = [{"category": c, "skills": members} for c, members in by_category.items()]
    return {
        "display_name": profile.display_name,
        "version": profile.version,
        "skills": skills,
        "skill_groups": groups,
        "skill_states": SKILL_STATES,
        "levels": {str(k): v for k, v in services.engagements.LEVELS.items()},
        "cause_stances": CAUSE_STANCES,
        "causes": [
            {"id": c.id, "name": c.name, "stance": profile.causes.get(c.id, "neutral")}
            for c in cat.causes
        ],
        "projects": {
            "active": active,
            "past": [suggestion(s) for s in suggestions if s["state"] in PAST_STATES],
            "matches_withheld": matches.withheld.reason if matches.withheld else None,
        },
    }
