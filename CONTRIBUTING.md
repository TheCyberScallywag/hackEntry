<p align="center">
  <img src="brand/images/logo.svg" width="112" alt="Volunteer Network logo: three jade volunteers around a golden heart">
</p>

<h1 align="center">Contributing</h1>

<p align="center">
  <em>Volunteer Network · thanks for helping</em>
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
4. **Check** with `uv run task lint` and `uv run task test` (`uv run task hooks` runs every
   pre-commit hook on every file). For mobile changes, also run
   `docker build -f docker/mobile.Dockerfile --target test .` (or `npm run lint && npm run
   test:ci` in `mobile/`).
5. **Fixtures**: if you edit `data/fixtures/*.json`, run `uv run task bundle` to rebuild
   `data/demo.rl`. A test fails if the bundled store drifts from the fixtures.
6. **Docs**: pages live in `docs/content`. Build them with `uv sync --locked --group docs` and
   `uv run task docs`; warnings fail the build. A new page goes in both `docs/zensical.toml`
   (`nav`) and `docs/content/manual.md` (a `toctree`).

Every tool, task and hook is listed in
[docs/content/project/tooling.md](docs/content/project/tooling.md). How the application uses
the Relate graph engine is in [docs/content/project/relate.md](docs/content/project/relate.md).

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
  <sub>Cyber Scallywags · Volunteer Network</sub>
</p>
