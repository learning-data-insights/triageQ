"""
GenAI Evidence Hub — Paper Screening Tool  (v18)

Screens academic papers for inclusion/exclusion in a systematic literature
review. Backend: Anthropic API (Claude Opus) only.

NEW IN v18
──────────
4. LOCAL FILE LIBRARY
   Point the tool at a folder of local copies — or drag files onto the Batch
   tab — and it works out which paper each file belongs to. No per-row
   file_path entry, and no requirement that filenames mean anything.

   Matching evidence, in descending order of trust: the paper ID appearing in
   the filename, a DOI inside the file matching the row, the row's title
   appearing in the file's text, the filename resembling the title.

   Assignment is one-to-one. Anything below the review floor is reported as
   unmatched rather than guessed, and near-ties are forced into human review
   even when the top score would otherwise auto-accept. A mis-assigned file
   produces a confident, fully-populated verdict for the wrong paper and
   nothing downstream ever flags it — so the structure refuses instead.

   PowerPoint decks are accepted, and are screened as screening_basis="slides":
   a deck is a summary of a study, so what it omits becomes UNCLEAR rather than
   NO. Converting a deck to PDF changes the transport, not the evidence.

   Every confirmed mapping is appended to file_mapping_log.csv in the
   repository folder, and can be exported as a batch CSV for exact reruns.

   Run Batch Analysis tries each row's url first, always — a file_path is
   only used once the url attempt has failed (or there is no url), and even
   then it is checked against the row's title/DOI before being trusted. A
   mismatch does not block the row; it forces MANUAL_REVIEW and logs why. A
   confirmed folder-scan match is the next fallback after that. There is no
   separate pre-flight step — this all happens in one pass.

FROM v17
────────
1. CONFIGURABLE CRITERIA
   Screening criteria are no longer hardcoded. Each review is a "criteria
   profile" (see criteria_profiles.py) that can be created from a file, from
   pasted text, or by editing an existing profile. The system prompt, the JSON
   output schema, the repository columns, and the decision rules are all
   rendered from the active profile.

   Pasted text is COMPILED into a structured profile with explicit include_if /
   exclude_if boundaries, then shown for human review before it can be used.
   That review step is deliberate: free-text criteria with no exclusion rules
   are the known cause of false INCLUDEs.

2. AUTOMATIC PDF RETRIEVAL
   Give the tool a link or a DOI and it resolves the PDF itself via
   Unpaywall → OpenAlex → Semantic Scholar → Europe PMC → arXiv → landing-page
   citation_pdf_url scraping (see pdf_resolver.py). Open-access copies only.

3. ABSTRACT-ONLY FALLBACK (opt-in)
   When no PDF can be retrieved, optionally screen on title + abstract instead.
   Verdicts default to UNCLEAR for anything the abstract doesn't state, so these
   papers route to MANUAL_REVIEW rather than being silently decided.

FILES
   app_v18.py            this file — GUI and orchestration
   criteria_profiles.py  profile schema, prompt rendering, criteria compiler
   pdf_resolver.py       PDF resolution waterfall (online)
   local_library.py      local file fingerprinting and paper-ID matching
   doi_utils.py          DOI parsing shared by pdf_resolver.py and local_library.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import json
import csv
import os
import time
import datetime
import base64
import re
import shutil
import anthropic
from pathlib import Path

import criteria_profiles as cp
import pdf_resolver as pr
import local_library as lib

# Drag-and-drop is optional. tkinterdnd2 ships the tkdnd Tcl extension; without
# it every drop target degrades to a "Select Folder…" button and the UI says so
# rather than presenting a zone that silently does nothing.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:                                    # pragma: no cover
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False

# ── Constants ─────────────────────────────────────────────────────────────────

APP_TITLE = "GenAI Evidence Hub — Paper Screener"
SETTINGS_FILE = Path.home() / ".genai_evidence_hub_settings.json"

REPO_DIR = Path.home() / "genai_evidence_hub"
REPO_JSON = REPO_DIR / "paper_repository.json"
REPO_CSV = REPO_DIR / "paper_repository.csv"
BATCH_TEMPLATE_CSV = REPO_DIR / "batch_template.csv"
MAPPING_LOG_CSV = REPO_DIR / "file_mapping_log.csv"


def set_repo_dir(new_dir: Path):
    """Update all repository path globals at runtime."""
    global REPO_DIR, REPO_JSON, REPO_CSV, BATCH_TEMPLATE_CSV, MAPPING_LOG_CSV
    REPO_DIR = Path(new_dir)
    REPO_JSON = REPO_DIR / "paper_repository.json"
    REPO_CSV = REPO_DIR / "paper_repository.csv"
    BATCH_TEMPLATE_CSV = REPO_DIR / "batch_template.csv"
    MAPPING_LOG_CSV = REPO_DIR / "file_mapping_log.csv"


CLAUDE_MODEL = "claude-opus-4-8"

PALETTE = {
    # Surfaces
    "bg":           "#F6F5F3",
    "surface":      "#FFFFFF",
    "surface_dim":  "#EFEDE9",
    "border":       "#E0DED9",
    # Type
    "ink":          "#18181A",
    "ink_mid":      "#52524E",
    "ink_faint":    "#96948E",
    # Brand / header
    "brand":        "#18181A",
    "brand_accent": "#C4984A",
    # Actions
    "action":       "#18181A",
    "action_text":  "#FFFFFF",
    "action_sec":   "#ECEAE5",
    "action_sec_t": "#18181A",
    # Signals
    "include":      "#1A6B45",
    "include_bg":   "#EAF5EE",
    "exclude":      "#B03030",
    "exclude_bg":   "#FAECEC",
    "manual":       "#8C6200",
    "manual_bg":    "#FDF4E3",
    # Console
    "console_bg":   "#111111",
    "console_fg":   "#D0CEC8",
    "console_ok":   "#4DC98A",
    "console_err":  "#E06060",
    "console_info": "#C4984A",
    "console_key":  "#78BFDA",
}

# Columns present for every paper regardless of which profile screened it.
# Per-criterion columns are appended from the profile — see cp.profile_csv_columns.
CSV_BASE_COLUMNS = [
    "paper_id", "title", "authors", "publication_year", "journal_or_venue",
    "doi", "abstract", "url", "file_path",
    "recommendation", "confidence",
    "key_decision_factors", "additional_notes",
    "profile_id", "profile_version", "screening_basis",
    "pdf_source", "pdf_url",
    "file_match_method", "file_match_score", "source_file_type",
    "analyzed_at", "model_used",
]

# title and doi are optional, but they are what makes content matching possible
# for files whose names carry no paper ID. A DOI in the url column also works.
BATCH_CSV_COLUMNS = ["paper_id", "url", "file_path", "title", "doi"]


# ── App settings (persisted; never contains the API key) ──────────────────────

DEFAULT_SETTINGS = {
    "repo_dir": str(Path.home() / "genai_evidence_hub"),
    "active_profile_id": cp.DEFAULT_PROFILE["profile_id"],
    "abstract_only_fallback": False,
    "resolver": pr.ResolverConfig().to_dict(),
    "library": lib.LibraryConfig().to_dict(),
}


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        if SETTINGS_FILE.exists():
            disk = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(disk, dict):
                s.update({k: v for k, v in disk.items() if k in DEFAULT_SETTINGS})
                if isinstance(disk.get("resolver"), dict):
                    s["resolver"] = {**DEFAULT_SETTINGS["resolver"], **disk["resolver"]}
                if isinstance(disk.get("library"), dict):
                    s["library"] = {**DEFAULT_SETTINGS["library"], **disk["library"]}
    except Exception:
        pass
    return s


def save_settings(s: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Repository ────────────────────────────────────────────────────────────────

def ensure_repo():
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    cp.ensure_default_profile(REPO_DIR)
    if not REPO_JSON.exists():
        REPO_JSON.write_text(json.dumps([], indent=2), encoding="utf-8")
    if not REPO_CSV.exists():
        with open(REPO_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_BASE_COLUMNS).writeheader()
    if not BATCH_TEMPLATE_CSV.exists():
        with open(BATCH_TEMPLATE_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=BATCH_CSV_COLUMNS)
            w.writeheader()
            w.writerow({
                "paper_id": "PAPER_001",
                "url": "https://doi.org/10.7717/peerj.4375",
                "file_path": "",
                "title": "",
                "doi": "",
            })


def load_repository():
    ensure_repo()
    try:
        repo = json.loads(REPO_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [_migrate_entry(r) for r in repo]


def _migrate_entry(r: dict) -> dict:
    """Records written before v17 carry no profile stamp. Attribute them to the
    built-in profile so historical screenings stay interpretable."""
    if not isinstance(r, dict):
        return r
    r.setdefault("profile_id", cp.DEFAULT_PROFILE["profile_id"])
    r.setdefault("profile_version", "1.0")
    r.setdefault("screening_basis", "full_text")
    r.setdefault("pdf_source", "")
    r.setdefault("pdf_url", "")
    r.setdefault("file_match_method", "")
    r.setdefault("file_match_score", "")
    r.setdefault("source_file_type", "pdf")
    return r


def _pid_sort_key(r: dict):
    """Sort key for repository entries: numeric paper_id first, then lexicographic."""
    pid = r.get("paper_id", "")
    try:
        return (0, int(pid))
    except (ValueError, TypeError):
        return (1, str(pid))


def save_to_repository(entry: dict):
    ensure_repo()
    repo = load_repository()
    for i, r in enumerate(repo):
        if r.get("paper_id") == entry.get("paper_id"):
            repo[i] = entry
            break
    else:
        repo.append(entry)
    repo.sort(key=_pid_sort_key)
    REPO_JSON.write_text(json.dumps(repo, indent=2, ensure_ascii=False), encoding="utf-8")
    _sync_csv(repo)


def _sync_csv(repo: list):
    """Write one combined CSV plus one CSV per criteria profile.

    Criteria columns differ between profiles, so a single flat file across
    profiles would be mostly empty cells. The combined file carries the base
    columns plus a compact verdict summary; each per-profile file carries that
    profile's full criteria columns.
    """
    # Combined
    with open(REPO_CSV, "w", newline="", encoding="utf-8") as f:
        cols = CSV_BASE_COLUMNS + ["criteria_summary"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in repo:
            row = dict(r)
            row["criteria_summary"] = _criteria_summary(r)
            w.writerow(row)

    # Per profile
    by_profile: dict[str, list] = {}
    for r in repo:
        by_profile.setdefault(r.get("profile_id") or "unknown", []).append(r)

    for pid, rows in by_profile.items():
        prof = cp.load_profile(REPO_DIR, pid)
        if prof:
            cols = cp.profile_csv_columns(prof, CSV_BASE_COLUMNS)
        else:
            extra = sorted({k for r in rows for k in r
                            if k not in CSV_BASE_COLUMNS and not k.startswith("_")})
            cols = CSV_BASE_COLUMNS + extra
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", pid)
        path = REPO_DIR / f"repository_{safe}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)


def _criteria_summary(entry: dict) -> str:
    """Compact 'id=VERDICT; id=VERDICT' string for the combined CSV."""
    full = entry.get("_full_result") or {}
    crits = full.get("criteria") or {}
    parts = []
    for cid, block in crits.items():
        if isinstance(block, dict) and block.get("verdict"):
            parts.append(f"{cid}={block['verdict']}")
    return "; ".join(parts)


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_paper(payload, profile: dict, api_key: str,
                  screening_basis: str = "full_text",
                  progress_callback=None) -> dict:
    """Screen one paper against a criteria profile.

    payload: PDF bytes, or a text string (abstract metadata, extracted slides).
    screening_basis: "full_text" | "abstract_only" | "slides".

    Transport is decided by the payload TYPE, not by the basis. A slide deck
    converted to PDF arrives as bytes but is still basis="slides" — the prompt
    must reflect what the evidence is, not how it got here.

    Returns the parsed result dict, with overall_recommendation verified against
    the profile's decision rules locally.
    """
    if progress_callback:
        progress_callback({
            "full_text": "Sending to Claude…",
            "abstract_only": "Screening from abstract…",
            "slides": "Screening from slides…",
        }.get(screening_basis, "Sending to Claude…"))

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = cp.build_system_prompt(profile, screening_basis)
    user_text = cp.build_user_message(profile, screening_basis)

    if isinstance(payload, (bytes, bytearray)):
        pdf_b64 = base64.standard_b64encode(bytes(payload)).decode("utf-8")
        content = [
            {"type": "document",
             "source": {"type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64}},
            {"type": "text", "text": user_text},
        ]
    else:
        content = [{"type": "text", "text": str(payload)},
                   {"type": "text", "text": user_text}]

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    result = cp.clean_json_response(text)

    # Recompute the recommendation locally. The prompt states the rules, but the
    # repository should never depend on the model applying them correctly.
    rec, correction = cp.apply_decision_rules(profile, result)
    result["overall_recommendation"] = rec
    if correction:
        result["_local_correction"] = correction

    result["_screening_basis"] = screening_basis
    return result


# ── GUI ───────────────────────────────────────────────────────────────────────

class PaperScreenerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x900")
        self.minsize(1000, 720)
        self.configure(bg=PALETTE["bg"])

        # Settings
        self.settings = load_settings()
        set_repo_dir(Path(self.settings.get("repo_dir") or REPO_DIR))
        ensure_repo()

        self.resolver_cfg = pr.ResolverConfig.from_dict(self.settings.get("resolver", {}))
        self.library_cfg = lib.LibraryConfig.from_dict(self.settings.get("library", {}))

        # tkdnd has to be loaded into the Tcl interpreter of this root window.
        # If it fails, the app still runs; drop zones become buttons.
        self._dnd_ready = False
        if _HAS_DND:
            try:
                TkinterDnD._require(self)
                self._dnd_ready = True
            except Exception:
                self._dnd_ready = False

        self._api_key = tk.StringVar()
        self._current_pdf_bytes = None
        self._current_meta_text = None      # abstract-only payload, if used
        self._current_payload = None        # bytes or str actually sent
        self._current_basis = "full_text"
        self._current_pdf_source = ""       # which resolver step produced it
        self._current_pdf_url = ""
        self._batch_csv_path = None

        # paper_id -> {"path", "method", "score", "kind"} confirmed by the user
        self._library_map: dict = {}
        self._library_report = None
        self._library_extra_paths: list = []   # files/folders added by drag-drop
        self._busy = False
        self._stop_requested = False

        # Active criteria profile
        self.active_profile = self._load_active_profile()

        # Timer state
        self._single_start_time = None
        self._batch_start_time = None
        self._paper_start_time = None
        self._timer_after_id = None

        self._apply_styles()
        self._build_ui()
        self._refresh_profile_widgets()
        self._refresh_repository_tab()

    # ── Profiles ──────────────────────────────────────────────────────────────

    def _load_active_profile(self) -> dict:
        pid = self.settings.get("active_profile_id") or cp.DEFAULT_PROFILE["profile_id"]
        prof = cp.load_profile(REPO_DIR, pid)
        if not prof:
            cp.ensure_default_profile(REPO_DIR)
            prof = cp.load_profile(REPO_DIR, cp.DEFAULT_PROFILE["profile_id"])
            self.settings["active_profile_id"] = cp.DEFAULT_PROFILE["profile_id"]
            save_settings(self.settings)
        return cp.normalize_profile(prof)

    def _profile_in_use(self, profile_id: str, version: str | None = None) -> int:
        """How many repository records were screened with this profile (version)."""
        n = 0
        for r in load_repository():
            if r.get("profile_id") == profile_id:
                if version is None or r.get("profile_version") == version:
                    n += 1
        return n

    # ── Styles ────────────────────────────────────────────────────────────────

    def _apply_styles(self):
        s = ttk.Style(self)
        s.theme_use("default")

        s.configure("TNotebook",
                    background=PALETTE["bg"], borderwidth=0, tabmargins=[0, 0, 0, 0])
        s.configure("TNotebook.Tab",
                    background=PALETTE["bg"], foreground=PALETTE["ink_faint"],
                    padding=[20, 10], font=("Helvetica", 9, "bold"),
                    borderwidth=0, relief="flat")
        s.map("TNotebook.Tab",
              background=[("selected", PALETTE["surface"])],
              foreground=[("selected", PALETTE["ink"])],
              expand=[("selected", [0, 0, 0, 0])])

        s.configure("Thin.Horizontal.TProgressbar",
                    troughcolor=PALETTE["border"], background=PALETTE["ink"],
                    borderwidth=0, thickness=3)
        s.configure("Indeterminate.Horizontal.TProgressbar",
                    troughcolor=PALETTE["border"], background=PALETTE["brand_accent"],
                    borderwidth=0, thickness=3)

        s.configure("Repo.Treeview",
                    rowheight=28, font=("Helvetica", 9),
                    background=PALETTE["surface"],
                    fieldbackground=PALETTE["surface"],
                    foreground=PALETTE["ink"],
                    borderwidth=0, relief="flat")
        s.configure("Repo.Treeview.Heading",
                    font=("Helvetica", 8, "bold"),
                    background=PALETTE["surface_dim"],
                    foreground=PALETTE["ink_mid"],
                    borderwidth=0, relief="flat",
                    padding=[8, 6])
        s.map("Repo.Treeview",
              background=[("selected", PALETTE["brand_accent"])],
              foreground=[("selected", PALETTE["surface"])])

        s.configure("Profile.TCombobox",
                    fieldbackground=PALETTE["surface_dim"],
                    background=PALETTE["surface_dim"],
                    foreground=PALETTE["ink"],
                    arrowcolor=PALETTE["ink_mid"],
                    borderwidth=0)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()

        self.notebook = ttk.Notebook(self, style="TNotebook")
        self.notebook.pack(fill="both", expand=True)

        self.tab_single = tk.Frame(self.notebook, bg=PALETTE["bg"])
        self.tab_batch = tk.Frame(self.notebook, bg=PALETTE["bg"])
        self.tab_repo = tk.Frame(self.notebook, bg=PALETTE["bg"])
        self.tab_config = tk.Frame(self.notebook, bg=PALETTE["bg"])

        self.notebook.add(self.tab_single, text="Single Paper")
        self.notebook.add(self.tab_batch, text="Batch Upload")
        self.notebook.add(self.tab_repo, text="Repository")
        self.notebook.add(self.tab_config, text="Settings")

        self._build_single_tab()
        self._build_batch_tab()
        self._build_repo_tab()
        self._build_config_tab()

    def _build_header(self):
        hdr = tk.Frame(self, bg=PALETTE["brand"], height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        left = tk.Frame(hdr, bg=PALETTE["brand"])
        left.pack(side="left", padx=28, fill="y")

        tk.Label(left, text="GenAI Evidence Hub",
                 font=("Georgia", 15, "bold"),
                 fg=PALETTE["brand_accent"],
                 bg=PALETTE["brand"]).pack(side="left", anchor="center")

        tk.Label(left, text="  ·  Paper Screener",
                 font=("Helvetica", 11),
                 fg="#6A6A60",
                 bg=PALETTE["brand"]).pack(side="left", anchor="center")

        right = tk.Frame(hdr, bg=PALETTE["brand"])
        right.pack(side="right", padx=28, fill="y")

        self._header_profile_label = tk.Label(
            right, text="", font=("Helvetica", 8, "bold"),
            fg=PALETTE["brand_accent"], bg=PALETTE["brand"], anchor="e")
        self._header_profile_label.pack(anchor="e", pady=(12, 0))

        tk.Label(right, text="Learning Data Insights, LLC",
                 font=("Helvetica", 8),
                 fg="#4A4A44",
                 bg=PALETTE["brand"]).pack(anchor="e")

    # ── Helpers: card and button factories ────────────────────────────────────

    def _card(self, parent, label=None, pad=(20, 12)):
        """A flat white card with optional section label above it."""
        outer = tk.Frame(parent, bg=PALETTE["bg"])
        outer.pack(fill="x", padx=24, pady=(8, 0))
        if label:
            tk.Label(outer, text=label.upper(),
                     bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                     font=("Helvetica", 7, "bold"),
                     anchor="w").pack(fill="x", pady=(0, 4))
        card = tk.Frame(outer, bg=PALETTE["surface"],
                        highlightbackground=PALETTE["border"],
                        highlightthickness=1)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=PALETTE["surface"])
        inner.pack(fill="x", padx=pad[0], pady=pad[1])
        return inner

    def _btn(self, parent, text, command, style="primary",
             padx=18, pady=7, width=None):
        """Flat button with primary / secondary / danger styles."""
        cfg = {
            "primary":   (PALETTE["action"],     PALETTE["action_text"]),
            "secondary": (PALETTE["action_sec"], PALETTE["action_sec_t"]),
            "danger":    (PALETTE["exclude"],    "#FFFFFF"),
            "ghost":     (PALETTE["bg"],         PALETTE["ink_mid"]),
        }
        bg, fg = cfg.get(style, cfg["primary"])
        kw = dict(text=text, command=command, bg=bg, fg=fg,
                  font=("Helvetica", 9, "bold"), relief="flat",
                  padx=padx, pady=pady, cursor="hand2",
                  activebackground=bg, activeforeground=fg,
                  bd=0)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    def _entry(self, parent, textvariable, width=None, show=None, dim=True):
        kw = dict(textvariable=textvariable, font=("Helvetica", 9),
                  bd=0, relief="flat",
                  bg=PALETTE["surface_dim"] if dim else PALETTE["surface"],
                  fg=PALETTE["ink"], insertbackground=PALETTE["ink"],
                  highlightthickness=1,
                  highlightbackground=PALETTE["border"],
                  highlightcolor=PALETTE["ink"])
        if width:
            kw["width"] = width
        if show:
            kw["show"] = show
        return tk.Entry(parent, **kw)

    def _divider(self, parent, vertical_pad=8):
        tk.Frame(parent, bg=PALETTE["border"], height=1).pack(
            fill="x", padx=24, pady=vertical_pad)

    # ── Single Paper Tab ──────────────────────────────────────────────────────

    def _build_single_tab(self):
        f = self.tab_single

        # ── Active criteria strip ──
        crit_inner = self._card(f, label="Screening Against")
        crit_row = tk.Frame(crit_inner, bg=PALETTE["surface"])
        crit_row.pack(fill="x")
        self._single_profile_label = tk.Label(
            crit_row, text="", bg=PALETTE["surface"], fg=PALETTE["ink"],
            font=("Helvetica", 10, "bold"), anchor="w")
        self._single_profile_label.pack(side="left")
        self._btn(crit_row, "Change", lambda: self.notebook.select(self.tab_config),
                  "ghost", padx=12, pady=3).pack(side="right")

        # ── Paper ID card ──
        id_inner = self._card(f, label="Paper ID")
        id_row = tk.Frame(id_inner, bg=PALETTE["surface"])
        id_row.pack(fill="x")

        self._paper_id_var = tk.StringVar()
        id_entry = tk.Entry(id_row, textvariable=self._paper_id_var,
                            font=("Helvetica", 11), bd=0, relief="flat",
                            bg=PALETTE["surface"], fg=PALETTE["ink"],
                            insertbackground=PALETTE["ink"],
                            highlightthickness=1,
                            highlightbackground=PALETTE["border"],
                            highlightcolor=PALETTE["ink"], width=22)
        id_entry.pack(side="left", ipady=6, padx=(0, 16))

        tk.Label(id_row, text="All other metadata is extracted automatically from the PDF.",
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic")).pack(side="left")

        # ── Source card ──
        src_inner = self._card(f, label="Paper Source")

        url_row = tk.Frame(src_inner, bg=PALETTE["surface"])
        url_row.pack(fill="x", pady=(0, 4))
        tk.Label(url_row, text="Link", bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                 font=("Helvetica", 8, "bold"), width=6, anchor="w").pack(side="left")
        self._url_var = tk.StringVar()
        url_entry = self._entry(url_row, self._url_var)
        url_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(6, 10))
        self._btn(url_row, "Find PDF", self._fetch_url, "secondary",
                  padx=14, pady=5).pack(side="left")

        tk.Label(src_inner,
                 text=("Accepts a DOI, a publisher or repository link, an arXiv id, or a "
                       "direct PDF URL. Open-access copies are located automatically."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic"), anchor="w",
                 justify="left", wraplength=760).pack(fill="x", padx=(48, 0), pady=(0, 6))

        sep_row = tk.Frame(src_inner, bg=PALETTE["surface"])
        sep_row.pack(fill="x", pady=4)
        tk.Frame(sep_row, bg=PALETTE["border"], height=1).pack(
            side="left", fill="x", expand=True)
        tk.Label(sep_row, text="  or  ", bg=PALETTE["surface"],
                 fg=PALETTE["ink_faint"], font=("Helvetica", 8)).pack(side="left")
        tk.Frame(sep_row, bg=PALETTE["border"], height=1).pack(
            side="left", fill="x", expand=True)

        file_row = tk.Frame(src_inner, bg=PALETTE["surface"])
        file_row.pack(fill="x")
        self._btn(file_row, "Upload PDF", self._upload_pdf,
                  "secondary", padx=14, pady=5).pack(side="left")
        self._file_label = tk.Label(file_row, text="No file selected",
                                    bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                                    font=("Helvetica", 8, "italic"))
        self._file_label.pack(side="left", padx=12)

        # Resolution trail
        self._resolve_trail = tk.Label(
            src_inner, text="", bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
            font=("Courier New", 8), anchor="w", justify="left", wraplength=880)
        self._resolve_trail.pack(fill="x", pady=(6, 0))

        # ── Analyze button + progress ──
        act_outer = tk.Frame(f, bg=PALETTE["bg"])
        act_outer.pack(fill="x", padx=24, pady=12)

        self._analyze_btn = self._btn(act_outer, "Analyze Paper",
                                      self._run_single_analysis, "primary",
                                      padx=24, pady=9)
        self._analyze_btn.pack(side="left")

        prog_right = tk.Frame(act_outer, bg=PALETTE["bg"])
        prog_right.pack(side="left", fill="x", expand=True, padx=20)

        prog_top = tk.Frame(prog_right, bg=PALETTE["bg"])
        prog_top.pack(fill="x")
        self._progress_label = tk.Label(
            prog_top, text="Ready.", bg=PALETTE["bg"],
            fg=PALETTE["ink_faint"], font=("Helvetica", 8, "italic"), anchor="w")
        self._progress_label.pack(side="left", fill="x", expand=True)
        self._single_timer_label = tk.Label(
            prog_top, text="", bg=PALETTE["bg"],
            fg=PALETTE["brand_accent"], font=("Courier", 9, "bold"), anchor="e", width=8)
        self._single_timer_label.pack(side="right")

        self._single_pbar = ttk.Progressbar(
            prog_right, mode="indeterminate",
            style="Indeterminate.Horizontal.TProgressbar")
        self._single_pbar.pack(fill="x", pady=(4, 0))

        self._divider(f, vertical_pad=0)

        # ── Results ──
        res_outer = tk.Frame(f, bg=PALETTE["bg"])
        res_outer.pack(fill="both", expand=True, padx=24, pady=(10, 16))

        tk.Label(res_outer, text="ANALYSIS RESULTS",
                 bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 7, "bold"), anchor="w").pack(fill="x", pady=(0, 4))

        res_card = tk.Frame(res_outer, bg=PALETTE["console_bg"],
                            highlightbackground=PALETTE["border"],
                            highlightthickness=1)
        res_card.pack(fill="both", expand=True)

        self._result_text = scrolledtext.ScrolledText(
            res_card, font=("Courier New", 9),
            bg=PALETTE["console_bg"], fg=PALETTE["console_fg"],
            insertbackground=PALETTE["console_fg"],
            bd=0, padx=16, pady=12, wrap="word",
            selectbackground=PALETTE["ink_mid"])
        self._result_text.pack(fill="both", expand=True)
        self._result_text.insert("1.0", "Results will appear here after analysis.")
        self._result_text.configure(state="disabled")

        for tag, color in [
            ("include", PALETTE["console_ok"]),
            ("exclude", PALETTE["console_err"]),
            ("manual",  PALETTE["console_info"]),
            ("yes",     PALETTE["console_ok"]),
            ("no",      PALETTE["console_err"]),
            ("unclear", PALETTE["console_info"]),
            ("key",     PALETTE["console_key"]),
            ("warn",    PALETTE["console_err"]),
        ]:
            self._result_text.tag_config(tag, foreground=color)
        self._result_text.tag_config(
            "heading", foreground=PALETTE["brand_accent"],
            font=("Courier New", 9, "bold"))

    # ── Batch Tab ─────────────────────────────────────────────────────────────

    def _build_batch_tab(self):
        f = self.tab_batch

        info_inner = self._card(f, label="Instructions")
        tk.Label(info_inner, bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                 font=("Helvetica", 9), justify="left", anchor="w",
                 text=(
                     "Upload a CSV with one paper per row.\n"
                     "Required column: paper_id\n"
                     "Source (at least one): url, file_path, or a matched local file\n"
                     "The url column accepts a DOI, publisher link, arXiv id, or direct PDF.\n"
                     "Optional: title, doi — these let local files be matched by content\n"
                     "when their filenames carry no paper ID."
                 )).pack(anchor="w", pady=(0, 8))
        self._btn(info_inner, "Download CSV Template",
                  self._download_batch_template, "secondary",
                  padx=14, pady=5).pack(anchor="w")

        self._batch_profile_label = tk.Label(
            info_inner, text="", bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
            font=("Helvetica", 8, "italic"), anchor="w")
        self._batch_profile_label.pack(anchor="w", pady=(8, 0))

        ctrl_inner = self._card(f, label="Batch File")
        ctrl_row = tk.Frame(ctrl_inner, bg=PALETTE["surface"])
        ctrl_row.pack(fill="x")

        self._btn(ctrl_row, "Select CSV", self._select_batch_csv,
                  "secondary", padx=14, pady=6).pack(side="left")
        self._batch_file_label = tk.Label(
            ctrl_row, text="No file selected",
            bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
            font=("Helvetica", 8, "italic"))
        self._batch_file_label.pack(side="left", padx=12)

        self._batch_stop_btn = self._btn(
            ctrl_row, "⏹  Stop", self._stop_batch, "danger", padx=14, pady=6)
        self._batch_stop_btn.pack(side="right")
        self._batch_stop_btn.config(state="disabled")

        self._batch_run_btn = self._btn(
            ctrl_row, "Run Batch Analysis", self._run_batch_analysis,
            "primary", padx=18, pady=6)
        self._batch_run_btn.pack(side="right", padx=(0, 10))
        self._batch_run_btn.config(state="disabled")

        self._build_library_card(f)

        prog_inner = self._card(f, label="Progress", pad=(20, 14))

        bar_row = tk.Frame(prog_inner, bg=PALETTE["surface"])
        bar_row.pack(fill="x", pady=(0, 6))
        self._batch_progress = ttk.Progressbar(
            bar_row, mode="determinate",
            style="Thin.Horizontal.TProgressbar")
        self._batch_progress.pack(side="left", fill="x", expand=True)
        self._batch_pct_label = tk.Label(
            bar_row, text="", bg=PALETTE["surface"],
            fg=PALETTE["ink_mid"], font=("Helvetica", 8, "bold"), width=5, anchor="e")
        self._batch_pct_label.pack(side="right")

        stat_row = tk.Frame(prog_inner, bg=PALETTE["surface"])
        stat_row.pack(fill="x")
        self._batch_status = tk.Label(
            stat_row, text="", bg=PALETTE["surface"],
            fg=PALETTE["ink_mid"], font=("Helvetica", 8), anchor="w")
        self._batch_status.pack(side="left", fill="x", expand=True)
        self._paper_timer_label = tk.Label(
            stat_row, text="", bg=PALETTE["surface"],
            fg=PALETTE["brand_accent"], font=("Courier", 8), anchor="e", width=14)
        self._paper_timer_label.pack(side="right")

        counts_row = tk.Frame(prog_inner, bg=PALETTE["surface"])
        counts_row.pack(fill="x", pady=(4, 0))
        self._batch_counts_label = tk.Label(
            counts_row, text="", bg=PALETTE["surface"],
            fg=PALETTE["ink_faint"], font=("Helvetica", 8), anchor="w")
        self._batch_counts_label.pack(side="left", fill="x", expand=True)
        self._batch_total_timer_label = tk.Label(
            counts_row, text="", bg=PALETTE["surface"],
            fg=PALETTE["brand_accent"], font=("Courier", 8, "bold"), anchor="e", width=14)
        self._batch_total_timer_label.pack(side="right")

        log_outer = tk.Frame(f, bg=PALETTE["bg"])
        log_outer.pack(fill="both", expand=True, padx=24, pady=(10, 16))

        tk.Label(log_outer, text="LOG",
                 bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 7, "bold"), anchor="w").pack(fill="x", pady=(0, 4))

        log_card = tk.Frame(log_outer, bg=PALETTE["console_bg"],
                            highlightbackground=PALETTE["border"],
                            highlightthickness=1)
        log_card.pack(fill="both", expand=True)

        self._batch_log = scrolledtext.ScrolledText(
            log_card, font=("Courier New", 9),
            bg=PALETTE["console_bg"], fg=PALETTE["console_fg"],
            bd=0, padx=16, pady=12, wrap="word")
        self._batch_log.pack(fill="both", expand=True)

        for tag, color in [
            ("ok",    PALETTE["console_ok"]),
            ("err",   PALETTE["console_err"]),
            ("info",  PALETTE["console_info"]),
            ("trail", PALETTE["ink_faint"]),
        ]:
            self._batch_log.tag_config(tag, foreground=color)

    # ── Batch Tab: local file library ─────────────────────────────────────────

    def _build_library_card(self, parent):
        inner = self._card(parent, label="Local File Library", pad=(20, 14))

        tk.Label(inner,
                 text=("For papers Run Batch Analysis could not source itself — no PDF "
                       "online and no working file_path in the CSV. Scan a folder (or "
                       "drop files below) and match its PDFs/PowerPoints against those "
                       "rows by title, DOI, or author, using whatever title/DOI is in "
                       "the CSV. Filenames do not have to mean anything. This is a "
                       "lookup aid, not a required step — Run Batch Analysis works "
                       "without it and always tries online retrieval and any file_path "
                       "already in the CSV first."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", pady=(0, 10))

        folder_row = tk.Frame(inner, bg=PALETTE["surface"])
        folder_row.pack(fill="x")
        tk.Label(folder_row, text="Folder", bg=PALETTE["surface"],
                 fg=PALETTE["ink_mid"], font=("Helvetica", 8, "bold"),
                 width=7, anchor="w").pack(side="left")
        self._lib_folder_var = tk.StringVar(value=self.library_cfg.folder)
        self._entry(folder_row, self._lib_folder_var).pack(
            side="left", fill="x", expand=True, ipady=5, padx=(6, 10))
        self._btn(folder_row, "Browse…", self._select_library_folder,
                  "secondary", padx=12, pady=5).pack(side="left")

        opt_row = tk.Frame(inner, bg=PALETTE["surface"])
        opt_row.pack(fill="x", pady=(8, 0))
        self._lib_recursive = tk.BooleanVar(value=self.library_cfg.recursive)
        tk.Checkbutton(
            opt_row, text="  Include subfolders", variable=self._lib_recursive,
            command=self._save_library_settings,
            bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
            activebackground=PALETTE["surface"], selectcolor=PALETTE["surface"],
            font=("Helvetica", 8), anchor="w", relief="flat",
            cursor="hand2", highlightthickness=0).pack(side="left", padx=(48, 16))

        tk.Label(opt_row,
                 text=("Local files fill gaps only — a paper Run Batch Analysis "
                       "sourced online, or found at its own file_path, is never "
                       "re-sourced from here."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic"), anchor="w").pack(side="left")

        # ── Drop zone ──
        drop = tk.Frame(inner, bg=PALETTE["surface_dim"],
                        highlightbackground=PALETTE["border"],
                        highlightthickness=1, height=54)
        drop.pack(fill="x", pady=(10, 0))
        drop.pack_propagate(False)
        self._lib_drop_label = tk.Label(
            drop,
            text=("Drop PDFs, PowerPoint files, or folders here"
                  if self._dnd_ready else
                  "Drag-and-drop unavailable — install tkinterdnd2, or use Browse… above"),
            bg=PALETTE["surface_dim"],
            fg=PALETTE["ink_mid"] if self._dnd_ready else PALETTE["ink_faint"],
            font=("Helvetica", 9, "bold" if self._dnd_ready else "italic"))
        self._lib_drop_label.pack(expand=True)
        if self._dnd_ready:
            for w in (drop, self._lib_drop_label):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_library_drop)

        act_row = tk.Frame(inner, bg=PALETTE["surface"])
        act_row.pack(fill="x", pady=(10, 0))
        self._lib_match_btn = self._btn(
            act_row, "Scan Folder for Matches", self._run_library_match,
            "primary", padx=16, pady=6)
        self._lib_match_btn.pack(side="left")
        self._lib_review_btn = self._btn(
            act_row, "Review Mapping…", self._open_match_review,
            "secondary", padx=14, pady=6)
        self._lib_review_btn.pack(side="left", padx=(8, 0))
        self._lib_review_btn.config(state="disabled")
        self._btn(act_row, "Export Mapping CSV", self._export_mapping,
                  "ghost", padx=12, pady=6).pack(side="left", padx=(8, 0))
        self._btn(act_row, "Clear", self._clear_library_map,
                  "ghost", padx=12, pady=6).pack(side="left")

        self._lib_status = tk.Label(
            inner, text="No folder scan has been run yet.", bg=PALETTE["surface"],
            fg=PALETTE["ink_faint"], font=("Helvetica", 8, "italic"),
            anchor="w", justify="left", wraplength=820)
        self._lib_status.pack(fill="x", pady=(8, 0))

    def _select_library_folder(self):
        d = filedialog.askdirectory(title="Select Folder of Local Papers",
                                    initialdir=self._lib_folder_var.get() or None)
        if d:
            self._lib_folder_var.set(d)
            self._save_library_settings()

    def _on_library_drop(self, event):
        """Accept dropped files and folders.

        Tcl hands over a list, brace-quoting any path with spaces, so it must be
        split with splitlist rather than by whitespace.
        """
        try:
            paths = list(self.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        added, rejected = 0, 0
        for raw in paths:
            p = Path(raw.strip("{}"))
            if p.is_dir():
                self._library_extra_paths.append(str(p))
                added += 1
            elif p.is_file():
                if p.suffix.lower() in lib.SUPPORTED_EXTS:
                    self._library_extra_paths.append(str(p))
                    added += 1
                else:
                    rejected += 1
        msg = f"{added} item(s) added — press Match Files to Papers."
        if rejected:
            msg += f" {rejected} skipped (only PDF and PowerPoint are supported)."
        self._lib_drop_label.config(text=msg, fg=PALETTE["ink"])
        self._refresh_library_status()

    def _clear_library_map(self):
        self._library_map = {}
        self._library_report = None
        self._library_extra_paths = []
        self._lib_review_btn.config(state="disabled")
        if self._dnd_ready:
            self._lib_drop_label.config(
                text="Drop PDFs, PowerPoint files, or folders here",
                fg=PALETTE["ink_mid"])
        self._refresh_library_status()

    def _refresh_library_status(self):
        n = len(self._library_map)
        bits = []
        if n:
            kinds = {}
            for v in self._library_map.values():
                kinds[v.get("kind", "pdf")] = kinds.get(v.get("kind", "pdf"), 0) + 1
            detail = ", ".join(f"{c} {k}" for k, c in sorted(kinds.items()))
            bits.append(f"{n} paper(s) mapped to local files ({detail}).")
        else:
            bits.append("No files mapped.")
        if self._library_extra_paths:
            bits.append(f"{len(self._library_extra_paths)} dropped item(s) pending.")
        rep = self._library_report
        if rep:
            if rep.proposals:
                bits.append(f"{len(rep.proposals)} proposal(s) awaiting confirmation.")
            if rep.unmatched_files:
                bits.append(f"{len(rep.unmatched_files)} file(s) unmatched.")
            if rep.unreadable:
                bits.append(f"{len(rep.unreadable)} file(s) unreadable.")
        self._lib_status.config(text="  ".join(bits))

    def _run_library_match(self):
        """Scan a folder (or dropped files) and propose paper↔file matches for
        rows Run Batch Analysis would otherwise have no way to source: no url,
        or a url/file_path that doesn't pan out. This is a standalone lookup
        aid — it only fills self._library_map, which Run Batch Analysis
        consults as a last resort, after trying online retrieval and any
        file_path already in the CSV.
        """
        if not self._batch_csv_path:
            messagebox.showwarning(
                "Select a CSV First",
                "Matching needs the batch CSV — that is where the paper IDs and "
                "titles/DOIs live. Select the CSV first.")
            return
        self._save_library_settings()
        self._stop_requested = False
        self._lib_match_btn.config(state="disabled", text="Scanning…")
        self._batch_stop_btn.config(state="normal", text="⏹  Stop")
        self._batch_log.delete("1.0", "end")
        threading.Thread(target=self._library_scan_worker, daemon=True).start()

    def _library_scan_worker(self):
        log = lambda m: self._log_batch(f"{m}\n", "info")      # noqa: E731
        try:
            rows = self._read_batch_rows()
            if not rows:
                self._log_batch("Could not read any rows from the CSV.\n", "err")
                return

            self._log_batch(
                f"FOLDER SCAN — {len(rows)} row(s). The only network calls in this "
                "phase are metadata lookups for rows that have a link but no title "
                "or DOI yet — no PDFs are downloaded here.\n\n", "info")

            csv_fp_by_pid = {}
            for i, row in enumerate(rows):
                pid = (row.get("paper_id") or f"PAPER_{i+1}").strip()
                fp_csv = (row.get("file_path") or "").strip()
                if fp_csv:
                    fp_csv = fp_csv.replace("\\", os.sep).replace("/", os.sep)
                csv_fp_by_pid[pid] = fp_csv

            all_targets = lib.targets_from_rows(
                rows, {r.get("paper_id"): r for r in load_repository()})

            # A row with its own working file_path doesn't need a scan match —
            # Run Batch Analysis will use that path directly.
            has_own_file = {pid for pid, fp in csv_fp_by_pid.items()
                            if fp and os.path.isfile(fp)}
            targets = [t for t in all_targets if t.paper_id not in has_own_file]

            need_title = [t for t in targets if not t.has_content_key and t.url]
            if need_title:
                self._log_batch(
                    f"Looking up titles for {len(need_title)} row(s) with a link "
                    "but no title/DOI…\n", "info")
                filled = lib.enrich_targets_online(
                    targets,
                    fetch_metadata=lambda src: pr.fetch_metadata_only(
                        src, self.resolver_cfg),
                    log=log)
                self._log_batch(f"Resolved {filled} title(s).\n", "info")

            matchable = [t for t in targets if t.has_content_key]
            skipped = len(targets) - len(matchable)

            self._log_batch(
                f"\n{len(has_own_file)} row(s) already have their own file_path — "
                f"skipped. {len(matchable)} row(s) are matchable"
                + (f", {skipped} have no title or DOI to match on."
                   if skipped else ".") + "\n", "info")

            if not matchable:
                self._log_batch("Nothing to match against.\n", "ok")
                self._library_map = {}
                self._library_report = None
                return
            if not (self.library_cfg.folder or self._library_extra_paths):
                self._log_batch(
                    f"{len(matchable)} paper(s) could use a local file, but no "
                    "library folder is set. Choose a folder or drop files, then "
                    "scan again.\n", "err")
                return

            files = lib.scan_folder(self.library_cfg,
                                    extra_paths=self._library_extra_paths, log=log)
            if not files:
                self._log_batch("No PDF or PowerPoint files found.\n", "err")
                return

            self._log_batch(f"Reading {len(files)} file(s)…\n", "info")
            fps = []
            for i, f in enumerate(files, 1):
                if self._stop_requested:
                    self._log_batch("\n⏹ Scan stopped.\n", "info")
                    break
                fp = lib.fingerprint_file(str(f), self.library_cfg)
                fps.append(fp)
                if fp.error:
                    self._log_batch(f"   · {fp.name}: {fp.error}\n", "trail")
                if i % 25 == 0:
                    self._log_batch(f"   … {i}/{len(files)}\n", "trail")

            report = lib.match_library(matchable, fps, self.library_cfg, log=log)
            self._library_report = report
            self._target_index = {t.paper_id: t for t in all_targets}

            self._library_map = {}
            for m in report.accepted:
                self._library_map[m.paper_id] = {
                    "path": m.path, "method": m.method, "score": m.score,
                    "kind": m.kind, "confirmed": "auto"}

            self._log_batch(f"\n{report.summary()}\n", "ok")
            for m in report.accepted:
                self._log_batch(f"   ✓ {m.paper_id:14} {Path(m.path).name}"
                                f"   [{m.label()}]\n", "ok")
            if report.proposals:
                self._log_batch("\nNeeds confirmation:\n", "info")
                for m in report.proposals:
                    self._log_batch(f"   ? {m.paper_id:14} {Path(m.path).name}"
                                    f"   [{m.label()}] {m.note}\n", "info")
            if report.unreadable:
                self._log_batch("\nUnreadable (assign by hand if needed):\n", "err")
                for path, err in report.unreadable:
                    self._log_batch(f"   ! {Path(path).name}: {err}\n", "err")

            self.after(0, self._lib_review_btn.config, {"state": "normal"})
            self.after(0, self._refresh_library_status)
            self.after(0, self._open_match_review)
        except Exception as exc:
            self._log_batch(f"Folder scan failed: {type(exc).__name__}: {exc}\n", "err")
        finally:
            self.after(0, self._lib_match_btn.config,
                       {"state": "normal", "text": "Scan Folder for Matches"})
            self.after(0, self._batch_stop_btn.config,
                       {"state": "disabled", "text": "⏹  Stop"})

    def _read_batch_rows(self) -> list:
        """Read the batch CSV with the same encoding fallbacks as the worker."""
        for enc in ("utf-8-sig", "latin-1", "cp1252"):
            try:
                with open(self._batch_csv_path, newline="", encoding=enc) as f:
                    return list(csv.DictReader(f))
            except UnicodeDecodeError:
                continue
            except Exception:
                return []
        with open(self._batch_csv_path, newline="", encoding="utf-8",
                  errors="replace") as f:
            return list(csv.DictReader(f))

    # ── Match review window ───────────────────────────────────────────────────

    def _open_match_review(self):
        """Confirm, reassign, or reject every proposed file→paper assignment.

        This window is the gate. Auto-accepted matches are shown too, because a
        reviewer who cannot see them cannot catch the one that is wrong.
        """
        rep = self._library_report
        if not rep:
            return
        all_ids = sorted(getattr(self, "_target_index", {}).keys())

        win = tk.Toplevel(self)
        win.title("Review File Mapping")
        win.geometry("1080x680")
        win.configure(bg=PALETTE["bg"])
        win.transient(self)

        head = tk.Frame(win, bg=PALETTE["brand"], height=46)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="Confirm file → paper assignments",
                 bg=PALETTE["brand"], fg=PALETTE["brand_accent"],
                 font=("Georgia", 12, "bold")).pack(side="left", padx=20)
        tk.Label(head, text=rep.summary(), bg=PALETTE["brand"],
                 fg="#6A6A60", font=("Helvetica", 8)).pack(side="right", padx=20)

        tk.Label(win,
                 text=("Set any row to (skip) to leave that paper to online retrieval. "
                       "A wrong assignment produces a confident verdict for the wrong "
                       "paper, so rows marked CONFIRM had ambiguous evidence and are "
                       "worth opening the file to check."),
                 bg=PALETTE["bg"], fg=PALETTE["ink_mid"], font=("Helvetica", 8),
                 anchor="w", justify="left", wraplength=1030).pack(
                     fill="x", padx=20, pady=(10, 6))

        canvas = tk.Canvas(win, bg=PALETTE["bg"], highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=(20, 0))
        body = tk.Frame(canvas, bg=PALETTE["bg"])
        cwin = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(cwin, width=e.width))

        hdr = tk.Frame(body, bg=PALETTE["bg"])
        hdr.pack(fill="x", pady=(0, 4))
        for text, w in (("STATUS", 10), ("FILE", 46), ("EVIDENCE", 30),
                        ("ASSIGN TO PAPER", 20)):
            tk.Label(hdr, text=text, bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                     font=("Helvetica", 7, "bold"), width=w, anchor="w").pack(side="left")

        rows_state = []       # (path, kind, method, score, StringVar, was_auto)

        def _add_row(path, kind, method, score, note, status, color, default_pid):
            card = tk.Frame(body, bg=PALETTE["surface"],
                            highlightbackground=PALETTE["border"],
                            highlightthickness=1)
            card.pack(fill="x", pady=2)
            row = tk.Frame(card, bg=PALETTE["surface"])
            row.pack(fill="x", padx=10, pady=7)

            tk.Label(row, text=status, bg=PALETTE["surface"], fg=color,
                     font=("Helvetica", 8, "bold"), width=10, anchor="w").pack(side="left")

            name = Path(path).name
            tk.Label(row, text=(name[:44] + "…") if len(name) > 45 else name,
                     bg=PALETTE["surface"], fg=PALETTE["ink"],
                     font=("Helvetica", 9), width=46, anchor="w").pack(side="left")

            ev = f"{method} {score:.2f}" if method else "—"
            tk.Label(row, text=ev, bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                     font=("Courier New", 8), width=30, anchor="w").pack(side="left")

            var = tk.StringVar(value=default_pid or "(skip)")
            combo = ttk.Combobox(row, textvariable=var, state="readonly",
                                 values=["(skip)"] + all_ids,
                                 font=("Helvetica", 9), width=18)
            combo.pack(side="left")

            if note:
                tk.Label(card, text=f"    {note}", bg=PALETTE["surface"],
                         fg=PALETTE["ink_faint"], font=("Helvetica", 8, "italic"),
                         anchor="w", justify="left", wraplength=980).pack(
                             fill="x", padx=10, pady=(0, 6))

            # Title of the paper currently selected, so the reviewer can sanity
            # check the assignment without leaving the window.
            tlabel = tk.Label(card, text="", bg=PALETTE["surface"],
                              fg=PALETTE["ink_mid"], font=("Helvetica", 8),
                              anchor="w", justify="left", wraplength=980)
            tlabel.pack(fill="x", padx=10, pady=(0, 6))

            def _sync_title(*_a):
                pid = var.get()
                t = getattr(self, "_target_index", {}).get(pid)
                if t and t.title:
                    src = f" [title from {t.title_source}]" if t.title_source else ""
                    tlabel.config(text=f"    → {t.title[:120]}{src}")
                elif t:
                    tlabel.config(text="    → (no title in CSV for this paper)")
                else:
                    tlabel.config(text="")
            var.trace_add("write", _sync_title)
            _sync_title()

            rows_state.append([path, kind, method, score, var])

        for m in rep.accepted:
            _add_row(m.path, m.kind, m.method, m.score, m.note,
                     "AUTO", PALETTE["include"], m.paper_id)
        for m in rep.proposals:
            _add_row(m.path, m.kind, m.method, m.score, m.note,
                     "CONFIRM", PALETTE["manual"], m.paper_id)
        for path, note in rep.unmatched_files:
            _add_row(path, "pdf", "", 0.0, note, "UNMATCHED",
                     PALETTE["ink_faint"], "")
        for path, err in rep.unreadable:
            _add_row(path, "pdf", "", 0.0, f"unreadable: {err}",
                     "UNREADABLE", PALETTE["exclude"], "")

        if rep.unmatched_papers:
            miss = tk.Frame(body, bg=PALETTE["bg"])
            miss.pack(fill="x", pady=(10, 4))
            tk.Label(miss,
                     text=("Papers with no local file (these fall back to online "
                           "retrieval): " + ", ".join(rep.unmatched_papers[:40])
                           + (" …" if len(rep.unmatched_papers) > 40 else "")),
                     bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                     font=("Helvetica", 8, "italic"), anchor="w",
                     justify="left", wraplength=1030).pack(fill="x")

        foot = tk.Frame(win, bg=PALETTE["bg"])
        foot.pack(fill="x", padx=20, pady=12)
        warn = tk.Label(foot, text="", bg=PALETTE["bg"], fg=PALETTE["exclude"],
                        font=("Helvetica", 8, "bold"), anchor="w")
        warn.pack(side="left", fill="x", expand=True)

        def _apply():
            chosen: dict = {}
            dupes = []
            for path, kind, method, score, var in rows_state:
                pid = var.get()
                if not pid or pid == "(skip)":
                    continue
                if pid in chosen:
                    dupes.append(pid)
                    continue
                chosen[pid] = {"path": path, "method": method or "manual",
                               "score": score, "kind": kind,
                               "confirmed": "user"}
            if dupes:
                warn.config(text=("Two files are assigned to the same paper: "
                                  + ", ".join(sorted(set(dupes)))
                                  + ". Each paper takes one file."))
                return
            self._library_map = chosen
            self._log_batch(
                f"\nMapping confirmed: {len(chosen)} paper(s) will screen from "
                "local files.\n", "ok")
            try:
                lib.write_mapping_csv(
                    MAPPING_LOG_CSV,
                    lib.mapping_rows(
                        [lib.Match(paper_id=k, path=v["path"], method=v["method"],
                                   score=v["score"], kind=v["kind"])
                         for k, v in chosen.items()],
                        confirmed_by="user"))
                self._log_batch(f"Mapping logged to {MAPPING_LOG_CSV.name}.\n", "trail")
            except Exception as exc:
                self._log_batch(f"Could not write mapping log: {exc}\n", "err")
            self._refresh_library_status()
            win.destroy()

        self._btn(foot, "Use This Mapping", _apply, "primary",
                  padx=18, pady=7).pack(side="right")
        self._btn(foot, "Cancel", win.destroy, "secondary",
                  padx=14, pady=7).pack(side="right", padx=(0, 8))

    def _export_mapping(self):
        if not self._library_map:
            messagebox.showinfo("Nothing to Export", "No files are mapped yet.")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="batch_with_local_files.csv")
        if not dest:
            return
        rows = self._read_batch_rows() if self._batch_csv_path else []
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=BATCH_CSV_COLUMNS,
                                   extrasaction="ignore")
                w.writeheader()
                if rows:
                    for r in rows:
                        pid = str(r.get("paper_id") or "").strip()
                        out = {k: r.get(k, "") for k in BATCH_CSV_COLUMNS}
                        out["paper_id"] = pid
                        if pid in self._library_map:
                            out["file_path"] = self._library_map[pid]["path"]
                        w.writerow(out)
                else:
                    for pid, v in sorted(self._library_map.items()):
                        w.writerow({"paper_id": pid, "url": "",
                                    "file_path": v["path"], "title": "", "doi": ""})
            messagebox.showinfo(
                "Exported",
                f"Batch CSV with resolved file paths saved to:\n{dest}\n\n"
                "Running this CSV reproduces the same file assignments without "
                "re-matching.")
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc))

    # ── Repository Tab ────────────────────────────────────────────────────────

    def _build_repo_tab(self):
        f = self.tab_repo

        bar = tk.Frame(f, bg=PALETTE["bg"])
        bar.pack(fill="x", padx=24, pady=12)

        self._btn(bar, "Refresh", self._refresh_repository_tab,
                  "secondary", padx=14, pady=5).pack(side="left")
        self._open_folder_btn = self._btn(
            bar, "Open Folder", self._open_repo_folder,
            "ghost", padx=14, pady=5)
        self._open_folder_btn.pack(side="left", padx=8)

        tk.Label(bar, text="Profile", bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8)).pack(side="left", padx=(12, 4))
        self._repo_profile_filter = tk.StringVar(value="All")
        self._repo_profile_combo = ttk.Combobox(
            bar, textvariable=self._repo_profile_filter, state="readonly",
            width=22, style="Profile.TCombobox", font=("Helvetica", 9))
        self._repo_profile_combo.pack(side="left")
        self._repo_profile_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._refresh_repository_tab())

        ff = tk.Frame(bar, bg=PALETTE["bg"])
        ff.pack(side="right")

        tk.Label(ff, text="Search", bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8)).pack(side="left", padx=(0, 4))
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._refresh_repository_tab())
        srch = self._entry(ff, self._filter_var, width=18, dim=False)
        srch.pack(side="left", ipady=4, padx=(0, 16))

        self._rec_filter = tk.StringVar(value="All")
        pill_cfg = [
            ("All",           PALETTE["ink_mid"]),
            ("INCLUDE",       PALETTE["include"]),
            ("EXCLUDE",       PALETTE["exclude"]),
            ("MANUAL_REVIEW", PALETTE["manual"]),
        ]
        for val, fg in pill_cfg:
            rb = tk.Radiobutton(
                ff, text=val.replace("_", " "), variable=self._rec_filter,
                value=val, command=self._refresh_repository_tab,
                bg=PALETTE["bg"], fg=fg, activebackground=PALETTE["bg"],
                activeforeground=fg, selectcolor=PALETTE["bg"],
                font=("Helvetica", 8, "bold"),
                indicatoron=0, relief="flat",
                padx=10, pady=4, cursor="hand2")
            rb.pack(side="left", padx=2)

        tf = tk.Frame(f, bg=PALETTE["bg"])
        tf.pack(fill="both", expand=True, padx=24, pady=(0, 4))

        cols = ("paper_id", "title", "authors", "publication_year",
                "journal_or_venue", "recommendation", "confidence",
                "basis", "profile", "analyzed_at")
        widths = (66, 210, 132, 48, 124, 100, 62, 62, 118, 112)

        self._tree = ttk.Treeview(tf, columns=cols, show="headings",
                                  style="Repo.Treeview", selectmode="browse")
        for col, w in zip(cols, widths):
            label = col.replace("_", " ").title()
            self._tree.heading(col, text=label,
                               command=lambda c=col: self._sort_tree(c))
            self._tree.column(col, width=w, anchor="w", minwidth=40)

        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._tree.tag_configure("include", background=PALETTE["include_bg"])
        self._tree.tag_configure("exclude", background=PALETTE["exclude_bg"])
        self._tree.tag_configure("manual", background=PALETTE["manual_bg"])
        self._tree.bind("<Double-1>", self._view_repo_entry)

        tk.Label(f, text=("Double-click a row to view full analysis  ·  "
                          "Click column headers to sort  ·  "
                          "Basis shows whether screening used full text or abstract only"),
                 bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 7, "italic")).pack(pady=4)

    # ── Settings Tab ──────────────────────────────────────────────────────────

    def _build_config_tab(self):
        outer = tk.Frame(self.tab_config, bg=PALETTE["bg"])
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=PALETTE["bg"], highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        f = tk.Frame(canvas, bg=PALETTE["bg"])
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>",
               lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _wheel)

        self._build_criteria_card(f)
        self._build_retrieval_card(f)
        self._build_library_settings_card(f)
        self._build_apikey_card(f)
        self._build_repo_card(f)
        tk.Frame(f, bg=PALETTE["bg"], height=24).pack()

    # ── Settings: criteria ────────────────────────────────────────────────────

    def _build_criteria_card(self, f):
        inner = self._card(f, label="Screening Criteria", pad=(20, 16))

        tk.Label(inner,
                 text=("Criteria are defined per review. Import a protocol, paste criteria "
                       "text, or edit an existing profile. Compiled criteria are shown for "
                       "review before they can screen a paper."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", pady=(0, 10))

        sel_row = tk.Frame(inner, bg=PALETTE["surface"])
        sel_row.pack(fill="x")
        tk.Label(sel_row, text="Active", bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                 font=("Helvetica", 8, "bold"), width=7, anchor="w").pack(side="left")

        self._profile_var = tk.StringVar()
        self._profile_combo = ttk.Combobox(
            sel_row, textvariable=self._profile_var, state="readonly",
            style="Profile.TCombobox", font=("Helvetica", 10))
        self._profile_combo.pack(side="left", fill="x", expand=True, padx=(6, 10), ipady=3)
        self._profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        self._btn(sel_row, "Preview Prompt", self._preview_prompt,
                  "secondary", padx=12, pady=5).pack(side="left")

        self._profile_meta_label = tk.Label(
            inner, text="", bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
            font=("Courier New", 8), anchor="w", justify="left", wraplength=820)
        self._profile_meta_label.pack(fill="x", pady=(8, 10))

        btn_row = tk.Frame(inner, bg=PALETTE["surface"])
        btn_row.pack(fill="x")
        for text, cmd, style in [
            ("New from Text…", self._new_profile_from_text, "primary"),
            ("Import File…",   self._import_profile_file,   "secondary"),
            ("Edit…",          self._edit_active_profile,   "secondary"),
            ("Duplicate",      self._duplicate_profile,     "ghost"),
            ("Export…",        self._export_profile,        "ghost"),
            ("Delete",         self._delete_profile,        "ghost"),
        ]:
            self._btn(btn_row, text, cmd, style, padx=12, pady=5).pack(
                side="left", padx=(0, 6))

    # ── Settings: retrieval ───────────────────────────────────────────────────

    def _build_retrieval_card(self, f):
        inner = self._card(f, label="PDF Retrieval", pad=(20, 16))

        rc = self.resolver_cfg

        self._res_enabled = tk.BooleanVar(value=rc.enabled)
        cb = tk.Checkbutton(
            inner, text="  Find PDFs automatically from links and DOIs",
            variable=self._res_enabled, command=self._save_resolver_settings,
            bg=PALETTE["surface"], fg=PALETTE["ink"], activebackground=PALETTE["surface"],
            selectcolor=PALETTE["surface"], font=("Helvetica", 10, "bold"),
            anchor="w", relief="flat", cursor="hand2", highlightthickness=0)
        cb.pack(fill="x")

        tk.Label(inner,
                 text=("Resolution order: direct link → arXiv → Unpaywall → OpenAlex → "
                       "Semantic Scholar → Europe PMC → citation_pdf_url scraping.\n"
                       "Open-access copies only. Paywalled articles still need a manual "
                       "upload — expect roughly 50–70% coverage."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", pady=(4, 10))

        # Contact email
        em_row = tk.Frame(inner, bg=PALETTE["surface"])
        em_row.pack(fill="x", pady=(0, 6))
        tk.Label(em_row, text="Contact email", bg=PALETTE["surface"],
                 fg=PALETTE["ink_mid"], font=("Helvetica", 8, "bold"),
                 width=16, anchor="w").pack(side="left")
        self._res_email = tk.StringVar(value=rc.contact_email)
        e = self._entry(em_row, self._res_email, width=38)
        e.pack(side="left", ipady=4)
        e.bind("<FocusOut>", lambda _e: self._save_resolver_settings())
        tk.Label(em_row, text="  Required by Unpaywall; gets faster OpenAlex responses.",
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic")).pack(side="left")

        # OpenAlex key
        oa_row = tk.Frame(inner, bg=PALETTE["surface"])
        oa_row.pack(fill="x", pady=(0, 6))
        tk.Label(oa_row, text="OpenAlex key", bg=PALETTE["surface"],
                 fg=PALETTE["ink_mid"], font=("Helvetica", 8, "bold"),
                 width=16, anchor="w").pack(side="left")
        self._res_oa_key = tk.StringVar(value=rc.openalex_api_key)
        e2 = self._entry(oa_row, self._res_oa_key, width=38)
        e2.pack(side="left", ipady=4)
        e2.bind("<FocusOut>", lambda _e: self._save_resolver_settings())
        tk.Label(oa_row, text="  Optional. Metadata is free; cached PDFs are billed.",
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic")).pack(side="left")

        tk.Frame(inner, bg=PALETTE["border"], height=1).pack(fill="x", pady=10)

        # Source toggles
        tk.Label(inner, text="SOURCES", bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 7, "bold"), anchor="w").pack(fill="x", pady=(0, 4))

        toggles = tk.Frame(inner, bg=PALETTE["surface"])
        toggles.pack(fill="x")
        self._res_toggles = {}
        specs = [
            ("use_unpaywall",        "Unpaywall"),
            ("use_openalex",         "OpenAlex"),
            ("use_semantic_scholar", "Semantic Scholar"),
            ("use_europe_pmc",       "Europe PMC"),
            ("use_arxiv",            "arXiv"),
            ("use_page_scrape",      "Page scraping"),
        ]
        for i, (key, label) in enumerate(specs):
            var = tk.BooleanVar(value=getattr(rc, key))
            self._res_toggles[key] = var
            tk.Checkbutton(
                toggles, text=f"  {label}", variable=var,
                command=self._save_resolver_settings,
                bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                activebackground=PALETTE["surface"], selectcolor=PALETTE["surface"],
                font=("Helvetica", 9), anchor="w", relief="flat",
                cursor="hand2", highlightthickness=0
            ).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 24), pady=1)

        # Billed option, separated
        self._res_oa_content = tk.BooleanVar(value=rc.use_openalex_content)
        tk.Checkbutton(
            inner,
            text="  Use the OpenAlex cached-PDF API when free sources fail  "
                 "(billed per file — requires a key)",
            variable=self._res_oa_content, command=self._save_resolver_settings,
            bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
            activebackground=PALETTE["surface"], selectcolor=PALETTE["surface"],
            font=("Helvetica", 9), anchor="w", relief="flat",
            cursor="hand2", highlightthickness=0).pack(fill="x", pady=(8, 0))

        tk.Frame(inner, bg=PALETTE["border"], height=1).pack(fill="x", pady=10)

        # Abstract-only fallback
        self._abstract_fallback = tk.BooleanVar(
            value=bool(self.settings.get("abstract_only_fallback")))
        tk.Checkbutton(
            inner,
            text="  When no PDF is found, screen from title and abstract instead",
            variable=self._abstract_fallback, command=self._save_resolver_settings,
            bg=PALETTE["surface"], fg=PALETTE["ink"],
            activebackground=PALETTE["surface"], selectcolor=PALETTE["surface"],
            font=("Helvetica", 10, "bold"), anchor="w", relief="flat",
            cursor="hand2", highlightthickness=0).pack(fill="x")

        tk.Label(inner,
                 text=("Stage-1 screening on title and abstract is standard practice and "
                       "avoids most PDF retrieval. Verdicts default to UNCLEAR for anything "
                       "the abstract does not state, so these papers route to MANUAL_REVIEW "
                       "rather than being decided on thin evidence. Records are stamped "
                       "screening_basis = abstract_only."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", pady=(4, 10))

        # Test row
        test_row = tk.Frame(inner, bg=PALETTE["surface"])
        test_row.pack(fill="x")
        tk.Label(test_row, text="Test", bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                 font=("Helvetica", 8, "bold"), width=16, anchor="w").pack(side="left")
        self._res_test_var = tk.StringVar(value="10.7717/peerj.4375")
        self._entry(test_row, self._res_test_var, width=38).pack(side="left", ipady=4)
        self._btn(test_row, "Resolve", self._test_resolver,
                  "secondary", padx=12, pady=4).pack(side="left", padx=8)

        self._res_test_out = tk.Label(
            inner, text="", bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
            font=("Courier New", 8), anchor="w", justify="left", wraplength=820)
        self._res_test_out.pack(fill="x", pady=(8, 0))

    # ── Settings: API key ─────────────────────────────────────────────────────

    def _build_apikey_card(self, f):
        key_inner = self._card(f, label="Anthropic API Key", pad=(20, 16))

        key_row = tk.Frame(key_inner, bg=PALETTE["surface"])
        key_row.pack(fill="x")

        self._key_entry = tk.Entry(
            key_row, textvariable=self._api_key,
            font=("Courier New", 10), show="*", width=50,
            bd=0, relief="flat",
            bg=PALETTE["surface_dim"], fg=PALETTE["ink"],
            insertbackground=PALETTE["ink"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["ink"])
        self._key_entry.pack(side="left", ipady=6, padx=(0, 10))
        self._btn(key_row, "Show / Hide", self._toggle_key_vis,
                  "ghost", padx=12, pady=5).pack(side="left")

        tk.Frame(key_inner, bg=PALETTE["border"], height=1).pack(fill="x", pady=12)

        info_lines = [
            ("Key is stored in memory only — never written to disk.", PALETTE["ink_faint"]),
            ("Get a key:    console.anthropic.com  →  API Keys  →  Create Key",
             PALETTE["ink_mid"]),
            ("Add credits:  console.anthropic.com  →  Billing  (minimum $5)",
             PALETTE["ink_mid"]),
            ("", PALETTE["ink_faint"]),
            (f"Model:   {CLAUDE_MODEL}", PALETTE["ink_mid"]),
            ("Cost:    ~$0.01–0.03 per paper  ·  200 papers ≈ $4–6 total",
             PALETTE["ink_mid"]),
        ]
        for text, color in info_lines:
            tk.Label(key_inner, text=text, bg=PALETTE["surface"],
                     fg=color, font=("Helvetica", 8), anchor="w",
                     justify="left").pack(fill="x")

    # ── Settings: repository ──────────────────────────────────────────────────

    def _build_repo_card(self, f):
        repo_inner = self._card(f, label="Repository", pad=(20, 16))

        loc_row = tk.Frame(repo_inner, bg=PALETTE["surface"])
        loc_row.pack(fill="x")
        self._repo_dir_label = tk.Label(
            loc_row, text=str(REPO_DIR),
            bg=PALETTE["surface"], fg=PALETTE["ink"],
            font=("Courier New", 9), anchor="w")
        self._repo_dir_label.pack(side="left", fill="x", expand=True)
        self._btn(loc_row, "Change", self._change_repo_location,
                  "secondary", padx=12, pady=4).pack(side="right")

        tk.Frame(repo_inner, bg=PALETTE["border"], height=1).pack(fill="x", pady=10)

        for label, fname in [
            ("Full data (JSON):",  "paper_repository.json"),
            ("All papers (CSV):",  "paper_repository.csv"),
            ("Per profile (CSV):", "repository_<profile_id>.csv"),
            ("Criteria profiles:", "criteria_profiles/*.json"),
            ("Batch template:",    "batch_template.csv"),
        ]:
            row = tk.Frame(repo_inner, bg=PALETTE["surface"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=PALETTE["surface"],
                     fg=PALETTE["ink_faint"], font=("Helvetica", 8),
                     width=18, anchor="w").pack(side="left")
            tk.Label(row, text=fname, bg=PALETTE["surface"],
                     fg=PALETTE["ink_mid"], font=("Courier New", 8),
                     anchor="w").pack(side="left")

        tk.Label(repo_inner,
                 text=("Criteria columns differ between profiles, so each profile also gets "
                       "its own CSV with that profile's full criteria columns."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic"), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", pady=(8, 0))

    # ══════════════════════════════════════════════════════════════════════════
    # Profile management actions
    # ══════════════════════════════════════════════════════════════════════════

    def _refresh_profile_widgets(self):
        profiles = cp.list_profiles(REPO_DIR)
        self._profile_lookup = {}
        labels = []
        for p in profiles:
            label = f"{p.get('profile_name')}  (v{p.get('profile_version')})"
            self._profile_lookup[label] = p["profile_id"]
            labels.append(label)
        self._profile_combo["values"] = labels

        active_id = self.active_profile["profile_id"]
        for label, pid in self._profile_lookup.items():
            if pid == active_id:
                self._profile_var.set(label)
                break

        # Repository filter
        filt = ["All"] + [p.get("profile_name") for p in profiles]
        self._repo_profile_combo["values"] = filt
        self._repo_profile_name_to_id = {
            p.get("profile_name"): p["profile_id"] for p in profiles}
        if self._repo_profile_filter.get() not in filt:
            self._repo_profile_filter.set("All")

        # Labels
        p = self.active_profile
        n_crit = len(p.get("criteria", []))
        n_req = sum(1 for c in p["criteria"] if c.get("required_for_include", True))
        used = self._profile_in_use(p["profile_id"], p["profile_version"])
        yr = p.get("min_publication_year") or "none"
        meta = (f"{n_crit} criteria ({n_req} required)   ·   min year: {yr}   ·   "
                f"language: {p.get('language_requirement') or 'any'}   ·   "
                f"{used} paper(s) screened with this version")
        if p.get("builtin"):
            meta += "   ·   built-in"
        self._profile_meta_label.config(text=meta)

        name_line = f"{p.get('profile_name')}  ·  v{p.get('profile_version')}"
        self._single_profile_label.config(text=name_line)
        self._batch_profile_label.config(text=f"Screening against: {name_line}")
        self._header_profile_label.config(text=p.get("profile_name", ""))

    def _on_profile_selected(self, _event=None):
        label = self._profile_var.get()
        pid = self._profile_lookup.get(label)
        if not pid or pid == self.active_profile["profile_id"]:
            return
        prof = cp.load_profile(REPO_DIR, pid)
        if not prof:
            messagebox.showerror("Profile", "Could not load that profile.")
            return
        self.active_profile = cp.normalize_profile(prof)
        self.settings["active_profile_id"] = pid
        save_settings(self.settings)
        self._refresh_profile_widgets()

    def _preview_prompt(self):
        prompt = cp.build_system_prompt(self.active_profile)
        self._show_text_window(
            f"Prompt Preview — {self.active_profile.get('profile_name')}",
            prompt,
            subtitle=("This is the exact system prompt sent to Claude for every paper "
                      "screened with this profile."))

    def _show_text_window(self, title, body, subtitle=""):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("900x720")
        win.configure(bg=PALETTE["bg"])

        hdr = tk.Frame(win, bg=PALETTE["brand"], height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text=title, bg=PALETTE["brand"], fg=PALETTE["brand_accent"],
                 font=("Helvetica", 10, "bold"), anchor="w",
                 padx=20).pack(fill="both", expand=True)

        if subtitle:
            tk.Label(win, text=subtitle, bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                     font=("Helvetica", 8, "italic"), anchor="w", justify="left",
                     wraplength=840).pack(fill="x", padx=20, pady=(10, 0))

        frame = tk.Frame(win, bg=PALETTE["console_bg"])
        frame.pack(fill="both", expand=True, padx=16, pady=12)
        txt = scrolledtext.ScrolledText(
            frame, font=("Courier New", 9),
            bg=PALETTE["console_bg"], fg=PALETTE["console_fg"],
            bd=0, padx=16, pady=12, wrap="word")
        txt.pack(fill="both", expand=True)
        txt.insert("end", body)
        txt.configure(state="disabled")

        row = tk.Frame(win, bg=PALETTE["bg"])
        row.pack(fill="x", padx=16, pady=(0, 12))

        def _copy():
            self.clipboard_clear()
            self.clipboard_append(body)
        self._btn(row, "Copy", _copy, "secondary", padx=14, pady=5).pack(side="left")
        self._btn(row, "Close", win.destroy, "ghost", padx=14, pady=5).pack(side="right")
        return win

    # ── New from text (the compile flow) ──────────────────────────────────────

    def _new_profile_from_text(self):
        win = tk.Toplevel(self)
        win.title("New Criteria Profile")
        win.geometry("880x700")
        win.configure(bg=PALETTE["bg"])

        hdr = tk.Frame(win, bg=PALETTE["brand"], height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="New Criteria Profile", bg=PALETTE["brand"],
                 fg=PALETTE["brand_accent"], font=("Helvetica", 10, "bold"),
                 anchor="w", padx=20).pack(fill="both", expand=True)

        tk.Label(win,
                 text=("Paste your inclusion and exclusion criteria below — a protocol "
                       "excerpt, a PICO statement, or plain prose. Claude will convert it "
                       "into a structured profile with explicit boundary rules, which you "
                       "then review and edit before saving.\n\n"
                       "Free-text criteria rarely state what to EXCLUDE. The compiler "
                       "infers exclusion rules and flags every one it added, so you can "
                       "check them rather than discover the gaps mid-review."),
                 bg=PALETTE["bg"], fg=PALETTE["ink_mid"],
                 font=("Helvetica", 9), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", padx=20, pady=(12, 8))

        frame = tk.Frame(win, bg=PALETTE["surface"],
                         highlightbackground=PALETTE["border"], highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=20)
        txt = scrolledtext.ScrolledText(
            frame, font=("Helvetica", 10),
            bg=PALETTE["surface"], fg=PALETTE["ink"],
            insertbackground=PALETTE["ink"],
            bd=0, padx=14, pady=12, wrap="word")
        txt.pack(fill="both", expand=True)

        status = tk.Label(win, text="", bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                          font=("Helvetica", 8, "italic"), anchor="w")
        status.pack(fill="x", padx=20, pady=(6, 0))

        row = tk.Frame(win, bg=PALETTE["bg"])
        row.pack(fill="x", padx=20, pady=12)

        def _load_file():
            path = filedialog.askopenfilename(
                title="Load Criteria Text",
                filetypes=[("Text and documents", "*.txt *.md *.docx *.json"),
                           ("All files", "*.*")])
            if not path:
                return
            try:
                raw, prof = cp.read_criteria_file(path)
            except Exception as exc:
                messagebox.showerror("Read Failed", str(exc), parent=win)
                return
            if prof:
                win.destroy()
                self._open_profile_editor(prof, is_new=True,
                                          notes=["Imported from an existing profile JSON."])
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", raw)
            status.config(text=f"Loaded {Path(path).name} ({len(raw)} characters)")

        def _compile():
            raw = txt.get("1.0", "end").strip()
            if len(raw) < 40:
                messagebox.showwarning(
                    "Not Enough Text",
                    "Please paste at least a few sentences of criteria.", parent=win)
                return
            key = self._api_key.get().strip()
            if not key:
                messagebox.showwarning(
                    "API Key Required",
                    "Enter your Anthropic API key in Settings first.", parent=win)
                return

            compile_btn.config(state="disabled", text="Compiling…")
            status.config(text="Compiling criteria with Claude — this takes 20–60 seconds…")

            def _worker():
                try:
                    profile, notes = cp.compile_criteria_with_claude(
                        raw, key, CLAUDE_MODEL,
                        progress=lambda m: self.after(0, status.config, {"text": m}))
                except Exception as exc:
                    self.after(0, compile_btn.config,
                               {"state": "normal", "text": "Compile with Claude"})
                    self.after(0, status.config, {"text": "Compilation failed."})
                    self.after(0, messagebox.showerror, "Compile Failed", str(exc))
                    return
                self.after(0, win.destroy)
                self.after(0, self._open_profile_editor, profile, True, notes, raw)

            threading.Thread(target=_worker, daemon=True).start()

        self._btn(row, "Load from File…", _load_file, "secondary",
                  padx=14, pady=6).pack(side="left")
        compile_btn = self._btn(row, "Compile with Claude", _compile, "primary",
                                padx=18, pady=6)
        compile_btn.pack(side="right")
        self._btn(row, "Cancel", win.destroy, "ghost",
                  padx=14, pady=6).pack(side="right", padx=(0, 8))

    # ── Import / edit / duplicate / export / delete ────────────────────────────

    def _import_profile_file(self):
        path = filedialog.askopenfilename(
            title="Import Criteria",
            filetypes=[("Criteria files", "*.json *.txt *.md *.docx"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            raw, prof = cp.read_criteria_file(path)
        except Exception as exc:
            messagebox.showerror("Import Failed", str(exc))
            return

        if prof:
            prof = cp.normalize_profile(prof)
            prof["builtin"] = False
            ok, errs = cp.validate_profile(prof)
            notes = [] if ok else ["This file did not fully validate:"] + errs
            self._open_profile_editor(prof, is_new=True, notes=notes)
            return

        # Raw text — hand it to the compiler
        key = self._api_key.get().strip()
        if not key:
            messagebox.showwarning(
                "API Key Required",
                "This file is free text, so it needs to be compiled into a structured "
                "profile. Enter your Anthropic API key in Settings first.")
            return
        if not messagebox.askyesno(
                "Compile Criteria",
                f"{Path(path).name} contains free text rather than a structured profile.\n\n"
                "Compile it into a criteria profile with Claude? You will review the "
                "result before it is saved."):
            return

        self._set_progress("Compiling criteria…")

        def _worker():
            try:
                profile, notes = cp.compile_criteria_with_claude(raw, key, CLAUDE_MODEL)
            except Exception as exc:
                self.after(0, messagebox.showerror, "Compile Failed", str(exc))
                return
            self.after(0, self._open_profile_editor, profile, True, notes, raw)

        threading.Thread(target=_worker, daemon=True).start()

    def _edit_active_profile(self):
        self._open_profile_editor(json.loads(json.dumps(self.active_profile)),
                                  is_new=False)

    def _duplicate_profile(self):
        p = json.loads(json.dumps(self.active_profile))
        p["profile_id"] = cp.slugify(p["profile_id"] + "-copy")
        p["profile_name"] = p["profile_name"] + " (copy)"
        p["profile_version"] = "1.0"
        p["builtin"] = False
        p["locked"] = False
        p["created_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._open_profile_editor(p, is_new=True,
                                  notes=["Duplicated from "
                                         f"{self.active_profile.get('profile_name')}."])

    def _export_profile(self):
        p = self.active_profile
        dest = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile=f"{p['profile_id']}_v{p['profile_version']}.json")
        if not dest:
            return
        out = {k: v for k, v in p.items() if not k.startswith("_")}
        Path(dest).write_text(json.dumps(out, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        messagebox.showinfo("Exported", f"Profile saved to:\n{dest}")

    def _delete_profile(self):
        p = self.active_profile
        if p.get("builtin"):
            messagebox.showwarning(
                "Built-in Profile",
                "The built-in profile cannot be deleted. Duplicate it and edit the copy "
                "instead.")
            return
        n = self._profile_in_use(p["profile_id"])
        msg = f"Delete the profile '{p['profile_name']}'?"
        if n:
            msg += (f"\n\n{n} repository record(s) were screened with it. Those records "
                    "will keep their profile stamp, but the criteria behind them will no "
                    "longer be readable. Export the profile first if you may need to "
                    "report on it.")
        if not messagebox.askyesno("Delete Profile", msg):
            return
        cp.delete_profile(REPO_DIR, p["profile_id"])
        self.settings["active_profile_id"] = cp.DEFAULT_PROFILE["profile_id"]
        save_settings(self.settings)
        self.active_profile = self._load_active_profile()
        self._refresh_profile_widgets()

    # ── The profile editor ────────────────────────────────────────────────────

    def _open_profile_editor(self, profile: dict, is_new: bool,
                             notes: list | None = None, raw_source: str = ""):
        win = tk.Toplevel(self)
        win.title(("Review Compiled Criteria" if is_new else "Edit Criteria")
                  + f" — {profile.get('profile_name', '')}")
        win.geometry("980x820")
        win.configure(bg=PALETTE["bg"])

        hdr = tk.Frame(win, bg=PALETTE["brand"], height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr,
                 text=("Review before use — check every exclusion rule"
                       if is_new else f"Editing: {profile.get('profile_name','')}"),
                 bg=PALETTE["brand"], fg=PALETTE["brand_accent"],
                 font=("Helvetica", 10, "bold"), anchor="w",
                 padx=20).pack(fill="both", expand=True)

        # Compiler notes
        if notes:
            nf = tk.Frame(win, bg=PALETTE["manual_bg"],
                          highlightbackground=PALETTE["manual"], highlightthickness=1)
            nf.pack(fill="x", padx=20, pady=(12, 0))
            tk.Label(nf, text="COMPILER NOTES — REVIEW THESE",
                     bg=PALETTE["manual_bg"], fg=PALETTE["manual"],
                     font=("Helvetica", 7, "bold"), anchor="w",
                     padx=12, pady=(8, 2)).pack(fill="x")
            body = "\n".join(f"•  {n}" for n in notes)
            tk.Label(nf, text=body, bg=PALETTE["manual_bg"], fg=PALETTE["ink"],
                     font=("Helvetica", 9), anchor="w", justify="left",
                     wraplength=900, padx=12, pady=(0, 10)).pack(fill="x")

        tk.Label(win,
                 text=("Edit the profile JSON below. Every criterion needs both include_if "
                       "and exclude_if conditions — a criterion with no exclusion rules is "
                       "the fastest way to get false INCLUDEs."),
                 bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic"), anchor="w", justify="left",
                 wraplength=920).pack(fill="x", padx=20, pady=(10, 6))

        frame = tk.Frame(win, bg=PALETTE["console_bg"],
                         highlightbackground=PALETTE["border"], highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=20)
        txt = scrolledtext.ScrolledText(
            frame, font=("Courier New", 9),
            bg=PALETTE["console_bg"], fg=PALETTE["console_fg"],
            insertbackground=PALETTE["console_fg"],
            bd=0, padx=14, pady=12, wrap="none")
        txt.pack(fill="both", expand=True)
        clean = {k: v for k, v in profile.items() if not k.startswith("_")}
        txt.insert("1.0", json.dumps(clean, indent=2, ensure_ascii=False))

        status = tk.Label(win, text="", bg=PALETTE["bg"], fg=PALETTE["ink_faint"],
                          font=("Helvetica", 8), anchor="w", justify="left",
                          wraplength=920)
        status.pack(fill="x", padx=20, pady=(6, 0))

        def _parse() -> dict | None:
            try:
                return json.loads(txt.get("1.0", "end"))
            except json.JSONDecodeError as exc:
                status.config(text=f"JSON error: {exc}", fg=PALETTE["exclude"])
                return None

        def _validate(silent=False):
            d = _parse()
            if d is None:
                return None
            ok, errs = cp.validate_profile(d)
            if ok:
                status.config(text="Valid. Ready to save.", fg=PALETTE["include"])
                return d
            status.config(text="Validation failed:\n  • " + "\n  • ".join(errs),
                          fg=PALETTE["exclude"])
            return None

        def _preview():
            d = _parse()
            if d is None:
                return
            try:
                self._show_text_window(
                    "Prompt Preview (unsaved)", cp.build_system_prompt(d),
                    subtitle="Rendered from the JSON currently in the editor.")
            except Exception as exc:
                status.config(text=f"Could not render prompt: {exc}",
                              fg=PALETTE["exclude"])

        def _save():
            d = _validate()
            if d is None:
                return

            existing = cp.load_profile(REPO_DIR, d["profile_id"])
            if existing and is_new and existing.get("profile_version") == d.get(
                    "profile_version"):
                if not messagebox.askyesno(
                        "Profile Exists",
                        f"A profile with id '{d['profile_id']}' and version "
                        f"{d['profile_version']} already exists.\n\nOverwrite it?",
                        parent=win):
                    return

            # Version locking: once records exist against a version, edits fork.
            if not is_new:
                n = self._profile_in_use(d["profile_id"], d.get("profile_version"))
                if n and existing and existing != d:
                    new_v = cp.bump_version(d.get("profile_version", "1.0"))
                    if not messagebox.askyesno(
                            "Screened Papers Exist",
                            f"{n} paper(s) were already screened with "
                            f"{d['profile_name']} v{d['profile_version']}.\n\n"
                            f"Saving will create version {new_v} so those decisions stay "
                            "interpretable. Existing records keep their original version "
                            "stamp.\n\nContinue?",
                            parent=win):
                        return
                    d["profile_version"] = new_v

            d["builtin"] = False
            if raw_source:
                d["source_text"] = raw_source[:20000]
            try:
                cp.save_profile(REPO_DIR, d)
            except Exception as exc:
                status.config(text=f"Save failed: {exc}", fg=PALETTE["exclude"])
                return

            self.active_profile = cp.normalize_profile(d)
            self.settings["active_profile_id"] = d["profile_id"]
            save_settings(self.settings)
            self._refresh_profile_widgets()
            win.destroy()
            messagebox.showinfo(
                "Saved",
                f"'{d['profile_name']}' v{d['profile_version']} is now the active "
                "screening profile.")

        row = tk.Frame(win, bg=PALETTE["bg"])
        row.pack(fill="x", padx=20, pady=12)
        self._btn(row, "Validate", lambda: _validate(), "secondary",
                  padx=14, pady=6).pack(side="left")
        self._btn(row, "Preview Prompt", _preview, "ghost",
                  padx=14, pady=6).pack(side="left", padx=8)
        self._btn(row, "Save and Activate", _save, "primary",
                  padx=18, pady=6).pack(side="right")
        self._btn(row, "Cancel", win.destroy, "ghost",
                  padx=14, pady=6).pack(side="right", padx=(0, 8))

        _validate(silent=True)

    # ══════════════════════════════════════════════════════════════════════════
    # Settings helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _toggle_key_vis(self):
        self._key_entry.config(show="" if self._key_entry.cget("show") == "*" else "*")

    def _save_resolver_settings(self):
        rc = self.resolver_cfg
        rc.enabled = bool(self._res_enabled.get())
        rc.contact_email = self._res_email.get().strip()
        rc.openalex_api_key = self._res_oa_key.get().strip()
        rc.use_openalex_content = bool(self._res_oa_content.get())
        for key, var in self._res_toggles.items():
            setattr(rc, key, bool(var.get()))
        self.settings["resolver"] = rc.to_dict()
        self.settings["abstract_only_fallback"] = bool(self._abstract_fallback.get())
        save_settings(self.settings)

    def _save_library_settings(self):
        cfg = self.library_cfg
        cfg.folder = self._lib_folder_var.get().strip()
        cfg.recursive = bool(self._lib_recursive.get())
        if hasattr(self, "_lib_auto_var"):
            try:
                cfg.auto_accept = max(0.60, min(0.99, float(self._lib_auto_var.get())))
            except (TypeError, ValueError):
                pass
        if hasattr(self, "_lib_floor_var"):
            try:
                cfg.review_floor = max(0.30, min(cfg.auto_accept - 0.01,
                                                 float(self._lib_floor_var.get())))
            except (TypeError, ValueError):
                pass
        if hasattr(self, "_lib_convert_var"):
            cfg.convert_slides_to_pdf = bool(self._lib_convert_var.get())
        if hasattr(self, "_lib_conservative_var"):
            cfg.slides_conservative_prompt = bool(self._lib_conservative_var.get())
        self.settings["library"] = cfg.to_dict()
        save_settings(self.settings)

    # ── Settings: local library ───────────────────────────────────────────────

    def _build_library_settings_card(self, f):
        inner = self._card(f, label="Local File Library", pad=(20, 16))

        caps = lib.capabilities()
        rows = [
            ("PDF text extraction (pypdf)", caps["pypdf"],
             "pip install pypdf"),
            ("PowerPoint reading (python-pptx)", caps["python_pptx"],
             "pip install python-pptx"),
            ("Deck → PDF conversion (LibreOffice)", caps["libreoffice"],
             "optional; without it decks are read as text and legacy .ppt "
             "cannot be opened at all"),
            ("Drag and drop (tkinterdnd2)", self._dnd_ready,
             "pip install tkinterdnd2"),
        ]
        tk.Label(inner, text="This install can:", bg=PALETTE["surface"],
                 fg=PALETTE["ink_mid"], font=("Helvetica", 8, "bold"),
                 anchor="w").pack(fill="x")
        for label, ok, hint in rows:
            row = tk.Frame(inner, bg=PALETTE["surface"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text="✓" if ok else "✗", bg=PALETTE["surface"],
                     fg=PALETTE["include"] if ok else PALETTE["exclude"],
                     font=("Helvetica", 9, "bold"), width=3).pack(side="left")
            tk.Label(row, text=label, bg=PALETTE["surface"], fg=PALETTE["ink"],
                     font=("Helvetica", 9), width=34, anchor="w").pack(side="left")
            if not ok:
                tk.Label(row, text=hint, bg=PALETTE["surface"],
                         fg=PALETTE["ink_faint"], font=("Courier New", 8),
                         anchor="w").pack(side="left")
        if caps["libreoffice_path"]:
            tk.Label(inner, text=f"    LibreOffice: {caps['libreoffice_path']}",
                     bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                     font=("Courier New", 8), anchor="w").pack(fill="x")

        self._divider(inner, vertical_pad=10)

        tk.Label(inner,
                 text=("Match confidence thresholds. Raising the auto-accept value "
                       "sends more assignments to manual review; lowering the floor "
                       "offers weaker guesses instead of leaving files unmatched."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8), anchor="w", justify="left",
                 wraplength=820).pack(fill="x", pady=(0, 8))

        thr = tk.Frame(inner, bg=PALETTE["surface"])
        thr.pack(fill="x")
        self._lib_auto_var = tk.StringVar(value=f"{self.library_cfg.auto_accept:.2f}")
        self._lib_floor_var = tk.StringVar(value=f"{self.library_cfg.review_floor:.2f}")
        for label, var in (("Auto-accept at or above", self._lib_auto_var),
                           ("Report unmatched below", self._lib_floor_var)):
            cell = tk.Frame(thr, bg=PALETTE["surface"])
            cell.pack(side="left", padx=(0, 24))
            tk.Label(cell, text=label, bg=PALETTE["surface"], fg=PALETTE["ink_mid"],
                     font=("Helvetica", 8, "bold"), anchor="w").pack(side="left")
            e = self._entry(cell, var, width=6)
            e.pack(side="left", ipady=3, padx=(6, 0))
            e.bind("<FocusOut>", lambda _e: self._save_library_settings())

        self._lib_convert_var = tk.BooleanVar(
            value=self.library_cfg.convert_slides_to_pdf)
        tk.Checkbutton(
            inner,
            text=("  Convert PowerPoint to PDF before screening (keeps figures and "
                  "charts; needs LibreOffice)"),
            variable=self._lib_convert_var, command=self._save_library_settings,
            bg=PALETTE["surface"], fg=PALETTE["ink"],
            activebackground=PALETTE["surface"], selectcolor=PALETTE["surface"],
            font=("Helvetica", 9), anchor="w", relief="flat",
            cursor="hand2", highlightthickness=0).pack(fill="x", pady=(10, 0))

        self._lib_conservative_var = tk.BooleanVar(
            value=self.library_cfg.slides_conservative_prompt)
        tk.Checkbutton(
            inner,
            text=("  Screen decks conservatively (a deck's silence on a criterion "
                  "becomes UNCLEAR rather than NO)"),
            variable=self._lib_conservative_var, command=self._save_library_settings,
            bg=PALETTE["surface"], fg=PALETTE["ink"],
            activebackground=PALETTE["surface"], selectcolor=PALETTE["surface"],
            font=("Helvetica", 9), anchor="w", relief="flat",
            cursor="hand2", highlightthickness=0).pack(fill="x", pady=(2, 0))
        tk.Label(inner,
                 text=("Off by default: decks are screened as full text. Turn on if "
                       "deck-sourced verdicts look overconfident — a deck omits "
                       "method detail a paper would state. Deck-sourced rows are "
                       "always tagged source_file_type=slides either way."),
                 bg=PALETTE["surface"], fg=PALETTE["ink_faint"],
                 font=("Helvetica", 8, "italic"), anchor="w", justify="left",
                 wraplength=800).pack(fill="x", padx=(24, 0), pady=(2, 0))

    def _test_resolver(self):
        self._save_resolver_settings()
        target = self._res_test_var.get().strip()
        if not target:
            return
        self._res_test_out.config(text="Resolving…", fg=PALETTE["ink_faint"])
        trail: list[str] = []

        def _worker():
            res = pr.resolve_pdf(target, self.resolver_cfg, log=trail.append)
            lines = [f"  {t}" for t in trail]
            if res.ok:
                head = f"✓ {res.summary()}   via {res.pdf_url[:90]}"
                color = PALETTE["include"]
            else:
                head = f"✗ {res.error}"
                color = PALETTE["exclude"]
                if self._abstract_fallback.get():
                    meta = pr.fetch_metadata_only(target, self.resolver_cfg)
                    if meta.get("abstract"):
                        head += ("   — abstract-only fallback WOULD apply "
                                 f"({len(meta['abstract'])} chars)")
                        color = PALETTE["manual"]
            body = head + ("\n" + "\n".join(lines) if lines else "")
            self.after(0, self._res_test_out.config, {"text": body, "fg": color})

        threading.Thread(target=_worker, daemon=True).start()

    def _open_repo_folder(self):
        try:
            if os.name == "nt":
                os.startfile(REPO_DIR)
            elif os.uname().sysname == "Darwin":
                os.system(f'open "{REPO_DIR}"')
            else:
                os.system(f'xdg-open "{REPO_DIR}"')
        except Exception as exc:
            messagebox.showerror("Could Not Open Folder", str(exc))

    def _change_repo_location(self):
        new_path = filedialog.askdirectory(
            title="Select Repository Folder", initialdir=str(REPO_DIR))
        if not new_path:
            return
        set_repo_dir(Path(new_path))
        ensure_repo()
        self.settings["repo_dir"] = str(REPO_DIR)
        save_settings(self.settings)
        self._repo_dir_label.config(text=str(REPO_DIR))
        self.active_profile = self._load_active_profile()
        self._refresh_profile_widgets()
        self._refresh_repository_tab()
        messagebox.showinfo(
            "Repository Location Updated",
            f"Repository is now at:\n{REPO_DIR}\n\n"
            "New analyses will be saved here.\n"
            "Previously analyzed papers in the old location are not moved.")

    # ══════════════════════════════════════════════════════════════════════════
    # Single paper actions
    # ══════════════════════════════════════════════════════════════════════════

    def _fetch_url(self):
        source = self._url_var.get().strip()
        if not source:
            messagebox.showwarning("No Link", "Enter a DOI, link, or arXiv id first.")
            return
        self._save_resolver_settings()
        self._set_progress("Locating PDF…")
        self._set_busy(True)
        self._resolve_trail.config(text="")
        trail: list[str] = []

        def _do():
            res = pr.resolve_pdf(source, self.resolver_cfg, log=trail.append)
            self.after(0, self._resolve_trail.config,
                       {"text": "\n".join(trail[-8:])})

            if res.ok:
                self._current_pdf_bytes = res.pdf_bytes
                self._current_meta_text = None
                self._current_payload = res.pdf_bytes
                self._current_basis = "full_text"
                self._current_source_kind = "pdf"
                self._current_pdf_source = res.resolved_via
                self._current_pdf_url = res.pdf_url
                self.after(0, lambda: self._file_label.config(
                    text=f"{res.resolved_via} — {len(res.pdf_bytes)//1024} KB",
                    fg=PALETTE["include"]))
                self.after(0, self._set_progress, "PDF located — ready to analyze.")
                self.after(0, self._set_busy, False)
                return

            # Abstract-only fallback
            if self._abstract_fallback.get():
                self.after(0, self._set_progress, "No PDF — trying abstract…")
                meta = pr.fetch_metadata_only(source, self.resolver_cfg, log=trail.append)
                self.after(0, self._resolve_trail.config,
                           {"text": "\n".join(trail[-8:])})
                if meta.get("abstract"):
                    self._current_pdf_bytes = None
                    self._current_meta_text = pr.format_metadata_as_text(meta)
                    self._current_payload = self._current_meta_text
                    self._current_basis = "abstract_only"
                    self._current_source_kind = "pdf"
                    self._current_pdf_source = "abstract only"
                    self._current_pdf_url = ""
                    self.after(0, lambda: self._file_label.config(
                        text="Abstract only — no full text", fg=PALETTE["manual"]))
                    self.after(0, self._set_progress,
                               "Abstract retrieved. Screening will be conservative.")
                    self.after(0, self._set_busy, False)
                    return

            self._current_pdf_bytes = None
            self._current_meta_text = None
            self._current_payload = None
            self._current_basis = "full_text"
            self._current_source_kind = "pdf"
            self._current_pdf_source = ""
            self.after(0, lambda: self._file_label.config(
                text=f"Not found: {res.error[:50]}", fg=PALETTE["exclude"]))
            self.after(0, self._set_progress, "No open-access PDF — upload manually.")
            self.after(0, messagebox.showwarning, "PDF Not Found",
                       f"Could not locate an open-access PDF.\n\n{res.error}\n\n"
                       "Tried:\n" + "\n".join(f"  • {t}" for t in trail[-6:]) +
                       "\n\nUpload the PDF manually to continue.")
            self.after(0, self._set_busy, False)

        threading.Thread(target=_do, daemon=True).start()

    def _upload_pdf(self):
        """Upload a PDF or a PowerPoint deck for single-paper screening."""
        path = filedialog.askopenfilename(
            title="Select PDF or PowerPoint",
            filetypes=[("PDF or PowerPoint", "*.pdf *.pptx *.pptm *.ppt"),
                       ("PDF files", "*.pdf"),
                       ("PowerPoint", "*.pptx *.pptm *.ppt")])
        if not path:
            return
        loaded = lib.load_for_screening(path, self.library_cfg,
                                        log=lambda m: self._set_progress(m))
        if loaded["error"]:
            messagebox.showerror(
                "Could Not Read File",
                f"{Path(path).name}\n\n{loaded['error']}")
            self._set_progress("Ready.")
            return

        payload = loaded["payload"]
        self._current_payload = payload
        self._current_basis = loaded["basis"]
        self._current_source_kind = loaded.get("source_kind", "pdf")
        self._current_pdf_bytes = payload if isinstance(payload, bytes) else None
        self._current_meta_text = None if isinstance(payload, bytes) else payload
        self._current_pdf_source = loaded["source"] or "manual upload"
        self._current_pdf_url = ""

        size = (f"{len(payload)//1024} KB" if isinstance(payload, bytes)
                else f"{len(payload):,} chars of slide text")
        note = "  ·  deck" if self._current_source_kind == "slides" else ""
        name = Path(path).name
        self._file_label.config(text=f"{name} ({size}){note}",
                                fg=PALETTE["include"])
        self._resolve_trail.config(text="")
        self._set_progress(f"Loaded: {name}")

    def _run_single_analysis(self):
        if not self._validate_ready(need_pdf=True):
            return
        paper_id = self._paper_id_var.get().strip()
        if not paper_id:
            messagebox.showwarning("Missing ID", "Please enter a Paper ID.")
            return
        self._set_busy(True)
        self._set_progress("Starting analysis…")
        self._single_timer_label.config(text="")
        self._start_single_timer()
        threading.Thread(target=self._analysis_worker, args=(paper_id,),
                         daemon=True).start()

    def _analysis_worker(self, paper_id: str):
        t0 = time.monotonic()
        # A deck upload sets _current_basis itself; the abstract-only fallback and
        # a plain PDF are inferred from which payload is present.
        payload = self._current_payload
        basis = self._current_basis or "full_text"
        if payload is None:
            payload = self._current_meta_text or self._current_pdf_bytes
            basis = "abstract_only" if self._current_meta_text else "full_text"
        source_kind = getattr(self, "_current_source_kind", "pdf")
        try:
            result = analyze_paper(
                payload, self.active_profile,
                api_key=self._api_key.get().strip(),
                screening_basis=basis,
                progress_callback=lambda m: self.after(0, self._set_progress, m),
            )
            elapsed = time.monotonic() - t0
            entry = self._build_repo_entry(paper_id, result, basis=basis,
                                           source_kind=source_kind)
            save_to_repository(entry)
            self.after(0, self._display_result, result, paper_id)
            self.after(0, self._refresh_repository_tab)
            self.after(0, self._set_progress,
                       f"Analysis complete — saved to repository.  "
                       f"({self._fmt_elapsed(elapsed)})")
            self.after(0, self._stop_single_timer, elapsed)
        except Exception as e:
            self.after(0, self._stop_single_timer, None)
            self.after(0, self._set_progress, "Analysis failed.")
            self.after(0, messagebox.showerror, "Error", str(e))
        finally:
            self.after(0, self._set_busy, False)

    # ── Repository entry construction (profile-driven) ────────────────────────

    def _build_repo_entry(self, paper_id: str, result: dict,
                          basis: str = "full_text",
                          url: str = None, file_path: str = "",
                          pdf_source: str = None, pdf_url: str = None,
                          match_method: str = "", match_score: str = "",
                          source_kind: str = "pdf") -> dict:
        """Flatten a result into a repository record using the active profile's shape."""
        profile = self.active_profile
        crit = result.get("criteria", {}) or {}

        entry = {
            "paper_id":             paper_id,
            "title":                str(result.get("title") or "").strip(),
            "authors":              str(result.get("authors") or "").strip(),
            "publication_year":     str(result.get("publication_year") or "").strip(),
            "journal_or_venue":     str(result.get("journal_or_venue") or "").strip(),
            "doi":                  str(result.get("doi") or "").strip(),
            "abstract":             str(result.get("abstract") or "").strip(),
            "url":                  (self._url_var.get().strip() if url is None else url),
            "file_path":            file_path,
            "recommendation":       result.get("overall_recommendation", ""),
            "confidence":           result.get("confidence_level", "") or "Medium",
            "key_decision_factors": result.get("key_decision_factors", ""),
            "additional_notes":     result.get("additional_notes", ""),
            "profile_id":           profile["profile_id"],
            "profile_version":      profile["profile_version"],
            "screening_basis":      basis,
            "pdf_source":           (self._current_pdf_source
                                     if pdf_source is None else pdf_source),
            "pdf_url":              (self._current_pdf_url
                                     if pdf_url is None else pdf_url),
            "file_match_method":    match_method,
            "file_match_score":     match_score,
            "source_file_type":     source_kind,
            "analyzed_at":          datetime.datetime.now().isoformat(timespec="seconds"),
            "model_used":           CLAUDE_MODEL,
            "_full_result":         result,
        }

        # One column per criterion verdict, plus any tag fields
        for c in profile.get("criteria", []):
            cid = c["id"]
            block = crit.get(cid, {}) or {}
            entry[cid] = block.get("verdict", "")
            tf = c.get("tag_field")
            if tf and tf.get("name"):
                vals = block.get(tf["name"])
                if isinstance(vals, list):
                    joined = "; ".join(str(v) for v in vals if str(v).strip())
                else:
                    joined = str(vals or "").strip()
                if not joined and tf.get("required") and tf.get("fallback_value"):
                    joined = tf["fallback_value"]
                entry[tf["name"]] = joined

        if result.get("_local_correction"):
            entry["additional_notes"] = (
                (entry["additional_notes"] + "  |  " if entry["additional_notes"] else "")
                + result["_local_correction"])

        return entry

    # ── Result display (profile-driven) ───────────────────────────────────────

    def _display_result(self, result: dict, paper_id: str):
        profile = self.active_profile
        rec = result.get("overall_recommendation", "?")
        conf = result.get("confidence_level", "?")
        crit = result.get("criteria", {}) or {}
        basis = result.get("_screening_basis", "full_text")
        rtag = {"INCLUDE": "include", "EXCLUDE": "exclude",
                "MANUAL_REVIEW": "manual"}.get(rec, "")
        vtag = {"YES": "yes", "NO": "no", "UNCLEAR": "unclear"}

        lines = []

        def w(text, tag=None):
            lines.append((text, tag))

        w("=" * 72)
        w(f"  PAPER: {paper_id}", "heading")
        w(f"  RECOMMENDATION:  {rec}", rtag)
        w(f"  CONFIDENCE:      {conf}")
        w(f"  CRITERIA:        {profile['profile_name']} v{profile['profile_version']}")
        if basis == "abstract_only":
            w("  SCREENING BASIS: TITLE AND ABSTRACT ONLY — full text unavailable", "warn")
        for label, key in [("TITLE", "title"), ("AUTHORS", "authors"),
                           ("YEAR", "publication_year"),
                           ("VENUE", "journal_or_venue"), ("DOI", "doi")]:
            if result.get(key):
                w(f"  {label + ':':<16} {result[key]}")
        w("=" * 72)

        for idx, c in enumerate(profile.get("criteria", []), start=1):
            cid = c["id"]
            block = crit.get(cid, {}) or {}
            v = block.get("verdict", "?")
            opt = "" if c.get("required_for_include", True) else "  (optional)"
            w(f"\n  Criterion {idx}: {c['label']}{opt}", "heading")
            w(f"  Verdict: {v}", vtag.get(v))
            tf = c.get("tag_field")
            if tf and tf.get("name") and block.get(tf["name"]):
                vals = block[tf["name"]]
                shown = ", ".join(vals) if isinstance(vals, list) else str(vals)
                w(f"  {tf.get('label') or tf['name']}: {shown}")
            if block.get("reasoning"):
                w(f"  Reasoning: {block['reasoning']}")
            if block.get("text_examples"):
                w(f"  Evidence:  {block['text_examples']}", "key")
            if block.get("location"):
                w(f"  Location:  {block['location']}")

        w("\n  Key Decision Factors", "heading")
        w(f"  {result.get('key_decision_factors','')}")
        if result.get("confidence_rationale"):
            w("\n  Confidence Rationale", "heading")
            w(f"  {result.get('confidence_rationale','')}")
        if result.get("additional_notes"):
            w("\n  Additional Notes", "heading")
            w(f"  {result.get('additional_notes','')}")
        if result.get("_local_correction"):
            w("\n  Decision Rule Correction", "heading")
            w(f"  {result['_local_correction']}", "warn")
        w("\n" + "=" * 72)

        self._result_text.configure(state="normal")
        self._result_text.delete("1.0", "end")
        for text, tag in lines:
            if tag:
                self._result_text.insert("end", text + "\n", tag)
            else:
                self._result_text.insert("end", text + "\n")
        self._result_text.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    # Batch actions
    # ══════════════════════════════════════════════════════════════════════════

    def _download_batch_template(self):
        dest = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="batch_papers_template.csv")
        if dest:
            shutil.copy(BATCH_TEMPLATE_CSV, dest)
            messagebox.showinfo("Saved", f"Template saved to:\n{dest}")

    def _select_batch_csv(self):
        path = filedialog.askopenfilename(
            title="Select Batch CSV", filetypes=[("CSV files", "*.csv")])
        if path:
            self._batch_csv_path = path
            self._batch_file_label.config(text=Path(path).name, fg=PALETTE["ink"])
            self._batch_run_btn.config(state="normal")

    def _run_batch_analysis(self):
        if not self._batch_csv_path:
            return
        if not self._validate_ready(need_pdf=False):
            return
        self._save_resolver_settings()
        self._stop_requested = False
        self._set_busy(True)
        self._batch_log.delete("1.0", "end")
        self._batch_progress["value"] = 0
        self._batch_progress["maximum"] = 1
        self._batch_pct_label.config(text="0%")
        self._batch_counts_label.config(text="")
        self._batch_status.config(text="")
        self._paper_timer_label.config(text="")
        self._batch_total_timer_label.config(text="")
        self._start_batch_timers()
        threading.Thread(target=self._batch_worker, daemon=True).start()

    def _stop_batch(self):
        self._stop_requested = True
        self._batch_stop_btn.config(state="disabled", text="Stopping…")
        self._log_batch("\n⏹ Stop requested — finishing current paper then halting.\n",
                        "info")

    def _batch_worker(self):
        key = self._api_key.get().strip()
        profile = self.active_profile
        allow_abstract = bool(self._abstract_fallback.get())
        rows = []
        try:
            for enc in ("utf-8-sig", "latin-1", "cp1252"):
                try:
                    with open(self._batch_csv_path, newline="", encoding=enc) as f:
                        rows = list(csv.DictReader(f))
                    self._log_batch(f"(Encoding: {enc})\n", "info")
                    break
                except UnicodeDecodeError:
                    continue
            else:
                with open(self._batch_csv_path, newline="",
                          encoding="utf-8", errors="replace") as f:
                    rows = list(csv.DictReader(f))
                self._log_batch("(Fallback encoding)\n", "info")
        except Exception as e:
            self._log_batch(f"Could not read CSV: {e}\n", "err")
            self.after(0, self._set_busy, False)
            return

        total = len(rows)
        self._log_batch(f"Loaded {total} papers.\n", "info")
        self._log_batch(
            f"Criteria: {profile['profile_name']} v{profile['profile_version']}\n", "info")
        if allow_abstract:
            self._log_batch("Abstract-only fallback: ON\n", "info")
        self.after(0, self._batch_progress.__setitem__, "maximum", total)

        # Targets carry whatever title/DOI is already known (CSV or
        # repository). Used only to verify a file_path fallback, never to
        # override anything a person typed in.
        all_targets = lib.targets_from_rows(
            rows, {r.get("paper_id"): r for r in load_repository()})
        target_by_pid = {t.paper_id: t for t in all_targets}

        n_included = n_excluded = n_manual = n_error = 0
        n_abstract = 0
        n_slides = 0
        n_local = 0
        n_mismatch = 0

        for i, row in enumerate(rows):
            if self._stop_requested:
                self._log_batch(f"\n⏹ Batch stopped after {i} of {total} papers.\n", "info")
                break

            pid = (row.get("paper_id") or f"PAPER_{i+1}").strip()
            url = (row.get("url") or "").strip()
            fp = (row.get("file_path") or "").strip()
            if fp:
                fp = fp.replace("\\", os.sep).replace("/", os.sep)
            target = target_by_pid.get(pid)

            self.after(0, self._batch_status.config,
                       {"text": f"Paper {i+1}/{total}: {pid}"})
            self.after(0, self._new_paper_timer)
            self._log_batch(f"\n[{i+1}/{total}] {pid} — ", "info")

            basis = "full_text"
            pdf_source = ""
            pdf_url = ""
            payload = None
            source_kind = "pdf"
            match_method = ""
            match_score = ""
            mismatch_note = ""
            online_meta = {}
            online_doi = ""

            # 1. Online retrieval — tried first for every row with a link,
            #    before any local file is considered.
            if url:
                self._log_batch("locating PDF… ", "info")
                trail: list[str] = []
                res = pr.resolve_pdf(url, self.resolver_cfg, log=trail.append)
                online_doi = res.doi or ""
                online_meta = dict(res.metadata or {})
                if res.ok:
                    payload = res.pdf_bytes
                    pdf_source = res.resolved_via
                    pdf_url = res.pdf_url
                    self._log_batch(f"{res.resolved_via}. ", "ok")
                else:
                    self._log_batch(f"not found online ({res.error}). ", "info")
                    self._log_trail(trail)

            # 2. CSV file_path — used only when online retrieval found
            #    nothing. Verified against the row's title/DOI (from the CSV,
            #    the repository, or whatever the failed online attempt turned
            #    up) so a mistyped path doesn't screen the wrong paper
            #    unnoticed. A mismatch does not block the row — the file is
            #    still screened — but forces MANUAL_REVIEW.
            if payload is None and fp and os.path.isfile(fp):
                if target and online_meta:
                    lib.apply_resolution_metadata(
                        [target],
                        {pid: {"status": "failed", "metadata": online_meta,
                               "doi": online_doi}})
                loaded = lib.load_for_screening(
                    fp, self.library_cfg,
                    log=lambda m: self._log_batch(f"\n      {m}", "trail"))
                if loaded["error"]:
                    self._log_batch(f"file unusable ({loaded['error']}). ", "err")
                else:
                    payload = loaded["payload"]
                    basis = loaded["basis"]
                    pdf_source = loaded["source"]
                    source_kind = loaded.get("source_kind", "pdf")
                    if source_kind == "slides":
                        n_slides += 1
                    self._log_batch(
                        f"loaded from {loaded['transport']}. "
                        + ("[slides] " if basis == "slides" else ""), "ok")
                    if target:
                        v = lib.verify_file_identity(target, fp, self.library_cfg)
                        if v["checked"] and not v["ok"]:
                            mismatch_note = f"file_path mismatch: {v['note']}"
                            n_mismatch += 1
                            self._log_batch(f"MISMATCH ({v['note']}). ", "err")
                        else:
                            self._log_batch(f"{v['note']}. ", "trail")

            # 3. A confirmed local-library match, from an earlier folder scan
            #    (Local File Library card) — already verified by the matcher
            #    itself, so it's used as-is.
            mapped = None
            if payload is None:
                mapped = self._library_map.get(pid)
                if mapped and os.path.isfile(mapped["path"]):
                    fp = mapped["path"]
                    match_method = mapped.get("method", "")
                    match_score = (f"{mapped.get('score', 0):.3f}"
                                   if mapped.get("score") else "")
                    loaded = lib.load_for_screening(
                        fp, self.library_cfg,
                        log=lambda m: self._log_batch(f"\n      {m}", "trail"))
                    if loaded["error"]:
                        self._log_batch(
                            f"matched file unusable ({loaded['error']}). ", "err")
                        fp = ""
                    else:
                        payload = loaded["payload"]
                        basis = loaded["basis"]
                        pdf_source = f"{loaded['source']} [{match_method} {match_score}]"
                        source_kind = loaded.get("source_kind", "pdf")
                        if source_kind == "slides":
                            n_slides += 1
                        n_local += 1
                        self._log_batch(
                            f"matched local file ({match_method} {match_score}). ", "ok")

            # 4. Abstract-only fallback (opt-in) — from whatever metadata the
            #    failed online attempt captured, when no PDF was secured any
            #    other way.
            if payload is None and allow_abstract and online_meta.get("abstract"):
                payload = pr.format_metadata_as_text(online_meta)
                basis = "abstract_only"
                pdf_source = "abstract only"
                n_abstract += 1
                self._log_batch("no PDF anywhere — using abstract. ", "info")

            if payload is None:
                self._log_batch("No source. Skipping.\n", "err")
                n_error += 1
                self.after(0, self._update_batch_counts, i + 1, total,
                           n_included, n_excluded, n_manual, n_error)
                continue

            paper_t0 = time.monotonic()
            try:
                self._log_batch("Analyzing… ", "info")
                result = analyze_paper(
                    payload, profile, api_key=key, screening_basis=basis,
                    progress_callback=None,
                )
                if mismatch_note:
                    result["overall_recommendation"] = "MANUAL_REVIEW"
                    result["_file_mismatch"] = mismatch_note

                paper_elapsed = time.monotonic() - paper_t0
                rec = result.get("overall_recommendation", "?")
                rtag = {"INCLUDE": "ok", "EXCLUDE": "err",
                        "MANUAL_REVIEW": "info"}.get(rec, "info")

                if rec == "INCLUDE":
                    n_included += 1
                elif rec == "EXCLUDE":
                    n_excluded += 1
                elif rec == "MANUAL_REVIEW":
                    n_manual += 1

                entry = self._build_repo_entry(
                    pid, result, basis=basis, url=url, file_path=fp,
                    pdf_source=pdf_source, pdf_url=pdf_url,
                    match_method=match_method, match_score=match_score,
                    source_kind=source_kind)
                save_to_repository(entry)

                suffix = "  [abstract only]" if basis == "abstract_only" else ""
                self._log_batch(
                    f"→ {rec}{suffix}  ({self._fmt_elapsed(paper_elapsed)})\n", rtag)
                if result.get("_local_correction"):
                    self._log_batch(f"      {result['_local_correction']}\n", "info")
                if mismatch_note:
                    self._log_batch(f"      {mismatch_note}\n", "err")

            except Exception as e:
                self._log_batch(f"ERROR: {e}\n", "err")
                n_error += 1

            self.after(0, self._update_batch_counts, i + 1, total,
                       n_included, n_excluded, n_manual, n_error)

        self._log_batch("\nBatch complete. Repository updated.\n", "ok")
        if n_abstract:
            self._log_batch(
                f"{n_abstract} paper(s) screened from abstract only — review these "
                "before treating their verdicts as final.\n", "info")
        if n_mismatch:
            self._log_batch(
                f"{n_mismatch} paper(s) flagged MANUAL_REVIEW because their "
                "file_path did not match the row's title/DOI — review these before "
                "trusting the verdict.\n", "err")
        self.after(0, self._batch_status.config,
                   {"text": (f"Done — {n_included} included, {n_excluded} excluded, "
                             f"{n_manual} manual review, {n_error} errors")})
        self.after(0, self._refresh_repository_tab)
        self.after(0, self._stop_batch_timers)
        self.after(0, self._set_busy, False)

    def _log_trail(self, trail: list):
        for t in trail[-4:]:
            self._log_batch(f"      · {t}\n", "trail")

    def _log_batch(self, msg: str, tag: str = ""):
        def _do():
            self._batch_log.insert("end", msg, tag)
            self._batch_log.see("end")
        self.after(0, _do)

    # ══════════════════════════════════════════════════════════════════════════
    # Repository view
    # ══════════════════════════════════════════════════════════════════════════

    def _refresh_repository_tab(self):
        if not hasattr(self, "_tree"):
            return
        repo = load_repository()
        query = self._filter_var.get().lower() if hasattr(self, "_filter_var") else ""
        rec_f = self._rec_filter.get() if hasattr(self, "_rec_filter") else "All"
        prof_f = (self._repo_profile_filter.get()
                  if hasattr(self, "_repo_profile_filter") else "All")
        prof_id = getattr(self, "_repo_profile_name_to_id", {}).get(prof_f)

        repo.sort(key=_pid_sort_key)

        profile_names = {p["profile_id"]: p.get("profile_name", p["profile_id"])
                         for p in cp.list_profiles(REPO_DIR)}

        for item in self._tree.get_children():
            self._tree.delete(item)
        for r in repo:
            rec = r.get("recommendation", "")
            if rec_f != "All" and rec != rec_f:
                continue
            if prof_f != "All" and prof_id and r.get("profile_id") != prof_id:
                continue
            if query and query not in json.dumps(r).lower():
                continue
            tag = {"INCLUDE": "include", "EXCLUDE": "exclude",
                   "MANUAL_REVIEW": "manual"}.get(rec, "")
            ts = str(r.get("analyzed_at", ""))[:16].replace("T", " ")
            basis = "abstract" if r.get("screening_basis") == "abstract_only" else "full"
            pname = profile_names.get(r.get("profile_id"), r.get("profile_id", ""))
            pver = r.get("profile_version", "")
            self._tree.insert(
                "", "end", iid=r.get("paper_id"),
                values=(r.get("paper_id", ""), r.get("title", ""),
                        r.get("authors", ""), r.get("publication_year", ""),
                        r.get("journal_or_venue", ""),
                        rec, r.get("confidence", ""), basis,
                        f"{pname} v{pver}", ts),
                tags=(tag,))

    def _sort_tree(self, col):
        rows = [(self._tree.set(k, col), k) for k in self._tree.get_children("")]
        rows.sort()
        for i, (_, k) in enumerate(rows):
            self._tree.move(k, "", i)

    def _view_repo_entry(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        repo = load_repository()
        entry = next((r for r in repo if r.get("paper_id") == sel[0]), None)
        if not entry:
            return

        win = tk.Toplevel(self)
        win.title(f"Paper Detail — {sel[0]}")
        win.geometry("900x720")
        win.configure(bg=PALETTE["bg"])

        hdr = tk.Frame(win, bg=PALETTE["brand"], height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text=f"{sel[0]}  ·  {entry.get('title','(no title)')}",
                 bg=PALETTE["brand"], fg=PALETTE["brand_accent"],
                 font=("Helvetica", 10, "bold"), anchor="w",
                 padx=20).pack(fill="both", expand=True)

        # Provenance strip
        strip = tk.Frame(win, bg=PALETTE["surface_dim"])
        strip.pack(fill="x")
        prov = (f"Criteria: {entry.get('profile_id','?')} "
                f"v{entry.get('profile_version','?')}    ·    "
                f"Basis: {entry.get('screening_basis','full_text')}    ·    "
                f"PDF via: {entry.get('pdf_source') or 'n/a'}")
        tk.Label(strip, text=prov, bg=PALETTE["surface_dim"], fg=PALETTE["ink_mid"],
                 font=("Courier New", 8), anchor="w",
                 padx=20, pady=6).pack(fill="x")

        txt_frame = tk.Frame(win, bg=PALETTE["console_bg"])
        txt_frame.pack(fill="both", expand=True, padx=16, pady=12)
        txt = scrolledtext.ScrolledText(
            txt_frame, font=("Courier New", 9),
            bg=PALETTE["console_bg"], fg=PALETTE["console_fg"],
            bd=0, padx=16, pady=12, wrap="word")
        txt.pack(fill="both", expand=True)
        full = entry.get("_full_result", {})
        display = full if full else {k: v for k, v in entry.items()
                                     if k != "_full_result"}
        txt.insert("end", json.dumps(display, indent=2, ensure_ascii=False))
        txt.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    # Timers, validation, state
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        s = int(seconds)
        if s < 3600:
            return f"{s // 60}:{s % 60:02d}"
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    def _start_single_timer(self):
        self._single_start_time = time.monotonic()
        self._single_pbar.start(12)
        self._tick_single()

    def _tick_single(self):
        if not self._busy or self._single_start_time is None:
            return
        elapsed = time.monotonic() - self._single_start_time
        self._single_timer_label.config(text=f"⏱ {self._fmt_elapsed(elapsed)}")
        self._timer_after_id = self.after(1000, self._tick_single)

    def _stop_single_timer(self, final_elapsed=None):
        if self._timer_after_id:
            self.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        self._single_pbar.stop()
        self._single_start_time = None
        if final_elapsed is not None:
            self._single_timer_label.config(text=f"✓ {self._fmt_elapsed(final_elapsed)}")
        else:
            self._single_timer_label.config(text="—")

    def _start_batch_timers(self):
        self._batch_start_time = time.monotonic()
        self._paper_start_time = time.monotonic()
        self._tick_batch()

    def _new_paper_timer(self):
        self._paper_start_time = time.monotonic()

    def _tick_batch(self):
        if not self._busy:
            return
        now = time.monotonic()
        if self._paper_start_time:
            self._paper_timer_label.config(
                text=f"Paper: {self._fmt_elapsed(now - self._paper_start_time)}")
        if self._batch_start_time:
            self._batch_total_timer_label.config(
                text=f"Total: {self._fmt_elapsed(now - self._batch_start_time)}")
        self._timer_after_id = self.after(1000, self._tick_batch)

    def _stop_batch_timers(self):
        if self._timer_after_id:
            self.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        if self._batch_start_time:
            total_e = time.monotonic() - self._batch_start_time
            self._batch_total_timer_label.config(
                text=f"Done: {self._fmt_elapsed(total_e)}")
        self._batch_start_time = None
        self._paper_start_time = None
        self._paper_timer_label.config(text="")

    def _update_batch_counts(self, done, total, included, excluded, manual, errors):
        pct = int(done / total * 100) if total else 0
        self._batch_progress["value"] = done
        self._batch_pct_label.config(text=f"{pct}%")
        self._batch_counts_label.config(
            text=(f"{done}/{total} papers  ·  "
                  f"Include: {included}  Exclude: {excluded}  "
                  f"Manual Review: {manual}  Errors: {errors}"))

    def _validate_ready(self, need_pdf=True):
        if not self._api_key.get().strip():
            messagebox.showwarning(
                "API Key Required",
                "Please enter your Anthropic API key in the Settings tab.")
            self.notebook.select(self.tab_config)
            return False
        if need_pdf and not self._current_pdf_bytes and not self._current_meta_text:
            messagebox.showwarning("No PDF", "Please find or upload a PDF first.")
            return False
        return True

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._analyze_btn.config(state="disabled" if busy else "normal")
        self._batch_run_btn.config(
            state="disabled" if busy
            else ("normal" if self._batch_csv_path else "disabled"))
        self._batch_stop_btn.config(
            state="normal" if busy else "disabled", text="⏹  Stop")

    def _set_progress(self, msg: str):
        self._progress_label.config(text=msg)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = PaperScreenerApp()
    app.mainloop()
