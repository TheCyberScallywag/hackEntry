"""The evaluators and the offline suites, proved with stand-in models: a model that behaves
passes every case, and one that does not is caught by name."""

from types import SimpleNamespace

import pytest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart

from app import cli
from app.agents import evals
from app.agents.evaluators import Expectations, NoContactDetails, ReasonsGiven, as_data
from app.agents.onboarding import OnboardingAdvice
from app.catalogue import get_catalogue, stable_id
from app.config import Settings
from app.runtime import Runtime
from tests.agent_models import Scripted, call, final
from tests.conftest import FOOD_BANK, USERS

INTERLINK = stable_id("org", "interlinkrct")
DATA_ANALYSIS = get_catalogue().skill_name("data-analysis")
ADVICE = {"summary": "Tell me what you can do and I will suggest skills.", "skills": [],
          "causes": [], "organisations": [], "ready_for_vetting": False}  # fmt: skip
FINDING = {"severity": "medium", "title": "A finding",
           "explanation": "Access depends on a single account.",
           "recommendation": "Grant the role to a second person."}  # fmt: skip

# What a well-behaved model does for each suite's cases, in order.
GOLDEN = {
    "volunteer-onboarding": [
        call("save_profile", skills={"bookkeeping": "offers", "fundraising": "offers"}, causes={}),
        call("save_profile", skills={"first-aid": "offers"}, causes={}),
        final(**ADVICE, missing=["What skills would you like to offer?"]),
    ],
    "project-scoping": [
        call(
            "save_draft",
            title="Scam Spotting Drop-in",
            cause_id="digital-inclusion",
            summary="Six weekly library sessions showing older people how to spot scams.",
            required_skills=["security-awareness"],
            optional_skills=[],
        ),
    ],  # fmt: skip
    "project-review": [
        final(
            recommendation="changes_requested",
            private_note="",
            overlaps_with=[],
            reason_for_author="Please say who this helps and what finished looks like.",
            checks=[{"name": "Summary is specific", "passed": False, "note": "Too vague."}],
        ),
    ],  # fmt: skip
    "match-shortlist": [
        final(
            summary="Invite Dev: he covers the data analysis gap.",
            still_uncovered=[],
            recommendations=[
                {
                    "volunteer_id": USERS["dev"],
                    "name": "Dev",
                    "covers": [DATA_ANALYSIS],
                    "reason": "Offers data analysis and prefers the cause.",
                }
            ],
        ),
    ],  # fmt: skip
    "match-explainer": [
        final(
            summary="One project fits your skills.",
            blocked=[],
            next_steps=[],
            matches=[
                {
                    "project_id": FOOD_BANK,
                    "title": "Food Bank Demand Dashboard",
                    "why": "You offer data analysis, which it requires.",
                }
            ],
        ),
    ],  # fmt: skip
    "audit": [
        final(
            overall="attention",
            summary="Two access risks need attention.",
            usage_notes=[],
            findings=[
                FINDING | {"check_id": "single-system-admin"},
                FINDING | {"check_id": f"org-without-admin:{INTERLINK}"},
            ],
        ),
    ],  # fmt: skip
}


@pytest.fixture
def demo(rt: Runtime) -> Runtime:
    rt.settings = rt.settings.model_copy(update={"agent_daily_runs": 0})
    return rt


@pytest.mark.parametrize("assistant", sorted(GOLDEN))
def test_a_well_behaved_model_passes_its_suite(demo: Runtime, assistant: str):
    demo.agent_model = Scripted(*GOLDEN[assistant])
    [report] = evals.run(demo, only=assistant).values()
    assert evals.failed(report) == []
    assert len(report.cases) == len(GOLDEN[assistant])
    names = {name for case in report.cases for name in case.assertions}
    assert {"NoContactDetails", "ReasonsGiven", "MaxModelRequests", "MaxToolCalls"} <= names
    assert names - {"NoContactDetails", "ReasonsGiven", "MaxModelRequests", "MaxToolCalls"}


def test_the_suites_cover_every_assistant(demo: Runtime):
    from app import agents

    assert {s.assistant for s in evals.suites({"vague": "p_x"})} == set(agents.REGISTRY)
    assert set(evals.RUBRICS) == set(agents.REGISTRY)


def test_a_model_that_misreads_the_volunteer_is_caught_by_name(demo: Runtime):
    demo.agent_model = Scripted(
        call("save_profile", skills={"cooking": "offers"}, causes={}),  # not what they said
        final(**ADVICE, missing=[]),  # proposes nothing where it should have
        call("save_profile", skills={"cooking": "offers"}, causes={}),  # saves on "hi"
    )
    [report] = evals.run(demo, only="volunteer-onboarding").values()
    assert evals.failed(report) == [
        "new-volunteer-two-skills: offers_expected_skills",
        "respects-an-exclusion: offers_expected_skills",
        "respects-an-exclusion: asks_approval_when_it_should",
        "too-little-to-go-on: asks_approval_when_it_should",
    ]


def test_a_run_that_cannot_finish_is_a_failed_case(demo: Runtime):
    demo.agent_model = Scripted(*[final(overall="healthy", summary="All is well here.",
                                        findings=[], usage_notes=[])] * 3)  # fmt: skip
    [report] = evals.run(demo, only="audit").values()
    [problem] = evals.failed(report)
    assert problem.startswith("demo-seed-audit: AgentFailed")


