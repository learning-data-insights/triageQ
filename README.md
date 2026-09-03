# GenAI Evidence Hub — Paper Screener

A desktop tool for screening research papers for inclusion/exclusion in a systematic
literature review. Built by Learning Data Insights, LLC.

The tool returns an **INCLUDE / EXCLUDE / MANUAL_REVIEW** recommendation for each paper,
with per-criterion verdicts, supporting evidence quoted from the paper, and a confidence
level. It is a screening assistant for human reviewers, not a replacement for them.

**Version 18** adds a local file library: point the tool at a folder of papers you already
hold and it works out which paper each file belongs to, filling in only the rows online
retrieval could not reach. Version 17 made criteria configurable per review and added
automatic PDF retrieval from links and DOIs.

---

## Contents

- [Files](#files)
- [Setup](#setup)
- [First-time configuration](#first-time-configuration)
- [Screening criteria](#screening-criteria)
- [PDF retrieval](#pdf-retrieval)
- [Local file library](#local-file-library)
- [Screening a single paper](#screening-a-single-paper)
- [Batch upload](#batch-upload)
- [Repository](#repository)
- [Decision rules](#decision-rules)
- [Confidence levels](#confidence-levels)
- [Output fields](#output-fields)
- [The built-in GenAI Evidence Hub profile](#the-built-in-genai-evidence-hub-profile)
- [Criteria profile schema reference](#criteria-profile-schema-reference)
- [Troubleshooting](#troubleshooting)

---

## Files

The application is five Python files that must live in the same folder:

| File | Contains |
|------|----------|
| `app_v18.py` | GUI, orchestration, repository management. This is what you run. |
| `criteria_profiles.py` | Profile schema, validation, prompt rendering, the criteria compiler, decision-rule engine |
| `pdf_resolver.py` | The online PDF resolution waterfall and abstract-only metadata fallback |
| `local_library.py` | Local file fingerprinting and file-to-paper matching |
| `doi_utils.py` | DOI parsing shared by `pdf_resolver.py` and `local_library.py` |

`pdf_resolver.py` and `local_library.py` can also be run directly from the command line for
testing — see [Troubleshooting](#troubleshooting). (`criteria_profiles.py` has no CLI; the
v17 README said otherwise, which was never accurate.) `doi_utils.py` has no CLI of its own —
it's a small shared dependency, not a tool you run.

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

> On Windows, use `python -m pip install -r requirements.txt` if `pip` is not recognized.

| Package | Why |
|---------|-----|
| `anthropic` | Claude API client |
| `requests` | HTTP for PDF retrieval |
| `beautifulsoup4` | Parsing publisher landing pages for PDF links |
| `pypdf` | Reading a local PDF's first pages to identify which paper it is |
| `python-pptx` | Reading PowerPoint decks |
| `tkinterdnd2` | Optional — drag-and-drop onto the Batch tab |

`beautifulsoup4` is technically optional — the resolver falls back to regex parsing if it
is missing — but it catches more link patterns, so install it.

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
python app_v18.py
```

If you prefer the shorter `python app.py`, rename the file. The other four modules are
imported by name and must keep their filenames.

---

## First-time configuration

1. Open the **Settings** tab (it scrolls — there are four cards)
2. Paste your **Anthropic API key** (`sk-ant-...`) in the API Key card
3. In the **PDF Retrieval** card, enter a **contact email**. Unpaywall requires one and
   OpenAlex uses it to give you faster responses. Any real address you own is fine.
4. Optionally click **Change** in the Repository card to set a custom save location

**Model:** `claude-opus-4-8`
**Cost:** ~$0.01–0.03 per paper · 200 papers ≈ $4–6 total

### What persists between sessions

Settings are saved to `~/.genai_evidence_hub_settings.json`:

- Repository location
- Active criteria profile
- All PDF retrieval settings, including the contact email
- Abstract-only fallback on/off

**The API key is never written to disk.** You re-enter it each session.

---

## Screening criteria

Criteria are no longer hardcoded. Each review is a **criteria profile** — a structured
definition of its criteria, boundary rules, decision logic, and output shape. The system
prompt, the JSON schema Claude returns, the repository columns, and the decision rules are
all generated from the active profile.

The tool ships with the **GenAI Evidence Hub** profile, which encodes the original three
criteria exactly. It is the default and cannot be deleted (duplicate it to make changes).

Manage profiles in **Settings → Screening Criteria**. The active profile is shown in the
window header, on the Single Paper tab, and on the Batch Upload tab, so you always know
what you are screening against.

### Creating a profile from your own criteria

Click **New from Text…**, paste your inclusion and exclusion criteria — a protocol excerpt,
a PICO statement, or plain prose — and click **Compile with Claude**. You can also load a
`.txt`, `.md`, or `.docx` file into the box first.

Claude converts the text into a structured profile and shows it for review before it can
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
using the profile's decision rules. If Claude's own `overall_recommendation` disagrees, the
locally computed value wins and the correction is recorded in `additional_notes` and shown
in the results panel.

The prompt still states the rules — this is a second line of defense so the repository does
not depend on the model applying them correctly.

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

Default location `~/genai_evidence_hub/`:

```
~/genai_evidence_hub/
  paper_repository.json              ← Full data including complete Claude analysis
  paper_repository.csv               ← All papers, base columns + criteria_summary
  repository_<profile_id>.csv        ← One per profile, with that profile's criteria columns
  batch_template.csv                 ← Template for batch uploads
  file_mapping_log.csv               ← Audit trail of local file → paper assignments
  criteria_profiles/
    genai-evidence-hub.json          ← The built-in profile
    <your-profile-id>.json           ← Profiles you create or import
```

### Why two kinds of CSV

Different profiles have different criteria, so a single flat file across all profiles would
be mostly empty cells. `paper_repository.csv` carries the columns every paper has plus a
compact `criteria_summary` (`genai_used=YES; relevant_domain=YES; …`). Each
`repository_<profile_id>.csv` carries that profile's full criteria columns and is the file
to use for analysis of a single review.

Both are rewritten from the JSON on every save. The JSON is the source of truth.

### Behavior

- Sorted by Paper ID ascending (numerically when IDs are numbers, alphabetically otherwise)
- Re-analyzing a paper with the same Paper ID overwrites the previous result — no duplicates
- Records created before v17 are automatically attributed to the built-in profile v1.0 with
  `screening_basis: full_text` when loaded
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
| `model_used` | Claude model version |

### Generated from the profile

One column per criterion holding its verdict (YES / NO / UNCLEAR), named after the criterion
`id`, plus one column per tag field.

For the built-in GenAI Evidence Hub profile that means: `genai_used`, `relevant_domain`,
`domains_identified`, `quality_assurance`, `metrics_identified` — the same columns v16
produced.

The full JSON record additionally holds each criterion's `reasoning`, `text_examples`
(quoted evidence), and `location` (page or section reference).

---

## The built-in GenAI Evidence Hub profile

This is the default profile, provided as a working example as much as a working review.

### Criterion 1: GenAI Used

The primary AI system must be a generative model (GPT-3/4/4o, Claude, Gemini, LLaMA,
Mistral, DeepSeek, T5, BERT variants used generatively). Ensembles combining a GenAI
component with traditional ML qualify.

Excluded: traditional/discriminative ML only (SVM, Random Forest, KNN, logistic regression,
XGBoost, CNN/RNN with no generative component); GenAI mentioned in the literature review but
not used.

### Criterion 2: Relevant Assessment Domain

The GenAI system must directly *perform* one of four tasks. Discussing or mentioning a
domain is not sufficient. `domains_identified` is required — every paper gets at least one
value.

| Domain | YES if |
|--------|--------|
| **Automated Item Scoring** | GenAI assigns scores, grades, or ratings to student-produced work (sometimes synthetic) — essays, short answers, code, drawings, simulations — of a kind typically scored by humans. Holistic, trait, and rubric-based scoring all count. |
| **Item Generation** | GenAI directly generates assessment questions, test items, prompts, or rubrics. Any item type. |
| **Formative Feedback** | GenAI generates feedback text delivered to students to improve learning, tied to their work or responses. |
| **Multimodal Inferences** | GenAI processes classroom audio or video to make assessment inferences — speech recognition, behavioral coding, engagement detection. |
| **Unknown** | Last resort only. Use when certain the paper fits none of the four — not as a hedge. |

Cross-cutting exclusions: AI detection, plagiarism detection, data annotation/labeling for
training future models, educational context as background framing only, fairness-only
analyses with no assessment task.

### Criterion 3: Quality Assurance

Quantitative evidence evaluating the GenAI system's performance or impact. Two equally valid
and independently sufficient paths:

**Path A — Direct output evaluation.** Precision/Recall/F1 with a baseline, accuracy against
a benchmark or human raters, Cohen's/Weighted/Quadratic Weighted Kappa, AUROC, BLEU, ROUGE,
GLEU, BERTScore, Pearson/Spearman correlation with human scores, agreement rates compared to
human rater agreement, human rater evaluation of output quality.

**Path B — Outcome-based evidence.** RCTs or quasi-experimental designs measuring learning
gains, pre/post comparisons, effect sizes (Cohen's *d*, partial eta-squared), statistical
tests on learning outcomes where the GenAI is the intervention, engagement or behavioral
metrics tied to GenAI use.

A well-designed RCT showing AI-generated feedback improved student scores satisfies this
criterion on its own.

Excluded: purely qualitative findings, system descriptions with no empirical evaluation,
quantitative metrics that apply only to a non-GenAI baseline, literature reviews and
theoretical frameworks with no empirical results.

### Other settings

- **Minimum publication year:** 2023
- **Language:** English
- **Reasoning language rule:** all `reasoning`, `text_examples`, `key_decision_factors`, and
  `additional_notes` fields contain only verifiable facts from the paper — model names, task
  descriptions, reported metrics, dataset names, sample sizes, direct quotes. Evaluative
  language ("well-documented", "high-quality", "impressive", "thorough") is prohibited.
  Describe what the paper does, not how good it is.

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
`authentication_error` means the key is wrong or expired. `rate_limit_error` means slow down
or check your account limits. `credit balance is too low` means add credits at
console.anthropic.com → Billing.
