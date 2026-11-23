"""The assistants, through the JSON API, with scripted stand-in models.

Each test says what the "model" does and proves what the application does about it: what it is
allowed to read, what waits for approval, what a validator sends back, and what is recorded. The
rules under test are the application's, so they hold whatever a real model decides to try.
"""

import pytest

from app import agents, services
from app.catalogue import get_catalogue, stable_id
from app.config import Settings
from app.runtime import DEMO_PASSWORD
from tests.agent_models import Scripted, call, final
from tests.conftest import AUDIT, CS, DK, FOOD_BANK, USERS, Web
from tests.test_rbac_onboarding import Client, as_, placement

DATA_ANALYSIS = get_catalogue().skill_name("data-analysis")
INTERLINK = stable_id("org", "interlinkrct")


def use(web: Web, *steps) -> Scripted:
    model = Scripted(*steps)
    web.rt.agent_model = model
    return model


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def signed_up(web: Web, name: str = "ffion") -> tuple[Client, str]:
    """A brand-new account: status draft, no skills, no organisation."""
    body = ok(
        web.client.post(
            "/api/v1/auth/signup",
            json={"email": f"{name}@cyberscallywags.uk", "display_name": name.title(),
                  "password": DEMO_PASSWORD},
        ),
        201,
    )  # fmt: skip
    return as_(web, name), body["user_id"]


def audit_rows(web: Web, action: str) -> list[dict]:
    return ok(as_(web, "sam").get("/admin/audit/events", params={"action": action}))


ADVICE = {"summary": "You offer bookkeeping; vetting is the next step.", "skills": [],
          "causes": [], "organisations": [], "missing": []}  # fmt: skip


# ---------------------------------------------------------------- volunteer onboarding
def test_onboarding_saves_a_profile_only_after_the_volunteer_approves(web: Web):
    ffion, ffion_id = signed_up(web)
    model = use(
        web,
        call("my_profile"),
        call("list_skills"),
        call("save_profile", skills={"bookkeeping": "offers"}, causes={"education": "prefer"}),
        final(**ADVICE, ready_for_vetting=True),
    )
    run = ok(ffion.post("/agents/volunteer-onboarding", {"message": "I keep the books."}))
    assert run["status"] == "awaiting_approval" and run["output"] is None
    [action] = run["pending"]
    assert action["tool"] == "save_profile"
    assert action["args"]["skills"] == {"bookkeeping": "offers"}
    assert action["summary"] == "Save to your profile: Bookkeeping: offers; Education: prefer"
    assert services.get_profile(web.rt.db, ffion_id).skills == {}  # nothing saved yet

    done = ok(ffion.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}))
    assert done["status"] == "completed" and done["output"]["ready_for_vetting"] is True
    profile = services.get_profile(web.rt.db, ffion_id)
    assert profile.skills == {"bookkeeping": "offers"} and profile.causes == {"education": "prefer"}
    assert "Saved. The profile now offers 1 skill(s)." in model.tool_results()

    record = ok(ffion.get(f"/agents/runs/{run['run_id']}"))
    assert record["status"] == "completed" and record["requests"] == 4  # both halves of the run
    assert record["tools"] == ["my_profile", "list_skills", "save_profile"]
    assert record["pending"] == [] and record["trace_id"]
    with web.rt.db.read() as conn:  # the stored conversation is gone once the run has ended
        assert conn.execute("SELECT messages FROM agent_runs").fetchone()[0] is None
    [approval] = audit_rows(web, "agent.approval")
    assert approval["actor"] == "Ffion" and approval["detail"] == {
        "approved": True, "tools": ["save_profile"],
    }  # fmt: skip


def test_onboarding_changes_nothing_when_the_volunteer_says_no(web: Web):
    ffion, ffion_id = signed_up(web)
    model = use(
        web,
        call("save_profile", skills={"bookkeeping": "offers"}, causes={}),
        final(**ADVICE, ready_for_vetting=False),
    )
    run = ok(ffion.post("/agents/volunteer-onboarding", {"message": "I keep the books."}))
    done = ok(
        ffion.post(f"/agents/runs/{run['run_id']}/approval",
                   {"approve": False, "reason": "Not bookkeeping, payroll."})
    )  # fmt: skip
    assert done["status"] == "completed"
    assert services.get_profile(web.rt.db, ffion_id).skills == {}
    assert "Not bookkeeping, payroll." in model.tool_results()  # the model is told why


