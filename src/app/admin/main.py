"""Streamlit admin console: a client of the JSON API only (no direct database or graph access).

Staff sign in with their own account. What they see follows their roles, enforced again by the
API on every call:
  * moderator / system admin: volunteer screening queue and decisions
  * org admin (or system admin): the organisation's suggestions, volunteers, candidates and teams
    (assign a project's lead; accept, suspend, reinstate or remove team members), its own
    onboarding workflow (steps and email templates) and each project's team onboarding
  * system admin: users and roles, new organisations, projection health and the Relate graph

One function per tab. A list shows its rows from one call; the detail of a row is fetched only
for the row that is selected, never for every row on every rerun.
"""

import httpx
import pandas as pd
import streamlit as st

from app.config import BRAND_DIR, get_settings
from app.meta import get_meta

API = get_settings().api_url.rstrip("/") + "/api/v1"

VETTING_STATUSES = ["screening", "info_requested", "active", "suspended", "declined", "draft"]
VETTING_ACTIONS = {  # what a moderator may do from each status (the API checks again)
    "screening": ["approve", "request_info", "decline"],
    "info_requested": ["approve", "decline"],
    "active": ["suspend"],
    "suspended": ["reinstate"],
}
REVIEW_ACTIONS = {
    "approved": "Approve and activate",
    "changes_requested": "Request scoping detail",
    "rejected": "Reject",
}


class ApiError(RuntimeError):
    pass


def client() -> httpx.Client:
    """One HTTP client per signed-in session, kept across reruns, so a click does not open a
    new connection for every call it makes."""
    if "client" not in st.session_state:
        st.session_state["client"] = httpx.Client(timeout=10)
    return st.session_state["client"]


def call(method: str, path: str, **kwargs) -> object:
    headers = {"Authorization": f"Bearer {st.session_state['token']}"}
    try:
        r = client().request(method, API + path, headers=headers, **kwargs)
    except httpx.HTTPError as error:
        raise ApiError(f"API unreachable: {error}") from error
    if r.status_code == 401:
        st.session_state.clear()
        st.rerun()
    if r.status_code >= 400:
        try:  # a proxy's error page is not JSON
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = f"The API answered {r.status_code}."
        raise ApiError(str(detail) if r.content else str(r.status_code))
    return r.json() if r.content else None


def act(label: str, method: str, path: str, **kwargs) -> None:
    """Run an action and report the outcome, then refresh."""
    try:
        call(method, path, **kwargs)
    except ApiError as error:
        st.session_state["flash"] = ("error", f"{label}: {error}")
    else:
        st.session_state["flash"] = ("success", f"{label}: done")
    st.rerun()


def tabs_for(me: dict) -> list[str]:
    """The sections a signed-in member of staff may see, from their roles."""
    roles = set(me["system_roles"])
    tabs = []
    if roles & {"admin", "moderator"}:
        tabs.append("Volunteer screening")
    if any(o["org_admin"] for o in me["organisations"]):
        tabs += ["Organisations", "Onboarding"]
    if "admin" in roles:
        tabs += ["Users and roles", "Audit", "System"]
    return tabs


# ------------------------------------------------------------------ sign in
def sign_in() -> None:
    with st.form("sign-in"):
        st.subheader("Staff sign in")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if not submitted:
        return
    try:
        r = client().request("POST", f"{API}/session", json={"email": email, "password": password})
    except httpx.HTTPError as error:
        st.error(f"API unreachable: {error}")
        return
    if r.status_code != 200:
        st.error("Wrong email or password")
        return
    st.session_state["token"] = r.json()["token"]
    st.rerun()


