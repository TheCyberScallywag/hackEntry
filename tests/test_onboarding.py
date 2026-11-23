"""Each organisation's own onboarding workflow: org admins edit the steps and email templates,
a volunteer who joins goes through them, and the emails are queued and sent over SMTP."""

import smtplib

import pytest

from app import mailer
from app.runtime import DEMO_PASSWORD
from app.services import onboarding
from tests.agent_models import Scripted, call, final
from tests.conftest import CS, DK, IL, USERS, Web
from tests.test_rbac_onboarding import Client, as_

BASE = f"/orgs/{CS}/onboarding"


def workflow(admin: Client, org: str = CS) -> dict:
    r = admin.get(f"/orgs/{org}/onboarding")
    assert r.status_code == 200, r.text
    return r.json()


def switch_on(admin: Client, org: str = CS) -> dict:
    flow = workflow(admin, org)
    r = admin.put(
        f"/orgs/{org}/onboarding",
        {"enabled": True, "completion_template_id": flow["completion_template_id"],
         "version": flow["version"]},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()


def sign_up(web: Web, key: str = "ffion") -> Client:
    r = web.client.post(
        "/api/v1/auth/signup",
        json={"email": f"{key}@cyberscallywags.uk", "display_name": key.title(),
              "password": DEMO_PASSWORD},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return Client(web, r.json()["token"])


def emails(web: Web) -> list[dict]:
    with web.rt.db.read() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM email_outbox ORDER BY id")]


class FakeSMTP:
    """Stands in for smtplib.SMTP: records what was sent, or refuses to connect."""

    sent: list = []
    refuse = False

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.refuse:
            raise ConnectionRefusedError("refused")
        self.host, self.port = host, port

    def send_message(self, message):
        FakeSMTP.sent.append(message)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.refuse = [], False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


# ------------------------------------------------------------------ the starter workflow
def test_every_organisation_starts_with_a_starter_workflow_switched_off(web: Web):
    kofi = as_(web, "kofi")
    flow = workflow(kofi)
    assert flow["enabled"] is False and flow["version"] == 1
    assert [s["title"] for s in flow["steps"]] == ["Read our code of conduct", "Welcome call"]
    assert [s["position"] for s in flow["steps"]] == [1, 2]
    assert [t["name"] for t in flow["templates"]] == ["Next step", "Onboarding complete", "Welcome"]
    assert {p["name"] for p in flow["placeholders"]} == set(mailer.PLACEHOLDERS)
    # the starter templates use only placeholders that exist
    for template in flow["templates"]:
        assert mailer.template_problems(template["subject"], template["body"]) is None
    # switched off, joining starts nothing
    ffion = sign_up(web)
    assert ffion.put(f"/orgs/{CS}/membership").status_code == 200
    assert ffion.get("/me/onboarding").json() == [] and emails(web) == []
    # a new organisation gets one too
    sam = as_(web, "sam")
    new = sam.post("/admin/orgs", {"key": "new-org", "name": "New Org"}).json()["id"]
    assert len(workflow(sam, new)["steps"]) == 2


def test_only_the_organisations_own_admins_see_and_edit_its_workflow(web: Web):
    kofi, rhiannon, asha, sam = (as_(web, k) for k in ("kofi", "rhiannon", "asha", "sam"))
    assert rhiannon.get(BASE).status_code == 403  # an admin of another organisation
    assert asha.get(BASE).status_code == 403  # a volunteer of this one
    assert sam.get(BASE).status_code == 200  # system admins hold every organisation role
    step = workflow(kofi)["steps"][0]
    body = {"title": "Changed", "completed_by": "volunteer", "version": 1}
    for other in (rhiannon, asha):
        assert other.put(f"{BASE}/steps/{step['id']}", body).status_code == 403
        assert other.post(f"{BASE}/steps", body).status_code == 403
        assert other.post(f"{BASE}/templates/preview", {"subject": "Hello", "body": "x" * 20}
                          ).status_code == 403  # fmt: skip
        assert other.get(f"{BASE}/volunteers").status_code == 403
        assert other.get(f"{BASE}/emails").status_code == 403
    # a step of one organisation cannot be edited through another's URL
    assert rhiannon.put(f"/orgs/{DK}/onboarding/steps/{step['id']}", body).status_code == 404
    assert workflow(kofi)["steps"][0]["title"] == "Read our code of conduct"
    assert kofi.get("/orgs/org_nope/onboarding").status_code == 404


# ------------------------------------------------------------------ editing
def test_an_org_admin_edits_steps_and_a_stale_version_is_refused(web: Web):
    kofi = as_(web, "kofi")
    flow = workflow(kofi)
    welcome = next(t for t in flow["templates"] if t["name"] == "Welcome")
    r = kofi.post(f"{BASE}/steps", {
        "title": "  Safeguarding   briefing ", "description": "Watch the short video.",
        "completed_by": "volunteer", "email_template_id": welcome["id"], "version": 1,
    })  # fmt: skip
    assert r.status_code == 201
    flow = r.json()
    assert flow["version"] == 2
    assert [s["title"] for s in flow["steps"]][-1] == "Safeguarding briefing"
    new = flow["steps"][-1]
    # the version the first edit was made from is stale now
    stale = kofi.post(f"{BASE}/steps", {"title": "Late", "completed_by": "volunteer", "version": 1})
    assert stale.status_code == 409 and "changed since you opened it" in stale.json()["detail"]
    flow = kofi.post(f"{BASE}/steps/{new['id']}/move", {"direction": "up", "version": 2}).json()
    assert [s["title"] for s in flow["steps"]] == [
        "Read our code of conduct", "Safeguarding briefing", "Welcome call",
    ]  # fmt: skip
    assert [s["position"] for s in flow["steps"]] == [1, 2, 3]
    first = flow["steps"][0]["id"]
    r = kofi.post(f"{BASE}/steps/{first}/move", {"direction": "up", "version": 3})
    assert r.status_code == 422 and "already first" in r.json()["detail"]
    flow = kofi.put(f"{BASE}/steps/{new['id']}", {
        "title": "Safeguarding", "completed_by": "org_admin", "version": 3,
    }).json()  # fmt: skip
    edited = next(s for s in flow["steps"] if s["id"] == new["id"])
    assert (edited["completed_by"], edited["email_template_id"], edited["description"]) == (
        "org_admin", None, "",
    )  # fmt: skip
    flow = kofi.delete(f"{BASE}/steps/{new['id']}?version={flow['version']}").json()
    assert [s["position"] for s in flow["steps"]] == [1, 2]
    assert kofi.delete(f"{BASE}/steps/{new['id']}?version={flow['version']}").status_code == 404


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"title": "ab", "completed_by": "volunteer"}, "Give the step a title"),
        ({"title": "Fine", "completed_by": "volunteer", "description": "x" * 1001},
         "Keep the description under"),
        ({"title": "Fine", "completed_by": "volunteer", "email_template_id": "tpl_other"},
         "one of this organisation's email templates"),
    ],
)  # fmt: skip
def test_invalid_steps_are_refused(web: Web, body: dict, message: str):
    r = as_(web, "kofi").post(f"{BASE}/steps", body | {"version": 1})
    assert r.status_code == 422 and message in r.json()["detail"]