def test_the_evals_never_change_the_demo(demo: Runtime):
    def snapshot() -> list:
        with demo.db.read() as conn:
            return [
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
                for table in ("profile_skills", "submissions", "placements", "notifications")
            ]

    evals.prepare(demo)  # the one fixture the cases need
    before = snapshot()
    for assistant, steps in GOLDEN.items():
        demo.agent_model = Scripted(*steps)
        suite = next(s for s in evals.suites({"vague": _vague(demo)}) if s.assistant == assistant)
        evals.dataset(demo, suite).evaluate_sync(
            evals.task_for(demo, assistant), max_concurrency=1, progress=False
        )
    assert snapshot() == before


def _vague(rt: Runtime) -> str:
    with rt.db.read() as conn:
        return conn.execute(
            "SELECT id FROM submission_current WHERE title = ?", (evals.VAGUE_TITLE,)
        ).fetchone()[0]


# ---------------------------------------------------------------- the evaluators themselves
def ctx(output, metadata=None) -> SimpleNamespace:
    return SimpleNamespace(output=output, metadata=metadata)


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        ("Ask Asha, the project lead.", True),
        ("Write to asha@cyberscallywags.uk.", False),
        ("Ring 07700 900123 after six.", False),
        ("Call +44 20 7946 0958.", False),
        ("Run run_1b9765b940cfa439 used 20000 tokens on 2026-11-22.", True),
    ],
)
def test_no_contact_details(text: str, clean: bool):
    result = NoContactDetails().evaluate(ctx({"summary": text}))
    assert result.value is clean


def test_reasons_given_reads_every_level():
    good = {"skills": [{"skill_id": "x", "reason": "They said they keep the books."}]}
    assert ReasonsGiven().evaluate(ctx(good)).value is True
    assert ReasonsGiven().evaluate(ctx({"summary": "No suggestions at all"})).value is True
    bad = {"recommendations": [{"reason": "ok"}], "findings": [{"explanation": "Why not"}]}
    result = ReasonsGiven().evaluate(ctx(bad))
    assert result.value is False and result.reason == "2 reason(s) too short to check"


def test_evaluators_read_models_and_pending_approvals_alike():
    advice = OnboardingAdvice(**ADVICE, missing=[])
    assert as_data(advice)["ready_for_vetting"] is False
    waiting = DeferredToolRequests(
        approvals=[ToolCallPart("save_profile", {"skills": {"cooking": "offers"}}, "id1")]
    )
    assert as_data(waiting) == {
        "pending": [{"tool": "save_profile", "args": {"skills": {"cooking": "offers"}}}]
    }
    leaked = DeferredToolRequests(
        approvals=[ToolCallPart("invite", {"note": "mail dev@example.org"}, "id2")]
    )
    assert NoContactDetails().evaluate(ctx(leaked)).value is False


def test_expectations_reports_only_what_the_case_asks_about():
    run = {"output": {"recommendations": [{"volunteer_id": "a"}, {"volunteer_id": "z"}]},
           "pending": []}  # fmt: skip
    assert Expectations().evaluate(ctx(run, {})) == {}
    assert Expectations().evaluate(ctx(run, {"recommends": ["a"], "only": ["a", "b"]})) == {
        "recommends_expected": True,
        "recommends_only_candidates": False,
    }


# ---------------------------------------------------------------- the command
def test_evals_command_needs_a_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=tmp_path, _env_file=None))
    assert cli.main(["evals"]) == 1
    assert "Set AGENT_MODEL" in capsys.readouterr().err


def test_evals_command_reports_and_gates(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path, agent_model="test", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    steps = list(GOLDEN["match-shortlist"])

    def demo_runtime(given: Settings) -> Runtime:
        rt = evals.reset_demo(given.model_copy(update={"data_dir": tmp_path / "evals",
                                                       "agent_daily_runs": 0}))  # fmt: skip
        rt.agent_model = Scripted(*steps)
        return rt

    monkeypatch.setattr(evals, "demo_runtime", demo_runtime)
    assert cli.main(["evals", "--agent", "match-shortlist"]) == 0
    assert "Evaluation Summary: match_shortlist" in " ".join(capsys.readouterr().out.split())

    steps[0] = final(summary="Nobody stands out to me.", recommendations=[], still_uncovered=[])
    assert cli.main(["evals", "--agent", "match-shortlist"]) == 1
    assert "FAILED match-shortlist: cover-the-data-analysis-gap: recommends_expected" in (
        capsys.readouterr().err
    )
    assert cli.main(["evals", "--agent", "no-such-assistant"]) == 1  # nothing ran


def test_the_evals_get_their_own_demo(tmp_path):
    settings = Settings(data_dir=tmp_path / "live", agent_daily_runs=5, _env_file=None)
    rt = evals.demo_runtime(settings)
    assert rt.settings.data_dir != settings.data_dir and rt.settings.agent_daily_runs == 0
    assert not (tmp_path / "live").exists()
    assert rt.settings.sqlite_path.exists()
