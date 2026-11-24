# Graph model recommendations: a fuller picture of the volunteer

Written 24 November 2026 against commit `de8702f`. It follows up two findings in
`HACKATHON_REVIEW.md`: no query crosses organisations, and the graph has no constraints or
indexes.

Nothing in the repository was changed apart from adding this file. Every statement and result
below was run with Relate 1.0.1 on a scratch copy of `data/demo.rl`. The levels, past projects,
ratings and credentials used in the results are invented for the prototype; section 7 says how
to make them real.

## 1. Summary

The graph today knows three things about a volunteer: which organisations they are with, which
skills they have, offer or exclude, and which causes they prefer or exclude. It knows nothing
about what they have done, how well, or what they could grow into. That is why no useful
question crosses organisations: there is nothing to carry across.

I recommend five changes, in this order:

1. **Declare the schema.** A uniqueness constraint on `id` for every label, an index on
   `Project.status`, and full-text indexes on skills and projects. All of it works in Relate
   1.0.1 and applies to the demo graph in about 30 ms.
2. **Turn the skill and cause lists into a small ontology.** Skill areas in a two-level
   hierarchy, `RELATED_TO` links between skills, alternative labels, and cause themes. This is a
   catalogue change only: no form, no new personal data.
3. **Add the cross-organisation question** "where am I needed?", for the volunteer and as an
   assistant tool. It runs on today's data plus the constraints.
4. **Record participation.** Keep completed projects in the graph and add an `Engagement` for
   each time a volunteer was on a team, with the role they held, the skills they showed and a
   four-step readiness rating from "needs support" to "could lead a project". Add a level to
   each skill on the profile.
5. **Later: credentials, availability and learning goals.** Each is small in the graph but
   needs a form and a policy decision.

Items 1 to 3 fit before the 11 December deadline and need no decision about personal data.
Item 4 does need one (section 6). If time is short, seed history in the demo fixture and show
it read-only, without building the rating form.

## 2. What is there today

### 2.1 The graph

Six labels and fifteen relationship types, defined once in `src/app/graph/cypher.py`. The
bundled graph has 49 nodes and 97 relationships.

| Finding | Detail |
|---|---|
| No constraints or indexes | `MERGE (s:Skill {id: ...})` relies on the projector never racing itself. A duplicate `id` would double-count matches silently. |
| No relationship carries a property | `HAS_SKILL` says a volunteer has a skill, not how well or since when. Relate does support relationship properties, typed and indexed. |
| `Skill.category` is a string | Ten categories for fifteen skills. A skill has no neighbours, so "nearly matches" cannot be asked. |
| `Project.status` is always `'published'` | A project that is withdrawn or finished is deleted from the graph (`PROJECT_REMOVE`), so the graph has no past. |
| Title and summary are stored, never searched | A full-text index over them works in Relate. |
| Every read starts from organisations the caller is already in | `MATCH_CANDIDATES`, `EXCLUDED_FOR_VOLUNTEER` and `BROWSE_PROJECTS` all filter on `o.id IN $orgs`. |
| `Volunteer` nodes exist for people who do not volunteer | All 14 seeded users have a node, including the admin and moderator. Harmless, since they have no `VOLUNTEERS_WITH` edge, but the label means "user". |

### 2.2 SQLite

SQLite is the source of truth and is in good shape. What it lacks for this work:

| Table | Gap |
|---|---|
| `skills` | `category` text only. No hierarchy, no related skills, no synonyms. |
| `causes` | Flat list of five. |
| `profile_skills` | Three booleans (`has`, `offers`, `excluded`). No level, no "want to learn". |
| `revision_skills` | `required` yes or no. No minimum level. |
| `submissions` | No `completed` state. A project is published until someone withdraws it. |
| `placements` | One row per project and volunteer, overwritten when a placement is reopened. The history is only in `placement_events`. No end date, role or outcome. |
| (none) | No credentials (DBS check, first aid certificate, driving licence), no availability, no location. |

### 2.3 Models

- `web/schemas.py`: `ProfileIn.skills` is `dict[str, str]` (skill to state). It has no room for
  a level without a change of shape.