def test_onboarding_cannot_touch_an_exclusion_or_invent_a_skill(web: Web):
    """Gwen excludes active listening. The assistant may not offer it for her, whatever the
    model tries, and an unknown skill never reaches the approval step."""
    gwen = as_(web, "gwen")
    model = use(
        web,
        call("save_profile", skills={"active-listening": "offers"}, causes={}),
        call("save_profile", skills={"juggling": "offers"}, causes={}),
        final(**ADVICE | {"skills": [{"skill_id": "active-listening", "state": "has",
                                      "reason": "She says she listens well."}]},
              ready_for_vetting=False),
        final(**ADVICE, ready_for_vetting=False),
    )  # fmt: skip
    run = ok(gwen.post("/agents/volunteer-onboarding", {"message": "I'm a great listener."}))
    assert run["status"] == "completed" and run["pending"] == []
    said = "\n".join(model.tool_results())
    assert "has excluded ['active-listening']" in said  # the tool, then the validator
    assert said.count("has excluded") == 2
    assert "Not in the skill catalogue: ['juggling']" in said
    assert services.get_profile(web.rt.db, USERS["gwen"]).skills["active-listening"] == "exclude"


def test_onboarding_cannot_claim_a_profile_is_ready_when_it_is_not(web: Web):
    ffion, _ = signed_up(web)
    model = use(
        web, final(**ADVICE, ready_for_vetting=True), final(**ADVICE, ready_for_vetting=False)
    )
    run = ok(ffion.post("/agents/volunteer-onboarding", {"message": "Hello"}))
    assert run["output"]["ready_for_vetting"] is False
    assert "ready_for_vetting must be false" in model.tool_results()[-1]


def test_onboarding_submits_for_vetting_after_approval(web: Web):
    ffion, ffion_id = signed_up(web)
    services.update_profile(
        web.rt.db, ffion_id, display_name="Ffion", skills={"first-aid": "offers"}, causes={},
        expected_version=0,
    )  # fmt: skip
    use(web, call("submit_for_vetting"), final(**ADVICE, ready_for_vetting=False))
    run = ok(ffion.post("/agents/volunteer-onboarding", {"message": "Put me forward."}))
    assert run["pending"][0]["summary"] == "Send your profile to a moderator for vetting."
    assert services.get_actor(web.rt.db, ffion_id).status == "draft"
    ok(ffion.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}))
    assert services.get_actor(web.rt.db, ffion_id).status == "screening"
    assert "A volunteer profile is waiting for screening." in as_(web, "mo").messages()


def test_onboarding_will_not_submit_a_profile_already_in_screening(web: Web):
    nia = as_(web, "nia")  # seeded in screening
    model = use(web, call("submit_for_vetting"), final(**ADVICE, ready_for_vetting=False))
    run = ok(nia.post("/agents/volunteer-onboarding", {"message": "Submit me again."}))
    assert run["status"] == "completed" and run["pending"] == []
    assert "In screening" in model.tool_results()[0]


def test_self_service_assistants_have_no_tool_that_names_a_person(web: Web):
    """Who the assistant acts for comes from the session. With no tool taking a user, a prompt
    cannot point it at somebody else's profile."""
    for name in ("volunteer_onboarding", "match_explainer", "project_scoping"):
        agent = {a.agent.name: a.agent for a in agents.REGISTRY.values()}[name]
        tools = agent._function_toolset.tools | {  # noqa: SLF001
            n: t for ts in agent.toolsets if hasattr(ts, "tools") for n, t in ts.tools.items()
        }
        assert tools, name
        for tool in tools.values():
            params = tool.function_schema.json_schema.get("properties", {})
            assert not [p for p in params if "user" in p or "volunteer" in p], (name, tool.name)


# ---------------------------------------------------------------- project scoping
DRAFT = {"title": "Scam Spotting Drop-in", "cause_id": "digital-inclusion",
         "summary": "Six weekly sessions showing older people how to spot scam emails.",
         "required_skills": ["security-awareness"], "optional_skills": []}  # fmt: skip