def test_a_step_cannot_use_another_organisations_template(web: Web):
    kofi, rhiannon = as_(web, "kofi"), as_(web, "rhiannon")
    theirs = workflow(rhiannon, DK)["templates"][0]["id"]
    r = kofi.post(f"{BASE}/steps", {
        "title": "Borrowed", "completed_by": "volunteer", "email_template_id": theirs,
        "version": 1,
    })  # fmt: skip
    assert r.status_code == 422
    assert workflow(kofi)["version"] == 1  # a refused edit changes nothing


def test_the_workflow_cannot_be_switched_on_without_steps_and_emptying_it_switches_it_off(
    web: Web,
):
    kofi = as_(web, "kofi")
    flow = switch_on(kofi)
    assert flow["enabled"] is True
    for step in flow["steps"]:
        flow = kofi.delete(f"{BASE}/steps/{step['id']}?version={flow['version']}").json()
    assert flow["steps"] == [] and flow["enabled"] is False
    r = kofi.put(BASE, {"enabled": True, "version": flow["version"]})
    assert r.status_code == 422 and "Add at least one step" in r.json()["detail"]


# ------------------------------------------------------------------ email templates
def test_an_org_admin_writes_previews_and_deletes_templates(web: Web):
    kofi = as_(web, "kofi")
    body = "Hi {{ volunteer_name }},\nwelcome to {{organisation_name}}.\n{{ onboarding_url }}"
    r = kofi.post(f"{BASE}/templates", {
        "name": "Induction", "subject": "Induction at {{ organisation_name }}", "body": body,
        "version": 1,
    })  # fmt: skip
    assert r.status_code == 201
    flow = r.json()
    made = next(t for t in flow["templates"] if t["name"] == "Induction")
    preview = kofi.post(f"{BASE}/templates/preview",
                        {"subject": made["subject"], "body": made["body"]}).json()  # fmt: skip
    assert preview == {
        "subject": "Induction at Cyber Scallywags",
        "body": "Hi Alex Example,\nwelcome to Cyber Scallywags.\nhttp://localhost:8080/onboarding",
    }
    # a second template of the same name
    again = kofi.post(f"{BASE}/templates", {
        "name": "Induction", "subject": "Another", "body": body, "version": flow["version"],
    })  # fmt: skip
    assert again.status_code == 409 and "already has a template" in again.json()["detail"]
    flow = kofi.put(f"{BASE}/templates/{made['id']}", {
        "name": "Induction", "subject": "See you soon", "body": body, "version": flow["version"],
    }).json()  # fmt: skip
    assert next(t for t in flow["templates"] if t["id"] == made["id"])["subject"] == "See you soon"
    # deleting a template a step uses leaves the step, without an email
    welcome = next(t for t in flow["templates"] if t["name"] == "Welcome")
    flow = kofi.delete(f"{BASE}/templates/{welcome['id']}?version={flow['version']}").json()
    assert flow["steps"][0]["email_template_id"] is None
    done = flow["completion_template_id"]
    flow = kofi.delete(f"{BASE}/templates/{done}?version={flow['version']}").json()
    assert flow["completion_template_id"] is None
    assert kofi.delete(f"{BASE}/templates/{done}?version={flow['version']}").status_code == 404


