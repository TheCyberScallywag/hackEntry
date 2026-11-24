# Hackathon review: Volunteer Network against the Relate by Neo4j Hackathon criteria

Reviewed 24 November 2026 on the branch `agentic_sandbox` (commit `eadf6a0` plus the uncommitted
team coordinator and demo fixture changes). It follows `RELATE_USAGE_REVIEW.md` of 19 November
and says what has changed since.

**Where the criteria come from.** I could not read `relate.devpost.com` or its rules page: the
site refuses automated access. The criteria and requirements below are the ones quoted from
Neo4j's announcement post in `RELATE_USAGE_REVIEW.md`. **Read the Devpost rules page yourself
before submitting; it is authoritative**, and anything it adds (eligibility, licence, video
hosting, team rules) is not covered here.

## 1. Verdict

The entry is strong on usefulness and completeness, and it now has a real answer to the
criterion it failed outright five days ago: six assistants that read the graph and act only
with approval. Three things stand between that work and the score it should earn:

1. **Nobody can see the assistants.** They exist only as JSON endpoints, plus one button in the
   admin console. The web and mobile apps have no assistant screen, the demo script and the
   Devpost draft do not mention them, and none has been run against a real model.
2. **The graph is still used shallowly.** No query crosses organisations, although "one
   volunteer, several organisations" is the product's premise. The graph has no constraints or
   indexes.
3. **The submission itself is not ready.** The video is not recorded, the Devpost text describes
   the app as it was before the assistants, and several documents repeat a claim about Relate's
   locking that the earlier review measured to be wrong.

There are 17 days to the deadline (11 December 2026, 6 pm Pacific). Section 5 gives an order of
work that fits.

## 2. Submission requirements

| Requirement | Status |
|---|---|
| Built with Relate | Met. Relate 1.0.1 is the matching engine and the assistants' source of context. |
| Public GitHub repository with a README that shows how to set it up | README is thorough. I did not check that `TheCyberScallywag/hackEntry` is public, or that this branch is merged to `main`. |
| Project description that explains the use case | Drafted in `docs/content/project/devpost.md`, but out of date: no mention of the assistants, the audit trail or onboarding workflows. |
| Demo video of 1–2 minutes | **Not recorded.** The script runs 1:45 without the optional shot and shows no assistant. |
| Working code, or the prompts used with Relate | Met by the code. |
| New work if building on an existing project | Covered by `reuse-and-attribution.md`. |

## 3. The six criteria

Judging is equal-weighted: creativity, usefulness, completeness, clarity, effective use of
connected context, and how fully AI and agentic capabilities are used.

| Criterion | 19 Nov | Now | What holds it back |
|---|---|---|---|
| Creativity | Reasonable | Reasonable to good | The distinctive ideas are not the ones the submission leads with |
| Usefulness | Strong | Strong | Little |
| Completeness | Strong | Strong, with gaps at the edges | Assistants have no screen and no real-model run; no video |
| Clarity | Strong | **Weaker** | Documentation volume, stale claims, submission text behind the code |
| Connected context | Moderate | Moderate, slightly better | No cross-organisation or multi-hop question; no schema |
| AI and agentic capabilities | Absent | **Good in code, invisible in the demo** | No UI, no real run, not in the video or description |

### 3.1 Creativity

What is original here:

- **Consent as graph structure.** An exclusion is an edge, and an unvetted volunteer has no
  `VOLUNTEERS_WITH` edge, so no query can reach them. Eligibility is the shape of the graph, not
  a filter someone might forget.
- **The graph as a privacy boundary for agents.** It holds opaque IDs only. An assistant can
  reason over it without ever seeing a name or an email.
- **Assistants that cannot outvote the graph.** Output validators re-read Relate and reject any
  person, skill or project it did not return.

None of these three is in the Devpost draft's opening or the demo script. The draft leads with
"one account across organisations", which is useful but not novel.

Suggestion: make "the graph decides, the agent explains, a person approves" the headline.

### 3.2 Usefulness

Strong. It addresses a real problem with a full workflow: vetting, matching, invitations, team
onboarding, review of project suggestions, roles, audit. The new team coordinator role and the
per-organisation onboarding workflows are the kind of detail real charities need.