def test_scoping_saves_a_private_draft_after_approval_and_never_submits(web: Web):
    asha = as_(web, "asha")
    model = use(
        web,
        call("organisation_brief"),
        call("my_suggestions"),
        call("save_draft", **DRAFT),
        final(**DRAFT, reason="Spotting scams is security awareness.", open_questions=[],
              overlaps_with=[]),
    )  # fmt: skip
    run = ok(asha.post("/agents/project-scoping", {"org_id": CS, "idea": "Scam drop-in"}))
    assert (
        run["pending"][0]["summary"]
        == "Save “Scam Spotting Drop-in” as a private draft suggestion."
    )
    before = {s["title"] for s in ok(asha.get("/submissions"))}
    assert "Scam Spotting Drop-in" not in before

    done = ok(asha.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}))
    assert done["output"]["title"] == "Scam Spotting Drop-in"
    mine = {s["title"]: s["state"] for s in ok(asha.get("/submissions"))}
    assert mine["Scam Spotting Drop-in"] == "draft"
    queue = ok(as_(web, "kofi").get(f"/orgs/{CS}/submissions"))
    assert "Scam Spotting Drop-in" not in {q["title"] for q in queue}  # reviewers see nothing
    assert "save_draft" in model.tools_offered and "submit_draft" not in model.tools_offered
    brief = model.tool_results()[0]
    assert "Winter Outreach Kitchen" in brief  # the organisation's active project, to avoid overlap


def test_scoping_holds_a_proposal_to_the_suggestion_forms_rules(web: Web):
    asha = as_(web, "asha")
    model = use(
        web,
        call("save_draft", **DRAFT | {"cause_id": "space-travel"}),
        final(**DRAFT | {"required_skills": []}, reason="No skills are needed for this.",
              open_questions=[], overlaps_with=[]),
        final(**DRAFT, reason="Spotting scams is security awareness.", open_questions=[],
              overlaps_with=["p_nope"]),
        final(**DRAFT | {"optional_skills": ["security-awareness", "fundraising"]},
              reason="Spotting scams is security awareness.", open_questions=[],
              overlaps_with=[]),
    )  # fmt: skip
    run = ok(asha.post("/agents/project-scoping", {"org_id": CS, "idea": "Scam drop-in"}))
    said = "\n".join(model.tool_results())
    assert "Choose a cause from the catalogue." in said
    assert "required_skills" in said  # the schema itself refuses an empty list
    assert "unknown project IDs: ['p_nope']" in said
    # Normalised as the form would: a skill is not both required and optional.
    assert run["output"]["optional_skills"] == ["fundraising"]


@pytest.mark.parametrize(
    ("who", "org", "status"),
    [("dev", CS, 403), ("nia", CS, 403), ("asha", "org_nope", 404)],
    ids=["not-a-member", "not-vetted", "unknown-organisation"],
)
def test_scoping_is_refused_before_any_model_is_asked(web: Web, who: str, org: str, status: int):
    model = use(web)
    r = as_(web, who).post("/agents/project-scoping", {"org_id": org, "idea": "Anything"})
    assert r.status_code == status
    assert model.requests == []
    assert ok(as_(web, who).get("/agents/runs")) == []  # no run was started
    if status == 403:
        [row] = audit_rows(web, "agent.run")
        assert row["outcome"] == "denied" and row["target_id"] == "project-scoping"


# ---------------------------------------------------------------- project review
def assessment(recommendation: str, *, passed: bool, overlaps: list[str] | None = None) -> dict:
    return {
        "recommendation": recommendation,
        "reason_for_author": "Please say what finished looks like and who the audit is for.",
        "private_note": "",
        "checks": [{"name": "Summary says what done looks like", "passed": passed,
                    "note": "It lists activities but no outcome."}],
        "overlaps_with": overlaps or [],
    }  # fmt: skip


