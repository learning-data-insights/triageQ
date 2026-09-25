"""
GUI-free screening orchestration shared by app.py (desktop) and web_app.py.

Everything here used to live in app.py. It was pulled out so the web front end
can reuse exactly the same repository format, record shape, and analysis path
as the desktop app — two copies of _build_repo_entry would drift, and a record
written by one front end should read identically in the other.

  analyze_paper      one paper → parsed, locally-verified result dict
  build_repo_entry   result dict → flat repository record, shaped by a profile
  Repository         a repository folder: JSON source of truth + CSV mirrors

Nothing in this module imports tkinter, and nothing holds global state: every
repository operation takes its folder explicitly, so many independent
workspaces can coexist in one process (the web app's per-visitor sessions).
"""

from __future__ import annotations

import csv
import datetime
import json
import re
from pathlib import Path

import criteria_profiles as cp
import model_providers as mp


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
    "analyzed_at", "model_used", "pdf_vision_used",
]

# title and doi are optional, but they are what makes content matching possible
# for files whose names carry no paper ID. A DOI in the url column also works.
BATCH_CSV_COLUMNS = ["paper_id", "url", "file_path", "title", "doi"]

BATCH_TEMPLATE_ROW = {
    "paper_id": "PAPER_001",
    "url": "https://doi.org/10.7717/peerj.4375",
    "file_path": "",
    "title": "",
    "doi": "",
}


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_paper(payload, profile: dict, provider_cfg: "mp.ProviderConfig",
                  screening_basis: str = "full_text",
                  progress_callback=None) -> dict:
    """Screen one paper against a criteria profile.

    payload: PDF bytes, or a text string (abstract metadata, extracted slides).
    screening_basis: "full_text" | "abstract_only" | "slides".

    Transport is decided by the payload TYPE, not by the basis. A slide deck
    converted to PDF arrives as bytes but is still basis="slides" — the prompt
    must reflect what the evidence is, not how it got here.

    Which provider is used, and whether it read the PDF natively or from
    locally-extracted text, are transport details — they don't change
    screening_basis, which is about how MUCH of the paper was available, not
    how the available part reached the model. That's tracked separately, on
    the returned result's "_pdf_vision_used" key.

    Returns the parsed result dict, with overall_recommendation verified against
    the profile's decision rules locally.
    """
    provider = mp.build_provider(provider_cfg)

    if progress_callback:
        label = {
            "full_text": f"Sending to {provider.display_name}…",
            "abstract_only": "Screening from abstract…",
            "slides": "Screening from slides…",
        }.get(screening_basis, f"Sending to {provider.display_name}…")
        progress_callback(label)

    system_prompt = cp.build_system_prompt(profile, screening_basis)
    user_text = cp.build_user_message(profile, screening_basis)

    pdf_bytes = None
    pdf_vision_used = ""
    if isinstance(payload, (bytes, bytearray)):
        if provider.supports_pdf_vision:
            pdf_bytes = bytes(payload)
            pdf_vision_used = "yes"
        else:
            if progress_callback:
                progress_callback(
                    f"{provider.display_name} has no native PDF input — "
                    "extracting text locally…")
            user_text = mp.extract_pdf_text(bytes(payload)) + "\n\n" + user_text
            pdf_vision_used = "no"
    else:
        user_text = str(payload) + "\n\n" + user_text

    text = provider.complete(system_prompt, user_text, pdf_bytes=pdf_bytes,
                             max_tokens=4096)
    result = cp.clean_json_response(text)

    # Recompute the recommendation locally. The prompt states the rules, but the
    # repository should never depend on the model applying them correctly.
    rec, correction = cp.apply_decision_rules(profile, result)
    result["overall_recommendation"] = rec
    if correction:
        result["_local_correction"] = correction

    result["_screening_basis"] = screening_basis
    result["_pdf_vision_used"] = pdf_vision_used
    result["_provider_model"] = f"{provider.display_name}: {provider_cfg.model}"
    return result


# ── Repository entry construction (profile-driven) ────────────────────────────