# ------------------------------------------------------------------ moderation
def screening_tab() -> None:
    status = st.selectbox("Status", VETTING_STATUSES, key="vet-status")
    queue = call("GET", "/moderation/volunteers", params={"status": status})
    if not queue:
        st.caption("Nobody in this status.")
        return
    person = st.selectbox(
        "Volunteer",
        queue,
        format_func=lambda p: f"{p['display_name']} · offers {p['skills_offered']}",
        key="vet-person",
    )
    rec = call("GET", f"/moderation/volunteers/{person['id']}")
    st.write({k: rec[k] for k in ("status", "created_at")})
    if "email" in rec:
        st.caption(f"Email (admins only): {rec['email']}")
    st.write("Skills:", rec["skills"] or "none")
    st.write("Causes:", rec["causes"] or "none")
    st.write("Organisations:", ", ".join(rec["organisations"]) or "none")
    if rec["history"]:
        st.dataframe(pd.DataFrame(rec["history"]), hide_index=True)
    actions = VETTING_ACTIONS.get(rec["status"], [])
    if not actions:
        return
    with st.form(f"vet-{person['id']}"):
        action = st.radio("Decision", actions, horizontal=True)
        note = st.text_input("Message to the volunteer")
        private = st.text_input("Private note (moderators only)")
        if st.form_submit_button("Record decision"):
            act(
                "Vetting",
                "POST",
                f"/moderation/volunteers/{person['id']}/decision",
                json={
                    "action": action,
                    "note": note,
                    "private_note": private,
                    "version": rec["status_version"],
                },
            )


# ------------------------------------------------------------------ org admin
def _suggestion(org: dict, sub: dict) -> None:
    detail = call("GET", f"/submissions/{sub['id']}")
    st.write(detail["current"]["summary"])
    st.write("Required:", ", ".join(detail["current"]["required"]))
    if detail["can_review"]:
        with st.form(f"review-{sub['id']}"):
            action = st.radio(
                "Decision", list(REVIEW_ACTIONS), format_func=REVIEW_ACTIONS.get, horizontal=True
            )
            reason = st.text_input("Reason (shown to the author)")
            if st.form_submit_button("Record decision"):
                act(
                    "Review",
                    "POST",
                    f"/orgs/{org['id']}/submissions/{sub['id']}/decision",
                    json={
                        "action": action,
                        "reason": reason,
                        "revision": detail["current_revision"],
                        "version": detail["version"],
                    },
                )
    if sub["state"] == "published":
        _lead(org, sub, detail)
        _candidates(sub["id"])


def _lead(org: dict, sub: dict, detail: dict) -> None:
    """Org admins assign the project's lead: an active volunteer or admin of the organisation."""
    people = [p for p in call("GET", f"/orgs/{org['id']}/volunteers") if p["status"] == "active"]
    if not people:
        return
    at = next((i for i, p in enumerate(people) if p["id"] == detail["lead_id"]), 0)
    with st.form(f"lead-{sub['id']}"):
        lead = st.selectbox(
            "Project lead", people, index=at, format_func=lambda p: p["display_name"],
            key=f"lead-pick-{sub['id']}",
        )  # fmt: skip
        if st.form_submit_button("Assign lead"):
            act("Project lead", "PUT", f"/orgs/{org['id']}/projects/{sub['id']}/lead",
                json={"user_id": lead["id"]})  # fmt: skip


def _candidates(project_id: str) -> None:
    st.markdown("**Candidates and team**")
    for p in call("GET", f"/projects/{project_id}/placements"):
        cols = st.columns([3, 3, 2, 1, 1])
        cols[0].write(p["display_name"])
        cols[1].write(", ".join(p["matched_skills"]))
        cols[2].write(p["state_label"])
        if not p["can_decide"]:
            continue
        base = f"/projects/{project_id}/placements/{p['volunteer_id']}"
        key = f"{project_id}-{p['volunteer_id']}"
        if p["status"] == "candidate":  # listed in ranked order: invite or pass over
            for col, answer, label in ((cols[3], "yes", "Invite"), (cols[4], "no", "Pass")):
                if col.button(label, key=f"{key}-{answer}"):
                    act("Candidate", "POST", f"{base}/answer",
                        json={"answer": answer, "version": p["version"]})  # fmt: skip
        elif p["status"] == "invited":
            if cols[3].button("Withdraw", key=f"{key}-withdraw"):
                act("Invitation", "POST", f"{base}/answer",
                    json={"answer": "no", "version": p["version"]})  # fmt: skip
        elif p["state"] in ("declined", "left", "removed"):
            if cols[3].button("Reopen", key=f"{key}-reopen"):
                act("Placement", "POST", f"{base}/reopen")
        elif p["state"] in ("onboarding", "team", "suspended"):
            _team_member(p, base, key, cols)


