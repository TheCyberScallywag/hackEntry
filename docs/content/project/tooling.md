# Tooling

Every tool this repository uses: what it is for here, where it is configured and how to run it.
Versions are the ones locked in `uv.lock` and `mobile/package-lock.json` in November 2026.

## 1. What you need to install

| To do this | Install | Everything else comes from |
|---|---|---|
| Run the web app, the CLI and the tests | [uv](https://docs.astral.sh/uv/) | `uv sync --locked` (uv also installs Python 3.14) |
| Run the whole stack in containers | Docker with Compose | The images build themselves |
| Work on the mobile app outside Docker | Node 24 and npm | `npm ci` in `mobile/` |
| Query a `.rl` graph file by hand (optional) | The [Relate CLI](https://relatedb.ai/docs/get-started/install/) 1.0.1 | — |
| Push the demo graph to AuraDB (optional) | A development AuraDB instance | `uv sync --locked --group aura` |

## 2. Tasks

Tasks are defined in `pyproject.toml` under `[tool.taskipy.tasks]` and run with
`uv run task <name>`. `uv run task --list` prints them.

| Task | What it does |
|---|---|
| `web` | Runs the web app on http://localhost:8080 (seeds the demo on a first start) |
| `admin` | Runs the Streamlit admin console on http://localhost:8501 (needs `uv sync --group admin`) |
| `reset` | Deletes `var/` stores and reseeds the disposable demo |
| `bundle` | Rebuilds the committed `data/demo.rl` from `data/fixtures` |
| `sync` | Writes the mobile and docs copies of `brand/` (theme, images, names); the tests fail while one is stale |
| `inspect` | Runs the sample graph inspection query on the bundled store |
| `lint` | `ruff check` and `ruff format --check` |
| `format` | `ruff format` and `ruff check --fix` |
| `hooks` | Runs every pre-commit hook on every file |
| `changelog_preview` | Prints the unreleased changelog section from the commit messages |
| `changelog` | Regenerates `CHANGELOG.md` with git-cliff (replaces the file) |
| `test` | `pytest`, with coverage |
| `up` / `down` | Builds and starts, or stops and removes, the Compose stack |
| `docs` | Builds the docs portal and the reference manual into `docs/_build/site` |
| `docs_serve` | Builds the docs and serves them on http://localhost:8200 |
| `docs_live` / `docs_live_manual` | Live-reloads the portal (:8201) or the manual (:8202) while writing |

The application's own command line is `python -m app.cli <command>`:

| Command | What it does |
|---|---|
| `reset --demo` | Wipes the runtime stores and reseeds the demo |
| `bundle [--out PATH]` | Rebuilds the bundled graph |
| `inspect [--store PATH] [--json]` | Organisation → project → cause → skill → eligible volunteers |
| `matches --org KEY [--store PATH]` | Ranked candidates for one organisation, straight from Relate |
| `status` | The projection outbox: pending, failing, and the items |
| `drain` | Projects every pending item now; exits 1 if any failed |
| `grant-reviewer --email E --org KEY` | Gives an existing account the reviewer role |
| `openapi [--out PATH]` | Writes the JSON API's OpenAPI document |
| `aura-push` / `aura-parity` | The optional AuraDB push and match-parity check |

`python -m app.serve web` and `python -m app.serve admin` start one service each; the Docker
images use them as their entry point.

## 3. Graph and data

| Tool | Version | Use here | Where |
|---|---|---|---|
| **Relate** (`relate-db`) | 1.0.1 (exact pin) | The embedded graph engine that makes every matching decision. See [Relate in this application](relate.md). | `src/app/graph/`, `data/demo.rl` |
| **Relate CLI** (`relate`) | 1.0.1 | Optional. Queries the same `.rl` files from a terminal. | Not a project dependency |
| SQLite | Bundled with Python | The transactional source of truth, in WAL mode, with the projection outbox | `src/app/sqlstore.py`, `var/dvn.sqlite3` |
| Neo4j Python driver | 6.4 | Optional. Used only by `aura-push` and `aura-parity` to reach AuraDB. | `src/app/graph/aura.py`, `aura` extra |
| Neo4j AuraDB | A development instance | Optional destination for the synthetic graph and the parity check. Never on a request path. | `AURA_*` settings, `.env.example` |

## 4. Backend

| Tool | Version | Use here | Where |
|---|---|---|---|
| CPython | 3.14 | The runtime | `.python-version`, `requires-python` |
| FastAPI | 0.162 | The JSON API, the HTML routes and the OpenAPI document | `src/app/web/` |
| Starlette | 1.10 | Under FastAPI: static files, middleware, the test client | — |
| Uvicorn | 0.54 | The HTTP server | `src/app/serve.py` |
| Pydantic | 2.14 | Request bodies and response models (the API contract) | `src/app/web/schemas.py` |
| pydantic-settings | 2.16 | Typed settings from the environment and `.env` | `src/app/config.py` |
| Jinja2 | 3.1 | Server-rendered pages; no client JavaScript | `src/app/web/templates/` |
| python-multipart | 0.0.33 | Form parsing | — |
| argon2-cffi | 25.1 | Argon2id password hashing | `src/app/auth.py` |
| Streamlit | 1.67 | The admin console, a client of the JSON API | `src/app/admin/main.py`, `admin` extra |
| httpx | 0.28 | The admin console's HTTP client; the test client | `admin` extra, dev group |
| pandas | 3.1 | Tables in the admin console (installed with Streamlit) | `admin` extra |

## 5. Mobile

| Tool | Version | Use here | Where |
|---|---|---|---|
| Angular | 22.2 | Standalone components, signals, guards and interceptors | `mobile/src/app` |
| Ionic | 9.0 | Mobile UI components | `mobile/` |
| Capacitor | 8.5 | Native shell for a later packaged build; only the web build is used today | `mobile/capacitor.config.ts` |
| TypeScript | 6.0 | The language | `mobile/tsconfig*.json` |
| RxJS | 7.8 | HTTP streams, including cancelling a superseded request | — |
| Vitest with jsdom | 5.0 / 30 | Unit tests: 39 tests in 11 files (`npm run test:ci`) | `mobile/src/**/*.spec.ts` |
| ESLint, angular-eslint, typescript-eslint | 10 / 22 / 8 | Lint (`npm run lint`) | `mobile/eslint.config.js` |
| Angular CLI | 22.2 | Build, serve, lint and test runner | `mobile/angular.json` |
| nginx (unprivileged) | 1.31 | Serves the built app and proxies `/api/` to the web service | `docker/mobile.nginx.conf` |

Mobile scripts, run in `mobile/`: `npm start` (dev server with an API proxy), `npm run lint`,
`npm run test:ci`, `npm run build`.

## 6. Quality

| Tool | Version | Use here | Where |
|---|---|---|---|
| pytest | 9.2 | 154 backend tests against temporary SQLite databases and real Relate stores | `tests/`, `[tool.pytest.ini_options]` |
| pytest-cov / coverage | 7.1 / 7.17 | Line coverage on every run (95%) | `addopts` in `pyproject.toml` |
| Ruff | 0.17 | Lint and format, line length 100; rule sets E, F, I, B, UP, SIM, S, TD | `[tool.ruff]` |
| pre-commit | 4.7 | Runs the hooks below on commit, on the commit message and on push | `.pre-commit-config.yaml` |

Two pytest markers gate tests that need something external: `aura` (AuraDB credentials) and
`compose` (a running stack). Both are skipped otherwise.

Pre-commit hooks, installed with `uv run pre-commit install`:

| Hook | Checks |
|---|---|
| `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-toml` | Basic file hygiene |
| `check-added-large-files`, `detect-private-key` | Nothing large or secret is committed |
| `no-commit-to-branch` | No commits straight to `main` |
| `branch-name` (on push) | Branches start with `feature/`, `bug/`, `task/`, `docs/` or `refactor/` (`scripts/check_branch_name.sh`) |
| `commit-msg` | Messages start with `ADDED:`, `FIXED:`, `TASK:`, `DOCS:` or `REFACTOR:` (`scripts/check_commit_msg.sh`) |
| `ruff-check`, `ruff-format` | Lint with fixes, and format |
| `uv-lock` | `uv.lock` matches `pyproject.toml` |
| `actionlint` | The GitHub Actions workflows are valid |

## 7. Packaging and dependencies

| Tool | Use here | Where |
|---|---|---|
| uv | Environments, the lock file, running tools | `uv.lock`, `[tool.uv]` |
| taskipy | The task runner behind `uv run task` | `[tool.taskipy]` |
| Hatchling | Build backend. The wheel carries the brand, the fixtures and `demo.rl` inside the `app` package. | `[build-system]`, `[tool.hatch]` |
| npm | The mobile app's dependencies | `mobile/package-lock.json` |

Optional Python dependencies are grouped so each environment installs only what it needs:

| Group or extra | Adds | Install |
|---|---|---|
| `dev` (default) | pytest, Ruff, pre-commit, taskipy, httpx, git-cliff | `uv sync --locked` |
| `admin` | Streamlit, httpx | `uv sync --locked --group admin` |
| `aura` | The Neo4j Python driver | `uv sync --locked --group aura` |
| `docs` | Sphinx, Furo, Zensical and their plugins | `uv sync --locked --group docs` |

## 8. Containers

| Image | Built from | Contains | Port |
|---|---|---|---|
| `dvn/web` | `docker/Dockerfile` with `EXTRAS="--extra aura"` | The web app, API, projector and CLI. No Streamlit or pandas. | 8080 |
| `dvn/admin` | `docker/Dockerfile` with `EXTRAS="--extra admin"` | The Streamlit console | 8501 |
| `dvn/mobile` | `docker/mobile.Dockerfile` | nginx serving the built Ionic app. The build runs the mobile lint and tests first. | 8100 |
| `dvn/docs` | `docker/docs.Dockerfile` | nginx serving the portal and the manual. The build fails on any docs warning. | 8200 |

`docker-compose.yml` starts all four. The web and admin services share one named volume,
`dvn-data`, holding the SQLite database and the writable Relate store. Host ports can be changed
with `WEB_PORT`, `ADMIN_PORT`, `MOBILE_PORT` and `DOCS_PORT`. Base images: `python:3.14-slim`,
`node:24-alpine`, `nginxinc/nginx-unprivileged`, with uv copied from `ghcr.io/astral-sh/uv`.

## 9. Documentation

One set of Markdown pages in `docs/content` is built twice.

| Tool | Version | Use here | Where |
|---|---|---|---|
| Zensical | 0.1 | The docs portal, served at `/` | `docs/zensical.toml` |
| mkdocstrings-python | 2.0 | The portal's Python API page, from docstrings | `docs/content/reference/python-api.md` |
| Sphinx | 9.2 | The reference manual, served at `/manual/` | `docs/sphinx/conf.py` |
| Furo | 2025.12 | The manual's theme | `docs/sphinx/` |
| MyST-Parser | 5.2 | Lets Sphinx read the same Markdown | `docs/sphinx/conf.py` |
| sphinx-design, sphinx-copybutton, sphinxcontrib-mermaid | 0.8 / 0.5 / 2.1 | Layout, copy buttons and Mermaid diagrams in the manual | `docs/sphinx/conf.py` |
| sphinx-autobuild | 2025.8 | Live reload for the manual | `task docs_live_manual` |
| Mermaid | — | The diagrams, written as fenced code in the pages | — |

Both builds treat warnings as errors. The home and about pages and the brand images are copied
from `brand/` by the docs tasks, so `docs/content/index.md` and `about.md` are not committed. A
new page must be added in two places: `nav` in `docs/zensical.toml` and a `toctree` in
`docs/content/manual.md`.

## 10. Continuous integration and repository automation

| Tool | Use here | Where |
|---|---|---|
| GitHub Actions: **CI** | On pull requests and on pushes to `main`. Jobs: `python` (lint and tests), `docs` (strict build), `mobile` (lint and tests in Docker), `compose` (builds the stack and runs the smoke test), and `ci-passed`, the one check to require on `main`. | `.github/workflows/ci.yml` |
| GitHub Actions: **CodeQL** | Static analysis of the Python, TypeScript and workflow code, on pull requests, on `main` and weekly | `.github/workflows/codeql.yml` |
| Dependabot | Weekly grouped updates for uv, npm, GitHub Actions and the Docker base images | `.github/dependabot.yml` |
| Issue and pull-request templates | — | `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md` |
| Copier | Records the template this repository was generated from (`sgaffald`) | `.copier-answers.yml` |
| git-cliff | Generates `CHANGELOG.md` from the commit prefixes. `uv run task changelog_preview` prints the unreleased section; `uv run task changelog` rewrites the file. | `cliff.toml`, dev group |

## 11. Configuration

Settings come from environment variables or a local `.env` file (`src/app/config.py`;
`.env.example` is the template).

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `test` | `production` disables demo seeding and refuses a database that holds demo accounts |
| `DATA_DIR` | `var` | Where the SQLite database and the writable Relate store live |
| `ORG_NAMES` | `{}` | JSON map from organisation key to display name, applied when the demo is seeded |
| `SESSION_TTL_HOURS` | `12` | How long a sign-in lasts |
| `COOKIE_SECURE` | unset | Forces the Secure cookie flag; unset means "whenever the request came over HTTPS" |
| `TRUST_PROXY_HEADERS` | `false` | Honour `X-Forwarded-Proto` behind a TLS-terminating proxy |
| `API_URL` | `http://127.0.0.1:8080` | Where the admin console reaches the JSON API |
| `PROJECTOR_INTERVAL_SECONDS` | `5` | How often the background projector retries pending graph updates |
| `SEED_DEMO_ON_START` | `true` | Seed the demo when the data directory is empty (test environment only) |
| `GRAPH_MAX_ROWS` | `500` | Upper bound on rows read from Relate in one statement |
| `AURA_URI`, `AURA_USERNAME`, `AURA_PASSWORD`, `AURA_DATABASE` | empty, `neo4j`, empty, `neo4j` | The optional development AuraDB instance |
| `HOST`, `PORT`, `ADMIN_PORT`, `SERVICE` | `127.0.0.1`, `8080`, `8501`, — | Read by `app.serve` |

## 12. Brand and editor

| Item | Use here | Where |
|---|---|---|
| Brand kit | Name, tagline, palette (navy, jade, honey, cream), fonts and images. The single source for every surface: the web app serves it as `/theme.css`, the admin console is started with it as its Streamlit theme, and `uv run task sync` writes the mobile theme (`mobile/src/theme/_brand.scss`), the docs tokens (`docs/content/assets/stylesheets/brand.css`), the image copies and the names. It refuses colours below WCAG AA contrast. | `brand/brand.toml`, `brand/images/`, `brand/README.md`, `src/app/brandkit.py` |
| VS Code settings | Recommended extension and workspace settings | `.vscode/` |
