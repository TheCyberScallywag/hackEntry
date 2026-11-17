"""Operator CLI: inspection output and demo guards."""

import pytest

from app import cli
from app.config import BUNDLED_GRAPH, Settings
from app.runtime import reset_demo


def test_inspect_bundled_store(capsys):
    assert cli.main(["inspect", "--store", str(BUNDLED_GRAPH)]) == 0
    out = capsys.readouterr().out
    assert "Winter Outreach Kitchen" in out and "— coverage gap —" in out
    assert "Cerys (u_" in out and "@" not in out


def test_matches_command(capsys):
    assert cli.main(["matches", "--org", "datakind-uk", "--store", str(BUNDLED_GRAPH)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("|")[1].strip() for line in lines[2:]] == ["Dev", "Asha", "Cerys", "Kofi"]


def test_demo_seed_is_refused_in_production(tmp_path):
    settings = Settings(data_dir=tmp_path, app_env="production", _env_file=None)
    with pytest.raises(RuntimeError, match="production"):
        reset_demo(settings)


def test_production_refuses_a_database_with_demo_accounts(tmp_path):
    from fastapi.testclient import TestClient

    from app.web.main import create_app

    reset_demo(Settings(data_dir=tmp_path, _env_file=None))  # a test-env demo database
    production = Settings(data_dir=tmp_path, app_env="production", _env_file=None)
    with pytest.raises(RuntimeError, match="demo accounts"), TestClient(create_app(production)):
        pass


def test_empty_compose_variables_mean_defaults(monkeypatch, tmp_path):
    for name in ("ORG_NAMES", "COOKIE_SECURE", "SESSION_TTL_HOURS", "AURA_URI"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings(_env_file=None)
    assert settings.org_names == {} and settings.cookie_secure is None
    assert settings.session_ttl_hours == 12


def test_org_names_are_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("ORG_NAMES", '{"datakind-uk": "Organisation A"}')
    settings = Settings(data_dir=tmp_path, _env_file=None)
    rt = reset_demo(settings)
    with rt.db.read() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM organisations ORDER BY name")]
    assert names == ["Cyber Scallywags", "InterLinkRCT", "Organisation A"]
    graph_names = rt.graph.read("MATCH (o:Organisation) RETURN o.name AS name")
    assert {"name": "Organisation A"} in graph_names


@pytest.fixture
def seeded(tmp_path, monkeypatch) -> Settings:
    """A demo data directory that `cli.main` operates on in place of the configured one."""
    settings = Settings(data_dir=tmp_path, _env_file=None)
    reset_demo(settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


def test_status_and_drain_report_the_outbox(seeded, capsys):
    import json

    from app.runtime import build_runtime
    from app.sqlstore import enqueue
    from tests.conftest import USERS

    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"pending": 0, "failed": 0, "items": []}
    with build_runtime(seeded).db.tx() as conn:
        enqueue(conn, "volunteer", USERS["dev"])
    assert cli.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["pending"] == 1 and status["items"][0]["entity_id"] == USERS["dev"]
    assert cli.main(["drain"]) == 0
    assert capsys.readouterr().out.strip() == "projected 1, failed 0"


def test_drain_exits_non_zero_when_a_projection_fails(seeded, capsys):
    from app.runtime import build_runtime
    from app.sqlstore import enqueue

    with build_runtime(seeded).db.tx() as conn:
        enqueue(conn, "volunteer", "u_does_not_exist")
    assert cli.main(["drain"]) == 1
    assert capsys.readouterr().out.strip() == "projected 0, failed 1"


def test_grant_reviewer_writes_an_iso_timestamp_like_every_other_row(seeded, capsys):
    from datetime import datetime

    from app.runtime import build_runtime
    from tests.conftest import DK, USERS

    assert cli.main(["grant-reviewer", "--email", " Gwen@cyberscallywags.uk ",
                     "--org", "datakind-uk"]) == 0  # fmt: skip
    assert capsys.readouterr().out.strip() == "granted"
    with build_runtime(seeded).db.read() as conn:
        granted_at = conn.execute(
            "SELECT granted_at FROM org_roles WHERE user_id = ? AND org_id = ? "
            "AND role = 'reviewer'",
            (USERS["gwen"], DK),
        ).fetchone()[0]
    parsed = datetime.fromisoformat(granted_at)
    assert "T" in granted_at and parsed.utcoffset() is not None  # not datetime('now')
    assert (
        cli.main(["grant-reviewer", "--email", "gwen@cyberscallywags.uk", "--org", "datakind-uk"])
        == 0
    )  # fmt: skip  (repeating is harmless)


def test_grant_reviewer_rejects_unknown_accounts(seeded):
    with pytest.raises(SystemExit, match="unknown email or organisation key"):
        cli.main(["grant-reviewer", "--email", "nobody@cyberscallywags.uk", "--org", "datakind-uk"])


def test_reset_and_json_inspect(seeded, capsys):
    import json

    assert cli.main(["reset", "--demo"]) == 0
    assert "0 pending" in capsys.readouterr().out
    assert cli.main(["inspect", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["organisation"] for r in rows} == {
        "DataKind UK",
        "Cyber Scallywags",
        "InterLinkRCT",
    }


def test_graph_errors_become_a_message_and_exit_code(tmp_path, capsys):
    assert cli.main(["inspect", "--store", str(tmp_path / "missing.rl")]) == 1
    assert "Relate:" in capsys.readouterr().err