def test_review_recommends_and_the_reviewer_still_decides(web: Web):
    rhiannon = as_(web, "rhiannon")
    model = use(
        web,
        call("suggestion"),
        call("organisation_brief"),
        final(**assessment("approved", passed=False)),  # inconsistent: sent back
        final(**assessment("changes_requested", passed=False)),
    )
    run = ok(rhiannon.post("/agents/project-review", {"org_id": DK, "submission_id": AUDIT}))
    assert run["status"] == "completed"
    assert run["output"]["recommendation"] == "changes_requested"
    assert "You recommend approval but these checks failed" in model.tool_results()[-1]
    assert "Outreach Data Audit" in model.tool_results()[0]
    # It has nothing to decide with: the suggestion is exactly where it was.
    assert not {"decide", "review_submission", "save_draft"} & set(model.tools_offered)
    assert ok(rhiannon.get(f"/submissions/{AUDIT}"))["state"] == "submitted"


@pytest.mark.parametrize(
    ("who", "org", "status"),
    [("kofi", DK, 403), ("asha", DK, 403), ("kofi", CS, 404)],
    ids=["another-organisations-admin", "the-author", "wrong-organisation-in-the-request"],
)
def test_review_is_for_the_organisations_reviewers_only(web: Web, who, org, status):
    model = use(web)
    r = as_(web, who).post("/agents/project-review", {"org_id": org, "submission_id": AUDIT})
    assert r.status_code == status and model.requests == []


def test_review_is_only_for_a_suggestion_awaiting_review(web: Web):
    use(web)
    r = as_(web, "rhiannon").post(
        "/agents/project-review", {"org_id": DK, "submission_id": FOOD_BANK}
    )
    assert r.status_code == 409  # already published


# ---------------------------------------------------------------- shortlist
def test_shortlist_can_only_recommend_the_graphs_candidates(web: Web):
    cerys = as_(web, "cerys")  # lead of Food Bank Demand Dashboard
    dev = {"volunteer_id": USERS["dev"], "name": "Somebody Else", "covers": [DATA_ANALYSIS],
           "reason": "Offers data analysis and prefers the cause."}  # fmt: skip
    model = use(
        web,
        call("project_picture"),
        final(summary="Invite Dev and Gwen.", still_uncovered=["Made up"],
              recommendations=[dev, dev | {"volunteer_id": USERS["gwen"]}]),
        final(summary="Invite Dev for the data.", still_uncovered=[],
              recommendations=[dev | {"covers": [DATA_ANALYSIS, "First aid"]}]),
        final(summary="Invite Dev for the data.", still_uncovered=["Made up"],
              recommendations=[dev]),
    )  # fmt: skip
    run = ok(cerys.post("/agents/match-shortlist", {"project_id": FOOD_BANK}))
    said = model.tool_results()
    assert f"{USERS['gwen']} is not a candidate for this project" in said[-2]
    assert "Dev was not matched on ['First aid']" in said[-1]
    [rec] = run["output"]["recommendations"]
    assert rec["name"] == "Dev"  # from the database, not from the model
    assert run["output"]["still_uncovered"] == []  # computed: the made-up gap is gone
    picture = said[0]
    assert "@" not in picture  # display names only


def test_shortlist_names_the_gap_the_model_left_out(web: Web):
    cerys = as_(web, "cerys")
    use(web, final(summary="Nobody to invite yet.", recommendations=[], still_uncovered=[]))
    run = ok(cerys.post("/agents/match-shortlist", {"project_id": FOOD_BANK}))
    assert run["output"]["still_uncovered"] == [DATA_ANALYSIS]


def test_shortlist_invites_only_after_the_lead_approves(web: Web):
    cerys = as_(web, "cerys")
    model = use(
        web,
        call("invite", volunteer_id=USERS["gwen"]),  # not a candidate here
        call("invite", volunteer_id=USERS["dev"], note="Strong on data"),
        final(summary="Dev has been invited.", recommendations=[], still_uncovered=[]),
    )
    run = ok(cerys.post("/agents/match-shortlist", {"project_id": FOOD_BANK,
                                                    "question": "Invite Dev."}))  # fmt: skip
    assert "That is not one of this project's candidates" in model.tool_results()[0]
    assert run["pending"][0]["summary"] == "Invite Dev to the team."
    assert placement(web, FOOD_BANK, USERS["dev"])["lead_said"] is None

    ok(cerys.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}))
    row = placement(web, FOOD_BANK, USERS["dev"])
    assert row["lead_said"] == "yes" and row["lead_actor_id"] == USERS["cerys"]
    assert row["lead_note"] == "Strong on data" and row["state"] == "eligible"
    assert any("You are invited to join the team" in m for m in as_(web, "dev").messages())