def build_repo_entry(profile: dict, paper_id: str, result: dict, *,
                     basis: str = "full_text", url: str = "", file_path: str = "",
                     pdf_source: str = "", pdf_url: str = "",
                     match_method: str = "", match_score: str = "",
                     source_kind: str = "pdf") -> dict:
    """Flatten a result into a repository record using the profile's shape."""
    crit = result.get("criteria", {}) or {}

    entry = {
        "paper_id":             paper_id,
        "title":                str(result.get("title") or "").strip(),
        "authors":              str(result.get("authors") or "").strip(),
        "publication_year":     str(result.get("publication_year") or "").strip(),
        "journal_or_venue":     str(result.get("journal_or_venue") or "").strip(),
        "doi":                  str(result.get("doi") or "").strip(),
        "abstract":             str(result.get("abstract") or "").strip(),
        "url":                  url,
        "file_path":            file_path,
        "recommendation":       result.get("overall_recommendation", ""),
        "confidence":           result.get("confidence_level", "") or "Medium",
        "key_decision_factors": result.get("key_decision_factors", ""),
        "additional_notes":     result.get("additional_notes", ""),
        "profile_id":           profile["profile_id"],
        "profile_version":      profile["profile_version"],
        "screening_basis":      basis,
        "pdf_source":           pdf_source,
        "pdf_url":              pdf_url,
        "file_match_method":    match_method,
        "file_match_score":     match_score,
        "source_file_type":     source_kind,
        "analyzed_at":          datetime.datetime.now().isoformat(timespec="seconds"),
        "model_used":           result.get("_provider_model", ""),
        "pdf_vision_used":      result.get("_pdf_vision_used", ""),
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


# ── Repository ────────────────────────────────────────────────────────────────

def _migrate_entry(r: dict) -> dict:
    """Older records may carry no profile stamp. Mark them as such rather than
    guessing which profile screened them."""
    if not isinstance(r, dict):
        return r
    r.setdefault("profile_id", "unknown")
    r.setdefault("profile_version", "")
    r.setdefault("screening_basis", "full_text")
    r.setdefault("pdf_source", "")
    r.setdefault("pdf_url", "")
    r.setdefault("file_match_method", "")
    r.setdefault("file_match_score", "")
    r.setdefault("source_file_type", "pdf")
    r.setdefault("pdf_vision_used", "")
    return r


def pid_sort_key(r: dict):
    """Sort key for repository entries: numeric paper_id first, then lexicographic."""
    pid = r.get("paper_id", "")
    try:
        return (0, int(pid))
    except (ValueError, TypeError):
        return (1, str(pid))


def criteria_summary(entry: dict) -> str:
    """Compact 'id=VERDICT; id=VERDICT' string for the combined CSV."""
    full = entry.get("_full_result") or {}
    crits = full.get("criteria") or {}
    parts = []
    for cid, block in crits.items():
        if isinstance(block, dict) and block.get("verdict"):
            parts.append(f"{cid}={block['verdict']}")
    return "; ".join(parts)


class Repository:
    """One repository folder.

    paper_repository.json is the source of truth; paper_repository.csv and the
    per-profile repository_<id>.csv files are regenerated from it on every
    save. The JSON is written first, so a CSV write failure (a file locked by
    Excel, typically) never loses a result.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def json_path(self) -> Path:
        return self.root / "paper_repository.json"

    @property
    def csv_path(self) -> Path:
        return self.root / "paper_repository.csv"

    @property
    def batch_template_path(self) -> Path:
        return self.root / "batch_template.csv"

    @property
    def mapping_log_path(self) -> Path:
        return self.root / "file_mapping_log.csv"

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.json_path.exists():
            self.json_path.write_text(json.dumps([], indent=2), encoding="utf-8")
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=CSV_BASE_COLUMNS).writeheader()
        if not self.batch_template_path.exists():
            with open(self.batch_template_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=BATCH_CSV_COLUMNS)
                w.writeheader()
                w.writerow(BATCH_TEMPLATE_ROW)

    def load(self) -> list:
        self.ensure()
        try:
            repo = json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [_migrate_entry(r) for r in repo]

    def save(self, entry: dict):
        self.ensure()
        repo = self.load()
        for i, r in enumerate(repo):
            if r.get("paper_id") == entry.get("paper_id"):
                repo[i] = entry
                break
        else:
            repo.append(entry)
        repo.sort(key=pid_sort_key)
        self.json_path.write_text(json.dumps(repo, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        self.sync_csv(repo)

    def profile_csv_path(self, profile_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", profile_id)
        return self.root / f"repository_{safe}.csv"

    def sync_csv(self, repo: list):
        """Write one combined CSV plus one CSV per criteria profile.

        Criteria columns differ between profiles, so a single flat file across
        profiles would be mostly empty cells. The combined file carries the base
        columns plus a compact verdict summary; each per-profile file carries that
        profile's full criteria columns.
        """
        # Combined
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            cols = CSV_BASE_COLUMNS + ["criteria_summary"]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in repo:
                row = dict(r)
                row["criteria_summary"] = criteria_summary(r)
                w.writerow(row)

        # Per profile
        by_profile: dict[str, list] = {}
        for r in repo:
            by_profile.setdefault(r.get("profile_id") or "unknown", []).append(r)

        for pid, rows in by_profile.items():
            prof = cp.load_profile(self.root, pid)
            if prof:
                cols = cp.profile_csv_columns(prof, CSV_BASE_COLUMNS)
            else:
                extra = sorted({k for r in rows for k in r
                                if k not in CSV_BASE_COLUMNS and not k.startswith("_")})
                cols = CSV_BASE_COLUMNS + extra
            with open(self.profile_csv_path(pid), "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
