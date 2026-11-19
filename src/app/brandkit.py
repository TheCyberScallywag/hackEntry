"""One brand on every surface, from brand/brand.toml.

The web app serves `tokens_css()` at /theme.css and the admin console is started with
`streamlit_flags()`, so both follow the brand folder they are run with. The mobile app and the
docs cannot read brand.toml, so `sync()` writes their copies (`uv run task sync`) and
`sync(check=True)` reports the ones that are out of date (tests/test_brand.py).

The CSS custom properties are the same on every surface:

    --brand-primary, -secondary, -tertiary, -cream   the palette of brand/images
    --brand-bar, --brand-bar-text, --brand-rule      the navy header bar and its jade-honey rule
    --brand-background, -surface, -card, -border     the page, per light or dark scheme
    --brand-text, --brand-text-muted
    --brand-accent, --brand-on-accent                jade: links and buttons
    --brand-highlight                                honey: badges and emphasis
"""

import html
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.config import BRAND_DIR

REPO = Path(__file__).resolve().parents[2]
SCHEMES = ("light", "dark")
AA = 4.5  # WCAG AA contrast for body text
CONTRIBUTORS_MARK = "<!-- contributors -->"  # where brand/docs/about.md lists its contributors
HEADER = "GENERATED from brand/brand.toml by `uv run task sync` (src/app/brandkit.py). Do not edit."


class BrandError(ValueError):
    """brand.toml is incomplete, or a colour pair is not readable."""


# ------------------------------------------------------------------ colour arithmetic
def _rgb(colour: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
        raise BrandError(f"'{colour}' is not a #rrggbb colour")
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def _triplet(colour: str) -> str:
    return ", ".join(map(str, _rgb(colour)))


def mix(colour: str, other: str, amount: float) -> str:
    """`colour` moved `amount` (0 to 1) of the way towards `other`."""
    pairs = zip(_rgb(colour), _rgb(other), strict=True)
    return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in pairs)


