"""Every Cypher statement the app runs. Labels and relationship types are fixed text; every value
is a bound parameter. Verified against Relate 1.0.1 (tests/test_projection.py and
tests/test_matching.py) and written in the Neo4j 5 dialect so the same text runs on AuraDB for
the parity check.

Graph model (no passwords, emails, sessions or review notes; volunteers are opaque IDs):

    (:Organisation)-[:OWNS_PROJECT]->(:Project)-[:SERVES_CAUSE]->(:Cause)
    (:Cause)-[:IN_THEME]->(:CauseTheme)              causes that are alike share a theme
    (:Skill)-[:IN_AREA]->(:SkillArea)-[:BROADER]->(:SkillArea)   two levels of skill area
    (:Skill)-[:RELATED_TO]->(:Skill)                 adjacent skills; stored once, read both ways
    (:Project)-[:REQUIRES_SKILL]->(:Skill)          required skill
    (:Project)-[:BENEFITS_FROM_SKILL]->(:Skill)     optional skill
    (:Volunteer)-[:VOLUNTEERS_WITH]->(:Organisation) only while opted in AND vetted (active)
    (:Volunteer)-[:ON_TEAM]->(:Project)              on the team: accepted, not suspended
    (:Volunteer)-[:HAS_SKILL|OFFERS_SKILL|EXCLUDES_SKILL]->(:Skill)
    (:Volunteer)-[:PREFERS_CAUSE|EXCLUDES_CAUSE]->(:Cause)
    (:Organisation)-[:HAS_ONBOARDING_STEP]->(:OnboardingStep)-[:NEXT_STEP]->(:OnboardingStep)
    (:Project)-[:HAS_TEAM_STEP]->(:OnboardingStep)-[:NEXT_STEP]->(:OnboardingStep)
    (:Volunteer)-[:AT_STEP]->(:OnboardingStep)        the step they have reached
    (:Volunteer)-[:COMPLETED_STEP]->(:OnboardingStep)

Every label has a uniqueness constraint on `id` (CONSTRAINTS below), applied by the projector.

An organisation's onboarding workflow is in the graph as it stands (`onboarding_enabled` on the
organisation says whether it is switched on), without its email templates: the assistants read
it from here. A project's own workflow, for volunteers joining its team, hangs off the project
the same way (HAS_TEAM_STEP, and `onboarding_enabled` on the project). A volunteer is ON_TEAM
only once the lead side has accepted them, and not while suspended from the team.

Reads that can return many rows end in `LIMIT $limit`. The store binds `$limit` itself
(app.graph.store.Tx.read) so that a result larger than the configured bound raises instead of
being cut short; callers never pass it.
"""

from typing import Any

# ------------------------------------------------------------------ schema
# The one definition of the graph's shape: relationship type -> (source label, target label).
# The reconcile statements below, the projector and the AuraDB push are all derived from it.
LABELS = (
    "Organisation",
    "Project",
    "Cause",
    "CauseTheme",
    "Skill",
    "SkillArea",
    "Volunteer",
    "OnboardingStep",
)
VOLUNTEER_RELS = {
    "VOLUNTEERS_WITH": "Organisation",
    "HAS_SKILL": "Skill",
    "OFFERS_SKILL": "Skill",
    "EXCLUDES_SKILL": "Skill",
    "PREFERS_CAUSE": "Cause",
    "EXCLUDES_CAUSE": "Cause",
}
PROJECT_RELS = {
    "SERVES_CAUSE": "Cause",
    "REQUIRES_SKILL": "Skill",
    "BENEFITS_FROM_SKILL": "Skill",
}
RELATIONSHIPS: dict[str, tuple[str, str]] = {
    **{rel: ("Volunteer", label) for rel, label in VOLUNTEER_RELS.items()},
    **{rel: ("Project", label) for rel, label in PROJECT_RELS.items()},
    "OWNS_PROJECT": ("Organisation", "Project"),
    "IN_THEME": ("Cause", "CauseTheme"),
    "IN_AREA": ("Skill", "SkillArea"),
    "BROADER": ("SkillArea", "SkillArea"),
    "RELATED_TO": ("Skill", "Skill"),
    "ON_TEAM": ("Volunteer", "Project"),
    "HAS_ONBOARDING_STEP": ("Organisation", "OnboardingStep"),
    "HAS_TEAM_STEP": ("Project", "OnboardingStep"),
    "NEXT_STEP": ("OnboardingStep", "OnboardingStep"),
    "AT_STEP": ("Volunteer", "OnboardingStep"),
    "COMPLETED_STEP": ("Volunteer", "OnboardingStep"),
}


