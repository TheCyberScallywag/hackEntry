# Review and refactor: Pydantic AI assistants, audit, Logfire and evals

Reviewed and implemented 22 November 2026 on the branch `agentic_sandbox`, from commit
`be20171`. Scope: everything under `src/app` and `tests`, `pyproject.toml`, `docker-compose.yml`
and `.env.example`. The mobile app, the docs build and the Docker images were not changed or
run.

This document says what was found, what was changed and what was left. How to use the result is
in the README ("Assistants (Pydantic AI)" and "Audit, Logfire and evals").

## 1. What was verified, and what was not

| | |
|---|---|
| Tests | 305 pass, 2 skipped (AuraDB and Compose, which need credentials and a running stack). Coverage 95%. 102 of the tests are new: 91 in four new files, 11 added to existing ones. |
| Lint | `ruff check` and `ruff format --check` clean. |
| A real model | **Not run.** There is no model key in this checkout, so no assistant has been run against a real model. Every agent test uses a scripted stand-in. The offline evals (`python -m app.cli evals`) are the check to run first once `AGENT_MODEL` and a key are set; expect to tune the instructions against them. |
| Logfire | Spans, metrics and evaluation events are asserted in memory. Nothing was sent to a Logfire project (no token). |
| Docker, mobile, docs | Not built. The web image gains the new dependencies through `uv.lock`. |

**The SQLite schema version went from 4 to 5** (`audit_log`, `agent_runs`). An existing data
directory is refused at start-up, as before: run `uv run task reset`.

## 2. Review findings

### Fixed

