# Code review and refactoring plan

Reviewed 16 November 2026 at commit `3308cdd` (branch `remove_demo_emails`). Scope: everything under `src/app`, `mobile/src`, `tests`, the Docker and CI files, and `pyproject.toml`. Generated docs and lock files were not reviewed.

The review itself changed no code. The findings were then implemented on the branch
`refactor/review-findings`; the next section says what was done about each one. The rest of the
document is the review as written, so its line numbers and figures describe commit `3308cdd`.

## Status of each finding

| Finding | Status |
|---|---|
| 1.1 Silent truncation of graph reads | **Fixed.** `Tx.read` binds `$limit`; the sweep reads per project or per changed volunteer. A read over the bound now raises (a 503 page or JSON error) where it used to return a short list. |
| 1.2 `aura-push` rejects the demo graph | **Fixed**, and tested against a stand-in driver. Still not run against a live AuraDB. |
| 1.3 Mobile notification links | **Fixed**, with specs. |
| 1.4 Smaller defects | **Fixed**: all seven. The CORS comment was corrected; CORS support was not added. |
| 2.1 Connection churn | **Done**: `connect()` runs one statement. Connections per page fell from 5–14 to 3–9. A single connection per request was not introduced: with a cheap `connect()` the remaining gain is small, and each service function still checks access for itself. |
| 2.2 Repeated work in one request | **Done**: page frame in one connection, `org_access` in one query, grouped `list_users` and review counts, one project read for the detail page, session looked up once. |
| 2.3 Sweep is a full scan | **Done**: scoped to what a drain projected; full sweep on start-up, `cli drain`, an admin replay, or after a failed sweep. |
| 2.4 Blocking work on the event loop | **Done**: every page and error handler is a plain function; a test keeps it that way. |
| 2.5 Tables that only grow | **Done**: partial outbox index; finished outbox rows, ended sessions and long-read notifications are pruned. |
| 2.6 Missing indexes | **Done**: all seven. |
| 3.1 One graph schema | **Done.** |
| 3.2 Repeated fragments | **Done**: `match_params`, `skill_flags` / `skill_state`, `require_reason`, `require_admin` / `require_vetter`, the `submission_current` view, one definition of each label set. |
| 3.3 Split `services.py` | **Done**: a package of seven modules with the same public names. |
| 3.4 `web/main.py` | **Done**: routers per area, table-driven error handlers, an `Org` dependency, one helper for the suggestion re-render. |
| 3.5 API contract | **Done on the server**: every route has a response model. The mobile types are still written by hand, now checked against the OpenAPI document by a test. Generating them needs the `openapi-typescript` package, which was not installed (see "Left open"). |
| 3.6 Mobile app | **Done**: labels and grouping come from the server, failed requests show a message, the matches request is cancelled when superseded. Not done: a shared load/busy helper, and refreshing the unread badge on a timer. |
| 3.7 Admin console | **Done**: one function per tab, detail fetched for the selected row only, covered by tests. |
| 4 Build and CI | **Done**: separate web and admin images (316 MB against 922 MB), CI runs once per pull request, `task hooks` added. The `httpx2` deprecation warning is unchanged. |
| 5 Test gaps | 154 tests and 95% coverage, from 81 and 89%. `serve.py` is still untested. |

### Left open

- **Generated mobile types.** Adding the `openapi-typescript` dev dependency was blocked by the
  session's permissions. `python -m app.cli openapi --out mobile/openapi.json` writes the input
  it needs.
- **A live AuraDB run** of `aura-push` and `aura-parity`.
- **`httpx2`**: Starlette's test client asks for it in place of `httpx`.
- **CORS** for a native mobile build.

## Summary

The codebase is in good shape. The tests pass (81 passed, 2 skipped), coverage is 89%, and `ruff check` and `ruff format --check` are clean. The architecture is sound: SQLite is the source of truth, the graph is a projection fed by an outbox, and both the web pages and the JSON API go through one service layer that enforces access.

The review found three defects and one structural performance problem that are worth fixing before anything else:

1. **Graph reads can be truncated silently**, and the placement sweep then treats the missing rows as "no longer eligible". This is a correctness bug that appears once an organisation has more than 500 match pairs.
2. **`aura-push` fails on the seeded demo**, because the relationship allow-list does not include `ON_TEAM`.
3. **Suggestion notifications are never tappable in the mobile app**, because the link check compares against paths the server never sends.
4. **Each page opens 5 to 14 SQLite connections**, and connection setup is about half of the request time on the heaviest page.

