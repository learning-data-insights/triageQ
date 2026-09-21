# triageQ — Changelog

Internal version numbers below 1.0.0 are a remapping of this project's original,
unpublished build history (v1–v18) onto semantic versioning, compiled from development
chat history. Dates reflect when each round of work took place.

---

## 1.0.0 — Public release: triageQ
**2026-09-21**

The project's first public release: a full rebrand and cleanup of the final internal build
(0.1.8) for open, general-purpose use. No functional changes beyond what's listed here — the
local file library and everything else from 0.1.8 carries forward unchanged.

- Renamed the project to **triageQ** throughout: window title, header, settings file
  (`~/.triageq_settings.json`), default repository directory (`~/triageq`), outbound
  User-Agent sent to Unpaywall/OpenAlex, module docstrings, README, and this changelog
- Files un-versioned: `app_v18.py` → `app.py`, `requirements_v18.txt` → `requirements.txt`,
  `README_v18.md` → `README.md`
- **Removed the built-in default criteria profile.** triageQ now ships with no criteria of
  its own — a fresh install starts with no active profile, the header and screening tabs
  say so plainly, and Analyze / Run Batch Analysis refuse to run until a profile is created
  or imported. This replaces the previous behavior of seeding a project-specific profile to
  disk on first run
- Added an **About card** under Settings with the AI-development disclosure (built with
  Claude Sonnet 5; experimental, first-pass-only; human review expected) and the CC BY-SA
  4.0 license notice; mirrored at the top of the README
- Fixed a **Batch Upload layout bug**: the tab's content — including the log — could run
  off the bottom of the window on shorter screens with no way to reach it. The whole tab is
  now wrapped in the same scrollable-canvas pattern already used on the Settings tab