# Declared schema: every node is found by its `id`, so each label has a uniqueness constraint (and
# with it an index). A duplicate would be counted twice by every match, silently; with the
# constraint the write is refused instead. `Project.status` is filtered on by every read.
# Each statement is idempotent; the projector applies them before it first writes
# (Projector.ensure_schema), so a store made before they existed gains them on the next start.
CONSTRAINTS = {
    f"{label.lower()}_id": (
        f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
    )
    for label in LABELS
}
INDEXES = {
    "project_status": "CREATE INDEX project_status IF NOT EXISTS FOR (p:Project) ON (p.status)",
    # A skill is found by its name or any of its other names (FIND_SKILLS).
    "skill_text": (
        "CREATE FULLTEXT INDEX skill_text IF NOT EXISTS "
        "FOR (s:Skill) ON EACH [s.name, s.alt_labels]"
    ),
}
SCHEMA = (*CONSTRAINTS.values(), *INDEXES.values())

SHOW_CONSTRAINTS = """
SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
RETURN name, type, labelsOrTypes AS labels, properties
ORDER BY name
LIMIT $limit
"""

SHOW_INDEXES = """
SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties
WHERE type <> 'LOOKUP'
RETURN name, type, state, labelsOrTypes AS labels, properties
ORDER BY name
LIMIT $limit
"""


def _edge_prune(source: str, rel: str, target: str) -> str:
    return f"""
MATCH (n:{source} {{id: $id}})-[r:{rel}]->(t:{target})
WHERE NOT t.id IN $targets
DELETE r
"""


def _edge_merge(source: str, rel: str, target: str) -> str:
    return f"""
MATCH (n:{source} {{id: $id}})
UNWIND $targets AS target
MATCH (t:{target} {{id: target}})
MERGE (n)-[:{rel}]->(t)
"""


# ------------------------------------------------------------------ catalogue + organisations
# The catalogue is reconciled as a whole (Projector._project_catalogue): nodes are upserted, areas
# and themes that are gone are removed, and the ontology's own edges are cleared and merged
# again, so a replay changes nothing. Causes and skills are never removed: profiles and
# projects point at them.
UPSERT_CAUSE_THEMES = """
UNWIND $rows AS row
MERGE (t:CauseTheme {id: row.id})
SET t.name = row.name
"""

UPSERT_CAUSES = """
UNWIND $rows AS row
MERGE (c:Cause {id: row.id})
SET c.name = row.name
"""

UPSERT_SKILL_AREAS = """
UNWIND $rows AS row
MERGE (a:SkillArea {id: row.id})
SET a.name = row.name
"""

UPSERT_SKILLS = """
UNWIND $rows AS row
MERGE (s:Skill {id: row.id})
SET s.name = row.name, s.category = row.category, s.kind = row.kind,
    s.alt_labels = row.alt_labels
"""

CATALOGUE_PRUNE = {
    "CauseTheme": "MATCH (n:CauseTheme) WHERE NOT n.id IN $ids DETACH DELETE n",
    "SkillArea": "MATCH (n:SkillArea) WHERE NOT n.id IN $ids DETACH DELETE n",
}

CATALOGUE_RELS = ("IN_THEME", "IN_AREA", "BROADER", "RELATED_TO")
CATALOGUE_EDGES_CLEAR = {
    rel: f"MATCH (:{RELATIONSHIPS[rel][0]})-[r:{rel}]->(:{RELATIONSHIPS[rel][1]}) DELETE r"
    for rel in CATALOGUE_RELS
}
CATALOGUE_EDGES_MERGE = {
    rel: f"""
UNWIND $rows AS row
MATCH (a:{RELATIONSHIPS[rel][0]} {{id: row.a}})
MATCH (b:{RELATIONSHIPS[rel][1]} {{id: row.b}})
MERGE (a)-[:{rel}]->(b)
"""
    for rel in CATALOGUE_RELS
}

