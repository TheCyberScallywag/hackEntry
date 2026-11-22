"""Every Cypher statement the app runs. Labels and relationship types are fixed text; every value
is a bound parameter. Verified against Relate 1.0.1 (tests/test_projection.py and
tests/test_matching.py) and written in the Neo4j 5 dialect so the same text runs on AuraDB for
the parity check.

Graph model (no passwords, emails, sessions or review notes; volunteers are opaque IDs):

    (:Organisation)-[:OWNS_PROJECT]->(:Project)-[:SERVES_CAUSE]->(:Cause)
    (:Project)-[:REQUIRES_SKILL]->(:Skill)          required skill
    (:Project)-[:BENEFITS_FROM_SKILL]->(:Skill)     optional skill
    (:Volunteer)-[:VOLUNTEERS_WITH]->(:Organisation) only while opted in AND vetted (active)
    (:Volunteer)-[:ON_TEAM]->(:Project)              invited by the lead side and said yes
    (:Volunteer)-[:HAS_SKILL|OFFERS_SKILL|EXCLUDES_SKILL]->(:Skill)
    (:Volunteer)-[:PREFERS_CAUSE|EXCLUDES_CAUSE]->(:Cause)

Reads that can return many rows end in `LIMIT $limit`. The store binds `$limit` itself
(app.graph.store.Tx.read) so that a result larger than the configured bound raises instead of
being cut short; callers never pass it.
"""

from typing import Any

# ------------------------------------------------------------------ schema
# The one definition of the graph's shape: relationship type -> (source label, target label).
# The reconcile statements below, the projector and the AuraDB push are all derived from it.
LABELS = ("Organisation", "Project", "Cause", "Skill", "Volunteer")
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
    "ON_TEAM": ("Volunteer", "Project"),
}


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
UPSERT_CAUSES = """
UNWIND $rows AS row
MERGE (c:Cause {id: row.id})
SET c.name = row.name
"""

UPSERT_SKILLS = """
UNWIND $rows AS row
MERGE (s:Skill {id: row.id})
SET s.name = row.name, s.category = row.category
"""

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
