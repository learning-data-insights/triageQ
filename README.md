# triageQ

An open, human-in-the-loop tool for sorting large volumes of content against custom
criteria. Licensed Apache 2.0.

This reference build screens academic papers, returning an **INCLUDE / EXCLUDE /
MANUAL_REVIEW** recommendation for each one, with per-criterion verdicts, supporting
evidence quoted from the paper, and a confidence level. It is a screening assistant for
human reviewers, not a replacement for them — every recommendation is meant to be worked
through by a person, not acted on automatically.

triageQ ships with **no criteria of its own**. The first thing any install needs is a
criteria profile — see [Screening criteria](#screening-criteria) to create one, or
[Getting started: your first profile](#getting-started-your-first-profile) for a worked
example.

> This application was developed with AI assistance (Claude Sonnet 5). It is experimental
> software intended for initial triage only; recommendations should be verified by a human
> reviewer. The same notice is available in-app under **Settings → About**.

---

## Contents

- [Files](#files)
- [Hosted web build](#hosted-web-build)
- [Setup](#setup)
- [First-time configuration](#first-time-configuration)
- [Choosing a model provider](#choosing-a-model-provider)
- [Visual identity](#visual-identity)
- [Screening criteria](#screening-criteria)
- [Getting started: your first profile](#getting-started-your-first-profile)
- [PDF retrieval](#pdf-retrieval)
- [Local file library](#local-file-library)
- [Screening a single paper](#screening-a-single-paper)
- [Batch upload](#batch-upload)
- [Repository](#repository)
- [Decision rules](#decision-rules)
- [Confidence levels](#confidence-levels)
- [Output fields](#output-fields)
- [Criteria profile schema reference](#criteria-profile-schema-reference)
- [Example profile](#example-profile)
- [Troubleshooting](#troubleshooting)

---

## Files

The application is seven Python files, plus two asset folders, that must all live in the
same folder:

| File | Contains |
|------|----------|
| `app.py` | GUI, orchestration, repository management. This is what you run. |
| `criteria_profiles.py` | Profile schema, validation, prompt rendering, the criteria compiler, decision-rule engine |
| `pdf_resolver.py` | The online PDF resolution waterfall and abstract-only metadata fallback |
| `local_library.py` | Local file fingerprinting and file-to-paper matching |
| `doi_utils.py` | DOI parsing shared by `pdf_resolver.py` and `local_library.py` |
| `model_providers.py` | Anthropic / OpenAI / OpenAI-compatible provider adapters — see [Choosing a model provider](#choosing-a-model-provider) |
| `branding.py` | Palette, bundled-font loading, and asset paths — see [Visual identity](#visual-identity) |
| `screening.py` | The analysis call, repository record construction, and repository read/write — shared by the desktop and web front ends |
| `fonts/` | Bundled Inter font files (OFL-licensed) |
| `assets/` | Logo, icon, and Learning Data Insights badge images |

`pdf_resolver.py` and `local_library.py` can also be run directly from the command line for
testing — see [Troubleshooting](#troubleshooting). `criteria_profiles.py`, `doi_utils.py`,
`model_providers.py`, and `branding.py` have no CLI of their own; they're shared
dependencies, not tools you run.

The hosted web build adds `web_app.py`, `web_backend.py`, and `net_guard.py` — see
[Hosted web build](#hosted-web-build).

---

## Hosted web build

`web_app.py` is a browser front end (Streamlit) over the same screening engine, for
running triageQ as a public reference deployment. Each visitor gets a private, temporary
workspace; they paste their own API key or use an optional, capped organisation key.
Local-folder scanning is replaced by uploads. Everything is configured through
environment variables — see `.env.example`.

| File | Contains |
|------|----------|
| `web_app.py` | The web UI. Run with `streamlit run web_app.py` |
| `web_backend.py` | Workspaces, limits, demo-key quota, background batch jobs, expiry cleanup |
| `net_guard.py` | Refuses outbound requests to private/internal addresses on a public server |
| `requirements-web.txt` | Web dependencies (no Tkinter needed) |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile` | Container build, plus Caddy for automatic HTTPS |

**[DEPLOY.md](DEPLOY.md)** walks through hosting it on AWS Lightsail step by step.

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

> On Windows, use `python -m pip install -r requirements.txt` if `pip` is not recognized.

| Package | Why |
|---------|-----|
| `requests` | HTTP for PDF retrieval |
| `beautifulsoup4` | Parsing publisher landing pages for PDF links |
| `anthropic` | Only needed if you'll use the Anthropic provider |
| `openai` | Only needed for the OpenAI provider, or the OpenAI-compatible provider (local models, self-hosted servers, any `/chat/completions` endpoint) |
| `pypdf` | Reading a local PDF's first pages to identify which paper it is, and — for text-only providers — extracting the full text sent for screening |
| `python-pptx` | Reading PowerPoint decks |
| `tkinterdnd2` | Optional — drag-and-drop onto the Batch tab |

`beautifulsoup4` is technically optional — the resolver falls back to regex parsing if it
is missing — but it catches more link patterns, so install it.

`anthropic` and `openai` are each imported lazily, only when you actually select that
provider in Settings, so it's fine to install just the one you plan to use. Installing both
is harmless and simplest if you might switch later — the OpenAI-compatible provider (local
models included) also depends on the `openai` package, since it's the same wire format.

`pypdf` and `python-pptx` are only used by the local file library, but the Batch tab
imports them at startup, so install both.

**LibreOffice (not a pip package, optional).** Used to convert PowerPoint decks to PDF
before screening, which preserves figures and charts that text extraction drops. It is
also the only way to open legacy binary `.ppt` files. Without it, `.pptx` decks fall back
to slide-text extraction and `.ppt` files are reported unreadable.

```
macOS     brew install --cask libreoffice
Windows   https://www.libreoffice.org/download/
Linux     sudo apt install libreoffice
```

**Settings → Local File Library** shows a ✓/✗ list of which of these are actually present
on your machine, so you never have to guess whether a capability is available.

### 2. Run the application

```bash
python app.py
```

---

## First-time configuration

1. Open the **Settings** tab (it scrolls — there are several cards)
2. In **Model Provider**, pick Anthropic, OpenAI, or an OpenAI-compatible endpoint, and
   fill in its key/model (or base URL) fields. See
   [Choosing a model provider](#choosing-a-model-provider) if you're not sure which
3. In **Screening Criteria**, create or import your first profile — triageQ ships with none.
   See [Screening criteria](#screening-criteria) below
4. In the **PDF Retrieval** card, enter a **contact email**. Unpaywall requires one and
   OpenAlex uses it to give you faster responses. Any real address you own is fine
5. Optionally click **Change** in the Repository card to set a custom save location

**Cost:** varies by provider and model — check their pricing page. As a rough baseline,
Claude Opus ran ~$0.01–0.03 per paper (200 papers ≈ $4–6 total); a local model has no
per-call cost at all.

### What persists between sessions

Settings are saved to `~/.triageq_settings.json`:

- Repository location
- Active criteria profile
- Which provider is selected, and its model name (and base URL, for OpenAI-compatible)
- All PDF retrieval settings, including the contact email
- Abstract-only fallback on/off

**API keys are never written to disk.** Each provider has its own key field, held only in
memory, and you re-enter it each session — switching providers mid-session doesn't lose
whatever you'd already typed into the other one, but restarting the app does.

---

## Choosing a model provider

triageQ's prompts are plain text — nothing about the criteria compiler or the screening
prompt is written for any one model. What differs between providers is how the request is
shaped, and whether the provider can read a PDF **natively**.

### PDF vision vs. text extraction

Reading a PDF "natively" means the model sees the paper's own layout — figures, tables,
multi-column text — the way you would looking at the page. That matters for papers where
the evidence lives in a table or a chart rather than a sentence.

| Provider | PDF vision | What happens instead if not |
|----------|:----------:|------------------------------|
| **Anthropic** | ✓ | — |
| **OpenAI** | ✓ (vision-capable models only) | — |
| **OpenAI-compatible** (local models, self-hosted servers, other aggregators) | ✗ | triageQ extracts the PDF's text locally with `pypdf` and screens from that instead |

The OpenAI-compatible option exists specifically because there's no reliable, universal way
to send a PDF to an arbitrary `/chat/completions` server — some hosted aggregators pass
one through, most self-hosted model servers don't implement anything like it at all. Rather
than guess at a given server's capabilities, triageQ treats every OpenAI-compatible
endpoint as text-only and is upfront about it: the **Model Provider** card in Settings
shows a live note on whether the active provider has PDF vision, and every screened record
carries a `pdf_vision_used` column (`yes` / `no` / blank if no PDF was ever involved) so
you can always tell which papers were read natively and which were screened from extracted
text.

Text extraction is a real degradation for tables and figures, but it is not a silent one,
and abstracts/full running text extract cleanly either way. A scanned PDF with no text
layer can't be screened by a text-only provider at all — you'll get a clear error rather
than an empty or wrong result.

### Anthropic

The original, and the only provider tested against the built-in decision-rule engine during
development. Get a key at console.anthropic.com → API Keys.

### OpenAI

Enter your API key and a vision-capable model name (check
platform.openai.com/docs/models for what's current — triageQ doesn't pin a default, since
model availability changes independently of this tool).

### OpenAI-compatible

Any server implementing OpenAI's `/chat/completions` API. Fill in:

- **Base URL** — e.g. `http://localhost:11434/v1` for Ollama's OpenAI-compatible endpoint,
  `http://localhost:8000/v1` for a typical vLLM server, or a hosted aggregator's endpoint
  (OpenRouter, Together, Groq, etc.)
- **API key** — many local servers ignore this entirely; leave it blank and triageQ sends a
  placeholder so the request still goes through
- **Model** — whatever name your endpoint expects

Because this path is text-only, a local model with a reasonably large context window
matters more here than it does for the vision providers — you're sending the paper's full
extracted text, not a compact document reference.

---

## Visual identity

triageQ's colors, logo, icon, and typography come from a reference brand sheet and are
implemented in `branding.py`, not hardcoded inline in `app.py`.

### Palette

| Color | Hex | Used for |
|-------|-----|----------|
| Deep Blue | `#244467` | Chrome — the header rule and every dialog's title bar |
| Brand Blue | `#367EC5` | Primary buttons and interactive accents |
| Teal | `#4B9F87` | Informational console tags |
| Amber | `#EFBC54` | Accent text/fills on dark bars, selection, progress — kept separate from verdict amber |
| Lavender | `#9E92C7` | "Read this, it needs judgment" callouts — currently just the criteria compiler's notes banner |

**Verdict colors (INCLUDE/EXCLUDE/MANUAL_REVIEW green/red/amber) are deliberately not part
of this palette swap.** They keep their original stoplight meaning — instant legibility
mattered more there than palette purity.

### Typography

Inter, per the brand sheet. There's no such thing as a live CDN font link in a Tkinter
desktop app — the equivalent here is bundling the actual font files
(`fonts/Inter.ttf`, `fonts/Inter-Italic.ttf`, OFL-licensed, see `fonts/OFL.txt`) and loading
them for the running process only.

- **Windows:** loaded privately at startup (`AddFontResourceExW`, `FR_PRIVATE`) — no
  install, no admin rights, nothing left behind when the app closes.
- **macOS / Linux:** not implemented in this reference build. `branding.py` checks whether
  Inter is already installed system-wide and uses it if so; otherwise it falls back to a
  close system font (Helvetica Neue on macOS, generic Helvetica elsewhere) rather than
  failing. Installing Inter system-wide (fonts.google.com/specimen/Inter) gets you the
  exact typography on these platforms too.

Either way, `branding.resolve_font_family()` always returns something usable, and every
font reference in the app reads from that one resolved value — nothing hard-fails on a
missing font.

### Logo, icon, and attribution

`assets/logo.png` (the header wordmark) and `assets/icon.ico` / `icon_256.png` (window and
taskbar icon) are generated from the reference brand sheet. The wordmark's background was
keyed transparent for the header; the icon was rendered at seven sizes into one `.ico` for
crisp taskbar display. `assets/ldi_badge_28.png` and `ldi_badge_44.png` are the Learning
Data Insights mark (a navy circle with a white waveform icon, transparent padding) resized
for the header and About-card contexts it appears in.

---

## Screening criteria

Criteria are no longer hardcoded. Each review is a **criteria profile** — a structured
definition of its criteria, boundary rules, decision logic, and output shape. The system
prompt, the JSON schema the model returns, the repository columns, and the decision rules are
all generated from the active profile.

triageQ ships with **no profile of its own**. Every review's criteria are something you
define — from scratch, from an existing protocol, or imported from a colleague. Until a
profile exists, the app shows "No criteria profile yet" in the header and the Analyze /
Run Batch Analysis buttons refuse to run, with a message pointing you back here.

Manage profiles in **Settings → Screening Criteria**. The active profile is shown in the
window header, on the Single Paper tab, and on the Batch Upload tab, so you always know
what you are screening against.

### Creating a profile from your own criteria

Click **New from Text…**, paste your inclusion and exclusion criteria — a protocol excerpt,
a PICO statement, or plain prose — and click **Compile Criteria**. You can also load a
`.txt`, `.md`, or `.docx` file into the box first.

Your configured model converts the text into a structured profile and shows it for review before it can
screen anything. **This review step is deliberate and not skippable.**

Here is why. Free-text criteria almost always state what to *include* and leave what to
*exclude* unstated. A screener with no exclusion rules over-includes — it accepts adjacent
topics, papers that merely discuss the subject rather than study it, and papers where the
intervention of interest is incidental. This was the single largest source of disagreement
with human reviewers during development of the original criteria, and it took explicit
YES-if/NO-if boundary rules to fix.

So the compiler is instructed to infer exclusion rules even where your source text has none,
and to report every one it added. Those appear as **compiler notes** in an amber banner
above the editor. Read them. They are the rules you did not write, and they will shape
every screening decision the profile makes.

The validator refuses to save any criterion that has no `exclude_if` conditions.

### Importing an existing profile

**Import File…** accepts:

| File type | Behavior |
|-----------|----------|
| `.json` | Treated as a structured profile, validated, opened in the editor |
| `.txt`, `.md`, `.docx` | Treated as raw criteria text, sent to the compiler |

This is how you share a profile between teams: **Export…** on one machine, **Import File…**
on another.

### Editing, versioning, and audit

**Edit…** opens the profile JSON with three actions:

- **Validate** — structural check with specific error messages
- **Preview Prompt** — renders the exact system prompt the current JSON produces, before saving
- **Save and Activate**

If you edit a profile that has already screened papers, the tool bumps the version
(1.0 → 1.1) rather than modifying it in place. Existing records keep their original version
stamp, so a decision made in March stays interpretable in June. Every repository record
carries `profile_id` and `profile_version`.

**Preview Prompt** is also available from the Screening Criteria card for the active
profile. Use it when you need to state in a methods section exactly what the screener was
instructed to do.

### Decision rules are enforced locally

After every API call, the tool recomputes the recommendation from the criterion verdicts
using the profile's decision rules. If the model's own `overall_recommendation` disagrees, the
locally computed value wins and the correction is recorded in `additional_notes` and shown
in the results panel.

The prompt still states the rules — this is a second line of defense so the repository does
not depend on the model applying them correctly.

---

## Getting started: your first profile

A minimal worked example, to make the abstract description above concrete. Say you're
screening papers for a review on remote-work productivity tools, and your protocol says:
"include studies that measure the effect of a specific software tool on individual output,
using a quantitative outcome measure."

1. In **Settings → Screening Criteria**, click **New from Text…**
2. Paste something like:

   > Include peer-reviewed studies that evaluate a named remote-work or collaboration
   > software tool's effect on individual employee output, using a quantitative outcome
   > measure (task completion rate, output volume, self-reported productivity score, etc.).
   > Exclude studies about workplace policy (e.g. hybrid schedules) with no specific tool
   > involved, studies with only qualitative or perception-based outcomes, and vendor
   > white papers.

3. Click **Compile Criteria**. It returns a structured draft — likely two criteria
   ("Named Tool Evaluated" and "Quantitative Outcome Measure") — each with explicit
   `include_if` and `exclude_if` conditions, plus a list of **compiler notes**: exclusion
   rules it inferred that weren't in your original text (for example, excluding studies
   where the tool is mentioned but not actually evaluated).
4. Read the compiler notes. They're the rules you didn't write, and they'll shape every
   screening decision from here on. Edit anything that doesn't match your intent.
5. Click **Save and Activate**. The profile now appears in the header and both screening
   tabs, and you're ready to analyze papers against it.

This same flow works for any domain the tool is pointed at — the criteria above are just
an example, not a preset. See [Criteria profile schema reference](#criteria-profile-schema-reference)
if you'd rather hand-write or script a profile instead of compiling one from prose.

---

## PDF retrieval

Give the tool a link or a DOI and it locates an open-access PDF itself. Configure this in
**Settings → PDF Retrieval**.

### Resolution order

| Step | Source | Notes |
|------|--------|-------|
| 1 | Direct link | The URL already is a PDF |
| 2 | arXiv | Recognizes arXiv IDs and `/abs/` URLs |
| 3 | Unpaywall | Requires the contact email. Usually the best hit rate. |
| 4 | OpenAlex | `best_oa_location` and OA URLs |
| 5 | Semantic Scholar | `openAccessPdf` field |
| 6 | Europe PMC | Anything in the PMC open-access subset |
| 7 | Landing page scrape | `<meta name="citation_pdf_url">`, then PDF-shaped links |

Each source can be toggled off individually. Scraping is deliberately last — the hard part
is knowing where a free copy lives, not parsing HTML.

Every step is logged. On the Single Paper tab the trail appears under the source card; in
batch runs it appears in the log when retrieval fails.

### What this does and does not do

- **Open-access copies only.** Nothing here bypasses a paywall. Expect roughly 50–70%
  coverage on an education-research corpus, lower for Elsevier- or Springer-heavy sets.
- **No shadow libraries.** Sci-Hub and equivalents are not used and will not be added.
- Some publishers block automated requests regardless of how polite the client is.
- Requests are rate limited, identify themselves with your contact email, and DOI lookups
  are cached within a session.

Papers that cannot be resolved still need a manual upload. That is expected, not a bug.

### Optional: OpenAlex cached PDFs

OpenAlex serves cached PDFs for a large share of open-access literature. This is a **paid**
endpoint (roughly $0.01 per file, with a free key allowing a small daily allowance), so it
is **off by default**. Enter an OpenAlex API key and tick the box to use it as a last resort
before giving up.

Metadata lookups from OpenAlex remain free and are unaffected by this setting.

### Testing retrieval

The **Resolve** button in the PDF Retrieval card takes a DOI or link and shows the full
attempt trail without screening anything. Use it to confirm your setup before a batch run.
`10.7717/peerj.4375` is a reliable gold-OA test case.

### Abstract-only fallback (off by default)

When enabled, papers with no retrievable PDF are screened from title and abstract instead
of being skipped.

Stage-1 screening on title and abstract is standard systematic review practice, and it
avoids most PDF retrieval work. The prompt changes in this mode: verdicts default to
UNCLEAR for anything the abstract does not explicitly state, because abstracts routinely
omit metrics, model names, and design details that would satisfy a criterion. Absence of
evidence in an abstract is not evidence of absence.

The practical consequence is a **higher MANUAL_REVIEW rate**, which is correct behavior for
stage-1 screening. These records are stamped `screening_basis: abstract_only` and flagged in
the Repository tab's Basis column, in the results panel, and in the batch summary.

If your MANUAL_REVIEW rate in this mode runs above roughly 60%, the fallback is not buying
you much over just fetching the PDFs.

---

## Local file library

For papers online retrieval cannot reach: interlibrary loan copies, author-supplied
manuscripts, conference proceedings, embargoed postprints. You hold the files; their
filenames are whatever the source called them (`s41539-023-00191-w.pdf`,
`download (4).pdf`, `Final_submission_v3.pptx`).

The tool identifies which paper each file is by reading the file, then comparing what it
finds against the metadata behind that row's `url`. Paper IDs are yours and internal, so
they are never expected to appear in a filename.

### How it fits into a batch run

Local files **fill gaps only**, and they are consulted last. Every row's `url` is tried
first — never skipped just because `file_path` is also filled in. The sequence, all within
a single **Run Batch Analysis** pass, is:

```
Run Batch Analysis, per row:

  1. Try the row's url                     ──▶  found it? screen it, done.
                                                        │ not found
                                                        ▼
  2. Row has a file_path? Load it, verify   ──▶  matches known title/DOI (or
     it against title/DOI (CSV, repository,      nothing to compare against)?
     or whatever the failed url attempt           screen it, done.
     turned up)                                        │ mismatch
                                                        ▼
                                                  screen it anyway, but force
                                                  MANUAL_REVIEW and log why
                                                        │ no file_path at all
                                                        ▼
  3. Confirmed match from an earlier        ──▶  found it? screen it, done.
     folder scan (see below)
                                                        │ still nothing
                                                        ▼
  4. Abstract-only fallback (if enabled)    ──▶  screen from title + abstract,
                                                  or skip the row as an error
```

Retrieval runs first for a second reason beyond correctness: it is free even when it fails.
`resolve_pdf` accumulates title, authors, year, and DOI as it walks the waterfall, and keeps
them even when no PDF is found — so a failed attempt still hands step 2 something to verify
`file_path` against, at no extra network cost.

**Why a `file_path` gets checked instead of trusted outright.** A wrong file typed into the
wrong row produces a confident, fully-populated verdict for the wrong paper, and nothing
downstream ever flags it. So before a `file_path` is screened, its own content is compared
against whatever title/DOI is known for that row. A mismatch never blocks the row — the file
is still screened, on the theory that a human named it deliberately — but the verdict is
forced to `MANUAL_REVIEW` and the mismatch is logged and added to the paper's notes. When
there's nothing to compare against (no title, no DOI, from any source), verification simply
can't happen, and the file is screened without a flag.

**Folder scans are a separate, optional lookup tool**, not a required step. Use it when you
don't already know a file's path: point it at a folder (or drag files onto the drop zone),
and it proposes paper↔file matches by reading each file and comparing it against the CSV's
title/DOI. Rows that already have a working `file_path` are skipped, since they'll be
checked directly at run time. Confirmed matches feed step 3 above; you can also export them
as a filled-in batch CSV to reuse without re-scanning.

### Using the folder scan

1. Go to the **Batch Upload** tab and select your CSV
2. In **Local File Library**, set the folder (or drag files and folders onto the drop zone)
3. Click **Scan Folder for Matches**
4. Review the mapping when the window opens; adjust anything that looks wrong
5. Click **Use This Mapping**, then **Run Batch Analysis**

Subfolders are included by default. Supported: `.pdf`, `.pptx`, `.pptm`, `.ppt`.

### Matching evidence

Each file is scored against each candidate paper (rows that don't already have their own
working `file_path`). The strongest available evidence wins:

| Evidence | Score | What it means |
|----------|-------|---------------|
| `doi` | 0.99 | A DOI printed in the file matches the row's DOI |
| `title_text` | 0.90–0.97 | The row's title appears in the file's text (0.97 exact, lower fuzzy) |
| `title_file` | ≤ 0.85 | The filename resembles the title |

Author surnames are then compared as corroboration:

- **Authors agree** — a small bonus, enough to lift a borderline title match
- **Authors conflict** — blocks auto-accept and sends the pair to review. This is the
  signal that two different papers share a title
- **Authors unknown** — changes nothing

One deliberate exception: **an author conflict does not override a DOI match.** A DOI is a
registered identifier; the author string it would be overruling was scraped off a page
layout with no font information available. Letting the weaker signal veto the stronger one
would send correct matches to review whenever author extraction misfires, which trains you
to click through the gate — the opposite of what the gate is for. The non-corroboration is
recorded in the note instead.

Assignment is **one-to-one**: each file serves one paper, each paper takes one file,
resolved best-score-first.

### Why it refuses to guess

A wrong file-to-paper assignment does not announce itself. It produces a confident,
well-reasoned, fully-populated verdict for the wrong paper, and nothing downstream ever
flags it. So the thresholds are deliberately conservative:

| Band | Outcome |
|------|---------|
| ≥ 0.92 | Auto-accepted (still shown in the review window) |
| 0.55 – 0.92 | Proposal — requires your confirmation |
| < 0.55 | Reported as unmatched, not assigned |

Two additional demotions, both of which override a high score:

- **Near-tie.** A runner-up within 0.06 of the winner forces review, because a confident
  score against two candidates is not a confident match to one.
- **No corroboration.** A file whose text could not be extracted (a scan, an encrypted
  PDF, a `.ppt` with no LibreOffice) can only be matched on its filename. Those always
  land in review, never auto-accept.

Rows whose `url` yields no metadata at all cannot be matched automatically — there is
nothing to compare against. They appear in the review window with a dropdown so you can
assign them by hand.

### The review window

Opens automatically when a folder scan finds anything needing attention. Every row shows the
file, the evidence and score, the note explaining any demotion, and the title of the
currently assigned paper so you can sanity check without leaving the window.

- `AUTO` — auto-accepted. Shown because a reviewer who cannot see these cannot catch the
  one that is wrong
- `CONFIRM` — ambiguous evidence, worth opening the file
- `UNMATCHED` — no candidate cleared the floor
- `UNREADABLE` — no text could be extracted

Set any row to `(skip)` to leave that paper to online retrieval. Two files cannot be
assigned to the same paper; the window refuses and tells you which ID collided.

### PowerPoint decks

Decks are screened **as full text**, the same as a paper. Rows sourced from a deck are
tagged `source_file_type=slides` in the repository so they remain identifiable.

If LibreOffice is available, decks are converted to PDF first, which keeps figures, charts,
and tables. Otherwise slide text is extracted and sent with a header noting that images are
absent.

**One caveat worth knowing.** A deck is a compressed summary written for an audience that
can ask questions, so it routinely omits sample sizes, metrics, and design detail a paper
would state. Screened as full text, "the deck does not mention inter-rater agreement"
reads as a **NO** on a quality-assurance criterion rather than an **UNCLEAR**. If
deck-sourced verdicts start looking overconfident, turn on **Screen decks conservatively**
in Settings → Local File Library; omissions then become UNCLEAR, at the cost of a higher
MANUAL_REVIEW rate. Off by default. You can find affected rows any time by filtering the
repository CSV on `source_file_type = slides`.

### Audit trail

Every confirmed mapping is appended to `file_mapping_log.csv` in the repository folder:

| Column | Contains |
|--------|----------|
| `paper_id`, `file_path` | What was assigned to what |
| `match_method`, `match_score` | The evidence and its strength |
| `file_kind` | `pdf` or `slides` |
| `confirmed_by` | `auto` or `user` |
| `note` | Any demotion reason |
| `mapped_at` | Timestamp |

**Export Mapping CSV** writes your batch CSV back out with `file_path` filled in for every
matched row. Running that CSV reproduces the same assignments without re-matching, which
matters for a review you may need to defend or repeat months later.

The repository also records `file_match_method` and `file_match_score` on each screened
paper, so a verdict can always be traced back to how its source file was identified.

### Drag and drop

If `tkinterdnd2` is installed, files and folders can be dropped onto the Batch tab.
Without it, the drop zone becomes a label saying so and **Browse…** does the same job —
the feature degrades rather than presenting a zone that silently does nothing.

---

## Screening a single paper

1. Go to the **Single Paper** tab
2. Confirm the criteria profile shown at the top is the one you want
3. Enter a **Paper ID** (required — the unique identifier in the repository)
4. Either:
   - Paste a **DOI, publisher link, arXiv ID, or direct PDF URL** and click **Find PDF**
   - Or click **Upload PDF** to load a local file
5. Click **Analyze Paper**
6. Results appear below and are saved to the repository automatically

All metadata (title, authors, year, journal, DOI, abstract) is extracted from the PDF — no
manual entry beyond the Paper ID.

---

## Batch upload

1. Go to the **Batch Upload** tab
2. Click **Download CSV Template**
3. Fill in one paper per row:

| Column | Required | Notes |
|--------|----------|-------|
| `paper_id` | ✓ | Your internal identifier |
| `url` | one of these | DOI, publisher link, arXiv ID, or direct PDF URL |
| `file_path` | one of these | Local path to a specific file |
| `title` | optional | Improves local file matching; trusted over fetched metadata |
| `doi` | optional | Same; a DOI in the `url` column is used automatically |

- If both `url` and `file_path` are provided, `url` is tried first; `file_path` is the
  fallback, verified against `title`/`doi` before being trusted
- All metadata written to the repository is extracted from the paper itself — `title` and
  `doi` are used only to help match local files
- Columns can be in any order; extra columns are ignored
- The `url` column no longer needs to be a direct PDF link

4. Click **Select CSV**
5. Optional: click **Scan Folder for Matches** if you have local copies for papers you
   suspect online retrieval won't reach — see [Local file library](#local-file-library).
   Not required; skip straight to step 6 if every row has a `url` or an exact `file_path`.
6. Click **Run Batch Analysis**
Progress, per-paper timing, and running counts appear in the progress panel. **⏹ Stop**
halts after the current paper — completed results are saved.

Source precedence per row, highest first:

1. The row's `url`, resolved online — always tried first, regardless of `file_path`
2. `file_path` in the CSV — used only when the url attempt found nothing (or there was no
   url). Verified against the row's title/DOI before being trusted; a mismatch doesn't
   block the row but forces `MANUAL_REVIEW` and is logged
3. A confirmed local-library match, from an earlier folder scan
4. Abstract-only fallback, if enabled

The log records which source produced each PDF, any decision-rule corrections, and — when
retrieval fails — the last few resolution attempts so you can see why.

Malformed JSON responses from the API (trailing commas, markdown fences, trailing prose)
are handled without failing the paper.

---

## Repository

Default location `~/triageq/`:

```
~/triageq/
  paper_repository.json              ← Full data including the complete model analysis
  paper_repository.csv               ← All papers, base columns + criteria_summary
  repository_<profile_id>.csv        ← One per profile, with that profile's criteria columns
  batch_template.csv                 ← Template for batch uploads
  file_mapping_log.csv               ← Audit trail of local file → paper assignments
  criteria_profiles/
    <your-profile-id>.json           ← Profiles you create or import
```

### Why two kinds of CSV

Different profiles have different criteria, so a single flat file across all profiles would
be mostly empty cells. `paper_repository.csv` carries the columns every paper has plus a
compact `criteria_summary` (`tool_evaluated=YES; quantitative_outcome=YES; …`). Each
`repository_<profile_id>.csv` carries that profile's full criteria columns and is the file
to use for analysis of a single review.

Both are rewritten from the JSON on every save. The JSON is the source of truth.

### Behavior

- Sorted by Paper ID ascending (numerically when IDs are numbers, alphabetically otherwise)
- Re-analyzing a paper with the same Paper ID overwrites the previous result — no duplicates
- Records with no profile stamp (e.g. carried over from a much older repository) are marked
  `profile_id: unknown` rather than attributed to any specific profile
- Changing the repository location initializes the new folder automatically; papers in the
  old location are not moved

### The Repository tab

- Browse all papers with title, authors, year, and venue visible
- Filter by recommendation: INCLUDE / EXCLUDE / MANUAL_REVIEW
- Filter by criteria profile
- Search across all fields
- **Basis** column shows `full` or `abstract` screening
- **Profile** column shows which criteria and version produced each decision
- Click column headers to re-sort
- Double-click any row for the full analysis JSON, with a provenance strip showing the
  profile, version, screening basis, and which source produced the PDF

---

## Decision rules

Generated from the active profile and enforced locally after every call.

| Decision | Condition |
|----------|-----------|
| **INCLUDE** | Every required criterion is YES |
| **EXCLUDE** | Any required criterion is NO; publication year before the profile's minimum; language other than the profile's requirement |
| **MANUAL_REVIEW** | At least one required criterion is UNCLEAR and none are NO |

MANUAL_REVIEW is only valid when at least one required verdict is UNCLEAR. If all required
verdicts are YES the recommendation is always INCLUDE — **low confidence does not change
this**. Confidence is a separate field.

Criteria can be marked `required_for_include: false` to collect information without
affecting the decision (useful for categorization fields like study design or setting).

---

## Confidence levels

Definitions come from the profile's `confidence_rules` and can be tailored per review. The
defaults:

| Level | Meaning |
|-------|---------|
| **High** | All criteria unambiguous — no boundary judgment required |
| **Medium** | At least one criterion required meaningful interpretation (also the fallback if unassigned) |
| **Low** | Genuine boundary case, missing information, or conflicting signals |

In abstract-only mode, High is only permitted when every criterion was explicitly stated in
the abstract.

---

## Output fields

### Present for every paper, regardless of profile

| Field | Source |
|-------|--------|
| `paper_id` | Entered by user |
| `title`, `authors`, `publication_year`, `journal_or_venue`, `doi`, `abstract` | Extracted from PDF |
| `url` / `file_path` | From CSV or manual entry |
| `recommendation` | INCLUDE / EXCLUDE / MANUAL_REVIEW |
| `confidence` | High / Medium / Low |
| `key_decision_factors` | Factual summary of determining evidence |
| `additional_notes` | Boundary flags, missing info, reviewer notes, decision-rule corrections |
| `profile_id` | Which criteria profile screened this paper |
| `profile_version` | Which version of that profile |
| `screening_basis` | `full_text` or `abstract_only` |
| `file_match_method` | How a local file was identified: `doi`, `title_text`, `title_file`, `manual`, or blank |
| `file_match_score` | Confidence of that match, 0–1 |
| `source_file_type` | `pdf` or `slides` — lets deck-sourced rows be filtered |
| `pdf_source` | Which resolution step found the PDF (e.g. `Unpaywall`, `manual upload`) |
| `pdf_url` | The resolved PDF URL, when there was one |
| `analyzed_at` | Timestamp |
| `model_used` | Provider and model that screened this paper, e.g. `Anthropic: claude-opus-4-8` |

### Generated from the profile

One column per criterion holding its verdict (YES / NO / UNCLEAR), named after the criterion
`id`, plus one column per tag field.

For example, a profile with criteria `tool_evaluated` and `outcome_measure` (the latter with
a `measure_type` tag field) would produce exactly those column names in the CSV and JSON
output — whatever criteria your profile defines, its `id`s and `tag_field` names become the
columns.

The full JSON record additionally holds each criterion's `reasoning`, `text_examples`
(quoted evidence), and `location` (page or section reference).

---

## Criteria profile schema reference

For hand-editing profiles or building them programmatically. Stored as JSON in
`criteria_profiles/`.

### Top level

| Field | Required | Description |
|-------|----------|-------------|
| `profile_id` | ✓ | Letters, numbers, hyphens, underscores, periods. Also the filename and CSV suffix. |
| `profile_name` | ✓ | Display name |
| `profile_version` | ✓ | e.g. `"1.0"`. Auto-bumped when edited after use. |
| `description` | | One sentence, included in the prompt |
| `criteria` | ✓ | Array of criterion objects, at least one |
| `min_publication_year` | | Integer year or `null` |
| `language_requirement` | | Default `"English"` |
| `language_rule` | | The facts-only reasoning rule. Editable but rarely worth changing. |
| `global_exclusions` | | Review-wide exclusions applied regardless of verdicts |
| `confidence_rules` | | Object with `high`, `medium`, `low` string definitions |
| `locked`, `builtin` | | Managed by the app |

### Criterion object

| Field | Required | Description |
|-------|----------|-------------|
| `id` | ✓ | lowercase snake_case. Becomes the JSON key and CSV column name. |
| `label` | ✓ | Display name |
| `definition` | ✓ | One or two sentences on what the criterion requires |
| `include_if` | ✓* | Array of conditions for a YES verdict |
| `exclude_if` | ✓ | Array of conditions for a NO verdict. **Cannot be empty.** |
| `required_for_include` | | Default `true`. Set `false` for informational criteria. |
| `notes` | | Extra lines appended to the criterion in the prompt |
| `include_if_groups` | | Alternative to `include_if` for multiple independently sufficient paths |
| `groups_note` | | Clarifying text after the groups |
| `tag_field` | | Categorization field — see below |
| `categories` | | Named sub-types each with their own `yes_if` / `no_if` |

\* One of `include_if`, `include_if_groups`, or `categories` is required.

### `tag_field` object

Adds a categorization field to a criterion's output, and a column to the CSV.

| Field | Description |
|-------|-------------|
| `name` | lowercase snake_case. Becomes the JSON key and CSV column. |
| `label` | Display name |
| `required` | Whether at least one value must always be returned |
| `allowed_values` | Closed list of permitted values. Empty array = free-form. |
| `fallback_value` | Used when required and nothing was found (e.g. `"Unknown"`) |
| `fallback_rule` | Prompt text explaining when the fallback is appropriate |

In the built-in profile, `domains_identified` is a required closed-list tag field and
`metrics_identified` is a free-form one.

### `include_if_groups`

Use when a criterion has two or more independently sufficient routes to YES. Each group is
`{ "label": "...", "items": [...] }`. The built-in profile's Criterion 3 uses this for
Path A and Path B.

---

## Example profile

[`criteria-profiles/genai-evidence-hub-profile.json`](criteria-profiles/genai-evidence-hub-profile.json)
is a complete profile from the GenAI Evidence Hub systematic review, included as a
worked example of the schema above. It has three criteria, one with a required closed-list
`tag_field` (`domains_identified`) and one using `include_if_groups` for two independently
sufficient evaluation paths (direct output metrics vs. outcome-based evidence). Import it
directly via **Import File…**, or read it alongside the schema reference to see the fields
in context.

---

## Troubleshooting

### Local file library

**"No PDF or PowerPoint files found"** — check the folder path and whether the files are in
subfolders (**Include subfolders** is on by default).

**Everything lands in UNMATCHED.** Usually means the rows have no title or DOI to compare
against: the `url` column is empty or its metadata could not be fetched. Add a `title`
column to the CSV, or assign files by hand in the review window.

**A file I expected to match is UNREADABLE.** The PDF is a scan with no text layer, or it
is encrypted. Those can only be matched by filename, and never automatically. Assign it in
the review window, or rename the file to resemble the paper's title.

**Author conflicts on matches I know are right.** Author extraction is heuristic — `pypdf`
provides no font sizes, so there is no reliable way to identify the author block. It is
built to return nothing rather than a bad guess, but unusual layouts still trip it. The
match is not blocked, only sent to review; confirm it and continue.

**Legacy `.ppt` reported unreadable.** `python-pptx` cannot open the old binary format.
Install LibreOffice, or re-save the file as `.pptx`.

**Diagnosing a match from the command line.** The matcher runs standalone, which is the
fastest way to see what was actually read out of a file — usually where the answer is:

```bash
python local_library.py --capabilities            # what this install can do
python local_library.py --folder ~/papers         # title, authors, DOI per file
python local_library.py --folder ~/papers --csv batch.csv   # full match report
```

Add `--auto 0.95` or `--floor 0.45` to try different thresholds before committing them in
Settings. Note that standalone matching uses only the `title`/`doi` columns in your CSV —
it does not fetch metadata from `url`, which a folder scan run from the GUI does.

**"not a PDF despite the .pdf extension"** — the file's header is not `%PDF`, so it is
something else renamed, or a truncated download. Checked deliberately before screening:
sending it would cost an API call and return an error pointing at the API rather than at
the file. Scanned PDFs with no text layer are still valid and pass this check.

**A folder scan is slow.** It makes one metadata lookup per row that has a `url` but no
title/DOI yet, with a polite rate limit (0.5s between calls by default). A 200-row CSV can
take a few minutes. **Export Mapping CSV** lets you skip re-scanning on later runs.

**A paper I know is right got flagged MANUAL_REVIEW with a "file_path mismatch" note.**
Run Batch Analysis checks a `file_path` fallback's own content against the row's title/DOI
before trusting it — the mismatch note in the log and in the paper's Additional Notes says
what it compared. If the title/DOI in the CSV (or repository) is wrong, fix that and rerun;
if the file is genuinely a different paper, the flag did its job.

**Higher MANUAL_REVIEW rate on deck-sourced papers** with **Screen decks conservatively**
turned on is expected behavior, not a bug — the same as the abstract-only fallback. Decks
omit detail a paper would state, and UNCLEAR is the honest verdict for what is absent.

**"PDF Not Found" for a paper you know is open access**
Check that the contact email is set in Settings. Unpaywall returns nothing without one. Then
use the **Resolve** test button to see the full attempt trail.

**Retrieval works in the test box but fails in a batch**
Some publishers rate-limit. The resolver already throttles itself, but a large batch against
a single publisher may still trip protection. Split the batch or supply `file_path` for
those rows.

**Everything comes back MANUAL_REVIEW**
Check the Basis column. If those rows say `abstract`, the abstract-only fallback is doing
what it is designed to do. If they say `full`, the profile's criteria are probably too
narrowly specified — review the `include_if` conditions.

**Everything comes back INCLUDE**
Almost always missing or weak `exclude_if` rules. Open the profile, read each criterion's
exclusions, and ask whether they would actually reject the adjacent papers you have been
seeing. This is the failure mode the compiler notes are meant to surface.

**A profile will not save**
The validator lists specific errors under the editor. The most common are a criterion with no
`exclude_if`, an `id` that is not lowercase snake_case, and no criterion marked
`required_for_include`.

**Testing the modules directly**

```bash
# Resolve a DOI and print the full attempt trail
python pdf_resolver.py 10.7717/peerj.4375 you@example.com
```

**API errors**
Messages vary by provider, but the causes are the same everywhere: `authentication_error` /
`401` means the key is wrong, expired, or (for OpenAI-compatible) not what the endpoint
expects. `rate_limit_error` / `429` means slow down or check your account's limits.
Anthropic's `credit balance is too low` means add credits at console.anthropic.com →
Billing; OpenAI's equivalent is under platform.openai.com → Billing.

**"This provider does not support native PDF input" / a caller-bug ProviderError**
This should never surface in normal use — the app checks a provider's PDF-vision support
before deciding whether to extract text first. If you see it, it means a PDF reached a
text-only provider unextracted; treat it as a bug report.

**OpenAI-compatible: connection refused, or the wrong model answers**
Check the base URL includes the right path (most local servers use `/v1`, e.g.
`http://localhost:11434/v1`, not just `http://localhost:11434`), and that the model name
matches exactly what the server has loaded — most servers reject an unrecognized model
name outright, but a couple silently fall back to whatever's loaded, which looks like the
model ignoring your criteria rather than a naming mismatch.