- `profile_view.py` says "There is no 'volunteer joined a project' record" and builds "active"
  and "past" projects from the volunteer's own suggestions. Placements have existed since then;
  the profile still does not show teams the volunteer was on.
- The assistants' tools each run a fixed statement the pages already run. None can ask about
  history or neighbouring skills, because the graph has none.

## 3. Facets of a volunteer

"Who sets it" matters as much as the data: a self-declared level and a lead's rating are
different kinds of evidence and should stay distinguishable.

| Facet | Question it answers | Who sets it | Graph shape | When |
|---|---|---|---|---|
| Skill level | How far can they go on their own with this skill? | Volunteer | `level` on `HAS_SKILL` | Step 4 |
| Participation | What have they done, where, in what role? | System, from placements | `Engagement` node | Step 4 |
| Readiness | Did they need support, or could they lead? | Lead side, when an engagement ends | `readiness` on `Engagement` | Step 4 |
| Demonstrated skill | Which skills did they use, at what level? | Lead side | `DEMONSTRATED {level}` from `Engagement` to `Skill` | Step 4 |
| Cause experience | Have they worked on this cause, or a similar one, elsewhere? | Derived | A path through `Engagement`, `Project`, `Cause`, `CauseTheme`; nothing stored | Step 4 |
| History sharing | Which organisations may see history from elsewhere? | Volunteer | `SHARES_HISTORY_WITH` to `Organisation` | Step 4 |
| Learning goals | What do they want to get better at? | Volunteer | `WANTS_TO_LEARN` to `Skill` | Step 5 |
| Credentials | Do they hold a current DBS check, licence, certificate? | Org admin verifies | `HOLDS {expires}` to `Credential` | Step 5 |
| Availability | How much time, remote or in person, roughly where? | Volunteer | Properties on `Volunteer`; `Area` nodes for place | Step 5 |
| Worked together | Has this team worked together before? | Derived | Two engagements on one project | Not recommended yet |
| Reliability | Do they answer invitations and finish onboarding? | Derived from `placement_events` | None | **Not recommended** |

I would not build a reliability score. It can be derived, but it rates a person on things with
many innocent causes, and it is the facet most likely to cause harm if it is wrong.

### 3.1 One scale, used twice

| Value | Key | Label | Meaning |
|---|---|---|---|
| 1 | `supported` | Needs support | Contributes with guidance from someone more experienced |
| 2 | `independent` | Works independently | Takes a task and finishes it without help |
| 3 | `guides` | Can guide others | Reviews, teaches, or unblocks other volunteers |
| 4 | `leads` | Could lead a project | Could scope the work and run the team |

It is used in two places, and they should not be merged:

- **Per skill**, self-declared on the profile (`HAS_SKILL.level`) and confirmed by a lead on an
  engagement (`DEMONSTRATED.level`). The screen can then say "says 3, shown at 3 on two
  projects".
- **Per engagement**, as the lead's overall view of the volunteer in that role
  (`Engagement.readiness`).

Store the integer in the graph so that queries can compare it. Keep the labels in one place, as
`SKILL_STATES` are in `services/profile.py`. Do not show an average: show the engagements.

## 4. A basic ontology for skills

The aim is modest: give each skill neighbours, so the graph can say "close" as well as "yes" or
"no". The structure follows SKOS, the usual pattern for a controlled vocabulary: concepts with
broader concepts, related concepts and alternative labels.

### 4.1 Structure

```
(:Skill)-[:IN_AREA]->(:SkillArea)-[:BROADER]->(:SkillArea)    two levels of area
(:Skill)-[:RELATED_TO]-(:Skill)                               adjacent; read in both directions
(:Skill {kind, alt_labels})                                   kind of skill; synonyms
(:Cause)-[:IN_THEME]->(:CauseTheme)                           the same idea for causes
```

Rules that keep it basic:

- A skill sits in exactly one area. Overlap is expressed with `RELATED_TO`, not a second parent.
- Areas are a separate label, so `REQUIRES_SKILL` can only ever point at a real skill.
- `RELATED_TO` is stored once per pair and matched without a direction.
- `kind` is one of `technical`, `practical`, `interpersonal`, `professional`, `language`.
- `alt_labels` is a list of synonyms. It feeds the full-text index, so "dashboards" finds data
  visualisation, and it lets the scoping assistant map free text to a catalogue skill.
- IDs never change once published, as the catalogue already says. A skill can be retired, not
  renamed.
- Leave room for an optional `external_uri` per skill if you later align with a public
  framework such as ESCO. Do not import one now: thousands of skills would swamp fifteen.

### 4.2 Seed

The existing ten categories become second-level areas unchanged, except that Technology splits
into three. Four top-level areas group them.

| Top area | Area | Skills |
|---|---|---|
| Digital and data | Data | Spreadsheet and data analysis, Data visualisation |
| | Software and web | Python programming, Web development |
| | Digital safety | Cyber security awareness |
| People and community | Community support | Active listening |
| | Education | Literacy tutoring |
| | Languages | Welsh translation |
| Organisation and finance | Finance | Bookkeeping |
| | Charity operations | Fundraising |
| Practical and logistics | Practical trades | Carpentry, Gardening |
| | Hospitality | Community cooking |
| | Transport | Van driving |
| | Health and safety | First aid |

Related pairs in the prototype: data analysis and data visualisation; Python and data analysis;
Python and web development; web development and security awareness; bookkeeping and data
analysis; fundraising and bookkeeping; cooking and first aid; literacy tutoring and active
listening; carpentry and gardening.

Cause themes: **Poverty and basic needs** (Homelessness, Food Security), **Learning and
inclusion** (Education, Digital Inclusion), **Environment and place** (Environment).

These groupings are my suggestion from the names alone. Someone who knows the organisations
should check them, the related pairs most of all.

### 4.3 How matching uses it

The hard rules do not change: exclusions, vetting and membership still decide eligibility, and
an adjacent skill never counts as coverage. The ontology adds two things:

- **A "close" list** beside the candidates: volunteers who lack a required skill but have a
  related one or one in the same area, ranked first if they have said they want to learn it.
- **Rank order** within the eligible: cause experience in the same theme ranks ahead of none.

## 5. Proposed graph model

Additions only. Every existing label, relationship and statement keeps working: with the whole
prototype applied, `MATCH_CANDIDATES` returns the same rows as on the bundled graph.

```
(:Skill)-[:IN_AREA]->(:SkillArea)-[:BROADER]->(:SkillArea)
(:Skill)-[:RELATED_TO]-(:Skill)
(:Cause)-[:IN_THEME]->(:CauseTheme)

(:Volunteer)-[:HAS_SKILL {level}]->(:Skill)                 level is new
(:Volunteer)-[:WANTS_TO_LEARN]->(:Skill)
(:Volunteer)-[:SHARES_HISTORY_WITH]->(:Organisation)        consent, as an edge

(:Volunteer)-[:HAD_ENGAGEMENT]->(:Engagement {role, readiness, started, ended, outcome})
(:Engagement)-[:ON_PROJECT]->(:Project {status: 'published' | 'completed'})
(:Engagement)-[:DEMONSTRATED {level}]->(:Skill)

(:Project)-[:REQUIRES_SKILL {min_level}]->(:Skill)          min_level is new and optional
(:Project)-[:REQUIRES_CREDENTIAL]->(:Credential)
(:Volunteer)-[:HOLDS {expires}]->(:Credential)
```

Why `Engagement` is a node and not a relationship from volunteer to project: one engagement
ties together a volunteer, a project, several skills and a rating, and a volunteer can rejoin
the same project. A single relationship cannot hold that.

Why history sharing is an edge: it follows the entry's own best idea, that consent is the shape
of the graph. A query from another organisation must pass through
`(o)<-[:SHARES_HISTORY_WITH]-(v)`. Without the edge the pattern does not match, so there is no
filter to forget.

### 5.1 Constraints and indexes

All of these were created on the demo graph without error, and `SHOW CONSTRAINTS` lists them.
A duplicate insert was then refused with `ConstraintValidationFailed`.