@pytest.mark.parametrize(
    ("who", "status"), [("dev", 403), ("bryn", 404)], ids=["a-candidate", "an-outsider"]
)
def test_shortlist_is_for_the_lead_side_only(web: Web, who: str, status: int):
    model = use(web)
    r = as_(web, who).post("/agents/match-shortlist", {"project_id": FOOD_BANK})
    assert r.status_code == status and model.requests == []


# ---------------------------------------------------------------- match explainer
def test_explainer_explains_exactly_the_graphs_matches(web: Web):
    asha = as_(web, "asha")
    note = {"project_id": FOOD_BANK, "title": "Whatever", "why": "You offer data analysis."}
    model = use(
        web,
        call("my_matches"),
        final(summary="You have no matches right now.", matches=[], blocked=[], next_steps=[]),
        final(summary="One project fits.", blocked=[], next_steps=[],
              matches=[note, note | {"project_id": "p_invented"}]),
        final(summary="One project fits.", matches=[note], blocked=[], next_steps=[]),
    )  # fmt: skip
    run = ok(asha.post("/agents/match-explainer", {"org_id": DK}))
    said = model.tool_results()
    assert f"Explain every match; these are missing: ['{FOOD_BANK}']" in said[-2]
    assert "p_invented is not a match from my_matches" in said[-1]
    assert run["output"]["matches"][0]["title"] == "Food Bank Demand Dashboard"


def test_explainer_is_for_the_organisations_own_volunteers(web: Web):
    model = use(web)
    r = as_(web, "rhiannon").post("/agents/match-explainer", {"org_id": DK})  # reviews, no profile
    assert r.status_code == 403 and model.requests == []


