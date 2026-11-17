"""The brand kit: brand/brand.toml is the one source of the name, colours and images, and
every surface (web, admin, mobile, docs) carries it."""

import re
import shutil
from pathlib import Path

import pytest

from app import brandkit
from app.brandkit import BrandError, contrast, load, mix
from tests.conftest import Web

REPO = Path(__file__).resolve().parents[1]
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def test_every_generated_copy_of_the_brand_is_current():
    stale = brandkit.sync(check=True)
    assert not stale, f"run `uv run task sync`: {[str(p) for p in stale]}"


def test_contrast_and_mix():
    assert contrast("#000000", "#ffffff") == pytest.approx(21)
    assert contrast("#31d8c5", "#31d8c5") == pytest.approx(1)
    assert mix("#000000", "#ffffff", 0.5) == "#808080"


def test_the_palette_is_readable_in_both_schemes():
    brand = load()
    assert contrast(brand.cream, brand.primary) >= brandkit.AA
    for name in brandkit.SCHEMES:
        s = brand.scheme(name)
        for colour in (s.text, s.muted, s.accent, s.highlight):
            assert contrast(colour, s.background) >= brandkit.AA
            assert contrast(colour, s.surface) >= brandkit.AA
        assert contrast(s.on_accent, s.accent) >= brandkit.AA


def _copy_of_brand(tmp_path: Path, old: str, new: str) -> Path:
    target = tmp_path / "brand"
    shutil.copytree(REPO / "brand", target)
    toml = target / "brand.toml"
    text = toml.read_text(encoding="utf-8")
    assert old in text
    toml.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('accent = "#007765"', 'accent = "#31d8c5"', "light accent on background"),
        ('accent = "#007765"', 'accent = "teal"', "not a #rrggbb colour"),
        ('accent = "#007765"\n', "", "accent"),
        ('logo_alt = "', 'logo_note = "', "alt text"),
        ('mark = "mark.svg"', 'mark = "missing.svg"', "no such file"),
        ('favicon = "favicon.png"', 'favicon = "mark.svg"', "real PNG"),
        ('scheme = "auto"', 'scheme = "sepia"', "auto, light or dark"),
    ],
)
def test_a_broken_brand_is_refused(tmp_path, old, new, message):
    with pytest.raises(BrandError, match=message):
        load(_copy_of_brand(tmp_path, old, new))


def test_the_neutral_example_brand_still_loads():
    brand = load(REPO / "examples/brands/neutral")
    assert brand.name == "Example Project"
    assert "--brand-accent: #1c6f65" in brandkit.tokens_css(brand)


def test_tokens_cover_light_dark_and_the_docs_toggles():
    brand = load()
    css = brandkit.tokens_css(brand)
    assert f"--brand-bar: {brand.primary};" in css
    assert css.count(f"--brand-accent: {brand.light.accent};") == 2  # :root and the docs toggle
    assert css.count(f"--brand-accent: {brand.dark.accent};") == 2  # system dark and the toggle
    assert "@media (prefers-color-scheme: dark)" in css
    assert '[data-md-color-scheme="slate"]' in css and 'body[data-theme="dark"]' in css


def test_the_mobile_theme_uses_the_same_colours():
    brand = load()
    scss = brandkit.ionic_scss(brand)
    assert f"--ion-color-primary: {brand.light.accent};" in scss
    assert f"--ion-color-primary: {brand.dark.accent};" in scss
    assert scss.count(f"--ion-toolbar-background: {brand.primary};") == 2
    assert f"--ion-background-color: {brand.dark.background};" in scss
    allowed = {brand.primary, brand.secondary, brand.tertiary, brand.cream, "#ffffff"}
    for s in (brand.light, brand.dark):
        allowed |= {s.background, s.surface, s.text, s.muted, s.border, s.accent, s.highlight}
    literal = {c for c in HEX.findall(scss) if "step" not in c}
    assert {brand.primary, brand.secondary, brand.tertiary} <= literal
    assert allowed & literal


def test_the_admin_console_is_started_with_the_brand_theme():
    brand = load()
    flags = brandkit.streamlit_flags(brand)
    options = dict(zip(flags[::2], flags[1::2], strict=True))
    assert options["--theme.light.primaryColor"] == brand.light.accent
    assert options["--theme.dark.backgroundColor"] == brand.dark.background
    assert options["--theme.dark.sidebar.backgroundColor"] == brand.primary
    assert options["--theme.headingFont"] == brand.font_display


def test_the_web_app_serves_the_brand(web: Web):
    brand = load()
    css = web.client.get("/theme.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert css.text == brandkit.tokens_css(brand)
    page = web.client.get("/").text
    assert f"<title>{brand.tagline} · {brand.name}</title>" in page
    assert 'href="/theme.css"' in page and brand.images["hero_alt"] in page
    assert f'<meta name="theme-color" content="{brand.primary}">' in page
    for image in ("logo", "mark", "favicon", "hero"):
        assert web.client.get(f"/brand/{brand.images[image]}").status_code == 200


def test_hand_written_styles_hold_no_brand_colours():
    """Colours live in brand.toml. The web stylesheet keeps only its status tints (the :root
    blocks at its top) and white on the danger button; the docs and mobile styles keep none."""
    web = (REPO / "src/app/web/static/style.css").read_text(encoding="utf-8")
    assert set(HEX.findall(web.split("* { box-sizing", 1)[1])) == set()
    for path in (
        "docs/content/assets/stylesheets/template.css",
        "docs/content/assets/stylesheets/zensical.css",
        "docs/sphinx/_static/furo.css",
        "mobile/src/theme/variables.scss",
        "mobile/src/global.scss",
        "mobile/src/app/app.component.scss",
    ):
        assert not HEX.findall((REPO / path).read_text(encoding="utf-8")), path