@pytest.mark.parametrize(
    ("subject", "body", "message"),
    [
        ("Hi", "A body that is long enough", "Give the email a subject"),
        ("A subject", "short", "Write the email's text"),
        ("A subject", "Hello {{ secret }}, welcome", "Unknown placeholder: {{ secret }}"),
        ("Hello {{ email }}", "A body that is long enough", "Unknown placeholder: {{ email }}"),
        ("A subject", "Hello {{ volunteer_name }, welcome", "check the curly brackets"),
        ("A subject", "Hello {{ volunteer.name }}, welcome", "check the curly brackets"),
        ("A subject", "{% for x in y %}loop{% endfor %} {{ 7*7 }}", "check the curly brackets"),
    ],
)  # fmt: skip
def test_templates_take_only_the_listed_placeholders(web: Web, subject, body, message):
    kofi = as_(web, "kofi")
    r = kofi.post(f"{BASE}/templates/preview", {"subject": subject, "body": body})
    assert r.status_code == 422 and message in r.json()["detail"]
    r = kofi.post(f"{BASE}/templates", {"name": "Bad", "subject": subject, "body": body,
                                        "version": 1})  # fmt: skip
    assert r.status_code == 422 and len(workflow(kofi)["templates"]) == 3


def test_values_are_inserted_as_text_and_never_read_as_placeholders():
    values = {"volunteer_name": "{{ organisation_name }}", "organisation_name": "Org"}
    assert mailer.render("Hi {{ volunteer_name }} at {{organisation_name}}", values) == (
        "Hi {{ organisation_name }} at Org"
    )
    assert mailer.render("{{ steps_done }}", {}) == ""


