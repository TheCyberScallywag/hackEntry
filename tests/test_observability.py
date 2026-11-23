"""Logfire: what a request, an assistant run and the background work put on the trace.

`capfire` captures the spans and metrics in memory; nothing leaves the process.
"""

import asyncio

from logfire.testing import CaptureLogfire
from pydantic_evals.online import wait_for_evaluations

from app import observability
from app.config import Settings
from tests.agent_models import Scripted, call, final
from tests.conftest import DK, FOOD_BANK, USERS, Web
from tests.test_rbac_onboarding import as_

ADVICE = {"summary": "You offer three skills already.", "skills": [], "causes": [],
          "organisations": [], "missing": [], "ready_for_vetting": False}  # fmt: skip


def spans(capfire: CaptureLogfire) -> list[dict]:
    return capfire.exporter.exported_spans_as_dict()


def named(capfire: CaptureLogfire, name: str) -> list[dict]:
    return [s for s in spans(capfire) if s["name"] == name]


def metrics(capfire: CaptureLogfire) -> dict[str, list[dict]]:
    """Every metric's data points. Collected once: a second collection only has what is new."""
    return {m["name"]: m["data"]["data_points"] for m in capfire.get_collected_metrics()}


def test_an_assistant_run_is_one_trace_from_request_to_tool(web: Web, capfire: CaptureLogfire):
    web.rt.agent_model = Scripted(call("my_profile"), final(**ADVICE))
    asha = as_(web, "asha")
    run = asha.post("/agents/volunteer-onboarding", {"message": "What next?"}).json()

    [request] = named(capfire, "POST /api/v1/agents/volunteer-onboarding")
    [assistant] = named(capfire, "assistant {agent}")
    [agent] = named(capfire, "invoke_agent volunteer_onboarding")
    [tool] = named(capfire, "execute_tool my_profile")
    chats = named(capfire, "chat scripted")
    trace = request["context"]["trace_id"]
    assert {s["context"]["trace_id"] for s in (assistant, agent, tool, *chats)} == {trace}
    assert assistant["parent"]["span_id"] == request["context"]["span_id"]
    assert agent["parent"]["span_id"] == assistant["context"]["span_id"]
    assert len(chats) == 2 and chats[0]["attributes"]["gen_ai.usage.input_tokens"] > 0

    attributes = assistant["attributes"]
    assert attributes["agent"] == "volunteer-onboarding" and attributes["run_id"] == run["run_id"]
    assert attributes["actor_id"] == USERS["asha"] and attributes["resumed"] is False
    # The run's row points at the same trace, so an admin can go from the record to Logfire.
    record = asha.get(f"/agents/runs/{run['run_id']}").json()
    assert record["trace_id"] == f"{trace:032x}"


def test_every_live_run_is_evaluated_on_its_own_trace(web: Web, capfire: CaptureLogfire):
    web.rt.agent_model = Scripted(final(**ADVICE))
    as_(web, "asha").post("/agents/volunteer-onboarding", {"message": "What next?"})
    asyncio.run(wait_for_evaluations())
    evaluated = {
        s["attributes"]["logfire.msg"] for s in named(capfire, "evaluator: {evaluator_name}")
    }
    assert evaluated == {"Calling evaluator: NoContactDetails", "Calling evaluator: ReasonsGiven"}
    results = {
        r["attributes"]["gen_ai.evaluation.name"]: r["attributes"]
        for r in capfire.log_exporter.exported_logs_as_dicts()
        if "gen_ai.evaluation.name" in r["attributes"]
    }
    assert set(results) == {"NoContactDetails", "ReasonsGiven"}
    assert all(r["gen_ai.evaluation.score.label"] == "pass" for r in results.values())
    assert results["ReasonsGiven"]["gen_ai.agent.name"] == "volunteer_onboarding"