def _team_member(p: dict, base: str, key: str, cols: list) -> None:
    """Someone with a place on the team, or on the way to one: accept them once the project's
    onboarding steps are done, suspend or reinstate them, or remove them."""
    if p["state"] == "onboarding":
        if p["steps_left"]:
            cols[3].caption(f"{p['steps_left']} steps left")
        elif cols[3].button("Accept", key=f"{key}-accept"):
            act("Team", "POST", f"{base}/accept")
    elif p["state"] == "suspended" and cols[3].button("Reinstate", key=f"{key}-reinstate"):
        act("Team", "POST", f"{base}/reinstate")
    with cols[4].popover("More"):
        reason = st.text_input("Reason (shown to the volunteer)", key=f"{key}-reason")
        if p["state"] == "team" and not p["is_lead"] and st.button("Suspend", key=f"{key}-suspend"):
            act("Team", "POST", f"{base}/suspend", json={"reason": reason})
        if st.button("Remove", key=f"{key}-remove"):
            act("Team", "POST", f"{base}/remove", json={"reason": reason})


def _volunteers(org: dict) -> None:
    st.subheader("Volunteers")
    people = call("GET", f"/orgs/{org['id']}/volunteers")
    st.dataframe(pd.DataFrame(people), hide_index=True, width="stretch")
    if not people:
        return
    with st.form("remove-volunteer"):
        who = st.selectbox("Remove a volunteer", people, format_func=lambda p: p["display_name"])
        reason = st.text_input("Reason (shown to the volunteer)")
        if st.form_submit_button("Remove from organisation"):
            act(
                "Remove volunteer",
                "POST",
                f"/orgs/{org['id']}/volunteers/{who['id']}/remove",
                json={"reason": reason},
            )


def organisations_tab(me: dict) -> None:
    admin_orgs = [o for o in me["organisations"] if o["org_admin"]]
    org = st.selectbox("Organisation", admin_orgs, format_func=lambda o: o["name"], key="org-pick")
    st.subheader("Suggestions")
    queue = call("GET", f"/orgs/{org['id']}/submissions")
    if queue:
        sub = st.selectbox(
            "Suggestion",
            queue,
            format_func=lambda s: f"{s['title']} · {s['state']} · by {s['author_name']}",
            key=f"suggestion-{org['id']}",
        )
        _suggestion(org, sub)
    else:
        st.caption("No suggestions yet.")
    _volunteers(org)
    cov = call("GET", f"/orgs/{org['id']}/coverage")
    st.subheader("Coverage gaps")
    st.write({p["title"]: p["gaps"] or "none" for p in cov["projects"]})


# ------------------------------------------------------------------ org admin: onboarding
COMPLETED_BY = {
    "volunteer": "The volunteer",
    "org_admin": "An organisation admin",
    "lead": "The project lead or an org admin",
}
EMAIL_STATUS = {
    "queued": "Queued",
    "sent": "Sent",
    "failed": "Failed",
    "not_configured": "Not sent: no mail server is configured",
}
NO_EMAIL = {"id": None, "name": "(no email)"}
NEW = {"id": None}


def _template_choice(label: str, templates: list[dict], current: str | None, key: str) -> dict:
    options = [NO_EMAIL, *templates]
    at = next((i for i, t in enumerate(options) if t["id"] == current), 0)
    return st.selectbox(label, options, index=at, format_func=lambda t: t["name"], key=key)


def _workflow_settings(base: str, flow: dict) -> None:
    with st.form("onboarding-settings"):
        enabled = st.checkbox(
            "Switched on: volunteers who join from now on go through these steps",
            value=flow["enabled"],
            key=f"onboarding-enabled-{flow['version']}",
        )
        done = _template_choice(
            "Email when a volunteer finishes", flow["templates"],
            flow["completion_template_id"], f"onboarding-completion-{flow['version']}",
        )  # fmt: skip
        if st.form_submit_button("Save settings"):
            body = {"enabled": enabled, "completion_template_id": done["id"],
                    "version": flow["version"]}  # fmt: skip
            act("Workflow", "PUT", base, json=body)