```cypher
// one per label: Organisation, Project, Cause, Skill, Volunteer, OnboardingStep,
// and the new SkillArea, CauseTheme, Engagement, Credential
CREATE CONSTRAINT skill_id IF NOT EXISTS FOR (n:Skill) REQUIRE n.id IS UNIQUE;

CREATE INDEX project_status IF NOT EXISTS FOR (p:Project) ON (p.status);
CREATE FULLTEXT INDEX skill_text IF NOT EXISTS FOR (s:Skill) ON EACH [s.name, s.alt_labels];
CREATE FULLTEXT INDEX project_text IF NOT EXISTS FOR (p:Project) ON EACH [p.title, p.summary];

CREATE CONSTRAINT has_skill_level IF NOT EXISTS
  FOR ()-[r:HAS_SKILL]-() REQUIRE r.level IS :: INTEGER;
CREATE CONSTRAINT engagement_readiness IF NOT EXISTS
  FOR (e:Engagement) REQUIRE e.readiness IS :: INTEGER;
```

Relate also accepted key and existence constraints, vector indexes and
`ALTER CURRENT GRAPH TYPE SET { ... }`. I would stop at the list above. Uniqueness constraints
run on any Neo4j 5; the two type constraints may not run on every edition, so check them on
AuraDB before adding them to the push. APOC and GDS are not available in Relate.

## 6. Privacy decisions to make first

Steps 4 and 5 put judgements about people into the system. These are the rules I would adopt;
they are product and legal decisions for the team, not facts about the code.

1. **The graph still holds opaque IDs and bands only.** A lead's free-text note about a
   volunteer stays in SQLite, like `private_note` today.
2. **A volunteer sees every rating made about them**, with who gave it and for which project.
3. **History crosses organisations only through `SHARES_HISTORY_WITH`.** Decide the default. I
   would make it off, and ask when the volunteer joins an organisation.
4. **Another organisation sees counts and bands, not sources.** "Two engagements in this theme,
   one elsewhere, best readiness 3", not the other organisation's name or project.
5. **The volunteer's own cross-organisation view returns organisations, skills and causes, never
   people.** It does reveal an organisation's unfilled skills to non-members. Today projects are
   visible to members only, so each organisation should be able to opt out.
6. **Credentials record "held, verified, expires" and nothing else.** No certificate numbers,
   no documents. A DBS result is criminal-record data; take advice before storing more than
   "check current".

## 7. Results from the prototype

The prototype added, on a scratch copy: 11 uniqueness constraints (the ten in 5.1 and one for
a `Role` label I did not go on to use), 3 indexes, 2 type constraints, 16 skill areas, 3 cause
themes, 9 related pairs, 16 skill levels, 3 completed projects, 5 engagements, 3 credentials
and the history-sharing edges. That took the graph from 49 nodes and 97 relationships to 79
and 179.

Each query took about 1 ms including opening the store. That says nothing about scale; the
graph is tiny.

### Q1. Where am I needed? (crosses organisations)

Organisations I have not joined, with a published project that requires a skill I offer, that
nobody on its team covers yet, and that none of my exclusions rule out.

```cypher
MATCH (v:Volunteer {id: $volunteer})
MATCH (o:Organisation)-[:OWNS_PROJECT]->(p:Project {status: 'published'})-[:REQUIRES_SKILL]->(s:Skill)
WHERE NOT (v)-[:VOLUNTEERS_WITH]->(o)
  AND (v)-[:HAS_SKILL]->(s) AND (v)-[:OFFERS_SKILL]->(s)
  AND NOT EXISTS { MATCH (p)-[:REQUIRES_SKILL]->(:Skill)<-[:EXCLUDES_SKILL]-(v) }
  AND NOT EXISTS { MATCH (t:Volunteer)-[:ON_TEAM]->(p)
                   WHERE (t)-[:HAS_SKILL]->(s) AND (t)-[:OFFERS_SKILL]->(s) }
MATCH (p)-[:SERVES_CAUSE]->(c:Cause)
WHERE NOT (v)-[:EXCLUDES_CAUSE]->(c)
RETURN o.name AS organisation, s.name AS skill_needed, c.name AS cause,
       EXISTS { MATCH (v)-[:PREFERS_CAUSE]->(c) } AS preferred, count(DISTINCT p) AS projects
ORDER BY preferred DESC, projects DESC, organisation, skill_needed
LIMIT $limit
```

