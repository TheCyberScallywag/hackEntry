# brand/

Everything that makes this project *this* project: the name, the palette of the images (a navy
field, jade volunteers, a honey heart, cream lettering) and the images themselves. No other file
in the repository writes a brand colour or the product name by hand.

To change the brand, edit this folder (or start again from `examples/brands/neutral/`) and run
`uv run task sync`. `uv run task test` fails until every copy is current.

| File | What it holds | Read by |
|---|---|---|
| `brand.toml` | Name, tagline, palette (`[theme]`), light and dark surfaces (`[theme.light]`, `[theme.dark]`), fonts, images (`[images]`) | The web app and the admin console at start-up; `task sync` for the mobile app and the docs, which cannot read it |
| `images/` | Logo, mark, favicon (a PNG of the mark) and hero banner | Served by the web app at `/brand/`; `task sync` copies them into the mobile app, and the docs tasks into the docs |
| `docs/*.md` | The docs portal's home (`home.md`) and about page | Copied into `docs/content/` by `uv run task docs` (home.md becomes index.md) |
| `content/` | Template sample content, not used by this application | Nothing |

## Where each surface gets the brand

| Surface | Colours and fonts | Name and images |
|---|---|---|
| Web app | `/theme.css`, built from `brand.toml` on request; `static/style.css` uses only its `--brand-*` tokens | Templates read `meta` (`src/app/meta.py`); images at `/brand/` |
| Admin console | `app.serve` passes the palette to `streamlit run` as its light and dark themes | `src/app/admin/main.py` reads the same `meta` |
| Mobile app | `mobile/src/theme/_brand.scss` (generated): the Ionic colours for light and dark | `mobile/src/app/core/meta.json`, `index.html` and `assets/brand/` (generated) |
| Docs portal and manual | `docs/content/assets/stylesheets/brand.css` (generated): the same tokens as `/theme.css` | `docs/zensical.toml` site name (generated); `docs/sphinx/conf.py` reads `brand.toml` |

Every surface has the same frame: a navy bar closed by a jade-to-honey rule, the wordmark's
typeface for headings, jade for links and buttons, honey for badges. Each follows the reader's
light or dark setting.

## What `task sync` refuses

A missing image, a logo or hero without alt text, a favicon that is not a real PNG, and any
colour pair below WCAG AA contrast (4.5:1): text, muted text, accent and highlight on that
scheme's background and surface, cream on navy, and the button text on the accent.

To deploy one built image under another brand, mount a brand folder into the web and admin
containers and set `BRAND_DIR` to it: both serve that brand's name, theme and images without a
rebuild. The mobile app and the docs are static builds, so they need `task sync` and a rebuild.
