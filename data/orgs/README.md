# data/orgs/

Logos, hero images and short profiles for the demo organisations, shown on the `/showcase` page
and the organisation directory. `profiles.json` holds the text; each folder is named after an
organisation key and holds that organisation's images. The web app serves the folders at
`/org-assets/`.

**These files are not covered by the repository's MIT or CC0 licences.** The logos and
photographs belong to the organisations named below and were copied from their public websites
on 2026-11-17 to identify them in a demo. No endorsement or partnership is implied. Ask each
organisation before using them anywhere public, and delete a folder (and its entry in
`profiles.json`) if you have not: the app then falls back to the organisation's initials.

| File | Source | Owner |
|---|---|---|
| `datakind-uk/logo.png` | Site header logo, <https://www.datakind.org.uk> (converted from WebP) | DataKind UK |
| `datakind-uk/hero.jpg` | Home page banner, "Staff and volunteers at an in-person event listen to Herts Young Homeless presenting their data project" (converted from WebP) | DataKind UK |
| `cyber-scallywags/logo.png` | <https://cyberscallywags.uk/static/images/logo/logo.png>, resized from 1024 to 512 px | Cyber Scallywags |
| `cyber-scallywags/hero.svg` | Drawn for this demo in the logo's colours; the site has no banner photograph | This repository (MIT) |
| `interlinkrct/logo.png` | Site icon, <https://interlinkrct.org.uk/wp-content/uploads/2022/02/cropped-Interlink-01.png> | Interlink RCT |
| `interlinkrct/hero.jpg` | Home page share image, <https://interlinkrct.org.uk/wp-content/uploads/2021/09/Main-image.jpg> (converted from WebP) | Interlink RCT |

The profile text in `profiles.json` is a summary of each organisation's own "about" page
(`source` in each entry). The projects listed beside it in the app are invented demo data.

A profile applies only while the organisation keeps the `name` in its entry. Renaming an
organisation with `ORG_NAMES` therefore drops its logo, photograph and description as well.