One risk to remove: the demo now seeds accounts at `datakind.uk`, which looks like a real
organisation's domain, with a published password. The docs say no endorsement is implied, but a
judge may still read it as a claimed partnership. Use an invented domain.

### 3.3 Completeness

Strong at the centre: web app, JSON API, mobile app, admin console, CLI, Compose stack, docs
site, and 366 backend tests against the real engine.

Gaps:

- **The assistants are the least complete part of the entry.** They have an API, tests with
  scripted stand-in models, and evaluation suites, but no screen in the web or mobile app, and
  they have never been run with a real model. Expect the instructions to need tuning the first
  time they are.
- **Test suite.** Green on the current working tree: 364 passed, 2 skipped (AuraDB and Compose, which need credentials and a running stack), 96% line coverage. None of this is committed yet.
- **The video does not exist.**
- **AuraDB push and parity** are built and have never been run. This is optional, and the docs
  say so.
- **`verification.md`** records a run of 154 tests from before the assistants. It is linked from
  the README as "latest results".

### 3.4 Clarity

This has slipped, mostly through growth:

- **Volume.** The README is 52 KB. A judge with a few minutes per entry needs the pitch, three
  commands and a screenshot in the first screen; the detail can live in the docs site.
- **Six internal review documents sit in the repository root** (`REFACTOR_REVIEW.md`,
  `MATCHING_REVIEW.md`, `RELATE_USAGE_REVIEW.md`, `PYDANTIC_AI_REVIEW.md`,
  `NAV_REFACTOR_PROPOSAL.md`, `DRAFT_SUGGESTIONS_PROPOSAL.md`, and now this one). They list the
  entry's own weaknesses and make the root hard to scan. Move them to `docs/reviews/` or keep
  them off `main`.
- **A wrong claim about Relate, repeated in six places.** `store.py`, the README, `relate.md`
  (twice), `solution-architecture.md` (three times) and the Devpost draft say Relate locks the
  store exclusively even for readers. The earlier review measured that two read-only openers
  coexist on a `.rl` file; only a writer excludes others. Judges from Neo4j will know this.
- **The Devpost draft and demo script describe an older app.** See 3.6.

Fixed since the last review: the node and relationship counts now match the bundled file
(49 and 97).

### 3.5 Effective use of connected context

Better than it was:

- `ON_TEAM` is now read: coverage counts a skill as covered only when a team member offers it.
- Onboarding workflows are in the graph as chains of `OnboardingStep` nodes joined by
  `NEXT_STEP`, with a volunteer's place as `AT_STEP` and `COMPLETED_STEP` edges. The onboarding
  assistant follows that chain. This is the entry's first real path traversal.

Still open from the earlier review, checked against the current code:

| Earlier finding | Now |
|---|---|
| No query crosses organisations | **Unchanged.** Every read is scoped to organisations the volunteer already belongs to. |
| No constraints or indexes | **Unchanged.** `SHOW CONSTRAINTS` on `data/demo.rl` returns nothing; only the two built-in lookup indexes exist. |
| `p.status = 'published'` filters on a constant | Unchanged. |
| `Skill.category` is a property, so skills have no neighbours | Unchanged. |
| Project title and summary are stored and never searched | Unchanged. |
| All readers queue behind one process-wide lock | Unchanged, though a read now gives up after about a second. |

The cross-organisation question is the one worth building: "which organisation I have not
joined has a gap I could fill, given what I exclude?" It is the question a spreadsheet per
organisation cannot answer, it is a genuine multi-hop traversal, and it is the product's own
premise. It needs a privacy decision first: return organisation and skill only, never people.

### 3.6 AI and agentic capabilities

In the code this is now a real strength:

- Six Pydantic AI agents: volunteer onboarding, project scoping, project review, match
  shortlist, match explainer, audit.
- Five of them read Relate. Writes wait for human approval, after validation.
- Role checks run before the model is asked anything and again inside every tool.
- Run limits, a daily quota, an audit trail, Logfire traces, online evaluators and offline
  evaluation suites.

What a judge will actually see is much less:

- **No assistant appears in the web or mobile app.** The only one with a button is the audit
  assistant in the admin console.