The rest of the document is ordered by priority within each section. Section 6 gives a suggested order of work.

## 1. Defects

### 1.1 Silent truncation of graph reads (high)

`Tx.read` in `src/app/graph/store.py:59` fetches `max_rows + 1` rows and raises `GraphTruncated` if it gets more than `max_rows`. The module docstring promises reads are "never silently truncated".

Every query also carries its own `LIMIT $limit`, and every caller passes a limit that is less than or equal to `max_rows` (200 or 500 against a default `max_rows` of 500). The query therefore never returns more than `max_rows` rows, and the guard cannot fire. I confirmed this on the bundled graph: with `max_rows=3` and `limit=3` the read returns 3 rows and raises nothing; with `limit=4` it raises.

The consequences, from most to least serious:

- `placements.sweep` (`src/app/placements.py:47`) reads matches per organisation with `limit=graph.max_rows`. Any eligible pair beyond row 500 is missing from `current`, so the second loop marks its placement `lapsed` and notifies the volunteer that the offer has closed.
- `matching.volunteer_matches`, `project_coverage` and `browse` cap at 200 rows and show an incomplete list with no indication.
- `aura._export` caps nodes and relationships at `max_rows`, so a push of a larger graph is incomplete.

Suggested fix: have `Tx.read` own the limit. Callers stop passing `limit`, and `Tx.read` binds `limit = max_rows + 1` itself. The guard then works everywhere, and four hand-written copies of the match parameters disappear (see 3.2). The sweep should additionally page or scope its reads (see 2.3) so that it does not depend on a row bound at all.

### 1.2 `aura-push` rejects the demo graph (medium)

`allowed_types` in `src/app/graph/aura.py:63` lists ten relationship types and omits `ON_TEAM`. The seeded demo creates one team, and the bundled `data/demo.rl` contains one `ON_TEAM` relationship, so `push` raises `ValueError: unexpected relationship Volunteer-ON_TEAM->Project` after it has already deleted and rewritten the nodes.

`tests/test_aura.py` is stale for the same reason: it expects 62 relationships, and the bundled graph now has 67. The test only runs with AuraDB credentials, so CI does not catch either problem. I could not run the push myself (no AuraDB instance); this finding comes from reading the code and counting relationships in the bundled store.

Suggested fix: derive the allow-list from one relationship schema shared with `cypher.py` (see 3.1), and add a test that checks every relationship type in the bundled graph against it. That test needs no AuraDB.

### 1.3 Mobile notification links never match (medium)

`appLink` in `mobile/src/app/pages/notifications/notifications.page.ts:41` checks `APP_ROUTES.includes(n.link)`. The list does not contain `/submissions`, so the `n.link === '/submissions'` branch is unreachable. The server sends `/submissions/{id}`, which matches nothing either. Notifications about a suggestion are therefore shown as plain text.

Suggested fix: map by prefix (`/submissions/…` to `/suggestions`) and add a spec for it.

### 1.4 Smaller defects

| Where | Problem | Fix |
|---|---|---|
| `src/app/web/api.py:51` | `_drain` suppresses every exception without logging. The web version in `main.py:269` logs. | Use one shared helper that logs. |
| `src/app/cli.py:141` | `grant-reviewer` writes `datetime('now')` (`YYYY-MM-DD HH:MM:SS`), while every other timestamp is ISO 8601 with an offset from `sqlstore.now()`. | Use `now()`. |
| `src/app/services.py:620` | `revise_submission` puts the suggestion back in review but notifies nobody. `create_submission` notifies the organisation's reviewers. | Call `notify_org_admins` here too. |
| `src/app/graph/cypher.py:2` | Docstring cites `tests/test_graph.py`, which does not exist. | Point to `tests/test_projection.py` and `tests/test_matching.py`. |
| `.pre-commit-config.yaml:1` | Mentions `uv run task hooks`; there is no such task. | Add the task or fix the comment. |
| `mobile/src/environments/environment.prod.ts:3` | Mentions a `CORS_ORIGINS` setting; the API has no CORS support. | Add the setting and middleware, or remove the comment. |
| `src/app/web/main.py:297`, `:363` | `# noqa: C901` on two functions, but C901 is not in the ruff rule set. | Remove them, or enable `C90` and `RUF100`. |

## 2. Performance

Measurements below were taken on the seeded demo with the FastAPI test client, 20 requests per page, on the development laptop. They show relative cost, not production latency.

### 2.1 Connection churn in SQLite (high)

`Database.read()` and `Database.tx()` open a new connection for every unit of work, and the service functions each open their own. A single page therefore opens many:

| Page | SQLite connections | Graph opens | Time per request |
|---|---|---|---|
| Project detail (org admin) | 14 | 2 | 9.3 ms |
| Project detail (lead) | 13 | 2 | 9.2 ms |
| Profile | 9 | 1 | 6.4 ms |
| Home | 8 | 1 | 6.8 ms |
| Matches | 7 | 1 | 5.2 ms |
| Notifications | 5 | 0 | 3.2 ms |

`connect()` (`src/app/sqlstore.py:214`) costs about 0.33 ms per call. Opening the same connection with only `PRAGMA foreign_keys` costs about 0.03 ms. On the project detail page, 14 connections come to roughly 4.5 ms of a 9.3 ms request.

There are two fixes, and they are independent.

**Make `connect()` cheap.** This is a small change with no design impact:

- `journal_mode = WAL` is stored in the database file. Set it once in `init_schema`.
- `busy_timeout` duplicates the `timeout=10` argument already passed to `sqlite3.connect`.
- `path.parent.mkdir` belongs in `init_schema`.

This removes about 90% of the per-connection cost.

**Use one connection per request.** Most service functions take `db` and open their own connection, and some call each other (`require_access`, then the query, then `get_org` again). The low-level helpers (`org_access`, `load_actor`, `get_org`) already take a `conn`. Extending that convention to the read-side functions would let a handler open one connection, pass it down, and close it at the end. The duplicated access checks in section 2.2 fall out of this as well.

### 2.2 Repeated work inside one request (medium)

- **`render()`** (`src/app/web/main.py:211`) runs on every page and opens four connections: the organisation directory, the pending-outbox count, the actor and the unread count. The session lookup is a fifth. One query, or one connection, covers all of them.
- **Project detail** (`main.py:516`) calls `require_access` three times for the same user and organisation (directly, in `browse`, and in `project_coverage`), fetches every project in the organisation to find one, and opens the graph twice. `BROWSE_PROJECTS` could take an optional `$project` the way `MATCH_CANDIDATES` does.
- **Home** (`main.py:430`) loads the full review queue of every organisation the user reviews only to count the `submitted` rows. A `SELECT org_id, count(*) … GROUP BY org_id` does this in one query.
- **`org_access`** (`services.py:97`) runs five queries. `volunteer_matches` calls it once per organisation. One query with two `EXISTS` subqueries would do, as `directory` already shows.
- **`list_users`** (`services.py:429`) runs two extra queries per user. Use two grouped queries and merge in Python.
- **Error handlers** call `optional_session` again, which repeats the session lookup the route already did. Store the session on `request.state`.

### 2.3 The placement sweep is a full scan on every change (medium)

`Projector.drain` calls `placements.sweep` after any successful projection. The web handlers call `drain` inline, so a profile save, a membership change or a placement answer each trigger a sweep inside the request.

`sweep` then runs `MATCH_CANDIDATES` for every organisation, loads every placement, every active user and every published project, and runs two more queries per new pair. The cost grows with the size of the whole system, whatever the size of the change.

Suggested fix: scope the sweep to what changed. The drain already knows which volunteers and projects it projected, and `MATCH_CANDIDATES` already accepts `$volunteer` and `$project`. Pass the affected IDs to the hook and reconcile only those pairs. Keep the full sweep for start-up and for `cli drain`.

### 2.4 Blocking work on the event loop (medium)

Every server-rendered route in `main.py` is `async def`, because it awaits `request.form()`, and then calls synchronous code directly: SQLite, the graph store, the projector drain with its sweep, and Argon2. While one of these runs, the server handles no other request, including `/healthz`. A sign-in holds the loop for about 24 ms (measured Argon2 verify time). A graph store locked by another process can hold it for several seconds, because `GraphStore._open` retries with `time.sleep` up to 40 times.

The JSON API routes are plain `def`, so FastAPI runs them in its thread pool and they do not have this problem.

Suggested fix: move form parsing and the CSRF check into an `async` dependency that returns the parsed form, and declare the page handlers as plain `def`. The exception handlers that call `render` need the same treatment (`run_in_threadpool`).

### 2.5 Tables that only grow (low)

- `outbox` rows are kept after they are done. `outbox_pending` indexes `done_at` first, so pending lookups stay fast, but a partial index (`WHERE done_at IS NULL`) would be smaller, and done rows older than a few days can be deleted during a drain.
- `sessions` keeps expired and revoked rows. Delete them at start-up or sign-in.
- `notifications` has no retention rule.

### 2.6 Missing indexes (low)