def _workflow_steps(base: str, flow: dict) -> None:
    st.markdown("**Steps**, in the order a volunteer goes through them")
    names = {t["id"]: t["name"] for t in flow["templates"]}
    st.dataframe(
        pd.DataFrame(
            [
                {"step": s["position"], "title": s["title"],
                 "completed by": COMPLETED_BY[s["completed_by"]],
                 "email": names.get(s["email_template_id"], "")}
                for s in flow["steps"]
            ]
        ),
        hide_index=True, width="stretch",
    )  # fmt: skip
    step = st.selectbox(
        "Step", [NEW, *flow["steps"]],
        format_func=lambda s: f"{s['position']}. {s['title']}" if s["id"] else "New step",
        key="onboarding-step",
    )  # fmt: skip
    version = flow["version"]
    # Widget keys carry the version: after a saved edit the form starts afresh from what was
    # saved, and after a refused one it keeps what was typed.
    key = f"{step['id']}-{version}"
    with st.form(f"onboarding-step-{step['id']}"):
        title = st.text_input("Title", value=step.get("title", ""), key=f"step-title-{key}")
        description = st.text_area("What the volunteer is asked to do",
                                   value=step.get("description", ""),
                                   key=f"step-description-{key}")  # fmt: skip
        # The workflow says who may complete its steps: an organisation's differs from a project's.
        who_options = [c["value"] for c in flow["completers"]]
        who = st.radio(
            "Completed by", who_options, format_func=COMPLETED_BY.get, horizontal=True,
            index=who_options.index(step.get("completed_by", "volunteer")),
            key=f"step-who-{key}",
        )  # fmt: skip
        email = _template_choice(
            "Email sent when the volunteer reaches this step", flow["templates"],
            step.get("email_template_id"), f"step-email-{key}",
        )  # fmt: skip
        if st.form_submit_button("Save step" if step["id"] else "Add step"):
            body = {"title": title, "description": description, "completed_by": who,
                    "email_template_id": email["id"], "version": version}  # fmt: skip
            if step["id"]:
                act("Step", "PUT", f"{base}/steps/{step['id']}", json=body)
            else:
                act("Step", "POST", f"{base}/steps", json=body)
    if not step["id"]:
        return
    cols = st.columns(3)
    for col, label, direction in ((cols[0], "Move up", "up"), (cols[1], "Move down", "down")):
        if col.button(label, key=f"onboarding-{direction}"):
            act("Step", "POST", f"{base}/steps/{step['id']}/move",
                  json={"direction": direction, "version": version})  # fmt: skip
    if cols[2].button("Delete step", key="onboarding-delete-step"):
        act("Step", "DELETE", f"{base}/steps/{step['id']}", params={"version": version})


def _workflow_templates(base: str, flow: dict) -> None:
    st.markdown("**Email templates**")
    st.caption(
        "Plain text. Placeholders: "
        + "; ".join(f"{{{{ {p['name']} }}}}: {p['meaning']}" for p in flow["placeholders"])
    )
    template = st.selectbox(
        "Template", [NEW, *flow["templates"]],
        format_func=lambda t: t["name"] if t["id"] else "New template",
        key="onboarding-template",
    )  # fmt: skip
    with st.form(f"onboarding-template-{template['id']}"):
        key = f"{template['id']}-{flow['version']}"  # as for the steps
        name = st.text_input("Name", value=template.get("name", ""), key=f"tpl-name-{key}")
        subject = st.text_input(
            "Subject", value=template.get("subject", ""), key=f"tpl-subject-{key}"
        )
        body = st.text_area(
            "Text", value=template.get("body", ""), height=260, key=f"tpl-body-{key}"
        )
        save = st.form_submit_button("Save template" if template["id"] else "Add template")
        preview = st.form_submit_button("Preview")
    if save:
        payload = {"name": name, "subject": subject, "body": body, "version": flow["version"]}
        if template["id"]:
            act("Template", "PUT", f"{base}/templates/{template['id']}", json=payload)
        else:
            act("Template", "POST", f"{base}/templates", json=payload)
    if preview:
        shown = call("POST", f"{base}/templates/preview", json={"subject": subject, "body": body})
        st.markdown(f"**Subject:** {shown['subject']}")
        st.code(shown["body"], language=None)
    if not template["id"]:
        return
    cols = st.columns(2)
    if cols[0].button("Send a test to me", key="onboarding-test"):
        act("Test email", "POST", f"{base}/templates/{template['id']}/test")
    if cols[1].button("Delete template", key="onboarding-delete-template"):
        act("Template", "DELETE", f"{base}/templates/{template['id']}",
              params={"version": flow["version"]})  # fmt: skip