| Volunteer | Result |
|---|---|
| Kofi (Cyber Scallywags) | DataKind UK needs Spreadsheet and data analysis, for Food Security |
| Farah (DataKind UK) | Cyber Scallywags needs First aid, for Homelessness |
| Asha, Dev, Gwen | Nothing |

This one needs no new data. It runs on the bundled graph today.

### Q2. Close matches through the ontology

For each required skill nobody in the organisation offers, who has a related skill or one in
the same area.

| Organisation | Project | Missing skill | Close | Because they have |
|---|---|---|---|---|
| InterLinkRCT | Community Garden Raised Beds | Gardening | Bryn | Carpentry |

The same idea from the volunteer's side, across organisations, returned nothing until I gave
Asha Python; then: InterLinkRCT needs Web development, which she wants to learn and is close to
through Python.

### Q3. Has this candidate done this before, anywhere?

For the lead of Food Bank Demand Dashboard at DataKind UK: each member's past engagements in the
project's cause theme, at any organisation, where the volunteer shares history with DataKind UK.

```cypher
MATCH (o:Organisation {id: $org})-[:OWNS_PROJECT]->(p:Project {id: $project})
      -[:SERVES_CAUSE]->(c:Cause)-[:IN_THEME]->(t:CauseTheme)
MATCH (v:Volunteer)-[:VOLUNTEERS_WITH]->(o)
OPTIONAL MATCH (o)<-[:SHARES_HISTORY_WITH]-(v)-[:HAD_ENGAGEMENT]->(e:Engagement)
      -[:ON_PROJECT]->(past:Project)-[:SERVES_CAUSE]->(pc:Cause)-[:IN_THEME]->(t)
OPTIONAL MATCH (past)<-[:OWNS_PROJECT]-(po:Organisation)
WITH v, c, t, count(DISTINCT e) AS engagements,
     count(DISTINCT CASE WHEN pc = c THEN e END) AS same_cause,
     count(DISTINCT CASE WHEN po <> o THEN po END) AS other_organisations,
     max(e.readiness) AS best_readiness
RETURN v.id AS volunteer, t.name AS theme, engagements, same_cause, other_organisations,
       best_readiness
ORDER BY best_readiness DESC, engagements DESC, volunteer
LIMIT $limit
```

| Volunteer | Engagements in theme | Same cause | Other organisations | Best readiness |
|---|---|---|---|---|
| Asha | 2 | 1 | 1 | 3 (can guide others) |
| Dev | 0 | 0 | 0 | none |
| Cerys, Farah, Rhiannon | 0 | 0 | 0 | none |

Dev does have an engagement on a food security project, rated 1. He has no
`SHARES_HISTORY_WITH` edge in the prototype, so the query cannot reach it. That is the consent
rule working as structure, though it also shows a decision to make: an organisation should
probably always see engagements on its own projects. That needs a second branch anchored on
`(o)-[:OWNS_PROJECT]->(past)`.

### Q4. Who could lead this project?

Members with an engagement rated 4, or rated 3 with one of this project's required skills
demonstrated at level 3 or above. For the same project: **Asha**, readiness 3, two engagements,
data analysis demonstrated, and she knows the theme. This fits the team coordinator's
"assign a lead" screen.

### Q5. Learning with a mentor

Projects in my organisations that use a skill I want to learn, and how many team members have
it at level 3 or above. Empty on the demo data. After adding one optional skill and one team
member: Dev, who wants to learn Data visualisation, gets Food Bank Demand Dashboard with one
mentor on the team.

### Q6. Credentials as a hard rule

Winter Outreach Kitchen, once it requires an enhanced DBS check:

| Volunteer | Eligible | Missing or expired |
|---|---|---|
| Asha | Yes | |
| Gwen | No | Enhanced DBS check (expired 31 March 2026) |
| Bryn | No | Enhanced DBS check (none held) |