# ------------------------------------------------------------------ a volunteer's run
def test_a_new_volunteer_goes_through_the_steps_in_order(web: Web):
    kofi = as_(web, "kofi")
    flow = switch_on(kofi)
    first, second = flow["steps"]
    ffion = sign_up(web)
    assert ffion.put(f"/orgs/{CS}/membership").status_code == 200
    assert ffion.put(f"/orgs/{CS}/membership").status_code == 200  # joining twice starts one run
    [run] = ffion.get("/me/onboarding").json()
    assert (run["organisation"], run["state"], run["steps_done"], run["steps_total"]) == (
        "Cyber Scallywags", "in_progress", 0, 2,
    )  # fmt: skip
    assert [(s["state"], s["can_complete"]) for s in run["steps"]] == [
        ("open", True), ("waiting", False),
    ]  # fmt: skip
    assert "Cyber Scallywags onboarding, step 1 of 2: Read our code of conduct." in ffion.messages()
    [mail] = emails(web)
    assert (mail["purpose"], mail["template_name"], mail["status"]) == ("step", "Welcome", "queued")
    assert mail["subject"] == "Welcome to Cyber Scallywags"
    assert "Hello Ffion," in mail["body"] and "Read our code of conduct" in mail["body"]
    assert "@" not in mail["body"]  # the address is read only when the message is sent

    # the second step is not open yet, and it is not the volunteer's to complete anyway
    r = ffion.post(f"{BASE}/steps/{second['id']}/complete")
    assert r.status_code == 403
    run = ffion.post(f"{BASE}/steps/{first['id']}/complete").json()
    assert [s["state"] for s in run["steps"]] == ["done", "open"] and run["steps_done"] == 1
    assert ffion.post(f"{BASE}/steps/{first['id']}/complete").status_code == 409  # already done
    assert not any(s["can_complete"] for s in run["steps"])
    assert any("step 2 of 2: Welcome call. Cyber Scallywags will confirm it." in m
               for m in ffion.messages())  # fmt: skip
    assert emails(web)[-1]["subject"] == "Cyber Scallywags: your next step"
    assert "finished 1 of 2" in emails(web)[-1]["body"]

    # the org admin sees where Ffion is, without her address, and confirms the call
    [row] = kofi.get(f"{BASE}/volunteers").json()
    assert (row["display_name"], row["current_step"], row["waiting_for"], row["steps_done"]) == (
        "Ffion", "Welcome call", "org_admin", 1,
    )  # fmt: skip
    assert "email" not in row and "@" not in str(row)
    done = f"{BASE}/volunteers/{row['user_id']}/steps/{second['id']}/complete"
    assert as_(web, "asha").post(done).status_code == 403
    run = kofi.post(done).json()
    assert run["state"] == "completed" and run["completed_at"] and run["steps_done"] == 2
    assert kofi.post(done).status_code == 409
    assert "You have finished onboarding with Cyber Scallywags." in ffion.messages()
    assert emails(web)[-1]["purpose"] == "completed"
    [row] = kofi.get(f"{BASE}/volunteers").json()
    assert row["state_label"] == "Completed" and row["current_step"] is None
    sent = kofi.get(f"{BASE}/emails").json()
    assert [e["recipient"] for e in sent] == ["Ffion"] * 3 and "@" not in str(sent)


def test_current_members_are_not_enrolled_and_leaving_cancels_a_run(web: Web):
    kofi = as_(web, "kofi")
    flow = switch_on(kofi)
    assert kofi.get(f"{BASE}/volunteers").json() == []  # Asha, Bryn, Gwen and Nia were here before
    assert as_(web, "asha").get("/me/onboarding").json() == []
    ffion = sign_up(web)
    ffion.put(f"/orgs/{CS}/membership")
    assert ffion.delete(f"/orgs/{CS}/membership").status_code == 200
    assert ffion.get("/me/onboarding").json() == []
    assert kofi.get(f"{BASE}/volunteers").json() == []
    r = ffion.post(f"{BASE}/steps/{flow['steps'][0]['id']}/complete")
    assert r.status_code == 409  # the run is cancelled
    # joining again starts afresh
    ffion.put(f"/orgs/{CS}/membership")
    [run] = ffion.get("/me/onboarding").json()
    assert run["state"] == "in_progress" and run["steps_done"] == 0
    # removal by the org admin cancels it too
    user_id = kofi.get(f"{BASE}/volunteers").json()[0]["user_id"]
    kofi.post(f"/orgs/{CS}/volunteers/{user_id}/remove", {"reason": "Moved away"})
    assert ffion.get("/me/onboarding").json() == []
    # someone with no run in the organisation
    r = as_(web, "asha").post(f"{BASE}/steps/{flow['steps'][0]['id']}/complete")
    assert r.status_code == 404


