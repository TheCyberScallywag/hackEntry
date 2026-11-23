"""Email: organisations' templates, the outbox they are queued in, and delivery over SMTP.

A template is plain text with `{{ placeholders }}` from a fixed list. Org admins write them, so
they are filled in by substitution only: there is no template language to escape from, and a
placeholder that is not on the list is refused when the template is saved
(app.services.onboarding).

Sending is split in two. `queue` writes the finished message to `email_outbox` in the caller's
transaction, so an email exists exactly when the change it announces was committed. `deliver`
then sends what is due through the SMTP server named by SMTP_HOST (the `mail` sidecar in the
Compose stack) and retries failures with a backoff. The recipient's address is read only at that
point, and is never shown to an organisation's admins.
"""

import logging
import re
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr

from app.config import Settings
from app.meta import get_meta
from app.sqlstore import Database, now

log = logging.getLogger(__name__)

# What a template may use, and what each stands for.
PLACEHOLDERS = {
    "volunteer_name": "The volunteer's display name",
    "organisation_name": "Your organisation's name",
    "project_title": "The project, in a project's own onboarding (empty in the organisation's)",
    "step_title": "The title of the step the volunteer has reached",
    "step_description": "That step's description",
    "steps_done": "How many steps the volunteer has finished",
    "steps_total": "How many steps the workflow has",
    "onboarding_url": "Link to the volunteer's onboarding page",
}
SAMPLE = {
    "volunteer_name": "Alex Example",
    "project_title": "Community newsletter",
    "step_title": "Read our code of conduct",
    "step_description": "It takes five minutes and covers how we work together.",
    "steps_done": "1",
    "steps_total": "3",
}
SUBJECT_MAX, BODY_MAX = 150, 5000
# Left in a queued message and replaced with the real link when it is sent: only the process
# that sends knows PUBLIC_URL.
LINK_MARK = "{{ onboarding_url }}"
MAX_ATTEMPTS = 5
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_BRACES = re.compile(r"\{\{|\}\}")


def template_problems(subject: str, body: str) -> str | None:
    """What is wrong with a template's placeholders, in words an org admin can act on."""
    for text in (subject, body):
        unknown = sorted(set(_PLACEHOLDER.findall(text)) - set(PLACEHOLDERS))
        if unknown:
            return "Unknown placeholder: " + ", ".join(f"{{{{ {n} }}}}" for n in unknown) + "."
        if _BRACES.search(_PLACEHOLDER.sub("", text)):
            return "A placeholder is written {{ name }}: check the curly brackets."
    return None


def render(text: str, values: dict[str, str]) -> str:
    """Fill the placeholders in. Values are inserted as they are and never read as templates."""
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), ""), text)


def queue(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    user_id: str,
    purpose: str,
    template: sqlite3.Row | dict,
    values: dict[str, str],
) -> None:
    """Queue one email to a user, in the caller's transaction."""
    stamp = now()
    values = values | {"onboarding_url": LINK_MARK}
    subject = " ".join(render(template["subject"], values).split())[:SUBJECT_MAX]
    conn.execute(
        "INSERT INTO email_outbox (org_id, user_id, purpose, template_name, subject, body, "
        "created_at, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (org_id, user_id, purpose, template["name"], subject, render(template["body"], values),
         stamp, stamp),
    )  # fmt: skip


@dataclass(frozen=True)
class Delivered:
    sent: int = 0
    failed: int = 0  # this attempt; a message is retried until MAX_ATTEMPTS
    not_configured: int = 0


def with_link(text: str, settings: Settings) -> str:
    return text.replace(LINK_MARK, settings.public_url.rstrip("/") + "/onboarding")


def _message(row: sqlite3.Row, settings: Settings) -> EmailMessage:
    message = EmailMessage()
    sender = formataddr((f"{row['org_name']} via {get_meta().name}", settings.email_from))
    message["From"] = sender
    message["To"] = formataddr((row["display_name"], row["email"]))
    message["Subject"] = with_link(row["subject"], settings)
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(with_link(row["body"], settings))
    return message


def _connect(settings: Settings) -> smtplib.SMTP:
    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    try:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
    except Exception:
        smtp.close()
        raise
    return smtp


def deliver(db: Database, settings: Settings, *, limit: int = 50) -> Delivered:
    """Send the queued emails that are due. Safe to call often: it does nothing when none is.

    No write transaction is held while talking to the mail server. One process should call this
    (the web app's background loop does), because two would both send a message they both read.
    """
    with db.read() as conn:
        due = conn.execute(
            "SELECT e.id, e.subject, e.body, e.attempts, u.email, u.display_name, "
            "o.name AS org_name FROM email_outbox e JOIN users u ON u.id = e.user_id "
            "JOIN organisations o ON o.id = e.org_id "
            "WHERE e.status = 'queued' AND e.next_attempt_at <= ? ORDER BY e.id LIMIT ?",
            (now(), limit),
        ).fetchall()
    if not due:
        return Delivered()
    if not settings.smtp_host:
        with db.tx() as conn:
            conn.executemany(
                "UPDATE email_outbox SET status = 'not_configured' WHERE id = ?",
                [(row["id"],) for row in due],
            )
        return Delivered(not_configured=len(due))
    outcomes: list[tuple[sqlite3.Row, str | None]] = []  # (message, error or None)
    try:
        smtp = _connect(settings)
    except (OSError, smtplib.SMTPException) as error:
        log.warning("mail server unreachable: %s", error)
        outcomes = [(row, f"mail server unreachable: {error}") for row in due]
    else:
        with smtp:
            for row in due:
                try:
                    smtp.send_message(_message(row, settings))
                except (OSError, smtplib.SMTPException, ValueError) as error:
                    log.warning("email %s not sent: %s", row["id"], error)
                    outcomes.append((row, str(error)))
                else:
                    outcomes.append((row, None))
    stamp = now()
    with db.tx() as conn:
        for row, error in outcomes:
            if error is None:
                conn.execute(
                    "UPDATE email_outbox SET status = 'sent', sent_at = ?, last_error = NULL, "
                    "attempts = attempts + 1 WHERE id = ?",
                    (stamp, row["id"]),
                )
                continue
            attempts = row["attempts"] + 1
            retry = datetime.now(UTC) + timedelta(minutes=2**attempts)
            conn.execute(
                "UPDATE email_outbox SET attempts = ?, last_error = ?, next_attempt_at = ?, "
                "status = ? WHERE id = ?",
                (attempts, error[:500], retry.isoformat(timespec="seconds"),
                 "failed" if attempts >= MAX_ATTEMPTS else "queued", row["id"]),
            )  # fmt: skip
    sent = sum(1 for _row, error in outcomes if error is None)
    return Delivered(sent=sent, failed=len(outcomes) - sent)