def contrast(colour: str, other: str) -> float:
    """WCAG 2 contrast ratio, from 1 (identical) to 21 (black on white)."""

    def luminance(c: str) -> float:
        channels = [v / 255 for v in _rgb(c)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(colour), luminance(other)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# ------------------------------------------------------------------ the brand
@dataclass(frozen=True)
class Scheme:
    background: str
    surface: str
    text: str
    muted: str
    border: str
    accent: str
    highlight: str
    card: str  # derived: a raised surface (code blocks, table heads)
    on_accent: str  # derived: text on an accent-coloured button


@dataclass(frozen=True)
class Brand:
    name: str
    tagline: str
    description: str
    organisation: str
    year: int
    primary: str
    secondary: str
    tertiary: str
    cream: str
    font: str
    font_display: str
    font_mono: str
    light: Scheme
    dark: Scheme
    docs_scheme: str
    images: dict[str, str]

    def scheme(self, name: str) -> Scheme:
        return self.light if name == "light" else self.dark


def load(brand_dir: Path = BRAND_DIR) -> Brand:
    """Read and check brand.toml. Raises BrandError naming the field that is wrong."""
    raw = tomllib.loads((brand_dir / "brand.toml").read_text(encoding="utf-8"))
    try:
        theme = raw["theme"]
        palette = {key: theme[key].lower() for key in ("primary", "secondary", "tertiary", "cream")}
        schemes = {}
        for name in SCHEMES:
            table = {key: value.lower() for key, value in theme[name].items()}
            accent = table["accent"]
            on_accent = max((palette["primary"], "#ffffff"), key=lambda c: contrast(c, accent))
            card = mix(table["surface"], table["text"], 0.06)
            schemes[name] = Scheme(**table, card=card, on_accent=on_accent)
        brand = Brand(
            name=raw["project"]["name"],
            tagline=raw["project"]["tagline"],
            description=raw["project"]["description"],
            organisation=raw["organisation"]["name"],
            year=raw["project"]["year"],
            **palette,
            font=theme["font"],
            font_display=theme["font_display"],
            font_mono=theme["font_mono"],
            light=schemes["light"],
            dark=schemes["dark"],
            docs_scheme=theme.get("docs", {}).get("scheme", "auto"),
            images=dict(raw["images"]),
        )
    except KeyError as missing:
        raise BrandError(f"brand.toml has no {missing}") from missing
    except TypeError as wrong:
        raise BrandError(f"brand.toml [theme.light] / [theme.dark]: {wrong}") from wrong
    _check(brand, brand_dir)
    return brand


def _check(brand: Brand, brand_dir: Path) -> None:
    if brand.docs_scheme not in ("auto", *SCHEMES):
        raise BrandError(f"[theme.docs] scheme must be auto, light or dark: '{brand.docs_scheme}'")
    pairs = [("cream on primary", brand.cream, brand.primary)]
    for name in SCHEMES:
        s = brand.scheme(name)
        pairs += [
            (f"{name} text on background", s.text, s.background),
            (f"{name} text on surface", s.text, s.surface),
            (f"{name} text on card", s.text, s.card),
            (f"{name} muted on background", s.muted, s.background),
            (f"{name} muted on surface", s.muted, s.surface),
            (f"{name} accent on background", s.accent, s.background),
            (f"{name} accent on surface", s.accent, s.surface),
            (f"{name} highlight on background", s.highlight, s.background),
            (f"{name} highlight on surface", s.highlight, s.surface),
            (f"{name} button text on accent", s.on_accent, s.accent),
        ]
    failed = [f"{label} is {contrast(a, b):.2f}:1" for label, a, b in pairs if contrast(a, b) < AA]
    if failed:
        raise BrandError(f"Below WCAG AA ({AA}:1): " + "; ".join(failed))
    for key in ("logo", "mark", "favicon", "hero"):
        image = brand_dir / "images" / brand.images.get(key, "")
        if not image.is_file():
            raise BrandError(f"[images] {key}: no such file brand/images/{image.name}")
        if not brand.images.get(f"{key}_alt") and key in ("logo", "hero"):
            raise BrandError(f"[images] {key}_alt: every picture needs its alt text")
    favicon = (brand_dir / "images" / brand.images["favicon"]).read_bytes()
    if favicon[:8] != b"\x89PNG\r\n\x1a\n":
        raise BrandError("[images] favicon must be a real PNG file")


def _indent(block: str) -> str:
    return "\n".join("  " + line for line in block.splitlines())


# ------------------------------------------------------------------ CSS (web app and docs)
def _scheme_css(s: Scheme) -> str:
    glow = f"rgba({_triplet(s.accent)}, 0.2)"
    return f"""\
  --brand-background: {s.background};
  --brand-surface: {s.surface};
  --brand-card: {s.card};
  --brand-border: {s.border};
  --brand-text: {s.text};
  --brand-text-muted: {s.muted};
  --brand-accent: {s.accent};
  --brand-accent-rgb: {_triplet(s.accent)};
  --brand-on-accent: {s.on_accent};
  --brand-highlight: {s.highlight};
  --brand-highlight-rgb: {_triplet(s.highlight)};
  --brand-gradient: linear-gradient(90deg, {s.accent}, {s.highlight});
  --brand-glow: 0 10px 15px -3px {glow}, 0 4px 6px -4px {glow};"""


def tokens_css(brand: Brand) -> str:
    """The brand as CSS custom properties, for the web app (/theme.css) and both docs sites.

    Light is the default and the system's dark setting switches it. The attribute selectors are
    the docs themes' own light/dark toggles (Zensical, then Furo), which override the system.
    """
    light, dark = _scheme_css(brand.light), _scheme_css(brand.dark)
    return f"""\
/* {HEADER} */
:root {{
  --brand-primary: {brand.primary};
  --brand-primary-rgb: {_triplet(brand.primary)};
  --brand-secondary: {brand.secondary};
  --brand-secondary-rgb: {_triplet(brand.secondary)};
  --brand-tertiary: {brand.tertiary};
  --brand-tertiary-rgb: {_triplet(brand.tertiary)};
  --brand-cream: {brand.cream};
  --brand-bar: {brand.primary};
  --brand-bar-text: {brand.cream};
  --brand-rule: linear-gradient(90deg, {brand.secondary}, {brand.tertiary});
  --brand-font: {brand.font};
  --brand-font-display: {brand.font_display};
  --brand-mono: {brand.font_mono};
  --brand-radius: 12px;
{light}
}}
@media (prefers-color-scheme: dark) {{
  :root {{
{_indent(dark)}
  }}
}}
[data-md-color-scheme="default"],
body[data-theme="light"] {{
{light}
}}
[data-md-color-scheme="slate"],
body[data-theme="dark"] {{
{dark}
}}
"""


# ------------------------------------------------------------------ SCSS (Ionic mobile app)
def _ion_colour(name: str, colour: str, on: str) -> str:
    return f"""\
  --ion-color-{name}: {colour};
  --ion-color-{name}-rgb: {_triplet(colour)};
  --ion-color-{name}-contrast: {on};
  --ion-color-{name}-contrast-rgb: {_triplet(on)};
  --ion-color-{name}-shade: {mix(colour, "#000000", 0.12)};
  --ion-color-{name}-tint: {mix(colour, "#ffffff", 0.1)};"""


def _ion_scheme(brand: Brand, s: Scheme, name: str) -> str:
    steps = "\n".join(
        f"  --ion-background-color-step-{n}: {mix(s.background, s.text, n / 1000)};\n"
        f"  --ion-text-color-step-{n}: {mix(s.text, s.background, n / 1000)};"
        for n in range(50, 1000, 50)
    )
    ink, on_ink = (brand.primary, brand.cream) if name == "light" else (brand.cream, brand.primary)
    light = _ion_colour("light", s.card, s.text).replace(
        f"-shade: {mix(s.card, '#000000', 0.12)}", f"-shade: {s.border}"
    )
    return f"""\
{_ion_colour("primary", s.accent, s.on_accent)}
{_ion_colour("secondary", ink, on_ink)}
{light}
{_ion_colour("medium", s.muted, s.background)}
  --ion-background-color: {s.background};
  --ion-background-color-rgb: {_triplet(s.background)};
  --ion-text-color: {s.text};
  --ion-text-color-rgb: {_triplet(s.text)};
  --ion-item-background: {s.surface};
  --ion-card-background: {s.surface};
  --ion-border-color: {s.border};
  --ion-toolbar-background: {brand.primary};
  --ion-toolbar-color: {brand.cream};
  --ion-toolbar-border-color: {brand.primary};
{steps}
  --brand-accent: {s.accent};
  --brand-highlight: {s.highlight};
  --brand-surface: {s.surface};
  --brand-border: {s.border};
  --brand-text-muted: {s.muted};"""


def ionic_scss(brand: Brand) -> str:
    """The brand as Ionic theme variables. Ionic draws every control (buttons, toggles, focus
    lines) in its `primary`, so that is the jade accent at the scheme's strength; toolbars are
    the navy bar; `secondary` is the navy as an ink (cream in dark) and `tertiary` the honey."""
    return f"""\
// {HEADER}
:root {{
{_ion_colour("tertiary", brand.tertiary, brand.primary)}
  --brand-bar: {brand.primary};
  --brand-bar-text: {brand.cream};
  --brand-secondary: {brand.secondary};
  --brand-rule: linear-gradient(90deg, {brand.secondary}, {brand.tertiary});
  --brand-font-display: {brand.font_display};
  --ion-font-family: {brand.font};
{_ion_scheme(brand, brand.light, "light")}
}}

// Ionic's own dark palette (palettes/dark.system.css) is replaced value for value.
@media (prefers-color-scheme: dark) {{
  :root,
  :root.ios,
  :root.md {{
{_indent(_ion_scheme(brand, brand.dark, "dark"))}
  }}
}}
"""


# ------------------------------------------------------------------ Streamlit (admin console)
def streamlit_flags(brand: Brand) -> list[str]:
    """`streamlit run` options that theme the admin console: the page follows the reader's
    light or dark setting and the sidebar is the navy bar in both."""
    options = {"theme.font": brand.font, "theme.headingFont": brand.font_display}
    for name in SCHEMES:
        s = brand.scheme(name)
        options |= {
            f"theme.{name}.primaryColor": s.accent,
            f"theme.{name}.backgroundColor": s.background,
            f"theme.{name}.secondaryBackgroundColor": s.card,
            f"theme.{name}.textColor": s.text,
            f"theme.{name}.linkColor": s.accent,
            f"theme.{name}.borderColor": s.border,
            f"theme.{name}.sidebar.primaryColor": brand.secondary,
            f"theme.{name}.sidebar.backgroundColor": brand.primary,
            f"theme.{name}.sidebar.secondaryBackgroundColor": mix(brand.primary, brand.cream, 0.1),
            f"theme.{name}.sidebar.textColor": brand.cream,
            f"theme.{name}.sidebar.linkColor": brand.secondary,
            f"theme.{name}.sidebar.borderColor": mix(brand.primary, brand.cream, 0.2),
        }
    return [part for key, value in options.items() for part in (f"--{key}", value)]


# ------------------------------------------------------------------ the about page (docs)
def contributors_html(people: list[dict]) -> str:
    """The contributors as a grid of GitHub avatars, each linked to its profile.

    One HTML block with no blank line, so Python-Markdown (the portal) and MyST (the manual)
    both pass it through as written. template.css styles it (.brand-contributors).
    """
    items = []
    for person in people:
        try:
            login, avatar, url = person["login"], person["avatar_url"], person["url"]
        except KeyError as missing:
            raise BrandError(f"contributors.json: an entry has no {missing}") from missing
        if not all(link.startswith("https://") for link in (avatar, url)):
            raise BrandError(f"contributors.json: {login}: avatar_url and url must be https://")
        name = person.get("name") or login
        avatar += ("&" if "?" in avatar else "?") + "s=160"  # 80px shown, sharp at 2x
        handle = "" if name == login else f"<small>@{html.escape(login)}</small>"
        items.append(
            f'<li><a href="{html.escape(url)}">'
            f'<img src="{html.escape(avatar)}" alt="" width="80" height="80" loading="lazy">'
            f"<span>{html.escape(name)}</span>{handle}</a></li>"
        )
    return '<ul class="brand-contributors">\n' + "\n".join(items) + "\n</ul>"


def about_page(brand_dir: Path = BRAND_DIR) -> str:
    """brand/docs/about.md with its contributors marker replaced by the people in
    brand/docs/contributors.json. A brand without the marker gets its page unchanged."""
    page = (brand_dir / "docs/about.md").read_text(encoding="utf-8")
    if CONTRIBUTORS_MARK not in page:
        return page
    source = brand_dir / "docs/contributors.json"
    try:
        people = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BrandError(f"about.md lists contributors, but contributors.json: {error}") from error
    return page.replace(CONTRIBUTORS_MARK, contributors_html(people))


# ------------------------------------------------------------------ the generated copies
def _patched(path: Path, replacements: dict[str, str]) -> bytes:
    """`path` with the first match of each pattern's group 1 replaced by the given text."""
    text = path.read_text(encoding="utf-8")
    for pattern, value in replacements.items():
        match = re.search(pattern, text)
        if not match:
            raise BrandError(f"{path.relative_to(REPO)}: nothing matches {pattern}")
        text = text[: match.start(1)] + value + text[match.end(1) :]
    return text.encode()


def outputs(brand: Brand, brand_dir: Path = BRAND_DIR) -> dict[Path, bytes]:
    """Every file outside brand/ that repeats it, with the content it should have."""
    mobile, docs = REPO / "mobile/src", REPO / "docs"
    quoted = {key: json.dumps(value, ensure_ascii=False) for key, value in {
        "site_name": brand.name,
        "site_description": f"{brand.name}: {brand.description}",
        "site_author": brand.organisation,
        "copyright": f"© {brand.year} {brand.organisation}",
    }.items()}  # fmt: skip
    files = {
        docs / "content/assets/stylesheets/brand.css": tokens_css(brand).encode(),
        docs / "zensical.toml": _patched(
            docs / "zensical.toml",
            {rf"(?m)^{key} = (.*)$": value for key, value in quoted.items()},
        ),
    }
    if not mobile.is_dir():  # an image that builds one surface (the docs) carries no mobile tree
        return files
    meta_path = mobile / "app/core/meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta |= {"name": brand.name, "tagline": brand.tagline}
    files |= {
        mobile / "theme/_brand.scss": ionic_scss(brand).encode(),
        meta_path: (json.dumps(meta, indent=2, ensure_ascii=False) + "\n").encode(),
        mobile / "index.html": _patched(
            mobile / "index.html",
            {
                r"<title>(.*?)</title>": brand.name,
                r'<meta name="theme-color" content="(.*?)"': brand.primary,
                r'<meta name="description" content="(.*?)"': brand.tagline,
            },
        ),
    }
    for key in ("logo", "mark", "favicon", "hero"):
        image = brand_dir / "images" / brand.images[key]
        files[mobile / "assets/brand" / f"{key}{image.suffix}"] = image.read_bytes()
    return files


def sync(check: bool = False) -> list[Path]:
    """Write the generated copies. Returns the files that were (or, with check, are) stale."""
    stale = []
    for path, content in outputs(load()).items():
        if not path.is_file() or path.read_bytes() != content:
            stale.append(path.relative_to(REPO))
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
    return stale