UPSERT_ORGANISATION = """
MERGE (o:Organisation {id: $id})
SET o.name = $name
"""

# ------------------------------------------------------------------ volunteer reconcile
# One relationship type at a time: delete edges to targets no longer listed, then MERGE the
# listed ones. Idempotent, so replaying an outbox item is harmless.
VOLUNTEER_UPSERT = "MERGE (v:Volunteer {id: $id})"

VOLUNTEER_EDGE_PRUNE = {
    rel: _edge_prune("Volunteer", rel, label) for rel, label in VOLUNTEER_RELS.items()
}
VOLUNTEER_EDGE_MERGE = {
    rel: _edge_merge("Volunteer", rel, label) for rel, label in VOLUNTEER_RELS.items()
}

# ------------------------------------------------------------------ project reconcile
PROJECT_UPSERT = """
MATCH (o:Organisation {id: $org})
MERGE (p:Project {id: $id})
SET p.title = $title, p.summary = $summary, p.revision = $revision, p.status = 'published'
MERGE (o)-[:OWNS_PROJECT]->(p)
"""

PROJECT_EDGE_PRUNE = {
    rel: _edge_prune("Project", rel, label) for rel, label in PROJECT_RELS.items()
}
PROJECT_EDGE_MERGE = {
    rel: _edge_merge("Project", rel, label) for rel, label in PROJECT_RELS.items()
}

PROJECT_REMOVE = "MATCH (p:Project {id: $id}) DETACH DELETE p"
# Run first: a removed project takes its own onboarding steps with it.
PROJECT_STEPS_REMOVE = """
MATCH (:Project {id: $id})-[:HAS_TEAM_STEP]->(s:OnboardingStep)
DETACH DELETE s
"""

# Team membership: both the volunteer and the project lead said yes (app.placements).
PROJECT_TEAM_PRUNE = """
MATCH (v:Volunteer)-[r:ON_TEAM]->(p:Project {id: $id})
WHERE NOT v.id IN $targets
DELETE r
"""

PROJECT_TEAM_MERGE = """
MATCH (p:Project {id: $id})
UNWIND $targets AS target
MATCH (v:Volunteer {id: target})
MERGE (v)-[:ON_TEAM]->(p)
"""

PROJECT_OWNED = """
MATCH (:Organisation {id: $org})-[:OWNS_PROJECT]->(p:Project {id: $id})
RETURN count(p) AS n
"""


# ------------------------------------------------------------------ onboarding reconcile
# One organisation's workflow at a time (app.services.onboarding): its switch, its steps in
# order, and where each volunteer is. Stale steps and edges are removed, then the current ones
# are merged, so a replay changes nothing.
ONBOARDING_ORG = """
MATCH (o:Organisation {id: $org})
SET o.onboarding_enabled = $enabled
RETURN count(o) AS n
"""

ONBOARDING_STEPS_PRUNE = """
MATCH (:Organisation {id: $org})-[:HAS_ONBOARDING_STEP]->(s:OnboardingStep)
WHERE NOT s.id IN $ids
DETACH DELETE s
"""

ONBOARDING_STEPS_UPSERT = """
MATCH (o:Organisation {id: $org})
UNWIND $rows AS row
MERGE (s:OnboardingStep {id: row.id})
SET s.title = row.title, s.description = row.description, s.position = row.position,
    s.completed_by = row.completed_by, s.sends_email = row.sends_email
MERGE (o)-[:HAS_ONBOARDING_STEP]->(s)
"""

ONBOARDING_EDGES_CLEAR = {
    "NEXT_STEP": """
MATCH (:Organisation {id: $org})-[:HAS_ONBOARDING_STEP]->(:OnboardingStep)-[r:NEXT_STEP]->()
DELETE r
""",
    "AT_STEP": """
MATCH (:Organisation {id: $org})-[:HAS_ONBOARDING_STEP]->(:OnboardingStep)<-[r:AT_STEP]-()
DELETE r
""",
    "COMPLETED_STEP": """
MATCH (:Organisation {id: $org})-[:HAS_ONBOARDING_STEP]->(:OnboardingStep)<-[r:COMPLETED_STEP]-()
DELETE r
""",
}

