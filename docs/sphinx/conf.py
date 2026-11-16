"""Sphinx + Furo: the reference manual. Build: uv run task docs   Live: uv run task docs_live_manual

Source pages are docs/content (shared with the Zensical portal, docs/zensical.toml), so
this configuration lives apart from them: sphinx-build -c docs/sphinx docs/content <out>.
"""

import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))  # autodoc imports app without install

# Import FastAPI before autodoc documents any Pydantic model's members: once it has, FastAPI's
# own models (extra="allow") fail to build, and app.web could not be documented.
import fastapi  # noqa: E402, F401

_brand = tomllib.loads((ROOT / "brand/brand.toml").read_text(encoding="utf-8"))
_org = _brand["organisation"]
project = _brand["project"]["name"]
author = _org["name"]
copyright = f"{_brand['project']['year']}, {_org['name']}"  # noqa: A001 - Sphinx's name for it
release = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_design",
    "sphinxcontrib.mermaid",
]

root_doc = "manual"
# Portal-only pages (Zensical grid-card home, mkdocstrings Python API).
exclude_patterns = ["index.md", "reference/python-api.md"]
source_suffix = {".md": "markdown"}

# MyST: the shared pages use plain Markdown plus `{.class}` on images and mermaid fences.
myst_enable_extensions = ["attrs_inline", "attrs_block", "colon_fence", "deflist"]
myst_heading_anchors = 3
myst_fence_as_directive = ["mermaid"]

# Links to the portal (`/`, `/manual/...`) are absolute site paths, not Sphinx documents.
suppress_warnings = ["myst.xref_missing"]
nitpicky = False

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "show-inheritance": True}

# sphinxcontrib-mermaid follows Furo's light/dark switch itself; a custom init config breaks
# its re-render, so diagrams are branded in _static/furo.css instead.

# ---------------------------------------------------------------- the brand on Furo
# Colours are the --brand-* tokens in brand.css (from brand/brand.toml [theme]), so this file
# holds none. The images are the copies the docs tasks write to assets/brand.
BRAND_ASSETS = HERE.parent / "content/assets/brand"
scheme = _brand["theme"].get("docs", {})
html_theme = "furo"
html_title = f"{project} manual"
html_static_path = ["_static", "../content/assets/stylesheets"]
html_css_files = ["brand.css", "template.css", "furo.css", "brand-extra.css"]
html_js_files = ["dark-first.js"] if scheme.get("scheme", "dark") == "dark" else []
html_logo = next((str(p) for p in BRAND_ASSETS.glob("mark.*")), None)
html_favicon = next((str(p) for p in BRAND_ASSETS.glob("favicon.*")), None)
templates_path = ["_templates"]
# The manual's home is manual.html (index.md belongs to the portal); /manual/ redirects there.
html_additional_pages = {"index": "redirect-to-manual.html"}
html_copy_source = False
html_show_sourcelink = False

_shared = {
    "font-stack": "var(--brand-font)",
    "font-stack--monospace": "var(--brand-mono)",
}
html_theme_options = {
    "sidebar_hide_name": False,
    "navigation_with_keys": True,
    "announcement": " · ".join(
        filter(
            None,
            [
                f'Part of the <a href="/">{project} docs portal</a>',
                _org["name"],
                _org.get("motto", ""),
            ],
        )
    ),
    "light_css_variables": {
        **_shared,
        "color-brand-primary": "var(--brand-accent-ink)",
        "color-brand-content": "var(--brand-accent-ink)",
        "color-brand-visited": "var(--brand-accent-2-ink)",
        "color-announcement-background": "var(--brand-surface)",
        "color-announcement-text": "var(--brand-text)",
    },
    "dark_css_variables": {
        **_shared,
        "color-brand-primary": "var(--brand-accent)",
        "color-brand-content": "var(--brand-accent)",
        "color-brand-visited": "var(--brand-accent-2-light)",
        "color-background-primary": "var(--brand-background)",
        "color-background-secondary": "var(--brand-surface)",
        "color-background-hover": "var(--brand-card)",
        "color-background-border": "var(--brand-border)",
        "color-background-item": "var(--brand-card)",
        "color-foreground-primary": "var(--brand-text)",
        "color-foreground-secondary": "var(--brand-text-muted)",
        "color-foreground-muted": "var(--brand-text-subtle)",
        "color-foreground-border": "var(--brand-border)",
        "color-code-background": "var(--brand-card)",
        "color-inline-code-background": "var(--brand-card)",
        "color-card-background": "var(--brand-surface)",
        "color-card-border": "var(--brand-border)",
        "color-announcement-background": "var(--brand-surface)",
        "color-announcement-text": "var(--brand-text)",
    },
}
pygments_style = "friendly"
pygments_dark_style = "github-dark"