- **No assistant has run against a real model.** Every test uses a scripted stand-in.
- **The assistants cannot ask the graph anything new.** Each tool runs a fixed statement the
  application already ran for its own pages. The hackathon's theme is agents that understand how
  data connects; here the agent explains an answer the application had already computed. That
  is a defensible design (it is why the agent cannot be wrong about eligibility), but it should
  be argued, and one tool that lets the agent explore would strengthen it.
- **No MCP surface.** Relate's own pitch is as a database for agents. An external agent cannot
  use this graph.
- **The submission text is silent about all of it.** `devpost.md` and `demo-script.md` contain
  no mention of an assistant.

## 4. Suggestions

Ordered by score gained for the effort.

| # | Suggestion | Criteria it moves | Effort |
|---|---|---|---|
| 1 | **Run the assistants against a real model** (`AGENT_MODEL`, a key, `python -m app.cli evals --judge`) and tune the instructions until the suites pass. | AI, completeness | Half a day |
| 2 | **Put two assistants on screen.** "Why am I not matched to this?" on *My matches* (match explainer, read-only, lowest risk) and "Suggest a shortlist" on the project page (shows approval before an invitation). The API is in place. | AI, completeness, clarity | 1–2 days |
| 3 | **Rewrite the demo script around one agent moment** and record the video. Suggested arc: Asha excludes a skill; the graph drops her; the explainer tells her why, citing the edge; the lead asks for a shortlist and approves one invitation. Stay under two minutes. | All six | 1 day |
| 4 | **Rewrite the Devpost draft**: lead with "the graph decides, the agent explains, a person approves"; add the assistants, the approval step, the privacy boundary, Logfire and the evals. | Clarity, creativity, AI | 2 hours |
| 5 | **Correct the locking claim** in the six places listed in 3.4, using the table in `RELATE_USAGE_REVIEW.md` F4. | Clarity | 1 hour |
| 6 | **Declare the graph schema**: a uniqueness constraint on `id` per label, applied by the projector; drop or index `Project.status`; rebuild the bundle; a test that `SHOW CONSTRAINTS` lists them. | Connected context, completeness | Half a day |
| 7 | **Add the cross-organisation query** ("where am I needed?") as a page and as a tool for the explainer or onboarding assistant. Organisation and skill only, never people. | Connected context, creativity, AI | 1–2 days |
| 8 | **Shorten the README** to pitch, quick start, demo accounts and links; move the rest to the docs site. Move the review documents out of the root. Update `verification.md` with a current run. | Clarity | Half a day |
| 9 | **Replace the `datakind.uk` demo addresses** with an invented domain. | Usefulness, clarity | 1 hour |
| 10 | **An MCP endpoint** serving the read tools, so an external agent can use the graph. Do not point `relate serve --mcp` at the live `.rl` file: a long-lived reader blocks the projector. | AI, connected context | 1–2 days; only if 1–7 are done |

Not worth doing before the deadline: a long-lived Relate connection or a directory store,
vector search, the web UI for all six assistants, sign-in lock-out, and the project page's
repeated graph opens.

## 5. Suggested order for the 17 days

| When | Work |
|---|---|
| First | 1 (real model run), 9 (demo addresses), 5 (locking claim). Commit the working tree while the suite is green. |
| Then | 2 (two assistants on screen), 6 (graph schema) |
| Then | 7 (cross-organisation query), if there is a week left after the above |
| Last three days | 3 (script and video), 4 (Devpost text), 8 (README, `verification.md`). Make the repository public, merge to `main`, and do a clean-clone run of the README on another machine. |

## 6. What was and was not checked

- Checked in the code and the bundled graph today: the status of every earlier finding in 3.5
  (including `SHOW CONSTRAINTS` and `SHOW INDEXES` on a copy of `data/demo.rl`), where the
  assistants read the graph, which screens show an assistant, and what the Devpost draft and
  demo script say.
- Taken from the earlier review, not re-measured: Relate's locking behaviour and the timings.
- Not checked: the Devpost rules page, whether the GitHub repository is public, a real-model
  run, the Docker and mobile builds, and how the entry compares with other submissions.
- The ratings in section 3 are my judgement against the six criteria as worded in the
  announcement. The judges may weigh them differently.