def _onboarding_progress(base: str) -> None:
    people = call("GET", f"{base}/volunteers")
    if not people:
        st.caption("Nobody has started onboarding yet. It starts when a volunteer joins.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {"volunteer": p["display_name"], "status": p["state_label"],
                 "steps done": f"{p['steps_done']} of {p['steps_total']}",
                 "current step": p["current_step"] or "",
                 "waiting for": COMPLETED_BY.get(p["waiting_for"], ""),
                 "started": p["started_at"]}
                for p in people
            ]
        ),
        hide_index=True, width="stretch",
    )  # fmt: skip
    waiting = [p for p in people if p["current_step_id"]]
    if not waiting:
        return
    with st.form("onboarding-complete"):
        who = st.selectbox(
            "Confirm a volunteer's current step", waiting,
            format_func=lambda p: f"{p['display_name']}: {p['current_step']}",
        )  # fmt: skip
        if st.form_submit_button("Mark step as done"):
            step = f"{who['user_id']}/steps/{who['current_step_id']}"
            act("Onboarding step", "POST", f"{base}/volunteers/{step}/complete")


def _onboarding_emails(base: str) -> None:
    emails = call("GET", f"{base}/emails")
    if not emails:
        st.caption("No emails yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {"when": e["created_at"], "to": e["recipient"], "template": e["template_name"],
                 "subject": e["subject"], "status": EMAIL_STATUS[e["status"]],
                 "sent": e["sent_at"] or ""}
                for e in emails
            ]
        ),
        hide_index=True, width="stretch",
    )  # fmt: skip


ONBOARDING_VIEWS = ["Workflow", "Volunteers", "Emails"]


WHOLE_ORG = {"id": None}
ONBOARDING_SCOPES = ["Volunteers joining the organisation", "Volunteers joining a project's team"]


def onboarding_tab(me: dict) -> None:
    """An organisation's own onboarding: its steps, its emails, and who is where. And the same
    for each of its active projects: the steps a volunteer takes before joining the team."""
    admin_orgs = [o for o in me["organisations"] if o["org_admin"]]
    org = st.selectbox(
        "Organisation", admin_orgs, format_func=lambda o: o["name"], key="onboarding-org"
    )
    project = WHOLE_ORG
    # The organisation's projects are fetched only when a project's workflow is asked for.
    if (
        st.radio("Workflow for", ONBOARDING_SCOPES, horizontal=True, key="onboarding-scope")
        != (ONBOARDING_SCOPES[0])
    ):
        queue = call("GET", f"/orgs/{org['id']}/submissions")
        live = [s for s in queue if s["state"] == "published"]
        if not live:
            st.caption("This organisation has no active projects yet.")
            return
        project = st.selectbox(
            "Project", live, format_func=lambda s: s["title"],
            key=f"onboarding-project-{org['id']}",
        )  # fmt: skip
    base = f"/orgs/{org['id']}/onboarding"
    if project["id"]:
        base = f"/orgs/{org['id']}/projects/{project['id']}/onboarding"
        st.caption(
            "A volunteer goes through these steps after accepting an invitation; the project "
            "lead or an org admin then accepts them onto the team. The organisation's own "
            "onboarding comes first, and the emails are the organisation's templates."
        )
    # One view at a time, for the same reason as the sections: only its calls are made.
    views = ONBOARDING_VIEWS[:2] if project["id"] else ONBOARDING_VIEWS
    view = st.radio("Show", views, horizontal=True, key="onboarding-view")
    if view == "Volunteers":
        _onboarding_progress(base)
    elif view == "Emails":
        _onboarding_emails(base)
    else:
        flow = call("GET", base)
        _workflow_settings(base, flow)
        _workflow_steps(base, flow)
        if not project["id"]:  # templates belong to the organisation
            _workflow_templates(base, flow)