def test_editing_the_steps_moves_volunteers_who_are_part_way_through(web: Web):
    kofi = as_(web, "kofi")
    flow = switch_on(kofi)
    first, second = flow["steps"]
    ffion = sign_up(web)
    ffion.put(f"/orgs/{CS}/membership")
    # reorder: the welcome call is now first, so it becomes Ffion's current step
    flow = kofi.post(f"{BASE}/steps/{second['id']}/move",
                     {"direction": "up", "version": flow["version"]}).json()  # fmt: skip
    [run] = ffion.get("/me/onboarding").json()
    assert [(s["title"], s["state"]) for s in run["steps"]] == [
        ("Welcome call", "open"), ("Read our code of conduct", "waiting"),
    ]  # fmt: skip
    # delete the step she is on: she moves to the next one, and is told once
    flow = kofi.delete(f"{BASE}/steps/{second['id']}?version={flow['version']}").json()
    [run] = ffion.get("/me/onboarding").json()
    assert [(s["title"], s["state"]) for s in run["steps"]] == [
        ("Read our code of conduct", "open")
    ]
    assert sum("Read our code of conduct" in m for m in ffion.messages()) == 2
    # a step added later does not reopen a finished run
    ffion.post(f"{BASE}/steps/{first['id']}/complete")
    kofi.post(f"{BASE}/steps", {"title": "Extra", "completed_by": "volunteer",
                                "version": flow["version"]})  # fmt: skip
    assert ffion.get("/me/onboarding").json()[0]["state"] == "completed"


# ------------------------------------------------------------------ delivery
def test_queued_emails_are_sent_over_smtp_to_the_volunteers_address(web: Web, smtp, settings):
    kofi = as_(web, "kofi")
    switch_on(kofi)
    sign_up(web).put(f"/orgs/{CS}/membership")
    configured = settings.model_copy(
        update={"smtp_host": "mail", "public_url": "https://volunteer.example/"}
    )
    assert mailer.deliver(web.rt.db, configured) == mailer.Delivered(sent=1)
    [message] = smtp.sent
    assert message["To"] == "Ffion <ffion@cyberscallywags.uk>"
    assert message["From"].startswith("Cyber Scallywags via ")
    assert message["Subject"] == "Welcome to Cyber Scallywags"
    assert "https://volunteer.example/onboarding" in message.get_content()
    [row] = emails(web)
    assert (row["status"], row["attempts"]) == ("sent", 1) and row["sent_at"]
    assert mailer.deliver(web.rt.db, configured) == mailer.Delivered()  # nothing is sent twice
    assert kofi.get(f"{BASE}/emails").json()[0]["status"] == "sent"


def test_with_no_mail_server_emails_are_kept_and_marked_not_configured(web: Web, smtp):
    kofi = as_(web, "kofi")
    template = workflow(kofi)["templates"][0]["id"]
    assert kofi.post(f"{BASE}/templates/{template}/test").json() == {"queued": True}
    assert kofi.post(f"{BASE}/templates/tpl_nope/test").status_code == 404
    assert mailer.deliver(web.rt.db, web.rt.settings) == mailer.Delivered(not_configured=1)
    assert smtp.sent == []
    [mail] = kofi.get(f"{BASE}/emails").json()
    assert (mail["recipient"], mail["purpose"], mail["status"]) == (
        "Kofi", "test", "not_configured",
    )  # fmt: skip


def test_an_unreachable_mail_server_is_retried_then_given_up_on(web: Web, smtp, settings):
    kofi = as_(web, "kofi")
    kofi.post(f"{BASE}/templates/{workflow(kofi)['templates'][0]['id']}/test")
    configured = settings.model_copy(update={"smtp_host": "mail"})
    smtp.refuse = True
    assert mailer.deliver(web.rt.db, configured) == mailer.Delivered(failed=1)
    [row] = emails(web)
    assert (row["status"], row["attempts"]) == ("queued", 1)
    assert "unreachable" in row["last_error"]
    assert mailer.deliver(web.rt.db, configured) == mailer.Delivered()  # backing off
    for _ in range(mailer.MAX_ATTEMPTS - 1):
        with web.rt.db.tx() as conn:
            conn.execute("UPDATE email_outbox SET next_attempt_at = created_at")
        mailer.deliver(web.rt.db, configured)
    assert emails(web)[0]["status"] == "failed"
    smtp.refuse = False
    assert mailer.deliver(web.rt.db, configured) == mailer.Delivered()  # failed is final