ONBOARDING_NEXT_MERGE = """
UNWIND $rows AS row
MATCH (a:OnboardingStep {id: row.a})
MATCH (b:OnboardingStep {id: row.b})
MERGE (a)-[:NEXT_STEP]->(b)
"""

ONBOARDING_PROGRESS_MERGE = {
    rel: f"""
UNWIND $rows AS row
MATCH (s:OnboardingStep {{id: row.step}})
MERGE (v:Volunteer {{id: row.volunteer}})
MERGE (v)-[:{rel}]->(s)
"""
    for rel in ("AT_STEP", "COMPLETED_STEP")
}

# A project's own workflow, reconciled with the project (Projector._project_project). Its steps
# are OnboardingStep nodes too, so NEXT_STEP and the progress edges above are shared.
PROJECT_ONBOARDING = """
MATCH (p:Project {id: $id})
SET p.onboarding_enabled = $enabled
"""

PROJECT_STEPS_PRUNE = """
MATCH (:Project {id: $id})-[:HAS_TEAM_STEP]->(s:OnboardingStep)
WHERE NOT s.id IN $ids
DETACH DELETE s
"""

PROJECT_STEPS_UPSERT = """
MATCH (p:Project {id: $id})
UNWIND $rows AS row
MERGE (s:OnboardingStep {id: row.id})
SET s.title = row.title, s.description = row.description, s.position = row.position,
    s.completed_by = row.completed_by, s.sends_email = row.sends_email
MERGE (p)-[:HAS_TEAM_STEP]->(s)
"""

PROJECT_STEP_EDGES_CLEAR = {
    "NEXT_STEP": """
MATCH (:Project {id: $id})-[:HAS_TEAM_STEP]->(:OnboardingStep)-[r:NEXT_STEP]->()
DELETE r
""",
    "AT_STEP": """
MATCH (:Project {id: $id})-[:HAS_TEAM_STEP]->(:OnboardingStep)<-[r:AT_STEP]-()
DELETE r
""",
    "COMPLETED_STEP": """
MATCH (:Project {id: $id})-[:HAS_TEAM_STEP]->(:OnboardingStep)<-[r:COMPLETED_STEP]-()
DELETE r
""",
}

# ------------------------------------------------------------------ onboarding reads
# The steps of the organisations whose workflow is switched on.
ONBOARDING_FLOWS = """
MATCH (o:Organisation)-[:HAS_ONBOARDING_STEP]->(s:OnboardingStep)
WHERE o.onboarding_enabled AND o.id IN $orgs
RETURN o.id AS org, s.id AS step, s.position AS position, s.title AS title,
       s.description AS description, s.completed_by AS completed_by
ORDER BY org, position
LIMIT $limit
"""

# One volunteer's onboarding: every step of each organisation whose workflow they are in, and
# whether they have completed it or have reached it.
ONBOARDING_FOR_VOLUNTEER = """
MATCH (v:Volunteer {id: $volunteer})-[:AT_STEP|COMPLETED_STEP]->(:OnboardingStep)
      <-[:HAS_ONBOARDING_STEP]-(o:Organisation)
WITH DISTINCT v, o
MATCH (o)-[:HAS_ONBOARDING_STEP]->(s:OnboardingStep)
RETURN o.id AS org, o.name AS organisation, s.id AS step, s.position AS position,
       s.title AS title, s.description AS description, s.completed_by AS completed_by,
       EXISTS { MATCH (v)-[:COMPLETED_STEP]->(s) } AS done,
       EXISTS { MATCH (v)-[:AT_STEP]->(s) } AS current
ORDER BY organisation, org, position
LIMIT $limit
"""

