"""Single source of configuration. Values come from environment variables (or a local `.env`).

Nothing here is a real secret: the demo defaults are for a disposable local stack only, and
`APP_ENV=production` refuses to seed or run with the demo accounts (app.runtime.bootstrap).
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE = Path(__file__).resolve().parent
_REPO = _PACKAGE.parents[1]


def _resource(packaged: str, checkout: str) -> Path:
    """Wheels carry data under app/ (pyproject force-include); a checkout reads the repo."""
    inside = _PACKAGE / packaged
    return inside if inside.exists() else _REPO / checkout


FIXTURES_DIR = _resource("fixtures", "data/fixtures")
BUNDLED_GRAPH = _resource("demo.rl", "data/demo.rl")
BRAND_DIR = _resource("brand", "brand")
ORG_ASSETS_DIR = _resource("orgs", "data/orgs")  # logos, hero images and profiles.json


class Settings(BaseSettings):
    # env_ignore_empty: docker compose passes unset variables as "", which means "use default".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: Literal["test", "production"] = Field(
        "test", description="`production` disables demo seeding and demands real secrets."
    )
    data_dir: Path = Field(
        Path("var"),
        description="Runtime state: the SQLite database and the writable Relate store.",
    )
    org_names: dict[str, str] = Field(
        {},
        description=(
            'JSON map from organisation key to display name, e.g. {"datakind-uk": "Org A"}. '
            "Applied when the demo is seeded."
        ),
    )
    session_ttl_hours: int = Field(12, description="How long a sign-in lasts.")
    signup_enabled: bool = Field(
        True,
        description=(
            "Let anyone create an account, on the web and through the API. Off: the sign-up "
            "page and POST /api/v1/auth/signup answer 403; existing accounts still sign in."
        ),
    )
    cookie_secure: bool | None = Field(
        None,
        description=(
            "Force the Secure cookie flag. Unset: Secure whenever the request arrived over "
            "HTTPS (directly or via X-Forwarded-Proto from a trusted proxy)."
        ),
    )
    trust_proxy_headers: bool = Field(
        False, description="Honour X-Forwarded-Proto (set when behind a TLS-terminating proxy)."
    )
    api_url: str = Field(
        "http://127.0.0.1:8080",
        description="Where the Streamlit admin console reaches the JSON API (its only data path).",
    )
    projector_interval_seconds: float = Field(
        5.0, description="How often the background projector retries pending graph updates."
    )
    seed_demo_on_start: bool = Field(
        True,
        description="Seed the disposable demo when the data directory is empty (test env only).",
    )
    graph_max_rows: int = Field(500, description="Upper bound on rows read from Relate.")
    invitation_reminder_days: int = Field(
        7, description="Remind a volunteer of an unanswered team invitation after this many days."
    )
    invitation_expiry_days: int = Field(
        14,
        description=(
            "An unanswered team invitation closes after this many days and the volunteer goes "
            "back to the project's candidate list. 0 keeps invitations open indefinitely."
        ),
    )

    # ---- email (app.mailer). Organisations' onboarding emails are queued in SQLite and sent
    # through an SMTP server, which in the Compose stack is the `mail` sidecar. With no SMTP_HOST
    # nothing is sent: queued messages are marked `not_configured` and stay readable.
    smtp_host: str = Field(
        "", description="SMTP server that sends queued emails. Empty: emails are not sent."
    )
    smtp_port: int = Field(1025, description="SMTP port (1025: the Compose `mail` sidecar).")
    smtp_username: str = Field("", description="SMTP user. Empty: no authentication.")
    smtp_password: SecretStr = Field(SecretStr(""), description="SMTP password.")
    smtp_starttls: bool = Field(
        False, description="Upgrade the SMTP connection with STARTTLS before sending."
    )
    email_from: str = Field(
        "no-reply@volunteer-network.invalid",
        description="The address onboarding emails are sent from.",
    )
    public_url: str = Field(
        "http://localhost:8080",
        description="Where volunteers reach the web app; the links in emails start with it.",
    )

    # ---- agents (app.agents). With no AGENT_MODEL the agent endpoints answer 503 and nothing
    # else changes: the graph still decides every match.
    agent_model: str = Field(
        "",
        description=(
            "Pydantic AI model for the assistants, e.g. `anthropic:claude-sonnet-5-5`. "
            "Empty: the assistants are switched off."
        ),
    )
    agent_fallback_model: str = Field(
        "", description="Optional second model, tried when the first one's API fails."
    )
    anthropic_api_key: SecretStr = Field(
        SecretStr(""), description="Key for `anthropic:` models (or set it in the environment)."
    )
    agent_request_limit: int = Field(8, description="Model requests one assistant run may make.")
    agent_tool_calls_limit: int = Field(16, description="Tool calls one assistant run may make.")
    agent_total_tokens_limit: int = Field(
        40_000, description="Tokens (input + output) one assistant run may use."
    )
    agent_daily_runs: int = Field(
        40, description="Assistant runs one account may start in 24 hours. 0: no limit."
    )
    agent_timeout_seconds: float = Field(90.0, description="Wall-clock limit for one run.")

    # ---- observability (app.observability)
    logfire_token: SecretStr = Field(
        SecretStr(""),
        description="Pydantic Logfire write token. Empty: traces are made but sent nowhere.",
    )
    logfire_include_content: bool | None = Field(
        None,
        description=(
            "Record prompts, tool arguments and model output on agent spans. Unset: on, except "
            "when APP_ENV=production (they can hold what a volunteer wrote about themselves)."
        ),
    )
    audit_retention_days: int = Field(
        400, description="Audit events and assistant run records older than this are pruned."
    )

    aura_uri: str = Field("", description="Optional neo4j+s:// URI of a designated dev AuraDB.")
    aura_username: str = Field("neo4j", description="AuraDB user.")
    aura_password: SecretStr = Field(SecretStr(""), description="AuraDB password.")
    aura_database: str = Field("neo4j", description="AuraDB database name.")

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "dvn.sqlite3"

    @property
    def graph_path(self) -> Path:
        return self.data_dir / "graph.rl"

    @property
    def demo_allowed(self) -> bool:
        return self.app_env != "production"

    @property
    def agents_enabled(self) -> bool:
        return bool(self.agent_model)

    @property
    def include_agent_content(self) -> bool:
        if self.logfire_include_content is None:
            return self.app_env != "production"
        return self.logfire_include_content


@lru_cache
def get_settings() -> Settings:
    return Settings()