def test_one_refused_message_does_not_stop_the_others(web: Web, smtp, settings, monkeypatch):
    kofi = as_(web, "kofi")
    template = workflow(kofi)["templates"][0]["id"]
    kofi.post(f"{BASE}/templates/{template}/test")
    kofi.post(f"{BASE}/templates/{template}/test")
    calls = []

    def send(self, message):
        calls.append(message)
        if len(calls) == 1:
            raise smtplib.SMTPRecipientsRefused({})
        FakeSMTP.sent.append(message)

    monkeypatch.setattr(FakeSMTP, "send_message", send)
    configured = settings.model_copy(update={"smtp_host": "mail"})
    assert mailer.deliver(web.rt.db, configured) == mailer.Delivered(sent=1, failed=1)
    assert [e["status"] for e in emails(web)] == ["queued", "sent"]


def test_old_emails_are_pruned_but_queued_ones_are_kept(web: Web):
    from app.sqlstore import prune

    kofi = as_(web, "kofi")
    template = workflow(kofi)["templates"][0]["id"]
    for _ in range(2):
        kofi.post(f"{BASE}/templates/{template}/test")
    with web.rt.db.tx() as conn:
        conn.execute("UPDATE email_outbox SET created_at = '2020-01-01T00:00:00+00:00'")
        first, second = (e["id"] for e in emails(web))
        conn.execute("UPDATE email_outbox SET status = 'sent' WHERE id = ?", (first,))
        prune(conn)
    assert [e["id"] for e in emails(web)] == [second]


# ------------------------------------------------------------------ the volunteer's page
def test_the_onboarding_page_lists_steps_and_completes_the_current_one(web: Web):
    flow = switch_on(as_(web, "kofi"))
    web.sign_in("dev")
    assert 'href="/onboarding"' not in web.get("/home").text  # nothing to do: no nav entry
    assert "Nothing to do" in web.get("/onboarding").text
    assert web.post(f"/orgs/{CS}/join").status_code == 303
    assert 'href="/onboarding"' in web.get("/home").text
    page = web.get("/onboarding").text
    assert "Cyber Scallywags" in page and "0 of 2 steps done" in page
    assert page.count("Mark as done") == 1 and 'aria-current="step"' in page
    first, second = flow["steps"]
    r = web.post(f"/onboarding/{CS}/steps/{second['id']}/complete", csrf_from="/onboarding")
    assert r.status_code == 403
    r = web.post(f"/onboarding/{CS}/steps/{first['id']}/complete", csrf_from="/onboarding")
    assert r.status_code == 303 and r.headers["location"] == "/onboarding?msg=step_done"
    page = web.get("/onboarding?msg=step_done").text
    assert "Step completed." in page and "1 of 2 steps done" in page
    assert "Mark as done" not in page and "Cyber Scallywags will confirm this step." in page
    r = web.client.post(f"/onboarding/{CS}/steps/{first['id']}/complete", data={})
    assert r.status_code == 403  # no CSRF token
    assert onboarding.my_onboarding(web.rt.db, USERS["dev"])[0]["steps_done"] == 1


# ------------------------------------------------------------------ the workflow in the graph
FLOW = """
MATCH (o:Organisation {id: $org})-[:HAS_ONBOARDING_STEP]->(s:OnboardingStep)
OPTIONAL MATCH (s)-[:NEXT_STEP]->(n:OnboardingStep)
RETURN o.onboarding_enabled AS enabled, s.title AS title, s.position AS position,
       s.completed_by AS completed_by, s.sends_email AS sends_email, n.title AS next
ORDER BY position LIMIT $limit
"""
PLACE = """
MATCH (v:Volunteer {id: $volunteer})-[r:AT_STEP|COMPLETED_STEP]->(s:OnboardingStep)
      <-[:HAS_ONBOARDING_STEP]-(:Organisation {id: $org})
RETURN type(r) AS rel, s.title AS title ORDER BY s.position LIMIT $limit
"""


def place(web: Web, volunteer: str, org: str = CS) -> list[tuple[str, str]]:
    rows = web.rt.graph.read(PLACE, {"volunteer": volunteer, "org": org})
    return [(r["rel"], r["title"]) for r in rows]