### Q7. Bench strength by area

Volunteers, skills offered and highest level per top-level area, per organisation, using
`BROADER*0..2`. For example DataKind UK: Organisation and finance, 3 volunteers, 2 skills, top
level 4; Digital and data, 2 volunteers, 1 skill, top level 3. InterLinkRCT has only Practical
and logistics. This is an org admin's view of where to recruit.

### Q8. Full-text search

`dashboards` finds Data visualisation; `excel OR phishing` finds Spreadsheet and data analysis
and Cyber security awareness, all through `alt_labels`.

### What the results show about the demo data

Several queries are thin or empty because the fixture has a handful of volunteers with two or
three skills each and no history. The fixture needs to grow with the model: a few completed projects,
engagements with ratings, one volunteer who shares history and one who does not, and one skill
that is only reachable through a neighbour.

## 8. Changes by layer

### 8.1 SQLite (`sqlstore.py`, `SCHEMA_VERSION` 9 to 10)

```sql
CREATE TABLE skill_areas  (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                           parent_id TEXT REFERENCES skill_areas(id));
CREATE TABLE skill_related(skill_id TEXT REFERENCES skills(id),
                           related_id TEXT REFERENCES skills(id),
                           CHECK (skill_id < related_id), PRIMARY KEY (skill_id, related_id));
CREATE TABLE cause_themes (id TEXT PRIMARY KEY, name TEXT NOT NULL);
-- skills: + area_id, kind, alt_labels (JSON list); keep category until the screens move over
-- causes: + theme_id
-- profile_skills: + level INTEGER CHECK (level BETWEEN 1 AND 4), + wants_to_learn INTEGER
-- revision_skills: + min_level INTEGER
-- submissions.state: + 'completed'; + completed_at
-- memberships: + share_history INTEGER NOT NULL DEFAULT 0

CREATE TABLE engagements (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES submissions(id),
    volunteer_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('contributor', 'lead')),
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    outcome      TEXT CHECK (outcome IN ('completed', 'left', 'removed')),
    readiness    INTEGER CHECK (readiness BETWEEN 1 AND 4),
    rated_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    rated_at     TEXT,
    note_for_volunteer TEXT NOT NULL DEFAULT '',
    private_note TEXT NOT NULL DEFAULT ''          -- never projected
);
CREATE TABLE engagement_skills (
    engagement_id TEXT REFERENCES engagements(id) ON DELETE CASCADE,
    skill_id TEXT REFERENCES skills(id),
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    PRIMARY KEY (engagement_id, skill_id)
);
```

An engagement opens when a placement reaches `team` and closes when the volunteer leaves, is
removed, or the project is completed. Existing ones can be backfilled from `placement_events`.

### 8.2 Projector and Cypher (`graph/projector.py`, `graph/cypher.py`)

- `ensure_schema()`: run the constraint and index statements at the start of
  `_project_catalogue`. They are idempotent.
- `_project_catalogue`: also upsert areas, themes, `IN_AREA`, `BROADER`, `RELATED_TO`,
  `IN_THEME`, and prune stale ones the way the edge helpers already do.
- `_project_volunteer`: set `level` on `HAS_SKILL`; add `WANTS_TO_LEARN`,
  `SHARES_HISTORY_WITH` and `HOLDS` to `VOLUNTEER_RELS`; reconcile the volunteer's engagements.
- `_project_project`: a `completed` project stays in the graph with `status = 'completed'`.
  Only withdrawn projects are removed. `p.status = 'published'` then becomes a real filter, and
  the existing matching statements need no change.
- Add the new labels and relationships to `LABELS` and `RELATIONSHIPS`. The AuraDB push and
  `test_graph_schema.py` derive from them.

### 8.3 Models (`web/schemas.py`, `services/profile.py`)

