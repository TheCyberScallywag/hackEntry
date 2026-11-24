"""The placement rules that came out of MATCHING_REVIEW.md: standing, who may decide, reopening,
the ranked candidate list, notifications to org admins, the event history, reminders and expiry,
and team coverage. Each test names the review finding it pins."""

from app import placements
from tests.conftest import CS, DK, FOOD_BANK, USERS, WINTER, Web
from tests.test_rbac_onboarding import (
    NIA,
    SAM,
    as_,
    full_profile,
    graph_edges,
    placement,
    status_version,
)

LEAD = f"/projects/{FOOD_BANK}/placements"


def vet(web: Web, user_id: str, action: str) -> None:
    body = {"action": action, "version": status_version(web, user_id)}
    if action != "approve":
        body["note"] = "Paused pending a check"
    r = as_(web, "mo").post(f"/moderation/volunteers/{user_id}/decision", body)
    assert r.status_code == 200, r.text


def invite(web: Web, who: str, project: str, volunteer: str, **extra) -> dict:
    version = placement(web, project, volunteer)["version"]
    r = as_(web, who).post(
        f"/projects/{project}/placements/{volunteer}/answer",
        {"answer": "yes", "version": version} | extra,
    )
    assert r.status_code == 200, r.text
    return r.json()


def accept(web: Web, who: str, project: str) -> dict:
    version = placement(web, project, USERS[who])["version"]
    r = as_(web, who).post(f"/placements/{project}/answer", {"answer": "yes", "version": version})
    assert r.status_code == 200, r.text
    return r.json()


def events(web: Web, project: str, volunteer: str) -> list[tuple]:
    with web.rt.db.read() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT event, actor_id, note FROM placement_events WHERE project_id = ? "
                "AND volunteer_id = ? ORDER BY id",
                (project, volunteer),
            )
        ]


def backdate_invitation(web: Web, project: str, volunteer: str, days: int) -> None:
    with web.rt.db.tx() as conn:
        conn.execute(
            "UPDATE placements SET lead_said_at = datetime('now', ?) "
            "WHERE project_id = ? AND volunteer_id = ?",
            (f"-{days} days", project, volunteer),
        )


# ------------------------------------------------------------------ 1: standing of team members
def test_suspension_takes_a_volunteer_off_their_teams(web: Web):
    bryn = USERS["bryn"]
    assert WINTER in graph_edges(web, bryn, "ON_TEAM")
    vet(web, bryn, "suspend")
    assert placement(web, WINTER, bryn)["state"] == "removed"
    assert graph_edges(web, bryn, "ON_TEAM") == []  # every team, in every organisation
    assert any("Bryn is no longer on the team" in m for m in as_(web, "asha").messages())
    assert events(web, WINTER, bryn)[-1][0] == "suspended"
    vet(web, bryn, "reinstate")
    assert placement(web, WINTER, bryn)["state"] == "removed"  # not put back automatically
    r = as_(web, "asha").post(f"/projects/{WINTER}/placements/{bryn}/reopen")
    assert r.json() == {"state": "eligible"}  # the lead can take him back as a candidate


def test_suspension_closes_open_offers_and_reinstatement_brings_them_back(web: Web):
    dev = USERS["dev"]
    invite(web, "cerys", FOOD_BANK, dev)
    vet(web, dev, "suspend")
    assert placement(web, FOOD_BANK, dev)["state"] == "lapsed"
    vet(web, dev, "reinstate")
    p = placement(web, FOOD_BANK, dev)
    assert (p["state"], p["lead_said"]) == ("eligible", None)  # a candidate, to be invited afresh


def test_leaving_an_organisation_gives_up_its_team_places(web: Web):
    bryn = as_(web, "bryn")
    assert bryn.delete(f"/orgs/{CS}/membership").status_code == 200
    assert placement(web, WINTER, USERS["bryn"])["state"] == "left"
    assert WINTER not in graph_edges(web, USERS["bryn"], "ON_TEAM")
    assert graph_edges(web, USERS["bryn"], "ON_TEAM")  # his teams elsewhere are untouched
    assert any("Bryn is no longer on the team" in m for m in as_(web, "asha").messages())