def test_the_demo_graph_holds_a_workflow_and_a_volunteer_part_way_through(web: Web):
    flow = web.rt.graph.read(FLOW, {"org": IL})
    assert [(s["title"], s["next"], s["completed_by"]) for s in flow] == [
        ("Read our code of conduct", "Welcome call", "volunteer"),
        ("Welcome call", None, "org_admin"),
    ]
    assert all(s["enabled"] is True and s["sends_email"] is True for s in flow)
    assert place(web, USERS["bryn"], IL) == [
        ("COMPLETED_STEP", "Read our code of conduct"), ("AT_STEP", "Welcome call"),
    ]  # fmt: skip
    assert web.rt.graph.read(FLOW, {"org": CS})[0]["enabled"] is False  # held, switched off
    assert emails(web) == []  # seeding the demo sends nobody an email
    # no template text, address or name reaches the graph
    steps = web.rt.graph.read(
        "MATCH (s:OnboardingStep) RETURN properties(s) AS props ORDER BY s.id LIMIT $limit"
    )
    assert {key for s in steps for key in s["props"]} == {
        "id", "title", "description", "position", "completed_by", "sends_email",
    }  # fmt: skip


def test_edits_and_progress_reach_the_graph_with_the_request_that_made_them(web: Web):
    kofi = as_(web, "kofi")
    flow = switch_on(kofi)
    first, second = flow["steps"]
    assert web.rt.graph.read(FLOW, {"org": CS})[0]["enabled"] is True
    flow = kofi.post(f"{BASE}/steps", {"title": "Safeguarding", "completed_by": "volunteer",
                                       "version": flow["version"]}).json()  # fmt: skip
    added = flow["steps"][-1]["id"]
    flow = kofi.post(f"{BASE}/steps/{added}/move",
                     {"direction": "up", "version": flow["version"]}).json()  # fmt: skip
    assert [(s["title"], s["next"], s["sends_email"]) for s in web.rt.graph.read(FLOW, {"org": CS})
            ] == [
        ("Read our code of conduct", "Safeguarding", True),
        ("Safeguarding", "Welcome call", False),
        ("Welcome call", None, True),
    ]  # fmt: skip
    ffion = sign_up(web)
    ffion_id = ffion.get("/me").json()["user"]["id"]
    ffion.put(f"/orgs/{CS}/membership")
    assert place(web, ffion_id) == [("AT_STEP", "Read our code of conduct")]
    ffion.post(f"{BASE}/steps/{first['id']}/complete")
    assert place(web, ffion_id) == [
        ("COMPLETED_STEP", "Read our code of conduct"), ("AT_STEP", "Safeguarding"),
    ]  # fmt: skip
    # deleting the step she is on removes it from the graph and moves her on
    kofi.delete(f"{BASE}/steps/{added}?version={flow['version']}")
    assert [s["title"] for s in web.rt.graph.read(FLOW, {"org": CS})] == [
        "Read our code of conduct", "Welcome call",
    ]  # fmt: skip
    assert place(web, ffion_id)[-1] == ("AT_STEP", "Welcome call")
    kofi.post(f"{BASE}/volunteers/{ffion_id}/steps/{second['id']}/complete")
    assert [rel for rel, _ in place(web, ffion_id)] == ["COMPLETED_STEP", "COMPLETED_STEP"]
    assert web.rt.projector.status().pending == 0


def test_leaving_takes_an_unfinished_run_out_of_the_graph(web: Web):
    switch_on(as_(web, "kofi"))
    dev = as_(web, "dev")
    dev.put(f"/orgs/{CS}/membership")
    assert place(web, USERS["dev"]) == [("AT_STEP", "Read our code of conduct")]
    dev.delete(f"/orgs/{CS}/membership")
    assert place(web, USERS["dev"]) == []


# ------------------------------------------------------------------ the onboarding assistant
ADVICE = {"summary": "Here is where your onboarding stands.", "skills": [], "causes": [],
          "organisations": [], "missing": [], "ready_for_vetting": False}  # fmt: skip


def assistant(web: Web, who: Client, *steps) -> tuple[dict, Scripted]:
    model = Scripted(*steps)
    web.rt.agent_model = model
    r = who.post("/agents/volunteer-onboarding", {"message": "Where am I with onboarding?"})
    assert r.status_code == 200, r.text
    return r.json(), model