def test_runs_and_audit_events_are_counted(web: Web, capfire: CaptureLogfire):
    web.rt.agent_model = Scripted(final(**ADVICE), call("my_profile"), call("my_profile"))
    web.rt.settings = web.rt.settings.model_copy(update={"agent_request_limit": 1})
    asha = as_(web, "asha")
    asha.post("/agents/volunteer-onboarding", {"message": "One"})
    asha.post("/agents/volunteer-onboarding", {"message": "Two"})  # stopped at the limit
    asha.get("/admin/users")  # refused
    collected = metrics(capfire)
    runs = {(p["attributes"]["agent"], p["attributes"]["status"]): p["value"]
            for p in collected["agent.runs"]}  # fmt: skip
    assert runs == {
        ("volunteer-onboarding", "completed"): 1,
        ("volunteer-onboarding", "failed"): 1,
    }
    [tokens] = collected["agent.tokens"]
    assert tokens["count"] == 1 and tokens["sum"] > 0
    audit = {(p["attributes"]["action"], p["attributes"]["outcome"]): p["value"]
             for p in collected["audit.events"]}  # fmt: skip
    assert audit[("agent", "ok")] == 1 and audit[("agent", "error")] == 1
    assert audit[("http", "denied")] == 1 and audit[("auth", "ok")] == 1


def test_a_refused_request_and_a_health_check_on_the_trace(web: Web, capfire: CaptureLogfire):
    as_(web, "asha").get("/admin/users")
    web.get("/healthz")
    web.get("/static/style.css")
    [refused] = named(capfire, "GET /api/v1/admin/users")
    assert refused["attributes"]["http.status_code"] == 403
    assert not [s for s in spans(capfire) if "healthz" in s["name"] or "static" in s["name"]]
    # Requests are traced without headers or arguments: no bearer token, and not the email
    # address that was just used to sign in.
    recorded = str(spans(capfire))
    assert "authorization" not in recorded.lower() and "fastapi.arguments.values" not in recorded
    assert "asha@cyberscallywags.uk" not in recorded and "demo-password" not in recorded


def test_the_placement_sweep_is_a_span(web: Web, capfire: CaptureLogfire):
    web.rt.projector.drain(ignore_backoff=True)  # nothing pending: no sweep
    assert named(capfire, "placements sweep") == []
    asha = as_(web, "asha")
    profile = asha.get("/profile").json()
    body = {"display_name": profile["display_name"], "version": profile["version"],
            "skills": {s["id"]: s["state"] for s in profile["skills"]} | {"data-analysis": "has"},
            "causes": {c["id"]: c["stance"] for c in profile["causes"]}}  # fmt: skip
    assert asha.put("/profile", body).status_code == 200
    [sweep] = named(capfire, "placements sweep")
    assert sweep["attributes"]["scoped"] is True and sweep["attributes"]["changes"] == 1
    with web.rt.db.read() as conn:  # Asha no longer offers what Food Bank needed from her
        state = conn.execute(
            "SELECT state FROM placements WHERE project_id = ? AND volunteer_id = ?",
            (FOOD_BANK, USERS["asha"]),
        ).fetchone()[0]
    assert state == "lapsed"


def test_prompts_stay_off_the_trace_in_production():
    assert Settings(_env_file=None).include_agent_content is True
    assert Settings(app_env="production", _env_file=None).include_agent_content is False
    kept = Settings(app_env="production", logfire_include_content=True, _env_file=None)
    assert kept.include_agent_content is True


def test_configure_runs_once_per_process(settings: Settings, monkeypatch):
    calls = []
    monkeypatch.setattr(observability.logfire, "configure", lambda **kw: calls.append(kw))
    monkeypatch.setattr(
        observability.logfire, "instrument_pydantic_ai", lambda **kw: calls.append(kw)
    )
    observability.configure(settings)  # tests/conftest.py already marked it configured
    assert calls == []
    observability.configure(settings, force=True)
    configured, instrumented = calls
    assert configured["send_to_logfire"] == "if-token-present" and configured["token"] is None
    assert configured["service_name"] == "volunteer-network" and configured["environment"] == "test"
    assert instrumented == {"include_content": True}
    assert DK  # (imported for the fixtures above)