# ------------------------------------------------------------------ 2: standing of the lead
def test_a_lead_who_left_the_organisation_loses_the_lead_powers(web: Web):
    vet(web, NIA, "approve")  # a candidate for Winter Outreach, which Asha leads
    asha = as_(web, "asha")
    assert asha.get(f"/projects/{WINTER}/placements").status_code == 200
    assert asha.delete(f"/orgs/{CS}/membership").status_code == 200
    assert asha.get(f"/projects/{WINTER}/placements").status_code == 404
    body = {"answer": "yes", "version": placement(web, WINTER, NIA)["version"]}
    assert asha.post(f"/projects/{WINTER}/placements/{NIA}/answer", body).status_code == 404
    assert placement(web, WINTER, NIA)["lead_said"] is None
    # the org admin is told the project needs a lead, and can still decide meanwhile
    assert any(
        "“Winter Outreach Kitchen” has no active lead" in m for m in as_(web, "kofi").messages()
    )
    assert invite(web, "kofi", WINTER, NIA) == {"state": "eligible"}


def test_a_suspended_lead_cannot_decide(web: Web):
    vet(web, NIA, "approve")
    asha = as_(web, "asha")
    vet(web, USERS["asha"], "suspend")
    body = {"answer": "yes", "version": placement(web, WINTER, NIA)["version"]}
    assert asha.post(f"/projects/{WINTER}/placements/{NIA}/answer", body).status_code == 403
    assert placement(web, WINTER, USERS["asha"])["state"] == "removed"  # and is off the team


# ------------------------------------------------------------------ 3: both acceptances
def test_nobody_decides_on_their_own_place_except_the_lead(web: Web):
    asha = USERS["asha"]  # a DataKind UK volunteer and a candidate for the food bank project
    assert as_(web, "sam").put(f"/admin/orgs/{DK}/roles/{asha}/org_admin").status_code == 200
    body = {"answer": "yes", "version": placement(web, FOOD_BANK, asha)["version"]}
    r = as_(web, "asha").post(f"{LEAD}/{asha}/answer", body)
    assert r.status_code == 403 and "Someone else must decide" in r.json()["detail"]
    rows = {p["display_name"]: p for p in as_(web, "asha").get(LEAD).json()}
    assert rows["Asha"]["can_decide"] is False and rows["Dev"]["can_decide"] is True
    assert invite(web, "rhiannon", FOOD_BANK, asha) == {"state": "eligible"}  # another admin can
    assert accept(web, "asha", FOOD_BANK) == {"state": "team"}


def test_the_lead_is_on_their_own_team_automatically(web: Web):
    cerys = placement(web, FOOD_BANK, USERS["cerys"])
    assert (cerys["state"], cerys["volunteer_said"], cerys["lead_said"]) == ("team", "yes", "yes")
    assert FOOD_BANK in graph_edges(web, USERS["cerys"], "ON_TEAM")
    assert events(web, FOOD_BANK, USERS["cerys"]) == [("lead_joined", USERS["cerys"], "")]
    rows = {p["display_name"]: p for p in as_(web, "cerys").get(LEAD).json()}
    assert rows["Cerys"]["is_lead"] and rows["Cerys"]["status"] == "team"


# ------------------------------------------------------------------ 4, 14: reopening
def test_a_declined_invitation_can_be_reopened_and_sent_again(web: Web):
    dev = USERS["dev"]
    invite(web, "cerys", FOOD_BANK, dev)
    version = placement(web, FOOD_BANK, dev)["version"]
    no = {"answer": "no", "version": version}
    assert as_(web, "dev").post(f"/placements/{FOOD_BANK}/answer", no).json() == {
        "state": "declined"
    }
    assert any("Dev declined the invitation" in m for m in as_(web, "cerys").messages())
    cerys = as_(web, "cerys")
    assert cerys.post(f"{LEAD}/{dev}/reopen").json() == {"state": "eligible"}
    assert cerys.post(f"{LEAD}/{dev}/reopen").status_code == 409  # nothing closed to reopen
    invite(web, "cerys", FOOD_BANK, dev)
    assert accept(web, "dev", FOOD_BANK) == {"state": "team"}
    assert [e[0] for e in events(web, FOOD_BANK, dev)] == [
        "matched", "invited", "declined", "reopened", "revived", "invited", "joined",
    ]  # fmt: skip


def test_reopening_needs_the_lead_side_and_a_volunteer_in_standing(web: Web):
    bryn = USERS["bryn"]
    as_(web, "asha").post(f"/projects/{WINTER}/placements/{bryn}/remove", {"reason": "Rota full"})
    assert as_(web, "bryn").post(f"/projects/{WINTER}/placements/{bryn}/reopen").status_code == 403
    assert as_(web, "gwen").post(f"/projects/{WINTER}/placements/{bryn}/reopen").status_code == 403
    vet(web, bryn, "suspend")
    r = as_(web, "asha").post(f"/projects/{WINTER}/placements/{bryn}/reopen")
    assert r.status_code == 422 and "not an active volunteer" in r.json()["detail"]


