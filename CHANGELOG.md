<p align="center">
  <img src="brand/images/logo.svg" width="112" alt="Volunteer Network logo: three teal volunteers around a golden heart">
</p>

<h1 align="center">Changelog</h1>

<p align="center">
  <em>Data Volunteer Network · what changed, release by release</em>
</p>

<p align="center">
  <img alt="Latest: 0.2.0 unreleased" src="https://img.shields.io/badge/latest-0.2.0%20unreleased-ffb21c?style=flat-square&labelColor=0b1730">
  <img alt="Built with Relate" src="https://img.shields.io/badge/built%20with-Relate-13cdb5?style=flat-square&labelColor=0b1730">
  <img alt="Neo4j Hackathon 2026" src="https://img.shields.io/badge/Neo4j%20Hackathon-2026-13cdb5?style=flat-square&labelColor=0b1730">
</p>

<p align="center">
  <a href="README.md">README</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="CODE_OF_CONDUCT.md">Code of conduct</a>
</p>

---

## 0.2.0 (unreleased): Data Volunteer Network hackathon entry

### Added

- Multi-organisation volunteer network: identity, organisation opt-in and switching,
  skills/cause profile with offers and exclusions, project suggestions with scoped review and
  publication, explainable Relate-backed matching and coverage gaps.
- SQLite transactional store with an outbox projection to an embedded Relate store
  (`relate-db==1.0.1`), bundled read-only `data/demo.rl`, operator CLI and Streamlit console.
- Ionic mobile companion on a bearer-token JSON API.
- Optional AuraDB push and match-parity check.

### Changed

- Replaced the template's FastUI UI, JWT API, Neo4j server and docs pipeline.

## 0.1.0

### Other

- Template baseline (sgaffald), recorded as git tree `ecfee3ce58cd966c4ccaf375e5811969da9aece7`.

---

<p align="center">
  <sub>Cyber Scallywags · Data Volunteer Network</sub>
</p>
