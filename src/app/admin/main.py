"""Streamlit admin console: a client of the JSON API only (no direct database or graph access).

Staff sign in with their own account. What they see follows their roles, enforced again by the
API on every call:
  * moderator / system admin: volunteer screening queue and decisions
  * org admin (or system admin): the organisation's suggestions, volunteers, candidates and teams
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


def call(method: str, path: str, **kwargs) -> object:
    headers = {"Authorization": f"Bearer {st.session_state['token']}"}
    try:
        r = httpx.request(method, API + path, headers=headers, timeout=10, **kwargs)
    except httpx.HTTPError as error:
        raise ApiError(f"API unreachable: {error}") from error
    if r.status_code == 401:
        st.session_state.clear()
        st.rerun()
    if r.status_code >= 400:
        raise ApiError(r.json().get("detail", r.text) if r.content else str(r.status_code))
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
    """The tabs a signed-in member of staff may see, from their roles."""
    roles = set(me["system_roles"])
    tabs = []
    if roles & {"admin", "moderator"}:
        tabs.append("Volunteer screening")
    if any(o["org_admin"] for o in me["organisations"]):
        tabs.append("Organisations")
    if "admin" in roles:
        tabs += ["Users and roles", "System"]
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
        r = httpx.request(
            "POST", f"{API}/session", json={"email": email, "password": password}, timeout=10
        )
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
        _candidates(sub["id"])


def _candidates(project_id: str) -> None:
    st.markdown("**Candidates and team**")
    for p in call("GET", f"/projects/{project_id}/placements"):
        cols = st.columns([3, 3, 2, 1, 1])
        cols[0].write(p["display_name"])
        cols[1].write(", ".join(p["matched_skills"]))
        cols[2].write(p["state_label"])
        if p["state"] != "eligible" or p["lead_said"]:
            continue
        for col, answer in ((cols[3], "yes"), (cols[4], "no")):
            if col.button(answer.title(), key=f"{project_id}-{p['volunteer_id']}-{answer}"):
                act(
                    "Candidate",
                    "POST",
                    f"/projects/{project_id}/placements/{p['volunteer_id']}/answer",
                    json={"answer": answer, "version": p["version"]},
                )


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
        for role in ("org_admin", "reviewer")
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
        "Users and roles": lambda: users_tab(me),
        "System": system_tab,
    }
    for tab, name in zip(st.tabs(names), names, strict=True):
        with tab:
            try:
                render[name]()
            except ApiError as error:  # one tab's failure does not take the others down
                st.error(str(error))


if __name__ == "__main__":  # `streamlit run` executes this file as __main__
    main()