- README rewritten: removed all project-specific criteria documentation (the old "built-in
  profile" section), added a worked "Getting started: your first profile" example using
  generic, illustrative criteria, and updated every stale path/filename reference
- Confirmed no hardcoded personal name, email address, or file path existed anywhere in the
  codebase — the contact-email and repository-location fields were already blank,
  user-entered settings
- **Bug fix, found during release testing:** the "Review Compiled Criteria" / "Edit
  Criteria" dialog crashed with a `TclError` any time it had compiler notes to show — the
  notes banner passed an asymmetric `pady` tuple as a widget-constructor argument, which
  Tkinter only accepts on `pack()`/`grid()`, not on the widget itself. This is the dialog
  every new profile passes through, compiled or imported, so it was exercised end-to-end
  under a virtual display (Xvfb) as part of this release: profile creation from pasted
  text, JSON import, duplication, switching the active profile, and deleting profiles down
  to zero (confirming the app returns cleanly to the no-profile state introduced above,
  with no crash and no stale reference to a deleted profile)

---

## 0.1.8 — Local file library
**2026-08-13 – 2026-08-14**

- New `local_library.py`: matches local PDF/PPTX files to CSV rows by content — DOI, title
  text, or filename resemblance — never by relying on filenames to carry meaning
- Tiered confidence system (HIGH/MEDIUM/LOW/NONE): only deterministic identifier matches
  auto-accept; fuzzy matches require human confirmation before anything is screened
- Bijective assignment (one file per row, one row per file), greedy scored candidate
  matching; near-ties and author conflicts are forced into review rather than resolved
  automatically; DOI matches are exempt from author-conflict demotion
- PowerPoint decks accepted and screened as `screening_basis="slides"`; optional
  conservative mode turns omissions into UNCLEAR rather than NO
- New review dialog for confirming ambiguous matches, with per-file evidence and manual
  reassignment
- `file_mapping_log.csv` audit trail in the repository folder; mappings exportable as a
  filled-in batch CSV for exact reruns
- Batch pipeline redesigned into a single pass with explicit source precedence per row:
  URL → verified `file_path` → confirmed folder-scan match → abstract-only fallback —
  replacing an earlier separate "pre-flight" step that ran before the online retrieval it
  depended on
- Shared DOI parsing extracted into `doi_utils.py`, removing duplicate logic that had been
  copied between `pdf_resolver.py` and `local_library.py`
- `pypdf` dependency clarified: it verifies local-file identity, independent of the
  screening pipeline itself, which sends raw PDF bytes to the API directly

---

## 0.1.7 — Configurable criteria + automated PDF retrieval
**2026-08-12**


- Replaced hardcoded screening logic with a **criteria profile** architecture —
  inclusion/exclusion rules, prompt rendering, decision logic, CSV shape, and output schema
  all derive from a structured JSON profile (new `criteria_profiles.py`)
- Built compiler flow: raw text → Claude-compiled draft → **mandatory human review** before
  use; validator enforces that every criterion must carry `exclude_if` conditions before it
  can be saved
- Profile versioning **forks rather than overwrites** when a profile is edited after use
- Added a local decision-rule engine that recomputes recommendations post-API-call,
  preventing model drift from silently affecting the repository
- New PDF resolution waterfall (new `pdf_resolver.py`): direct link → arXiv → Unpaywall →
  OpenAlex → Semantic Scholar → Europe PMC → `citation_pdf_url` scraping, with per-source
  toggles, session-level DOI caching, and rate limiting
- Added abstract-only fallback mode (forces UNCLEAR verdicts on anything not explicitly
  stated, by design produces a higher `MANUAL_REVIEW` rate)
- README and requirements rewritten to document the profile system and a full schema
  reference

---

## 0.1.6 — Outcome-based evidence for Criterion 3
**2026-08-12**

- Following a review of decision discrepancies between builds and direct SME guidance,
  Criterion 3 was expanded to accept two independently sufficient paths: direct
  output-evaluation metrics (as before) and outcome-based evidence (RCTs,
  quasi-experimental designs, effect sizes, learning-gains statistics)
- Updated the scoring criterion for student-produced work to explicitly allow synthetic or
  simulated work, not only human-produced work

---

## 0.1.5 — Decision-order bug fix and cleanup
**2026-08-12**

- **Root-cause fix:** `overall_recommendation` moved to the *last* field in the JSON output
  schema. Previously the model committed to a verdict before reasoning through the
  criteria, occasionally self-correcting in the notes fields without being able to revise
  the recommendation it had already written. Ordering the schema so the verdict comes last
  forces full analysis first
- Fixed trailing-comma JSON parse failures with a regex cleanup pass before parsing
- Removed six pieces of accumulated dead code: an unused `io` import, an unused palette
  key, a dead `if False` branch, a stale exception handler left over from a removed local
  backend, an unused tuple element, and a duplicated sort-key function (extracted to one
  shared implementation)

---

## 0.1.4 — Deterministic decision rules
**2026-08-12**

- Closed a loophole where the model could return `MANUAL_REVIEW` despite all-required-YES
  verdicts; decision rules rewritten as explicit, numbered, strictly deterministic logic
- Made the domain-classification field required with an "Unknown" fallback, both in the
  prompt and as a code-level safeguard, so the repository never stores a blank value
- Established the standing practice of updating the README with every application change

---

## 0.1.3 — Model update, confidence fallback, configurable repository location
**2026-08-12**

- Model updated to the newest available Opus at the time
- Added a hard prompt rule that confidence must never be blank, plus a code-level `or
  "Medium"` fallback as a second line of defense
- Repository location became user-configurable from Settings (a **Change** button), with
  all path globals updating at runtime via a new `set_repo_dir()` function

---

## 0.1.2 — Visual redesign and dependency cleanup
**2026-08-12**

- Full visual redesign: warm off-white palette, refined header and tab styling, signal
  colors (green/red/amber) reserved exclusively for verdict communication
- Removed the `pdfplumber` dependency, unused since papers began being sent to the API as
  raw bytes rather than extracted text

---

## 0.1.1 — Inclusion cutoff and README rewrite
**2026-08-12**

- Inclusion cutoff moved from 2022 to 2023
- Full README rewrite to match current behavior

---

## 0.1.0 — Automatic metadata extraction
**2026-08-12**

- Every PDF now has its title, authors, publication year, venue, DOI, and abstract
  extracted automatically; the paper identifier became the only manually-entered field
- Batch CSV template simplified accordingly
- Repository sorted ascending by paper identifier, numerically when identifiers are numbers

---

## 0.0.9 — JSON parsing robustness
**2026-08-12**

- Fixed parse failures on responses with trailing text after the JSON object by switching
  to a decoder that reads exactly one JSON object and ignores anything after it

---

## 0.0.8 — System prompt overhaul
**2026-08-12**

- Added a facts-only language rule prohibiting qualitative judgments in reasoning fields
- Added cross-cutting exclusions and an explicit confidence-calibration section to the
  prompt
- Added automatic publication-year extraction

---

## 0.0.7 — Local-model backend removed
**2026-08-12**

- Removed the local-model (Ollama) backend entirely; API-only from this point forward
- Fixed a crash affecting the progress timer on failed or timed-out analyses

---

## 0.0.6 — Local-model performance tuning
**2026-08-12**

- Smarter text extraction for the (since-removed) local-model path: stripped references
  sections and noise lines before truncation, kept the front and back of long documents
  rather than just the front
- Set generation temperature to fully deterministic

---

## 0.0.5 — Live progress indicators
**2026-08-12**

- Added live elapsed-time timers and running counts/percentages to both the single-item
  and batch screening tabs

---

## 0.0.4 — Batch stop control and stability fixes
**2026-08-12**

- Added a Stop button for in-progress batch runs
- Fixed a Windows path-separator bug and added exception handling for mid-batch stops

---

## 0.0.3 — Institutional branding removed
**2026-08-12**

- Removed all references to the original academic host institution from the header,
  system prompt, and README

---

## 0.0.2 — Stability and compatibility fixes
**2026-08-12**

- Added an optional free local-model backend as an alternative to the paid API (later
  removed in 0.0.7)
- Fixed invisible radio buttons on Windows (a `selectcolor` rendering bug)
- Fixed a `UnicodeDecodeError` on CSV files exported from Excel, by trying several
  encodings in sequence before falling back to lossy replacement

---

## 0.0.1 — Initial build
**2026-08-12**

- Initial prototype: desktop GUI (four tabs), API-based document analysis sending raw PDF
  bytes, JSON + CSV repository storage, a hardcoded three-criterion screening rubric,
  single-item and CSV batch upload

---

## Notes on this changelog

- Versions 0.0.1 through 0.1.7 document a single unpublished internal build session
  (2026-08-12); 0.1.8 documents a later session (2026-08-13 – 2026-08-14). Both are
  reconstructed from chat history. 1.0.0 is the first version distributed outside the
  original project.
- The one path that has never been exercised in the build/test environment across every
  version is live network PDF retrieval, since it depends on external services unreachable
  from that environment.