# The same for the projects whose teams the volunteer is joining or has joined. `on_team` says
# whether the lead side has accepted them: steps all done and not on the team means waiting.
PROJECT_ONBOARDING_FOR_VOLUNTEER = """
MATCH (v:Volunteer {id: $volunteer})-[:AT_STEP|COMPLETED_STEP]->(:OnboardingStep)
      <-[:HAS_TEAM_STEP]-(p:Project)<-[:OWNS_PROJECT]-(o:Organisation)
WITH DISTINCT v, p, o
MATCH (p)-[:HAS_TEAM_STEP]->(s:OnboardingStep)
RETURN o.id AS org, o.name AS organisation, p.id AS project, p.title AS project_title,
       s.id AS step, s.position AS position, s.title AS title, s.description AS description,
       s.completed_by AS completed_by,
       EXISTS { MATCH (v)-[:COMPLETED_STEP]->(s) } AS done,
       EXISTS { MATCH (v)-[:AT_STEP]->(s) } AS current,
       EXISTS { MATCH (v)-[:ON_TEAM]->(p) } AS on_team
ORDER BY organisation, org, project, position
LIMIT $limit
"""


# ------------------------------------------------------------------ matching
# The matching decision is made here, by graph relationships:
#   - project in scope:   (o)-[:OWNS_PROJECT]->(p) with p.status 'published'
#   - membership:         (v)-[:VOLUNTEERS_WITH]->(o)
#   - hard exclusions:    no (v)-[:EXCLUDES_CAUSE]->(cause), and no required skill of p that
#                         v EXCLUDES_SKILL; either removes the pair even if another skill fits
#   - eligible skill:     required skill that v both HAS_SKILL and OFFERS_SKILL
#   - optional skills:    BENEFITS_FROM_SKILL that v has and offers and has not excluded
#   - preference:         (v)-[:PREFERS_CAUSE]->(cause) ranks ahead; absence is no barrier
# Rank: preferred cause, required coverage, optional coverage, then stable IDs.
def match_params(
    *, orgs: list[str], project: str = "", volunteer: str = "", preferred_only: bool = False
) -> dict[str, Any]:
    """Parameters for MATCH_CANDIDATES; an empty project or volunteer means "all of them"."""
    return {
        "orgs": orgs,
        "project": project,
        "volunteer": volunteer,
        "preferred_only": preferred_only,
    }


MATCH_CANDIDATES = """
MATCH (o:Organisation)-[:OWNS_PROJECT]->(p:Project)-[:SERVES_CAUSE]->(c:Cause)
WHERE o.id IN $orgs AND p.status = 'published' AND ($project = '' OR p.id = $project)
MATCH (v:Volunteer)-[:VOLUNTEERS_WITH]->(o)
WHERE ($volunteer = '' OR v.id = $volunteer)
  AND NOT (v)-[:EXCLUDES_CAUSE]->(c)
  AND NOT EXISTS { MATCH (p)-[:REQUIRES_SKILL]->(:Skill)<-[:EXCLUDES_SKILL]-(v) }
MATCH (p)-[:REQUIRES_SKILL]->(s:Skill)<-[:HAS_SKILL]-(v)
WHERE (v)-[:OFFERS_SKILL]->(s)
WITH o, p, c, v, collect(DISTINCT s.id) AS matched
OPTIONAL MATCH (p)-[:BENEFITS_FROM_SKILL]->(os:Skill)<-[:OFFERS_SKILL]-(v)
WHERE (v)-[:HAS_SKILL]->(os) AND NOT (v)-[:EXCLUDES_SKILL]->(os)
WITH o, p, c, v, matched, collect(DISTINCT os.id) AS optional_matched
CALL (p) {
  MATCH (p)-[:REQUIRES_SKILL]->(r:Skill)
  RETURN collect(r.id) AS required
}
WITH o, p, c, v, matched, optional_matched, required,
     EXISTS { MATCH (v)-[:PREFERS_CAUSE]->(c) } AS preferred
WHERE NOT $preferred_only OR preferred
RETURN o.id AS org, p.id AS project, p.title AS title, p.revision AS revision,
       c.id AS cause, c.name AS cause_name, v.id AS volunteer,
       matched, optional_matched, required,
       [r IN required WHERE NOT r IN matched] AS remaining,
       preferred, size(matched) AS coverage, size(optional_matched) AS optional_coverage
ORDER BY preferred DESC, coverage DESC, optional_coverage DESC, project, volunteer
LIMIT $limit
"""