| # | Finding | Fix |
|---|---|---|
| 1 | **A sweep that failed was lost on restart.** `Projector._hook_due` lived in memory only, and the start-up drain ran the placement sweep only if the outbox had work. A crash after a failed sweep left candidates uncreated or unlapsed until something else touched them. The same happened without a restart if the sweep raised anything other than a graph or SQLite error. | The first drain of a process always sweeps everything, and the sweep stays owed until it finishes, whatever it failed with (`graph/projector.py`). |
| 2 | **A project lead out of standing got a 403 on their own project page.** The page decided "is lead" from `lead_id` alone, then `for_project` refused them. | The page falls back to the ordinary member view (`web/pages/projects.py`). |
| 3 | **A scoped sweep still read two whole tables** (every published project, every active user) inside the write transaction, on every profile save. | It now asks only about the projects and volunteers in hand (`placements.sweep`). |
| 4 | **`end_team_membership` authorised outside its transaction**, so a lead reassigned in between could still remove someone. | The check runs in the same transaction as the removal. |
| 5 | **Granting a role someone already held notified them again**, every time, and the admin check ran before the transaction that used it. | Notification (and now the audit row) only when something changed; the check is inside the transaction (`services/roles.py`). |
| 6 | **Catalogue lookups were linear scans** (`skill_name`, `cause_name`, and a new set built on every `skill_ids` access), called once per row on every match, coverage and placement screen. | Built once per catalogue (`catalogue.py`). |
| 7 | **The admin console crashed on a non-JSON error body** (a proxy's 502 page). | Reported as an error, like any other. |
| 8 | **`cli bundle` deleted the committed graph before the rebuild succeeded.** | Built beside it and swapped in on success. |
| 9 | **Logfire's FastAPI integration records endpoint arguments by default**, which would have put the sign-in email and whatever a volunteer typed on the trace. Found while testing the new instrumentation. | Arguments are not recorded (`observability.instrument_app`); a test asserts it. |

### Fixed in a second pass

| Finding | Fix |
|---|---|
| **The admin console refetched every tab on every click**, with a new connection per call. | The sections are a sidebar choice, so a click fetches only the one on screen, over one HTTP client kept for the session (`admin/main.py`). |
| **The graph lock retry could sleep about 18 seconds while holding the process-wide lock.** | A read gives up after about a second (`GraphStore.read_attempts`); a write keeps the longer window. A busy store is a quick 503 with "try again shortly". |
| **`cli matches --org KEY` found only seeded organisations.** | It asks the database for the ID by key, and derives it only for a bare `--store` file. |
| `solution-architecture.md` described schema versions 2 and 3 and did not mention the assistants. | Versions corrected; sections 7.7 to 7.9 added, with the stack, API, module and configuration tables. |

### Left open

| Finding | Why it was left |
|---|---|
| **The project page opens the Relate store up to three times** and repeats the access check on separate connections. | Needs the matching functions to accept an open transaction: a wider change, and only a cost under load. |
| **There is no sign-in rate limit or lock-out.** Failed sign-ins are audited and a burst is a check, but nothing stops them. | A policy decision: a lock-out lets anyone lock a known account, and the demo accounts are published. Behind Cloudflare Access it may not be wanted at all. |

## 3. What was built

```
src/app/
  audit.py              the audit trail, RBAC snapshot, usage summary, deterministic checks
  observability.py      Logfire: configure once, instrument the app, spans and counters
  agents/
    deps.py             AgentDeps, the model, the tool guard (Hooks), shared toolsets, house rules
    runner.py           authorise, limit, run, record, resume; the registry of assistants
    onboarding.py       volunteer-onboarding
    projects.py         project-scoping, project-review
    match.py            match-shortlist, match-explainer
    audit.py            audit (a pydantic_graph pipeline around one agent)
    evaluators.py       NoContactDetails, ReasonsGiven, Expectations; the online capability
    evals.py            offline datasets and the task that runs them
  web/agents_api.py     /api/v1/agents/* and /api/v1/admin/audit/*
```

### Which part of the Pydantic AI stack does what

| Piece | Where it is used |
|---|---|
| `Agent` with `deps_type`, typed `output_type`, static `instructions` | all six agents |
| `FunctionToolset` shared between agents; `@agent.tool` for the rest | `deps.catalogue_tools`, `deps.self_tools` |
| Output validators raising `ModelRetry` | every agent: IDs grounded in the data, exclusions untouched, verdicts consistent |
| Human in the loop: `ApprovalRequired`, `DeferredToolRequests`, `DeferredToolResults`, `ToolDenied`, stored message history | `save_profile`, `submit_for_vetting`, `save_draft`, `invite`; `runner.resume` |
| Capabilities: `Hooks` | `deps.guard`: domain errors become tool results, refusals are audited, tools used are recorded |
| `UsageLimits`, run timeout, `ModelSettings`, retries | `runner._limits`, `deps.MODEL_SETTINGS` |
| `FallbackModel`, model chosen at run time | `deps.build_model`, `Runtime.agent_model` |
| `pydantic_graph` (`GraphBuilder`) | the audit pipeline: three code steps, then the agent |
| `format_as_xml` for data in a prompt | the audit prompt |
| `pydantic_evals`: `Dataset`, `Case`, custom `Evaluator`s, `MaxModelRequests`, `MaxToolCalls`, `LLMJudge` | `agents/evals.py` |
| `pydantic_evals` online evaluation (`OnlineEvaluation` capability) | on every agent, results to Logfire |
| Logfire: `instrument_pydantic_ai`, `instrument_fastapi`, spans, metrics | `observability.py` |
| `TestModel` / `FunctionModel`, `ALLOW_MODEL_REQUESTS = False`, `capfire` | the tests |

Not used, deliberately: MCP, streaming, built-in web tools, durable execution, multi-agent
delegation. None of them answers a need this application has today, and each widens what an
assistant can reach.

### Design decisions worth knowing

- **The graph still decides.** No assistant computes eligibility. The shortlist and the explainer
  read what Relate returned and their validators reject anything else.
- **One way in.** Every agent runs through `runner.start`: authorise, quota, record, trace. An
  agent called any other way would skip all four, so nothing else calls `agent.run`.
- **Approval is asked after validation.** A write tool validates its arguments first and only
  then stops for approval, so a person is never asked to approve something that would fail.
- **Conversations are not kept.** A run's messages are stored only while it waits for approval
  and are deleted when it ends, or after two days if nobody answers.
- **No screening assistant.** Volunteer vetting is a decision about a person; a model's opinion
  on it was left out on purpose. The moderator's queue is unchanged.
- **Assistants are off by default** and the application behaves exactly as before without them.

## 4. Suggested next steps

1. Set `AGENT_MODEL` and a key, run `uv run python -m app.cli evals --judge`, and tune the
   instructions until the suites pass. Add cases as real use shows where an assistant goes wrong.
2. Set `LOGFIRE_TOKEN` and look at one run of each assistant end to end.
3. Put the assistants in front of people: an "ask for help" panel on the profile and suggestion
   pages, and a shortlist panel on the project page, each showing the pending action and an
   approve button. The API they need is in place.
4. Decide the sign-in lock-out policy and the open findings in section 2.
