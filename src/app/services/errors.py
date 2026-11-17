"""The errors every domain operation raises, and the input limits they enforce."""

TITLE_MAX, SUMMARY_MAX, REASON_MAX, NOTE_MAX = 120, 2000, 1000, 2000


class NotFound(LookupError):
    """Unknown, or not visible to this user (deliberately indistinguishable)."""


class Forbidden(PermissionError):
    """Known to this user, but the action needs a role or membership they lack."""


class Conflict(RuntimeError):
    """Stale or repeated form: the record changed since the form was rendered."""


class Invalid(ValueError):
    """Input that fails validation; the message is safe to show."""


def require_reason(text: str, ask: str = "Give the volunteer a reason") -> str:
    """A stripped reason of 3 to REASON_MAX characters, or Invalid starting with `ask`."""
    text = text.strip()
    if not 3 <= len(text) <= REASON_MAX:
        raise Invalid(f"{ask} (3–{REASON_MAX} characters).")
    return text