These queries currently scan, because the primary key leads with the other column or there is no index. None matters at demo size.

| Query | Suggested index |
|---|---|
| Placements for a volunteer (`for_volunteer`, `org_volunteers`) | `placements(volunteer_id, state)` |
| Members of an organisation (`org_volunteers`, coverage names, `_org_pending`) | `memberships(org_id, opted_in)` |
| A user's suggestions (`my_submissions`) | `submissions(author_id)` |
| Reviews of a suggestion (`get_submission`) | `reviews(submission_id)` |
| Last active organisation (`create_session`) | `sessions(user_id, created_at)` |
| Reviewers of an organisation (`notify_org_admins`) | `org_roles(org_id)` |
| Vetting history (`vetting_record`) | `vetting_events(user_id)` |

### 2.7 Things that are fine as they are

- Opening the graph store per unit of work costs about 0.2 ms on the demo graph, well below the 8 ms the docstring quotes. It is not worth restructuring.
- `Catalogue.skill_name` is a linear scan and `skill_ids` rebuilds a set on each access. With 15 skills each call takes about 0.2 microseconds. A dict lookup would be tidier but will not be measurable.

## 3. Duplication and structure

### 3.1 One graph schema instead of six copies

The mapping from relationship type to target label is written out four times in `cypher.py` (lines 45, 62, 86 and 100), again as `VOLUNTEER_EDGES` in `projector.py:30`, and again as `allowed_types` in `aura.py:63`. The sixth copy is the one that drifted (finding 1.2).

Define it once in `cypher.py`:

```python
VOLUNTEER_RELS = {"VOLUNTEERS_WITH": "Organisation", "HAS_SKILL": "Skill", ...}
PROJECT_RELS = {"SERVES_CAUSE": "Cause", "REQUIRES_SKILL": "Skill", ...}
ALL_RELS = {*VOLUNTEER_RELS, *PROJECT_RELS, "OWNS_PROJECT", "ON_TEAM"}
```

Build the prune and merge statements from these dicts, and import them in the projector and in `aura.py`.

### 3.2 Repeated fragments in the service layer

| Fragment | Occurrences | Suggestion |
|---|---|---|
| Match parameter dict (`orgs`, `project`, `volunteer`, `preferred_only`, `limit`) | `matching.py:93`, `placements.py:56`, `cli.py:107`, `aura.py:89` | Export one `match_params()` from `cypher.py`. |
| Skill state to `(has, offers, excluded)` and back | `services.py:499` and `:542`, `runtime.py:142`, `projector.py:205` | Two small functions next to `SKILL_STATES`. |
| `if not 3 <= len(reason) <= REASON_MAX: raise Invalid(...)` | `services.py` five times, `placements.py:215` | `_require_reason(text, who)`. |
| `if not get_actor(db, actor_id).admin: raise Forbidden("admin")` | `services.py` four times, `api.py:564` | `require_admin(db, actor_id)`, alongside `require_active`. |
| Join from a submission to its current revision's title | `my_submissions`, `review_queue`, `withdraw_project`, `_project_info`, `for_volunteer` | A SQL view `submission_current`. |
| `SKILL_STATES`, `CAUSE_STANCES` and project state labels | `services.py:15`, `profile_view.py:19`, `profile.page.ts:27`, `submissions.page.ts:17` | One Python definition. See 3.5 for the mobile side. |

### 3.3 `services.py` is five modules in one file

At 896 lines it holds organisations and access, vetting, roles, profiles and submissions, already separated by banner comments. Splitting along those banners into a `services` package (`access.py`, `orgs.py`, `vetting.py`, `roles.py`, `profile.py`, `submissions.py`) is mechanical. Re-export the public names from `services/__init__.py` so that callers do not change.

### 3.4 `web/main.py`

- `_register_routes` is a 380-line closure holding about 35 handlers. Use `APIRouter` modules per area (identity, organisations, projects, suggestions, placements), as `api.py` already does. Creating them with `include_in_schema=False` also replaces the loop at `main.py:127` that mutates routes after registration.
- `_register_errors` has six handlers with the same shape. A table of `exception -> (status, title, detail)` and one handler would replace them.
- `with rt.db.read() as conn: org = dict(services.get_org(conn, org_id))` appears five times. It should be a dependency.
- The `try … except services.Invalid … render("submission.html", …)` block appears three times (`revise`, `review`, `withdraw`).
- Two imports sit inside functions without a circular-import reason: `secrets` in `_anon_csrf` and `get_catalogue` in `placements._shape`.

### 3.5 The API contract is maintained by hand in three places

