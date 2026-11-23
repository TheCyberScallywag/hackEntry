"""System and organisation roles. Only a system admin grants or removes them."""

from collections import defaultdict

from app import audit
from app.notify import notify
from app.services.access import get_org, load_actor, require_admin
from app.services.errors import Forbidden, Invalid, NotFound
from app.sqlstore import Database, now


def _require_user(conn, user_id: str) -> None:
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise NotFound("user")


def _require_admin(conn, actor_id: str) -> None:
    """Checked inside the write transaction, so a role removed a moment ago cannot grant one."""
    if not load_actor(conn, actor_id).admin:
        raise Forbidden("admin")


def set_system_role(db: Database, actor_id: str, user_id: str, role: str, *, granted: bool) -> None:
    if role not in ("admin", "moderator"):
        raise Invalid("Unknown system role.")
    if not granted and role == "admin" and actor_id == user_id:
        raise Invalid("You cannot remove your own admin role.")
    with db.tx() as conn:
        _require_admin(conn, actor_id)
        _require_user(conn, user_id)
        if granted:
            changed = conn.execute(
                "INSERT OR IGNORE INTO system_roles (user_id, role, granted_at, granted_by) "
                "VALUES (?, ?, ?, ?)",
                (user_id, role, now(), actor_id),
            ).rowcount
        else:
            changed = conn.execute(
                "DELETE FROM system_roles WHERE user_id = ? AND role = ?", (user_id, role)
            ).rowcount
        if not changed:
            return  # already so: nothing to tell anyone, nothing to audit
        verb = "given" if granted else "removed from"
        notify(conn, user_id, "role", f"You were {verb} the system {role} role.")
        audit.record(
            conn, "role.grant" if granted else "role.revoke", actor_id=actor_id,
            target_kind="user", target_id=user_id, detail={"role": role, "scope": "system"},
        )  # fmt: skip


def set_org_role(
    db: Database, actor_id: str, org_id: str, user_id: str, role: str, *, granted: bool
) -> None:
    if role not in ("org_admin", "reviewer"):
        raise Invalid("Unknown organisation role.")
    with db.tx() as conn:
        _require_admin(conn, actor_id)
        org = get_org(conn, org_id)
        _require_user(conn, user_id)
        if granted:
            changed = conn.execute(
                "INSERT OR IGNORE INTO org_roles (user_id, org_id, role, granted_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, org_id, role, now()),
            ).rowcount
        else:
            changed = conn.execute(
                "DELETE FROM org_roles WHERE user_id = ? AND org_id = ? AND role = ?",
                (user_id, org_id, role),
            ).rowcount
        if not changed:
            return
        label = role.replace("_", " ")
        notify(conn, user_id, "role",
               f"You were {'given' if granted else 'removed from'} the {label} role for "
               f"{org['name']}.")  # fmt: skip
        audit.record(
            conn, "role.grant" if granted else "role.revoke", actor_id=actor_id,
            target_kind="user", target_id=user_id, org_id=org_id,
            detail={"role": role, "scope": "organisation"},
        )  # fmt: skip


def list_users(db: Database, actor_id: str) -> list[dict]:
    """Every account with its roles; system admins only (they may see emails)."""
    require_admin(db, actor_id)
    system_roles: dict[str, list[str]] = defaultdict(list)
    org_roles: dict[str, list[dict]] = defaultdict(list)
    with db.read() as conn:
        users = [
            dict(r)
            for r in conn.execute(
                "SELECT id, display_name, email, status, created_at FROM users "
                "ORDER BY display_name, id"
            )
        ]
        for r in conn.execute("SELECT user_id, role FROM system_roles ORDER BY role"):
            system_roles[r["user_id"]].append(r["role"])
        for r in conn.execute(
            "SELECT r.user_id, r.org_id, o.name, r.role FROM org_roles r "
            "JOIN organisations o ON o.id = r.org_id ORDER BY o.name, r.role"
        ):
            org_roles[r["user_id"]].append(
                {"org_id": r["org_id"], "org": r["name"], "role": r["role"]}
            )
    for user in users:
        user["system_roles"] = system_roles.get(user["id"], [])
        user["org_roles"] = org_roles.get(user["id"], [])
    return users
