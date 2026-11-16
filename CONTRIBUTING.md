<p align="center">
  <img src="brand/images/logo.svg" width="112" alt="Volunteer Network logo: three teal volunteers around a golden heart">
</p>

<h1 align="center">Contributing</h1>

<p align="center">
  <em>Data Volunteer Network · thanks for helping</em>
</p>

<p align="center">
  <img alt="Python 3.14" src="https://img.shields.io/badge/python-3.14-13cdb5?style=flat-square&labelColor=0b1730">
  <img alt="uv" src="https://img.shields.io/badge/managed%20with-uv-13cdb5?style=flat-square&labelColor=0b1730">
  <img alt="pre-commit enabled" src="https://img.shields.io/badge/pre--commit-enabled-ffb21c?style=flat-square&labelColor=0b1730">
</p>

<p align="center">
  <a href="README.md">README</a> ·
  <a href="CODE_OF_CONDUCT.md">Code of conduct</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

---

Please follow the [Code of conduct](CODE_OF_CONDUCT.md) and report security problems as described
in [SECURITY.md](SECURITY.md).

## Workflow

1. **Set up**: `uv sync --locked`, then `uv run pre-commit install`.
2. **Branch** from `main` using one of the prefixes below. The hooks refuse commits to `main` and
   badly named branches.
3. **Commit** with one of the prefixes below (see `scripts/check_commit_msg.sh`).
4. **Check** with `uv run task lint` and `uv run task test`. For mobile changes, also run
   `docker build -f docker/mobile.Dockerfile --target test .` (or `npm run lint && npm run
   test:ci` in `mobile/`).
5. **Fixtures**: if you edit `data/fixtures/*.json`, run `uv run task bundle` to rebuild
   `data/demo.rl`. A test fails if the bundled store drifts from the fixtures.

## Naming

**Branches** start with `feature/`, `bug/`, `task/`, `docs/` or `refactor/`.

**Commits** start with a prefix, which decides where they land in the [Changelog](CHANGELOG.md):

| Commit prefix | Changelog section |
|---|---|
| `ADDED:` | Added |
| `FIXED:` | Fixed |
| `REFACTOR:` | Changed |
| `DOCS:` | Documentation |
| `TASK:` | Chores |

> [!WARNING]
> Keep demo data fictional. Never commit credentials, `.env` or anything under `var/`.

---

<p align="center">
  <sub>Cyber Scallywags · Data Volunteer Network</sub>
</p>