def test_the_assistant_reads_the_workflow_from_the_graph(web: Web):
    bryn = as_(web, "bryn")
    il_steps = {s["title"]: s["id"] for s in workflow(as_(web, "sam"), IL)["steps"]}
    call_step = il_steps["Welcome call"]
    run, model = assistant(
        web, bryn, call("my_onboarding"), call("list_organisations"),
        final(**ADVICE, next_steps=[{"org_id": IL, "step_id": call_step,
                                     "advice": "InterLinkRCT will confirm your welcome call."}]),
    )  # fmt: skip
    assert {"my_onboarding", "complete_onboarding_step"} <= set(model.tools_offered)
    assert run["status"] == "completed" and run["output"]["next_steps"][0]["step_id"] == call_step
    mine, orgs = model.tool_results()[:2]
    assert "organisation='InterLinkRCT'" in mine and "finished=False" in mine
    assert "title='Read our code of conduct'" in mine and "state='done'" in mine
    assert "title='Welcome call'" in mine and "state='current'" in mine
    assert "onboarding_steps=['Read our code of conduct', 'Welcome call']" in orgs  # InterLinkRCT
    assert orgs.count("onboarding_steps=[]") == 2  # the workflows that are switched off
    # what it read is the graph: remove the edges there and the assistant sees no onboarding
    with web.rt.graph.write() as tx:
        tx.run("MATCH (:Volunteer)-[r:AT_STEP|COMPLETED_STEP]->() DELETE r")
    _run, model = assistant(web, bryn, call("my_onboarding"), final(**ADVICE))
    assert model.tool_results()[0] == "[]"


def test_the_assistant_cannot_skip_ahead_or_confirm_the_organisations_step(web: Web):
    bryn = as_(web, "bryn")
    steps = {s["title"]: s["id"] for s in workflow(as_(web, "sam"), IL)["steps"]}
    done, waiting_on_org = steps["Read our code of conduct"], steps["Welcome call"]
    run, model = assistant(
        web, bryn,
        call("complete_onboarding_step", org_id=IL, step_id=waiting_on_org),
        call("complete_onboarding_step", org_id=IL, step_id=done),
        final(**ADVICE, next_steps=[{"org_id": IL, "step_id": done, "advice": "Read it again."}]),
        final(**ADVICE),
    )  # fmt: skip
    assert run["status"] == "completed" and run["pending"] == []  # nothing reached approval
    said = model.tool_results()
    assert "confirmed by the organisation, not by the volunteer" in said[0]
    assert "not the volunteer's current step" in said[1]  # already done: no going back
    assert "Not the volunteer's current step" in said[2]  # the validator, on the advice
    _run, model = assistant(
        web, bryn, call("complete_onboarding_step", org_id=CS, step_id="step_invented"),
        final(**ADVICE),
    )  # fmt: skip
    assert "not the volunteer's current step" in model.tool_results()[0]
    [mine] = bryn.get("/me/onboarding").json()
    assert (mine["state"], mine["steps_done"]) == ("in_progress", 1)


def test_the_assistant_ticks_off_the_volunteers_own_step_after_approval(web: Web):
    flow = switch_on(as_(web, "kofi"))
    first = flow["steps"][0]["id"]
    dev = as_(web, "dev")
    dev.put(f"/orgs/{CS}/membership")
    run, model = assistant(
        web, dev, call("my_onboarding"),
        call("complete_onboarding_step", org_id=CS, step_id=first), final(**ADVICE),
    )  # fmt: skip
    assert run["status"] == "awaiting_approval"
    assert run["pending"][0]["summary"] == "Mark as done: Read our code of conduct"
    assert dev.get("/me/onboarding").json()[0]["steps_done"] == 0  # nothing done yet
    done = dev.post(f"/agents/runs/{run['run_id']}/approval", {"approve": True}).json()
    assert done["status"] == "completed"
    assert "Done. 1 of 2 steps are finished." in model.tool_results()
    assert dev.get("/me/onboarding").json()[0]["steps_done"] == 1
    assert place(web, USERS["dev"]) == [
        ("COMPLETED_STEP", "Read our code of conduct"), ("AT_STEP", "Welcome call"),
    ]  # fmt: skip