def test_a_volunteer_who_rejoins_an_organisation_can_be_taken_back(web: Web):
    bryn = USERS["bryn"]
    as_(web, "kofi").post(f"/orgs/{CS}/volunteers/{bryn}/remove", {"reason": "Moved away"})
    assert placement(web, WINTER, bryn)["state"] == "removed"
    assert as_(web, "bryn").put(f"/orgs/{CS}/membership").status_code == 200
    r = as_(web, "asha").post(f"/projects/{WINTER}/placements/{bryn}/reopen")
    assert r.json() == {"state": "eligible"}


def test_the_lead_can_withdraw_an_invitation_with_a_reason(web: Web):
    dev = USERS["dev"]
    invite(web, "cerys", FOOD_BANK, dev)
    body = {"answer": "no", "version": placement(web, FOOD_BANK, dev)["version"],
            "reason": "The role has been filled"}  # fmt: skip
    assert as_(web, "cerys").post(f"{LEAD}/{dev}/answer", body).json() == {"state": "declined"}
    assert any(
        "has been withdrawn: The role has been filled" in m for m in as_(web, "dev").messages()
    )
    p = placement(web, FOOD_BANK, dev)
    assert (p["lead_note"], p["lead_actor_id"]) == ("The role has been filled", USERS["cerys"])


# ------------------------------------------------------------------ 5, 6: the ranked list
def test_candidates_are_ranked_and_stay_current(web: Web):
    rows = [p for p in as_(web, "cerys").get(LEAD).json() if p["status"] == "candidate"]
    # preferred cause first, then required skills covered, then optional skills, then name
    assert [(p["display_name"], p["preferred"], len(p["matched_skills"])) for p in rows] == [
        ("Dev", True, 2), ("Asha", True, 1),
    ]  # fmt: skip
    version = rows[1]["version"]
    asha = as_(web, "asha")
    asha.put("/profile", full_profile(asha, bookkeeping="offers"))
    after = {p["display_name"]: p for p in as_(web, "cerys").get(LEAD).json()}
    assert after["Asha"]["matched_skills"] == ["Bookkeeping", "Spreadsheet and data analysis"]
    assert after["Asha"]["version"] == version  # refreshed without invalidating an open form


def test_candidates_are_not_told_until_invited(web: Web):
    assert as_(web, "dev").get("/placements").json() == []
    assert not any("Food Bank" in m for m in as_(web, "dev").messages())
    matches = as_(web, "dev").get(f"/orgs/{DK}/matches").json()["matches"]
    assert matches[0]["title"] == "Food Bank Demand Dashboard" and matches[0]["placement"] is None
    invite(web, "cerys", FOOD_BANK, USERS["dev"])
    matches = as_(web, "dev").get(f"/orgs/{DK}/matches").json()["matches"]
    assert (matches[0]["placement"], matches[0]["placement_label"]) == ("invited", "Invited")


# ------------------------------------------------------------------ 7, 8, 11: who is told
def test_an_org_admins_answer_stands_in_for_the_leads_and_is_on_record(web: Web):
    dev = USERS["dev"]
    invite(web, "rhiannon", FOOD_BANK, dev)  # the org admin, not the lead
    rows = {p["display_name"]: p for p in as_(web, "cerys").get(LEAD).json()}
    assert rows["Dev"]["decided_by"] == "Rhiannon" and rows["Dev"]["status"] == "invited"
    assert events(web, FOOD_BANK, dev)[-1] == ("invited", USERS["rhiannon"], "")
    accept(web, "dev", FOOD_BANK)
    for who in ("cerys", "rhiannon"):  # the lead and the admin who invited are both told
        assert any("Dev joined the team" in m for m in as_(web, who).messages())


def test_org_admins_hear_of_candidates_when_there_is_no_active_lead(web: Web):
    vet(web, USERS["asha"], "suspend")  # Winter Outreach now has no active lead
    vet(web, NIA, "approve")
    assert any(
        "Nia is a new candidate for “Winter Outreach Kitchen”" in m
        for m in as_(web, "kofi").messages()
    )
    assert not any("Nia is a new candidate" in m for m in as_(web, "asha").messages())