Every API route returns a bare `dict` or `list[dict]`. As a result:

- The OpenAPI document describes request bodies and no response shapes.
- `mobile/src/app/core/api.types.ts` (106 lines) is a hand-written copy of what the server happens to return.
- `get_submission` returns `SELECT s.*` straight to the client (`services.py:666` and `:696`), so any column added to `submissions` is published automatically.
- Several routes reshape service output by hand (`org_matches`, `org_coverage`, `org_projects`, `me`).

Suggested fix: add Pydantic response models to the routes. The reshaping moves into the models, `SELECT *` can no longer leak a column, and the mobile types can be generated from `/api/openapi.json` (for example with `openapi-typescript`) in place of the hand-written file.

### 3.6 Mobile app

- The project state labels are defined twice with identical content (`profile.page.ts:40` and `submissions.page.ts:17`). Either share one constant or have the server send `state_label`, as it already does for `status_label` and for placements.
- The API response includes `skill_groups`, and `ProfilePage.groups` (`profile.page.ts:96`) recomputes the same grouping from `skills`. Use the server's grouping, or stop sending it.
- Several subscriptions have no error handler, so a failed request leaves the page empty with no message: `OpportunitiesPage.load` and `leave`, `NotificationsPage.load` and `readAll`, `SubmissionsPage`, and `OrgStore.switchTo`.
- `MatchesPage` starts a request from an `effect` each time the organisation or the toggle changes and never cancels the previous one. A slow earlier response can overwrite a newer one. `rxResource`, or `toObservable` with `switchMap`, fixes this and removes the manual `load`.
- Four pages repeat the same load, message and busy handling. A small shared helper would remove it.
- The unread badge refreshes only when something calls `OrgStore.load()`.

### 3.7 Admin console

`src/app/admin/main.py` is a single 262-line script with all four tabs inside one loop, deeply nested, and excluded from coverage. One function per tab would make it readable and testable.

It also makes one API call per row while rendering: every expander in the screening queue and in the suggestions list fetches its detail whether or not it is opened, on every rerun. Fetch the detail only for the selected row.

## 4. Build and CI

- **Image contents.** `docker/Dockerfile` installs the `admin` and `aura` extras into the one image used for both services, so the web container ships Streamlit, pandas and the Neo4j driver. `pyproject.toml` says the docs group is opt-in "so the web image stays lean". Two build targets, `web` and `admin`, from the same Dockerfile would keep that promise.
- **Duplicate CI runs.** The workflow triggers on push to any non-main branch and on pull requests to main, so each push to a PR branch runs CI twice. Trigger on `pull_request` plus `push` to `main`.
- **Test client warning.** The test run prints a deprecation warning from Starlette asking for `httpx2` in place of `httpx`. Worth tracking before it becomes an error.

## 5. Test gaps

Coverage is 89% overall. The gaps that relate to findings above:

| Area | Coverage | Gap |
|---|---|---|
| `graph/aura.py` | 23% | Only tested against a live AuraDB. The export and allow-list logic can be tested locally (1.2). |
| `cli.py` | 64% | `status`, `drain`, `grant-reviewer`, `matches` are untested. |
| `serve.py` | 0% | Thin entry point; low value. |
| `web/main.py` | 89% | Untested: the placement routes (`answer`, `leave`, `lead_remove`), `org_remove_volunteer`, and the invalid-input re-render paths. |
| Truncation | none | No test reads more rows than the bound (1.1). |
| Mobile | no figure | Notification link mapping (1.3) and the error paths in 3.6 have no specs. |

## 6. Suggested order of work

Each step is independent and leaves the tests green.

1. **Fix the three defects** in 1.1, 1.2 and 1.3, each with a test. Fix the small items in 1.4 in the same pass.
2. **Make `connect()` cheap** (2.1, first fix). A few lines; by the figures in 2.1 it should save about 4 ms of the 9 ms on the heaviest page (an estimate, not yet measured).
3. **Single graph schema and shared helpers** (3.1 and 3.2). This removes the cause of defect 1.2.
4. **Stop blocking the event loop** (2.4).
5. **One connection per request**, then remove the repeated access checks and N+1 queries (2.1 second fix, and 2.2).
6. **Scope the placement sweep** to changed entities (2.3).
7. **Split `services.py` and `web/main.py`** (3.3 and 3.4). Mechanical; do it after the behaviour changes above so the diffs stay readable.
8. **Response models and generated mobile types** (3.5), then the mobile clean-up (3.6).
9. Indexes, retention, image split and CI trigger (2.5, 2.6 and 4) as time allows.