def test_a_tool_refused_mid_run_is_told_to_the_model_and_audited(web: Web, monkeypatch):
    """Access is checked again inside every tool. If it has gone since the run started, the
    model is told the user may not, and the refusal is in the audit trail."""
    from app.agents import match

    real, calls = match.matching.volunteer_matches, []

    def volunteer_matches(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise services.Forbidden("volunteer")
        return real(*args, **kwargs)

    monkeypatch.setattr(match.matching, "volunteer_matches", volunteer_matches)
    note = {"project_id": FOOD_BANK, "title": "x", "why": "You offer data analysis."}
    model = use(
        web,
        call("my_matches"),
        final(summary="One project fits.", matches=[note], blocked=[], next_steps=[]),
    )
    ok(as_(web, "asha").post("/agents/match-explainer", {"org_id": DK}))
    assert model.tool_results()[0].startswith("REFUSED:")
    [row] = audit_rows(web, "agent.tool_denied")
    assert (row["actor"], row["outcome"], row["target_id"]) == ("Asha", "denied", "my_matches")
    assert row["source"] == "agent" and row["detail"]["agent"] == "match_explainer"


# ---------------------------------------------------------------- audit
def finding(check_id: str | None, severity: str) -> dict:
    return {"check_id": check_id, "severity": severity, "title": "A finding",
            "explanation": "This matters because access depends on it.",
            "recommendation": "Grant the role to a second person."}  # fmt: skip


NO_ADMIN = f"org-without-admin:{INTERLINK}"
BOTH = [finding("single-system-admin", "medium"), finding(NO_ADMIN, "medium")]
REPORT = {"overall": "healthy", "summary": "Two things need attention.", "usage_notes": []}


def test_audit_gathers_the_facts_itself_and_judges_the_report_by_rule(web: Web):
    sam = as_(web, "sam")
    model = use(
        web,
        call("audit_events", action_prefix="auth.", outcome="ok"),
        final(**REPORT, findings=[finding(None, "low"), *BOTH]),
    )
    out = ok(sam.post("/agents/audit", {"days": 7}))["output"]
    assert out["overall"] == "attention"  # by rule; the model said healthy
    assert [f["severity"] for f in out["findings"]] == ["medium", "medium", "low"]

    prompt = model.requests[0][0].parts[0].content
    assert "<audit_data>" in prompt and "single-system-admin" in prompt
    assert "InterLinkRCT has no org admin" in prompt and "@" not in prompt  # no emails
    assert model.tools_offered == ["audit_events"]  # read-only
    assert "auth.sign_in" in model.tool_results()[0]


@pytest.mark.parametrize(
    ("findings", "sent_back"),
    [
        ([BOTH[0]], f"Write a finding for every check. Missing: ['{NO_ADMIN}']"),
        ([finding("single-system-admin", "low"), BOTH[1]], "a finding cannot rate it lower"),
        ([*BOTH, finding(None, "high")], "rated high without a check behind it"),
        ([*BOTH, finding("made-up", "low")], "There is no check with ID 'made-up'"),
    ],
    ids=["omits-a-check", "plays-one-down", "unfounded-high", "invents-a-check"],
)
def test_audit_report_is_held_to_the_checks(web: Web, findings: list, sent_back: str):
    model = use(web, final(**REPORT, findings=findings), final(**REPORT, findings=BOTH))
    ok(as_(web, "sam").post("/agents/audit", {"days": 7}))
    assert sent_back in model.tool_results()[-1]


def test_a_model_that_never_gets_it_right_fails_the_run(web: Web):
    use(web, *[final(**REPORT, findings=[])] * 3)
    sam = as_(web, "sam")
    assert sam.post("/agents/audit", {"days": 7}).status_code == 502
    assert ok(sam.get("/agents/runs"))[0]["error"] == "UnexpectedModelBehavior"


def test_audit_assistant_is_for_system_admins(web: Web):
    model = use(web)
    for who in ("mo", "rhiannon", "asha"):
        assert as_(web, who).post("/agents/audit").status_code == 403
    assert model.requests == []


# ---------------------------------------------------------------- the runner
def test_assistants_are_off_without_a_model(web: Web):
    asha = as_(web, "asha")
    assert web.rt.agent_model is None
    listing = ok(asha.get("/agents"))
    assert [a["name"] for a in listing] == sorted(agents.REGISTRY) and not listing[0]["enabled"]
    r = asha.post("/agents/match-explainer", {"org_id": DK})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    use(web)
    assert all(a["enabled"] for a in ok(asha.get("/agents")))


def test_a_run_is_stopped_at_its_limits_and_recorded_as_failed(web: Web, settings: Settings):
    web.rt.settings = settings.model_copy(update={"agent_request_limit": 2})
    use(web, call("my_profile"), call("my_profile"), call("my_profile"))
    asha = as_(web, "asha")
    r = asha.post("/agents/volunteer-onboarding", {"message": "Loop for ever"})
    assert r.status_code == 502 and "Nothing was changed" in r.json()["detail"]
    [record] = ok(asha.get("/agents/runs"))
    assert record["status"] == "failed" and record["error"] == "UsageLimitExceeded"
    assert record["finished_at"]
    assert audit_rows(web, "agent.run")[0]["outcome"] == "error"


def test_each_account_has_a_daily_quota(web: Web, settings: Settings):
    web.rt.settings = settings.model_copy(update={"agent_daily_runs": 1})
    use(web, final(**ADVICE, ready_for_vetting=False), final(**ADVICE, ready_for_vetting=False))
    asha, bryn = as_(web, "asha"), as_(web, "bryn")
    ok(asha.post("/agents/volunteer-onboarding", {"message": "One"}))
    r = asha.post("/agents/volunteer-onboarding", {"message": "Two"})
    assert r.status_code == 429 and "Try again tomorrow" in r.json()["detail"]
    ok(bryn.post("/agents/volunteer-onboarding", {"message": "Mine is separate"}))


def test_only_whoever_started_a_run_can_answer_it_and_only_once(web: Web):
    ffion, _ = signed_up(web)
    use(
        web,
        call("save_profile", skills={"bookkeeping": "offers"}, causes={}),
        final(**ADVICE, ready_for_vetting=False),
    )
    run = ok(ffion.post("/agents/volunteer-onboarding", {"message": "Books"}))
    path = f"/agents/runs/{run['run_id']}/approval"
    assert as_(web, "sam").post(path, {"approve": True}).status_code == 404  # even an admin
    assert as_(web, "sam").get(f"/agents/runs/{run['run_id']}").status_code == 404
    ok(ffion.post(path, {"approve": True}))
    assert ffion.post(path, {"approve": True}).status_code == 409


def test_the_agent_api_needs_a_session(web: Web):
    r = web.client.post("/api/v1/agents/audit", json={})
    assert r.status_code == 401
    r = web.client.get("/api/v1/admin/audit/events")
    assert r.status_code == 401


def test_every_agent_route_declares_its_response(web: Web):
    schema = web.get("/api/openapi.json").json()
    paths = [p for p in schema["paths"] if "/agents" in p or "/admin/audit" in p]
    assert len(paths) == 14
    for path in paths:
        for operation in schema["paths"][path].values():
            body = operation["responses"]["200"]["content"]["application/json"]["schema"]
            assert "$ref" in body.get("items", body), path


def test_runs_are_private_to_whoever_started_them(web: Web):
    use(web, final(**ADVICE, ready_for_vetting=False))
    asha = as_(web, "asha")
    ok(asha.post("/agents/volunteer-onboarding", {"message": "Hello"}))
    assert len(ok(asha.get("/agents/runs"))) == 1
    assert ok(as_(web, "sam").get("/agents/runs")) == []  # an admin sees usage, not the runs
    [usage] = ok(as_(web, "sam").get("/admin/audit/usage"))["agents"]
    assert (usage["agent"], usage["runs"], usage["people"]) == ("volunteer-onboarding", 1, 1)


def test_scoping_reworks_an_existing_draft_in_place(web: Web):
    asha = as_(web, "asha")
    clinic = stable_id("p", "password-manager-clinic")  # her seeded draft, incomplete
    use(
        web,
        call("save_draft", **DRAFT | {"title": "Password Manager Clinic"}),
        final(**DRAFT | {"title": "Password Manager Clinic"}, overlaps_with=[], open_questions=[],
              reason="Setting up a password manager is security awareness."),
    )  # fmt: skip
    body = {"org_id": CS, "idea": "Finish my draft", "submission_id": clinic}
    run = ok(asha.post("/agents/project-scoping", body))
    ok(asha.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}))
    draft = ok(asha.get(f"/submissions/{clinic}"))
    assert draft["state"] == "draft" and draft["current"]["required"] == ["security-awareness"]
    assert len(ok(asha.get("/submissions"))) == 3  # reworked, not duplicated
    # Somebody else's suggestion, or one already submitted, cannot be reworked this way.
    assert asha.post("/agents/project-scoping", body | {"submission_id": AUDIT}).status_code == 409
    bryn = as_(web, "bryn")
    assert bryn.post("/agents/project-scoping", body).status_code == 404