# Required skills that no eligible, opted-in volunteer of the organisation can cover.
COVERAGE_GAPS = """
MATCH (o:Organisation)-[:OWNS_PROJECT]->(p:Project)-[:REQUIRES_SKILL]->(s:Skill)
MATCH (p)-[:SERVES_CAUSE]->(c:Cause)
WHERE o.id = $org AND p.status = 'published' AND ($project = '' OR p.id = $project)
  AND NOT EXISTS {
    MATCH (v:Volunteer)-[:VOLUNTEERS_WITH]->(o)
    WHERE (v)-[:HAS_SKILL]->(s) AND (v)-[:OFFERS_SKILL]->(s)
      AND NOT (v)-[:EXCLUDES_CAUSE]->(c)
      AND NOT EXISTS { MATCH (p)-[:REQUIRES_SKILL]->(:Skill)<-[:EXCLUDES_SKILL]-(v) }
  }
RETURN p.id AS project, p.title AS title, s.id AS skill, s.name AS skill_name
ORDER BY project, skill
LIMIT $limit
"""

# Who on the team covers each required skill. An empty `team` is a skill the team still lacks,
# whatever the candidate list says: coverage counts only once both sides have said yes.
TEAM_COVERAGE = """
MATCH (o:Organisation {id: $org})-[:OWNS_PROJECT]->(p:Project)-[:REQUIRES_SKILL]->(s:Skill)
WHERE p.status = 'published' AND ($project = '' OR p.id = $project)
OPTIONAL MATCH (v:Volunteer)-[:ON_TEAM]->(p)
WHERE (v)-[:HAS_SKILL]->(s) AND (v)-[:OFFERS_SKILL]->(s)
WITH p, s, v ORDER BY v.id
RETURN p.id AS project, s.id AS skill, collect(v.id) AS team
ORDER BY project, skill
LIMIT $limit
"""

# For the volunteer's own inspection only: which in-scope projects their exclusions remove.
EXCLUDED_FOR_VOLUNTEER = """
MATCH (v:Volunteer {id: $volunteer})-[:VOLUNTEERS_WITH]->(o:Organisation)
      -[:OWNS_PROJECT]->(p:Project)-[:SERVES_CAUSE]->(c:Cause)
WHERE o.id IN $orgs AND p.status = 'published'
CALL (v, p) {
  OPTIONAL MATCH (p)-[:REQUIRES_SKILL]->(s:Skill)<-[:EXCLUDES_SKILL]-(v)
  RETURN collect(s.id) AS excluded_skills
}
WITH o, p, c, excluded_skills, EXISTS { MATCH (v)-[:EXCLUDES_CAUSE]->(c) } AS cause_excluded
WHERE cause_excluded OR size(excluded_skills) > 0
RETURN o.id AS org, p.id AS project, p.title AS title, c.id AS cause, c.name AS cause_name,
       cause_excluded, excluded_skills
ORDER BY org, project
LIMIT $limit
"""

# Optional skills the volunteer has excluded, per project (omitted from scoring/explanations).
OPTIONAL_SKILLS_EXCLUDED = """
MATCH (v:Volunteer {id: $volunteer})-[:VOLUNTEERS_WITH]->(o:Organisation)
      -[:OWNS_PROJECT]->(p:Project)-[:BENEFITS_FROM_SKILL]->(s:Skill)<-[:EXCLUDES_SKILL]-(v)
WHERE o.id IN $orgs AND p.status = 'published'
RETURN p.id AS project, collect(s.id) AS skills
ORDER BY project
LIMIT $limit
"""

# ------------------------------------------------------------------ the skills ontology
# "Close" means adjacent in the ontology: the two skills are RELATED_TO each other, or sit in the
# same area. It never makes anyone eligible: it is shown beside the candidates, not among them.
#
# Volunteers of the organisation who do not have a required skill but offer one close to it.
# The hard rules still apply: no excluded cause, no excluded required skill. Only offered skills
# count as the way in, because offering is the volunteer's consent to be matched on a skill.
CLOSE_CANDIDATES = """
MATCH (o:Organisation {id: $org})-[:OWNS_PROJECT]->(p:Project)-[:REQUIRES_SKILL]->(s:Skill)
WHERE p.status = 'published' AND ($project = '' OR p.id = $project)
MATCH (p)-[:SERVES_CAUSE]->(c:Cause)
MATCH (v:Volunteer)-[:VOLUNTEERS_WITH]->(o)
WHERE NOT (v)-[:HAS_SKILL]->(s)
  AND NOT (v)-[:EXCLUDES_CAUSE]->(c)
  AND NOT EXISTS { MATCH (p)-[:REQUIRES_SKILL]->(:Skill)<-[:EXCLUDES_SKILL]-(v) }
MATCH (v)-[:OFFERS_SKILL]->(near:Skill)
WHERE (near)-[:RELATED_TO]-(s)
   OR EXISTS { MATCH (near)-[:IN_AREA]->(:SkillArea)<-[:IN_AREA]-(s) }
WITH p, s, v, near ORDER BY near.id
RETURN p.id AS project, s.id AS skill, v.id AS volunteer, collect(DISTINCT near.id) AS via
ORDER BY project, skill, volunteer
LIMIT $limit
"""