# ------------------------------------------------------------------ system admin
def users_tab(me: dict) -> None:
    users = call("GET", "/admin/users")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    **u,
                    "system_roles": ", ".join(u["system_roles"]),
                    "org_roles": ", ".join(f"{r['org']}: {r['role']}" for r in u["org_roles"]),
                }
                for u in users
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    orgs = me["organisations"]
    # Each choice carries its path, so nothing is parsed back out of a display label.
    choices = [
        (f"system: {role}", f"/system-roles/{role}", None) for role in ("admin", "moderator")
    ]
    choices += [
        (f"{o['name']}: {role}", f"/{role}", o["id"])
        for o in orgs
        for role in ("org_admin", "reviewer", "team_coordinator")
    ]
    with st.form("roles"):
        who = st.selectbox("User", users, format_func=lambda u: u["display_name"])
        _label, suffix, org_id = st.selectbox("Role", choices, format_func=lambda c: c[0])
        grant = st.radio("Change", ["grant", "revoke"], horizontal=True)
        if st.form_submit_button("Apply"):
            path = (
                f"/admin/orgs/{org_id}/roles/{who['id']}{suffix}"
                if org_id
                else f"/admin/users/{who['id']}{suffix}"
            )
            act("Role", "PUT" if grant == "grant" else "DELETE", path)
    with st.form("new-org"):
        st.markdown("**New organisation**")
        key = st.text_input("Key (lower-case, hyphens)")
        org_name = st.text_input("Name")
        description = st.text_input("Description")
        if st.form_submit_button("Create"):
            body = {"key": key, "name": org_name, "description": description}
            act("Organisation", "POST", "/admin/orgs", json=body)


def system_tab() -> None:
    status = call("GET", "/admin/projection")
    c1, c2, c3 = st.columns(3)
    c1.metric("Pending graph updates", status["pending"])
    c2.metric("Failing (3+ attempts)", status["failed"])
    if c3.button("Replay outbox now"):
        act("Replay", "POST", "/admin/projection/replay")
    if status["items"]:
        st.dataframe(pd.DataFrame(status["items"]), width="stretch")
    graph = call("GET", "/admin/graph")
    g1, g2 = st.columns(2)
    g1.dataframe(pd.DataFrame(graph["labels"]), hide_index=True, width="stretch")
    g2.dataframe(pd.DataFrame(graph["relationships"]), hide_index=True, width="stretch")
    st.subheader("Organisation → project → cause / skill → eligible volunteers")
    st.dataframe(
        pd.DataFrame(
            [
                {**r, "eligible_volunteers": ", ".join(r["eligible_volunteers"]) or "coverage gap"}
                for r in graph["inspect"]
            ]
        ),
        hide_index=True,
        width="stretch",
    )


AUDIT_FILTERS = {
    "Role changes": "role.",
    "Refusals": "",
    "Sign-ins": "auth.",
    "Assistants": "agent.",
    "Requests": "http.",
    "Everything": "",
}


