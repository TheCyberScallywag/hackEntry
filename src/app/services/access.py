"""Who is acting, and what they may do in one organisation."""

import sqlite3
from dataclasses import dataclass

from app.services.errors import Forbidden, NotFound
from app.sqlstore import Database


@dataclass(frozen=True)
class Actor:
    """Who is acting: account status plus system-wide roles. Loaded from SQLite per request."""

    user_id: str
    status: str
    system_roles: frozenset[str]

    @property
    def admin(self) -> bool:
        return "admin" in self.system_roles

    @property
    def can_vet(self) -> bool:
        return bool(self.system_roles & {"admin", "moderator"})

    @property
    def active(self) -> bool:
        return self.status == "active"


def load_actor(conn: sqlite3.Connection, user_id: str) -> Actor:
    row = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise NotFound("user")
    roles = frozenset(
        r[0] for r in conn.execute("SELECT role FROM system_roles WHERE user_id = ?", (user_id,))
    )
    return Actor(user_id, row["status"], roles)


def get_actor(db: Database, user_id: str) -> Actor:
    with db.read() as conn:
        return load_actor(conn, user_id)


def require_active(db: Database, user_id: str) -> Actor:
    actor = get_actor(db, user_id)
    if not actor.active:
        raise Forbidden("active")
    return actor


def require_admin(db: Database, user_id: str) -> Actor:
    actor = get_actor(db, user_id)
    if not actor.admin:
        raise Forbidden("admin")
    return actor


def require_vetter(db: Database, user_id: str) -> Actor:
    actor = get_actor(db, user_id)
    if not actor.can_vet:
        raise Forbidden("moderator")
    return actor


@dataclass(frozen=True)
class Access:
    """A user's standing in one organisation. System admins hold every organisation role."""

    org_id: str
    volunteer: bool  # currently opted in
    reviewer: bool  # may review suggestions: reviewer, org_admin or system admin
    org_admin: bool = False  # manages projects and volunteers: org_admin or system admin
    system_admin: bool = False

    @property
    def any(self) -> bool:
        return self.volunteer or self.reviewer or self.org_admin

    def allows(self, role: str) -> bool:
        return {
            "any": self.any,
            "volunteer": self.volunteer,
            "reviewer": self.reviewer,
            "org_admin": self.org_admin,
        }[role]


def get_org(conn: sqlite3.Connection, org_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, key, name, description FROM organisations WHERE id = ?", (org_id,)
    ).fetchone()
    if row is None:
        raise NotFound("organisation")
    return row


_ORG_ACCESS = """
SELECT EXISTS (SELECT 1 FROM organisations WHERE id = :org) AS org_known,
       EXISTS (SELECT 1 FROM users WHERE id = :user) AS user_known,
       EXISTS (SELECT 1 FROM memberships WHERE user_id = :user AND org_id = :org
               AND opted_in = 1) AS volunteer,
       EXISTS (SELECT 1 FROM org_roles WHERE user_id = :user AND org_id = :org
               AND role = 'reviewer') AS reviewer,
       EXISTS (SELECT 1 FROM org_roles WHERE user_id = :user AND org_id = :org
               AND role = 'org_admin') AS org_admin,
       EXISTS (SELECT 1 FROM system_roles WHERE user_id = :user AND role = 'admin') AS admin
"""


def org_access(conn: sqlite3.Connection, user_id: str, org_id: str) -> Access:
    """The user's standing in one organisation, in a single query."""
    row = conn.execute(_ORG_ACCESS, {"user": user_id, "org": org_id}).fetchone()
    if not row["org_known"]:
        raise NotFound("organisation")
    if not row["user_known"]:
        raise NotFound("user")
    admin = bool(row["admin"])
    org_admin = admin or bool(row["org_admin"])
    return Access(
        org_id,
        volunteer=bool(row["volunteer"]),
        reviewer=org_admin or bool(row["reviewer"]),
        org_admin=org_admin,
        system_admin=admin,
    )


def check_access(
    conn: sqlite3.Connection, user_id: str, org_id: str, *, role: str = "any"
) -> Access:
    """The user's standing in the organisation, or Forbidden if it does not include `role`."""
    access = org_access(conn, user_id, org_id)
    if not access.allows(role):
        raise Forbidden(role)
    return access


def require_access(db: Database, user_id: str, org_id: str, *, role: str = "any") -> Access:
    with db.read() as conn:
        return check_access(conn, user_id, org_id, role=role)


def load_directory(conn: sqlite3.Connection, user_id: str, *, admin: bool) -> list[dict]:
    """Every organisation with this user's standing in it. `admin`: the user is a system admin."""
    rows = conn.execute(
        "SELECT o.id, o.key, o.name, o.description, "
        "COALESCE(m.opted_in, 0) AS opted_in, "
        "(? OR EXISTS (SELECT 1 FROM org_roles r WHERE r.user_id = ? AND r.org_id = o.id)) "
        "  AS reviewer, "
        "(? OR EXISTS (SELECT 1 FROM org_roles r WHERE r.user_id = ? AND r.org_id = o.id "
        "              AND r.role = 'org_admin')) AS org_admin "
        "FROM organisations o LEFT JOIN memberships m ON m.org_id = o.id AND m.user_id = ? "
        "ORDER BY o.name",
        (int(admin), user_id, int(admin), user_id, user_id),
    ).fetchall()
    return [dict(r) for r in rows]


def directory(db: Database, user_id: str) -> list[dict]:
    with db.read() as conn:
        return load_directory(conn, user_id, admin=load_actor(conn, user_id).admin)


def accessible(orgs: list[dict]) -> list[dict]:
    """The organisations of a directory the user volunteers with or reviews for."""
    return [o for o in orgs if o["opted_in"] or o["reviewer"]]


def accessible_orgs(db: Database, user_id: str) -> list[dict]:
    return accessible(directory(db, user_id))