```python
Readiness = Literal[1, 2, 3, 4]


class ProfileSkillIn(BaseModel):
    state: SkillState  # none | has | offers | exclude
    level: Readiness | None = None  # only with has or offers
    wants_to_learn: bool = False


class ProfileIn(BaseModel):
    display_name: str = Field(max_length=60)
    version: int
    skills: dict[str, ProfileSkillIn | SkillState]  # the old string form still accepted
    causes: dict[str, str]


class Engagement(BaseModel):  # shown to the volunteer and to the owning organisation
    project: str
    title: str
    organisation: str
    role: Literal["contributor", "lead"]
    ended: date | None
    readiness: Readiness | None
    readiness_label: str | None
    demonstrated: list[SkillLevel]


class Experience(BaseModel):  # what another organisation may see: bands, no sources
    engagements: int
    same_cause: int
    other_organisations: int
    best_readiness: Readiness | None


class Opportunity(BaseModel):  # Q1: never a person
    organisation: str
    skill_needed: str
    cause: str
    preferred: bool
    projects: int
```

Accepting both shapes for `skills` keeps the mobile app working until it is updated.

### 8.4 Assistants (`agents/`)

This is where the new graph answers the review's point that the assistants cannot ask the
graph anything new.

| Tool | Assistant | Query |
|---|---|---|
| `where_am_i_needed` | Match explainer, onboarding | Q1 |
| `skill_neighbours(skill_id)` | Project scoping, match explainer | Area, related skills, synonyms |
| `candidate_experience` | Match shortlist | Q3 and Q4 |
| `find_skill(text)` | Project scoping | Q8 |

The output validators must be extended in the same way as today: reject any organisation,
skill or band the tool did not return.

### 8.5 Fixtures and tests

- `catalogue.json`: add `areas`, `themes`, `related`, and `kind` and `alt_labels` per skill.
- `demo.json`: add `level` to skills, completed submissions, `engagements`, `share_history`.
- Rebuild `data/demo.rl`; the counts in `test_graph_schema.py` and the docs change.
- New tests: constraints are listed by `SHOW CONSTRAINTS`; a duplicate is refused; Q1 never
  returns an organisation the volunteer is in, an excluded cause, or a person; Q3 returns zero
  for a volunteer without the sharing edge; `MATCH_CANDIDATES` is unchanged by completed
  projects.

## 9. Steps to implement

Efforts are my estimates for one person who knows the code.

| Step | Work | Needs a privacy decision | Effort |
|---|---|---|---|
| 1 | Constraints and indexes in the projector; rebuild the bundle; test | No | Half a day |
| 2 | Ontology: catalogue file, three tables, projector, cause themes; group the profile screen by area | No | 1 day |
| 3 | Q1 "where am I needed": service function, page, API route, assistant tool; organisation opt-out | Small (rule 5) | 1 to 2 days |
| 4a | `completed` state; `engagements` tables; projector; seed history in the fixture; show it read-only on the profile and to the lead (Q3, Q4) | Yes (rules 1 to 4) | 2 days |
| 4b | Skill level on the profile form; rating form when an engagement ends; history-sharing switch | Yes | 2 to 3 days |
| 5 | Credentials, then learning goals and close matches (Q2, Q5), then availability | Yes (rule 6) | 1 to 2 days each |

For the hackathon I would do steps 1 to 3, then 4a if a week remains. That gives the demo a
cross-organisation question, a schema, an ontology and a visible history without building
forms for ratings. Steps 4b and 5 are product work for after the deadline.

## 10. What was and was not checked

- **Run on a scratch copy of the demo graph with Relate 1.0.1:** every constraint and index in
  5.1, relationship properties, variable-length and quantified paths, `COUNT` and `COLLECT`
  subqueries, full-text queries over a list property, and queries Q1 to Q8. `MATCH_CANDIDATES`
  returned the same rows before and after.
- **Not run:** anything on AuraDB; the test suite (no code changed); the queries at any
  realistic size.
- **Not built:** the SQL, projector, model and assistant changes in section 8 are designs.
- **My judgement, not measured:** the skill areas, related pairs and cause themes; the
  four-step scale wording; the effort estimates; the privacy rules, which need the team's
  decision and, for credentials, proper advice.
- The prototype scripts are not in the repository. The statements in section 7 are the ones
  that were run.