def audit_tab() -> None:
    """Who can do what, how the application is used, and what the checks found."""
    days = st.selectbox("Period (days)", [7, 1, 30, 90])
    usage = call("GET", "/admin/audit/usage", params={"days": days})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Allowed", usage["outcomes"]["ok"])
    c2.metric("Refused", usage["outcomes"]["denied"])
    c3.metric("Invalid", usage["outcomes"]["invalid"])
    c4.metric("Errors", usage["outcomes"]["error"])

    st.subheader("Checks")
    checks = call("GET", "/admin/audit/checks", params={"days": days})
    if checks:
        st.dataframe(
            pd.DataFrame([{k: c[k] for k in ("severity", "title", "evidence")} for c in checks]),
            hide_index=True, width="stretch",
        )  # fmt: skip
    else:
        st.success("No findings.")
    if st.button("Ask the audit assistant", key="audit-assistant"):
        run = call("POST", "/agents/audit", json={"days": days})
        st.session_state["audit_report"] = run["output"]
    if report := st.session_state.get("audit_report"):
        st.write(f"**{report['overall'].replace('_', ' ').title()}.** {report['summary']}")
        for item in report["findings"]:
            st.markdown(
                f"- **{item['severity']}: {item['title']}.** {item['explanation']} "
                f"*{item['recommendation']}*"
            )
        for note in report["usage_notes"]:
            st.caption(note)

    st.subheader("Who holds which role")
    roles = call("GET", "/admin/audit/rbac")
    holders = [
        {"scope": "system", "name": r["display_name"], "role": r["role"],
         "account": r["status"], "granted": r["granted_at"]}
        for r in roles["system_roles"]
    ] + [
        {"scope": o["name"], "name": r["display_name"], "role": r["role"],
         "account": r["status"], "granted": r["granted_at"]}
        for o in roles["organisations"] for r in o["roles"]
    ]  # fmt: skip
    st.dataframe(pd.DataFrame(holders), hide_index=True, width="stretch")

    if usage["agents"]:
        st.subheader("Assistants")
        st.dataframe(pd.DataFrame(usage["agents"]), hide_index=True, width="stretch")
    if usage["most_refused"]:
        st.subheader("Refused most often")
        st.dataframe(
            pd.DataFrame([{"name": r["display_name"], "refusals": r["refusals"]}
                          for r in usage["most_refused"]]),
            hide_index=True, width="stretch",
        )  # fmt: skip

    st.subheader("Audit trail")
    shown = st.selectbox("Show", list(AUDIT_FILTERS))
    params = {"action": AUDIT_FILTERS[shown], "limit": 200}
    if shown == "Refusals":
        params["outcome"] = "denied"
    events = call("GET", "/admin/audit/events", params=params)
    st.dataframe(
        pd.DataFrame(
            [
                {"when": e["created_at"], "who": e["actor"] or "(not signed in)",
                 "action": e["action"], "outcome": e["outcome"],
                 "what": f"{e['target_kind']} {e['target_id']}".strip(),
                 "organisation": e["org"] or "", "detail": str(e["detail"])}
                for e in events
            ]
        ),
        hide_index=True, width="stretch",
    )  # fmt: skip


# ------------------------------------------------------------------ page
def main() -> None:
    # The colours and fonts are the brand's too: app.serve passes them to `streamlit run`.
    brand = get_meta()
    images = BRAND_DIR / "images"
    st.set_page_config(
        page_title=f"{brand.name}: admin",
        page_icon=str(images / brand.images["favicon"]),
        layout="wide",
    )
    st.logo(str(images / brand.images["mark"]), size="large")
    st.title(f"{brand.name}: admin")
    st.caption(brand.tagline)
    if "token" not in st.session_state:
        sign_in()
        return
    try:
        me = call("GET", "/me")
    except ApiError as error:
        st.error(str(error))
        return
    roles = sorted(me["system_roles"])
    roles += [f"org admin: {o['name']}" for o in me["organisations"] if o["org_admin"]]
    roles += [
        f"team coordinator: {o['name']}"
        for o in me["organisations"]
        if o["team_coordinator"] and not o["org_admin"]
    ]
    st.sidebar.write(f"Signed in as **{me['user']['display_name']}**")
    st.sidebar.caption(", ".join(roles) or "no staff role")
    if st.sidebar.button("Sign out"):
        try:
            call("DELETE", "/session")
        finally:
            st.session_state.clear()
            st.rerun()
    if flash := st.session_state.pop("flash", None):
        getattr(st, flash[0])(flash[1])
    names = tabs_for(me)
    if not names:
        st.warning("This console is for moderators, org admins and system admins.")
        return
    render = {
        "Volunteer screening": screening_tab,
        "Organisations": lambda: organisations_tab(me),
        "Onboarding": lambda: onboarding_tab(me),
        "Users and roles": lambda: users_tab(me),
        "Audit": audit_tab,
        "System": system_tab,
    }
    # One section at a time: st.tabs would run every section's API calls on every click.
    name = st.sidebar.radio("Section", names)
    st.subheader(name)
    try:
        render[name]()
    except ApiError as error:
        st.error(str(error))


if __name__ == "__main__":  # `streamlit run` executes this file as __main__
    main()