def test_changing_the_lead_tells_both_and_puts_the_new_lead_on_the_team(web: Web):
    dev, cerys = USERS["dev"], USERS["cerys"]
    r = as_(web, "rhiannon").put(f"/orgs/{DK}/projects/{FOOD_BANK}/lead", {"user_id": dev})
    assert r.status_code == 200
    assert any(
        "You are now the lead for “Food Bank Demand Dashboard”. 1 candidate is waiting" in m
        for m in as_(web, "dev").messages()
    )
    assert any("Dev is now the lead for “Food Bank" in m for m in as_(web, "cerys").messages())
    assert placement(web, FOOD_BANK, dev)["state"] == "team"
    assert FOOD_BANK in graph_edges(web, dev, "ON_TEAM")
    assert placement(web, FOOD_BANK, cerys)["state"] == "team"  # keeps the place she had
    body = {"answer": "yes", "version": placement(web, FOOD_BANK, USERS["asha"])["version"]}
    assert as_(web, "cerys").post(f"{LEAD}/{USERS['asha']}/answer", body).status_code == 403


# ------------------------------------------------------------------ 9: reminders and expiry
def test_unanswered_invitations_are_reminded_once_then_expire(web: Web):
    dev = USERS["dev"]
    invite(web, "cerys", FOOD_BANK, dev)
    chase = lambda: placements.chase(web.rt.db, remind_days=7, expire_days=14)  # noqa: E731
    assert chase() == 0
    backdate_invitation(web, FOOD_BANK, dev, 8)
    version = placement(web, FOOD_BANK, dev)["version"]
    assert chase() == 1 and chase() == 0  # one reminder only
    assert any(
        "Reminder: you are invited" in m and "7 days" in m for m in as_(web, "dev").messages()
    )
    assert placement(web, FOOD_BANK, dev)["version"] == version  # the open form still works
    backdate_invitation(web, FOOD_BANK, dev, 15)
    assert chase() == 1
    p = placement(web, FOOD_BANK, dev)
    assert (p["state"], p["lead_said"]) == ("eligible", None)  # back in the candidate list
    assert any("was not answered in time" in m for m in as_(web, "dev").messages())
    assert any("Dev did not answer the invitation" in m for m in as_(web, "cerys").messages())
    assert as_(web, "dev").get("/placements").json() == []
    assert placements.chase(web.rt.db, remind_days=0, expire_days=0) == 0  # both switched off


# ------------------------------------------------------------------ 10: coverage from the team
def test_coverage_counts_a_skill_only_once_it_is_on_the_team(web: Web):
    def coverage() -> dict:
        return as_(web, "rhiannon").get(f"/orgs/{DK}/coverage").json()["projects"][0]

    before = coverage()
    assert before["title"] == "Food Bank Demand Dashboard"
    assert before["gaps"] == [] and before["team_gaps"] == ["Spreadsheet and data analysis"]
    invite(web, "cerys", FOOD_BANK, USERS["dev"])
    row = next(r for r in coverage()["per_skill"] if r["skill"].startswith("Spreadsheet"))
    assert ("Dev", "invited") in [(c["name"], c["status"]) for c in row["candidates"]]
    assert coverage()["team_gaps"] == ["Spreadsheet and data analysis"]  # invited is not covered
    accept(web, "dev", FOOD_BANK)
    after = coverage()
    assert after["team_gaps"] == []
    row = next(r for r in after["per_skill"] if r["skill"].startswith("Spreadsheet"))
    assert row["team"] == ["Dev"] and "Dev" not in [c["name"] for c in row["candidates"]]
    matches = as_(web, "dev").get(f"/orgs/{DK}/matches").json()["matches"]
    assert matches[0]["placement"] == "team"


# ------------------------------------------------------------------ 13: answers re-check standing
def test_an_invitation_cannot_be_accepted_after_losing_standing(web: Web):
    from app import services

    dev = USERS["dev"]
    invite(web, "cerys", FOOD_BANK, dev)
    version = placement(web, FOOD_BANK, dev)["version"]
    with web.rt.db.tx() as conn:  # membership gone, but nothing else has caught up yet
        conn.execute("UPDATE memberships SET opted_in = 0 WHERE user_id = ?", (dev,))
    try:
        placements.volunteer_respond(web.rt.db, dev, FOOD_BANK, "yes", version=version)
    except services.Conflict as error:
        assert "not an active volunteer" in str(error)
    else:
        raise AssertionError("joined a team without standing")
    assert placement(web, FOOD_BANK, dev)["state"] == "eligible"
    assert SAM  # (the system admin holds every organisation's lead side; covered elsewhere)