# A skill found by words from its name or its other names, with where it sits in the ontology.
# `$query` is a full-text query; the caller builds it from plain words (app.catalogue.text_query).
FIND_SKILLS = """
CALL db.index.fulltext.queryNodes('skill_text', $query) YIELD node, score
MATCH (node)-[:IN_AREA]->(a:SkillArea)-[:BROADER]->(top:SkillArea)
RETURN node.id AS skill, node.name AS name, node.kind AS kind, a.name AS area,
       top.name AS broader_area, score,
       COLLECT { MATCH (node)-[:RELATED_TO]-(r:Skill) RETURN r.id ORDER BY r.id } AS related
ORDER BY score DESC, skill
LIMIT $limit
"""

# ------------------------------------------------------------------ browse + inspection
BROWSE_PROJECTS = """
MATCH (o:Organisation {id: $org})-[:OWNS_PROJECT]->(p:Project)-[:SERVES_CAUSE]->(c:Cause)
WHERE p.status = 'published' AND ($project = '' OR p.id = $project)
CALL (p) {
  OPTIONAL MATCH (p)-[:REQUIRES_SKILL]->(r:Skill)
  RETURN collect(r.id) AS required
}
CALL (p) {
  OPTIONAL MATCH (p)-[:BENEFITS_FROM_SKILL]->(b:Skill)
  RETURN collect(b.id) AS optional
}
RETURN p.id AS project, p.title AS title, p.summary AS summary, p.revision AS revision,
       c.id AS cause, c.name AS cause_name, required, optional
ORDER BY title, project
LIMIT $limit
"""

# The judges' query: organisation -> approved project -> cause and skills -> eligible volunteer.
INSPECT = """
MATCH (o:Organisation)-[:OWNS_PROJECT]->(p:Project)-[:SERVES_CAUSE]->(c:Cause)
MATCH (p)-[:REQUIRES_SKILL]->(s:Skill)
OPTIONAL MATCH (v:Volunteer)-[:VOLUNTEERS_WITH]->(o)
WHERE (v)-[:HAS_SKILL]->(s) AND (v)-[:OFFERS_SKILL]->(s)
  AND NOT (v)-[:EXCLUDES_CAUSE]->(c)
  AND NOT EXISTS { MATCH (p)-[:REQUIRES_SKILL]->(:Skill)<-[:EXCLUDES_SKILL]-(v) }
WITH o, p, c, s, v ORDER BY v.id
RETURN o.name AS organisation, p.title AS project, c.name AS cause, s.name AS skill,
       collect(v.id) AS eligible_volunteers
ORDER BY organisation, project, skill
LIMIT $limit
"""

COUNT_LABELS = """
MATCH (n)
RETURN labels(n)[0] AS label, count(n) AS nodes
ORDER BY label
LIMIT $limit
"""

COUNT_RELATIONSHIPS = """
MATCH ()-[r]->()
RETURN type(r) AS type, count(r) AS relationships
ORDER BY type
LIMIT $limit
"""

ALL_NODES = """
MATCH (n)
RETURN labels(n)[0] AS label, n.id AS id, properties(n) AS props
ORDER BY label, id
LIMIT $limit
"""

ALL_RELATIONSHIPS = """
MATCH (a)-[r]->(b)
RETURN labels(a)[0] AS from_label, a.id AS from_id, type(r) AS type,
       labels(b)[0] AS to_label, b.id AS to_id
ORDER BY from_label, from_id, type, to_label, to_id
LIMIT $limit
"""