def test_a_stale_write_is_reported_to_the_model_as_a_conflict(web: Web, monkeypatch):
    from app.agents import onboarding

    def stale(*_args, **_kwargs):
        raise services.Conflict("Your profile changed in another tab. Reload and try again.")

    ffion, _ = signed_up(web)
    model = use(
        web,
        call("save_profile", skills={"bookkeeping": "offers"}, causes={}),
        final(**ADVICE, ready_for_vetting=False),
    )
    run = ok(ffion.post("/agents/volunteer-onboarding", {"message": "Books"}))
    monkeypatch.setattr(onboarding.services, "update_profile", stale)
    ok(ffion.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}))
    assert model.tool_results()[-1].startswith("CONFLICT: Your profile changed in another tab.")


def test_the_model_comes_from_settings(tmp_path):
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.models.test import TestModel

    from app.agents.deps import build_model

    def settings(**kw) -> Settings:
        return Settings(data_dir=tmp_path, _env_file=None, **kw)

    assert build_model(settings()) is None  # off by default
    assert isinstance(build_model(settings(agent_model="test")), TestModel)
    keyed = build_model(settings(agent_model="anthropic:claude-sonnet-5-5",
                                 anthropic_api_key="sk-not-a-real-key"))  # fmt: skip
    assert isinstance(keyed, AnthropicModel) and keyed.model_name == "claude-sonnet-5-5"
    both = build_model(settings(agent_model="test", agent_fallback_model="test"))
    assert isinstance(both, FallbackModel)
