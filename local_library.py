"""
local_library.py
────────────────────────────────────────────────────────────────────────────────
Map a folder of arbitrarily-named local files onto the paper IDs in a batch CSV.

THE PROBLEM
  You have legally-obtained copies of papers that no resolver can reach
  (interlibrary loan, author-supplied, conference proceedings, embargoed
  postprints). Their filenames are whatever the source called them:
  "s41539-023-00191-w.pdf", "Final_submission_v3.pptx", "download (4).pdf".
  Filling in file_path by hand for 200 rows is exactly the kind of clerical
  work that produces silent mis-assignments.

THE APPROACH
  Paper IDs are internal database numbering and mean nothing to the file, so
  filenames are not trusted to carry them. Identifying evidence is read out of
  each file and compared against bibliographic metadata resolved from the URL in
  the batch CSV.

  Evidence, in descending order of trust:

    1. doi         — a DOI printed in the file matches the row's DOI  (0.99)
    2. title_text  — the row's title appears in the file's text       (0.90-0.97)
    3. title_file  — the filename resembles the row's title           (<=0.85)

  Author surnames are corroboration, never a decision on their own: pypdf gives
  no font sizes, so there is no reliable signal for which line is the author
  block. Agreement nudges a score up. A genuine conflict — both sides list
  surnames and none overlap — forces human review no matter how well the titles
  matched, because that is the signature of two different papers sharing a title.

  Assignment is one-to-one and greedy best-first: a file serves exactly one
  paper, a paper takes exactly one file.

WHERE THIS SITS IN THE PIPELINE
  Local files fill gaps; they do not compete. Matching runs against only those
  rows whose online retrieval failed, which also means fewer candidate papers per
  file and so fewer chances to collide. A file_path given explicitly in the CSV
  still wins — that is a deliberate instruction, not a guess.

WHY IT REFUSES TO GUESS
  A wrong file/paper assignment does not announce itself. It produces a
  plausible, well-reasoned, fully-populated screening verdict for the wrong
  paper, and nothing downstream will ever flag it. So:

    - Anything below `review_floor` is reported as UNMATCHED, not guessed.
    - Anything between the floor and `auto_accept` is a PROPOSAL requiring
      human confirmation.
    - A near-tie between two candidate papers (within `tie_margin`) is forced
      into review even when the top score would otherwise auto-accept.
    - An author conflict forces review regardless of title score.

  This mirrors the exclude_if rule in criteria_profiles: the structure refuses
  the unsafe thing rather than trusting a human to notice it.

SLIDE DECKS
  Decks are screened as full text, per product decision. A deck is a compressed
  summary, so it can omit detail a criterion depends on, which risks turning "the
  deck does not mention it" into a NO where UNCLEAR would be honest. Every
  deck-sourced screening is therefore stamped source_kind="slides" in the
  repository so those verdicts stay findable afterwards. Setting
  LibraryConfig.slides_conservative_prompt switches decks to the conservative
  "slides" prompt instead (see criteria_profiles.build_system_prompt).

DEPENDENCIES
  pypdf        required for PDF text extraction (matching only; screening still
               sends the original bytes to the API)
  python-pptx  required for .pptx text extraction
  LibreOffice  optional; enables deck-to-PDF conversion (preserves figures and
               charts that text extraction drops) and is the only way to read
               legacy binary .ppt
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from doi_utils import normalize_doi, extract_dois

try:
    from pypdf import PdfReader
    _HAS_PYPDF = True
except ImportError:  # pragma: no cover
    _HAS_PYPDF = False

try:
    from pptx import Presentation
    _HAS_PPTX = True
except ImportError:  # pragma: no cover
    _HAS_PPTX = False


PDF_EXTS = {".pdf"}
SLIDE_EXTS = {".pptx", ".pptm", ".ppt"}
SUPPORTED_EXTS = PDF_EXTS | SLIDE_EXTS

MAX_FILE_BYTES = 80 * 1024 * 1024      # skip anything implausibly large
CONVERT_TIMEOUT = 120                   # seconds for a LibreOffice conversion

# Lines that are never a title, checked against the lowercased line.
_TITLE_NOISE = re.compile(
    r"(doi:|https?://|www\.|arxiv:|preprint|copyright|©|all rights reserved|"
    r"issn|isbn|volume\s+\d|vol\.\s*\d|no\.\s*\d|\bpp\.\s*\d|received\s|accepted\s|"
    r"published\s|licen[cs]e|creative commons|downloaded from|"
    r"journal of|proceedings of the \d|conference on \w+ \d{4})",
    re.IGNORECASE)

_AUTHOR_LINE = re.compile(
    r"^[A-Z][a-z]+(\s+[A-Z]\.?)*\s+[A-Z][a-z]+"      # Firstname M. Lastname
    r"(\s*[,;]\s*[A-Z][a-z]+(\s+[A-Z]\.?)*\s+[A-Z][a-z]+)+\s*\d*$")


# ══════════════════════════════════════════════════════════════════════════════
# Config & data types
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LibraryConfig:
    """Persisted with app settings. Thresholds are deliberately conservative."""
    enabled: bool = True
    folder: str = ""
    recursive: bool = True

    auto_accept: float = 0.92      # at or above this, accept without asking
    review_floor: float = 0.55     # below this, report as unmatched
    tie_margin: float = 0.06       # runner-up this close ⇒ force human review
    author_bonus: float = 0.03     # applied when surnames corroborate

    max_pdf_pages: int = 3         # pages read for fingerprinting
    max_slides: int = 5            # slides read for fingerprinting
    fingerprint_chars: int = 6000  # cap on retained text per file

    convert_slides_to_pdf: bool = True
    # Off by default: decks are screened as full text per product decision.
    # Turning this on routes them through the conservative "slides" prompt.
    slides_conservative_prompt: bool = False

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> "LibraryConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FileFingerprint:
    path: str
    name: str = ""
    ext: str = ""
    kind: str = "pdf"                  # "pdf" | "slides"
    size_bytes: int = 0
    text: str = ""                     # first-pages/slides text, normalized
    embedded_title: str = ""           # from PDF/PPTX document properties
    title_guess: str = ""              # heuristic from the first page
    author_guess: str = ""             # heuristic author line from the first page
    dois: list = field(default_factory=list)
    year_guess: str = ""
    error: str = ""

    @property
    def readable(self) -> bool:
        return not self.error


@dataclass
class PaperTarget:
    """One row of the batch CSV, plus metadata resolved from its URL."""
    paper_id: str
    title: str = ""
    doi: str = ""
    authors: str = ""
    year: str = ""
    url: str = ""
    row_index: int = -1
    title_source: str = ""             # csv | repository | resolver
    retrieval_status: str = ""         # ok | failed | not_attempted

    @property
    def has_content_key(self) -> bool:
        """True when this row carries anything matchable."""
        return bool(self.title or self.doi)


@dataclass
class PairScore:
    score: float = 0.0
    method: str = ""
    note: str = ""
    force_review: bool = False
    author_state: str = "unknown"      # agree | conflict | unknown


@dataclass
class Match:
    paper_id: str
    path: str
    method: str = ""                   # paper_id | doi | title_text | title_file
    score: float = 0.0
    kind: str = "pdf"
    note: str = ""
    runner_up: str = ""                # "PAPER_014 (0.88)" when close
    author_state: str = "unknown"      # agree | conflict | unknown

    def label(self) -> str:
        return f"{self.method} {self.score:.2f}"


@dataclass
class MatchReport:
    accepted: list = field(default_factory=list)      # list[Match] — auto
    proposals: list = field(default_factory=list)     # list[Match] — confirm
    unmatched_files: list = field(default_factory=list)   # list[(path, note)]
    unmatched_papers: list = field(default_factory=list)  # list[paper_id]
    unreadable: list = field(default_factory=list)        # list[(path, error)]
    skipped_ids: list = field(default_factory=list)       # ids too generic to use

    def summary(self) -> str:
        return (f"{len(self.accepted)} matched, {len(self.proposals)} to confirm, "
                f"{len(self.unmatched_files)} files unmatched, "
                f"{len(self.unmatched_papers)} papers without a file")


# ══════════════════════════════════════════════════════════════════════════════
# Text normalization
# ══════════════════════════════════════════════════════════════════════════════

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize_text(s: str) -> str:
    """Lowercase, de-accent, collapse to single-spaced alphanumerics."""
    s = _strip_accents(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    """Blend of sequence ratio and token overlap on normalized strings."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    # Coverage of the shorter side: filenames are truncated titles more often
    # than they are different titles.
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(seq, overlap * 0.95)


def _filename_stem_words(path: str) -> str:
    """Filename with separators and common cruft turned into words."""
    stem = Path(path).stem
    stem = re.sub(r"[_\-\.\+]+", " ", stem)
    stem = re.sub(r"\(\s*\d+\s*\)", " ", stem)                     # "download (4)"
    stem = re.sub(r"\b(final|draft|copy|v\d+|rev\d*|clean|"
                  r"manuscript|preprint|postprint|accepted|submission|"
                  r"fulltext|full text|pdf|paper)\b", " ", stem, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stem).strip()


# ══════════════════════════════════════════════════════════════════════════════
# Title guessing
# ══════════════════════════════════════════════════════════════════════════════

def guess_title(first_page_text: str) -> str:
    """Best-effort title from the top of a paper's first page.

    pypdf gives no font sizes, so this cannot use the real signal (the title is
    the biggest type on the page). It uses position and shape instead, which is
    good enough for scoring — never good enough to write into the repository.
    Metadata for the record still comes from the model reading the paper.
    """
    lines = [ln.strip() for ln in (first_page_text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()][:25]

    # Join lines that are clearly one wrapped sentence fragment.
    merged, buf = [], ""
    for ln in lines:
        if buf and not buf.rstrip().endswith((".", ":", "?", "!")) and ln[:1].islower():
            buf = f"{buf} {ln}"
        else:
            if buf:
                merged.append(buf)
            buf = ln
    if buf:
        merged.append(buf)

    best, best_score = "", 0.0
    for i, ln in enumerate(merged[:15]):
        words = ln.split()
        if not (3 <= len(words) <= 40):
            continue
        if len(ln) < 20:
            continue
        if _TITLE_NOISE.search(ln):
            continue
        if _AUTHOR_LINE.match(ln):
            continue
        letters = sum(c.isalpha() for c in ln)
        if letters < len(ln) * 0.6:                # tables, numbers, headers
            continue
        if ln.isupper() and len(ln) > 60:          # running head in caps
            pass                                    # still allowed, scored lower
        score = 1.0 - (i * 0.06)                   # earlier is better
        if ln.endswith(("?", ":")) or ":" in ln:
            score += 0.05                          # titles love colons
        if ln.isupper():
            score -= 0.10
        if score > best_score:
            best, best_score = ln, score
    return best.strip()


def guess_year(text: str) -> str:
    years = re.findall(r"\b(19[89]\d|20[0-4]\d)\b", text or "")
    return max(years) if years else ""


# ══════════════════════════════════════════════════════════════════════════════
# Fingerprinting
# ══════════════════════════════════════════════════════════════════════════════

def find_soffice() -> str:
    """Path to a LibreOffice binary, or "" if none is installed."""
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",
              r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if os.path.isfile(p):
            return p
    return ""


def convert_to_pdf(path: str, timeout: int = CONVERT_TIMEOUT) -> tuple:
    """Convert any LibreOffice-readable file to PDF bytes.

    Returns (pdf_bytes | None, error_string).
    """
    soffice = find_soffice()
    if not soffice:
        return None, "LibreOffice not found"
    src = Path(path)
    if not src.is_file():
        return None, "file not found"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(
                [soffice, "--headless", "--norestore", "--invisible",
                 "--convert-to", "pdf", "--outdir", tmp, str(src)],
                capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"conversion timed out after {timeout}s"
        except Exception as exc:                       # pragma: no cover
            return None, f"conversion failed: {exc}"
        out = list(Path(tmp).glob("*.pdf"))
        if not out:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-200:]
            return None, f"conversion produced no PDF{': ' + tail if tail else ''}"
        return out[0].read_bytes(), ""


def _fingerprint_pdf_bytes(fp: FileFingerprint, data: bytes,
                           cfg: LibraryConfig) -> None:
    if not _HAS_PYPDF:
        fp.error = "pypdf not installed — cannot read PDF text"
        return
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        fp.error = f"unreadable PDF: {type(exc).__name__}"
        return
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            fp.error = "PDF is password protected"
            return
    try:
        fp.embedded_title = str((reader.metadata or {}).get("/Title", "") or "").strip()
    except Exception:
        fp.embedded_title = ""
    pages = []
    for page in reader.pages[:max(1, cfg.max_pdf_pages)]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    first = pages[0] if pages else ""
    fp.text = "\n".join(pages)[:cfg.fingerprint_chars]
    if not fp.text.strip():
        fp.error = ("no extractable text — likely a scan; "
                    "match by filename or assign manually")
        return
    fp.title_guess = guess_title(first)
    fp.author_guess = guess_authors(first, skip_line=fp.title_guess)
    fp.dois = extract_dois(fp.text)
    fp.year_guess = guess_year(first)


def _fingerprint_pptx(fp: FileFingerprint, cfg: LibraryConfig) -> None:
    if not _HAS_PPTX:
        fp.error = "python-pptx not installed — cannot read .pptx"
        return
    try:
        prs = Presentation(fp.path)
    except Exception as exc:
        fp.error = f"unreadable presentation: {type(exc).__name__}"
        return
    try:
        fp.embedded_title = str(prs.core_properties.title or "").strip()
    except Exception:
        fp.embedded_title = ""
    chunks, first_slide = [], ""
    for i, slide in enumerate(prs.slides):
        if i >= max(1, cfg.max_slides):
            break
        bits = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = shape.text_frame.text.strip()
                if t:
                    bits.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        bits.append(" | ".join(cells))
        blob = "\n".join(bits)
        if i == 0:
            first_slide = blob
        chunks.append(blob)
    fp.text = "\n".join(chunks)[:cfg.fingerprint_chars]
    if not fp.text.strip():
        fp.error = "no text found in the first slides"
        return
    fp.title_guess = fp.embedded_title or guess_title(first_slide)
    fp.author_guess = guess_authors(first_slide, skip_line=fp.title_guess)
    fp.dois = extract_dois(fp.text)
    fp.year_guess = guess_year(fp.text)


def fingerprint_file(path: str, cfg: LibraryConfig) -> FileFingerprint:
    """Read enough of one file to identify which paper it is."""
    p = Path(path)
    fp = FileFingerprint(path=str(p), name=p.name, ext=p.suffix.lower())
    fp.kind = "slides" if fp.ext in SLIDE_EXTS else "pdf"
    try:
        fp.size_bytes = p.stat().st_size
    except Exception as exc:
        fp.error = f"cannot stat file: {exc}"
        return fp
    if fp.size_bytes > MAX_FILE_BYTES:
        fp.error = f"file is {fp.size_bytes // (1024*1024)} MB — skipped"
        return fp
    if fp.size_bytes == 0:
        fp.error = "file is empty"
        return fp

    if fp.ext in PDF_EXTS:
        try:
            _fingerprint_pdf_bytes(fp, p.read_bytes(), cfg)
        except Exception as exc:
            fp.error = f"read failed: {type(exc).__name__}"
    elif fp.ext in (".pptx", ".pptm"):
        _fingerprint_pptx(fp, cfg)
    elif fp.ext == ".ppt":
        # Legacy binary PowerPoint. python-pptx cannot open it; go via LibreOffice.
        data, err = convert_to_pdf(fp.path)
        if data:
            _fingerprint_pdf_bytes(fp, data, cfg)
            if not fp.error:
                fp.note_converted = True    # type: ignore[attr-defined]
        else:
            fp.error = f"legacy .ppt needs LibreOffice ({err})"
    else:
        fp.error = f"unsupported file type {fp.ext}"
    return fp


def scan_folder(cfg: LibraryConfig, extra_paths=None, log=None) -> list:
    """Collect supported files from cfg.folder plus any explicitly-added paths."""
    def _log(m):
        if log:
            log(m)

    found, seen = [], set()

    def _add(p: Path):
        rp = str(p.resolve())
        if rp in seen:
            return
        seen.add(rp)
        found.append(p)

    for raw in (extra_paths or []):
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if cfg.recursive else p.glob("*")
            for c in it:
                if c.is_file() and c.suffix.lower() in SUPPORTED_EXTS:
                    _add(c)
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            _add(p)

    if cfg.folder:
        root = Path(cfg.folder)
        if root.is_dir():
            it = root.rglob("*") if cfg.recursive else root.glob("*")
            for c in sorted(it):
                if c.is_file() and c.suffix.lower() in SUPPORTED_EXTS:
                    _add(c)
        else:
            _log(f"Folder not found: {cfg.folder}")

    _log(f"{len(found)} candidate file(s) found.")
    return found


# ══════════════════════════════════════════════════════════════════════════════
# Matching
# ══════════════════════════════════════════════════════════════════════════════

_NAME_CHUNK = re.compile(
    r"""^\s*(?:
        (?:[A-Z]\.?\s*){1,3}[A-Z][A-Za-z'\-]{1,}      |   # J. R. Smith
        [A-Z][A-Za-z'\-]{1,}(?:\s+[A-Z]\.?){1,3}      |   # Smith, J. R.
        [A-Z][A-Za-z'\-]+\s+[A-Z][A-Za-z'\-]+            # Jane Smith
    )\s*$""", re.VERBOSE)

# Prefixes that belong to the following surname rather than standing alone.
_NAME_PARTICLES = ("van ", "von ", "de ", "del ", "della ", "di ", "da ",
                   "den ", "der ", "dos ", "el ", "al ", "bin ", "ibn ",
                   "mc", "mac", "o'")


def name_likeness(line: str) -> float:
    """Fraction of a line's comma/semicolon chunks that look like personal names.

    This is the guard that stops a Title Case sentence from being read as an
    author list. "Generative AI Feedback and Student Revision Behavior" splits
    into chunks that are not name-shaped; "M. Delgado, P. Nwosu" splits into
    chunks that are.
    """
    s = re.sub(r"\d+", "", str(line or ""))              # affiliation markers
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\*|†|‡|§|¶", "", s)
    s = s.replace("&", ",")
    s = re.sub(r"\band\b", ",", s, flags=re.IGNORECASE)
    # Trailing venue/date fragments ("… — AERA 2024") are separated, not names.
    chunks = [c.strip(" .,;") for c in re.split(r"[,;|—–]", s)]
    chunks = [c for c in chunks if c]
    if not chunks:
        return 0.0
    hits = 0
    for c in chunks:
        low = c.lower()
        for p in _NAME_PARTICLES:
            if low.startswith(p):
                c = c[len(p):].strip()
                break
        if len(c.split()) > 4:                            # too long to be a name
            continue
        if _NAME_CHUNK.match(c):
            hits += 1
    return hits / len(chunks)


def guess_authors(first_page_text: str, skip_line: str = "") -> str:
    """Best-effort author line from the top of a first page.

    Heuristic and openly so: the author block is usually the first line after
    the title that reads as a list of personal names. Used only to corroborate
    or contradict a title match — never to make one. Returns "" rather than a
    doubtful guess, because a wrong author string invents conflicts, and an
    invented conflict sends a correct match to review for no reason.
    """
    lines = [ln.strip() for ln in (first_page_text or "").splitlines() if ln.strip()]
    nskip = normalize_text(skip_line)
    for ln in lines[:20]:
        if len(ln) < 6 or len(ln) > 300:
            continue
        if _TITLE_NOISE.search(ln):
            continue
        if nskip and normalize_text(ln) == nskip:          # this is the title
            continue
        nl = name_likeness(ln)
        chunk_count = len([c for c in re.split(r"[,;&]|\band\b", ln) if c.strip()])
        # One chunk is only credible with explicit initials ("J. R. Smith");
        # otherwise require a list, most of which is name-shaped.
        if chunk_count == 1:
            if nl >= 1.0 and re.search(r"\b[A-Z]\.", ln):
                return ln
            continue
        if nl >= 0.6:
            return ln
    return ""


# Words that look like surnames but never are, when splitting an author string.
_NOT_SURNAMES = {
    "and", "the", "for", "with", "van", "von", "den", "der", "de", "di", "da",
    "et", "al", "jr", "sr", "phd", "md", "university", "college", "school",
    "department", "institute", "center", "centre", "corresponding", "author",
}


def author_surnames(raw: str) -> set:
    """Pull probable surnames out of an author string in any common format.

    Handles "Smith, J.; Okoro, A.", "Jane R. Smith and Alan Okoro",
    "J. R. Smith, A. Okoro" and the superscript-digit variants of each.
    """
    s = _strip_accents(str(raw or ""))
    s = re.sub(r"\d+", " ", s)                       # affiliation markers
    s = re.sub(r"\([^)]*\)", " ", s)                 # parenthetical notes
    s = s.replace("&", ",").replace(" and ", ",")
    out = set()
    for chunk in re.split(r"[,;|—–]", s):
        chunk = chunk.strip().strip(".")
        if not chunk:
            continue
        words = [w for w in re.split(r"\s+", chunk) if w]
        # Drop initials ("J.", "R", "J.R.")
        words = [w for w in words if not re.fullmatch(r"(?:[A-Za-z]\.?){1,3}", w)
                 or len(w.strip(".")) > 2]
        cands = [w.strip(".-'") for w in words if len(w.strip(".-'")) >= 3]
        cands = [w for w in cands if w.lower() not in _NOT_SURNAMES]
        cands = [w for w in cands if re.fullmatch(r"[A-Za-z][A-Za-z\-']+", w)]
        if not cands:
            continue
        # In "Jane R. Smith" the surname is last; in "Smith, J." it is first and
        # the chunk has already been split, leaving one candidate either way.
        out.add(cands[-1].lower())
    return {w for w in out if len(w) >= 3}


def author_agreement(target_authors: str, file_authors: str,
                     file_text: str = "") -> tuple:
    """Compare two author strings.

    Returns (state, detail) where state is "agree", "conflict", or "unknown".
    "conflict" requires surnames on BOTH sides with zero overlap — the signature
    of two different papers that happen to share a title. Anything thinner is
    "unknown", which changes nothing: a heuristic that cannot read the authors
    must not be allowed to veto a good title match.
    """
    ta = author_surnames(target_authors)
    fa = author_surnames(file_authors)
    if not ta:
        return "unknown", "no authors in row metadata"
    if not fa:
        # Fall back to looking for the row's surnames anywhere in the file text.
        if file_text:
            nx = normalize_text(file_text)
            hits = {s for s in ta if re.search(rf"\b{re.escape(s)}\b", nx)}
            if hits:
                return "agree", f"surname(s) found in text: {', '.join(sorted(hits))}"
        return "unknown", "no author line found in file"
    shared = ta & fa
    if shared:
        return "agree", f"shared surname(s): {', '.join(sorted(shared))}"
    return "conflict", (f"row lists {', '.join(sorted(ta))[:60]}; "
                        f"file lists {', '.join(sorted(fa))[:60]}")


def _title_in_text(title: str, text: str) -> float:
    """Score for the row's title appearing in the file's extracted text."""
    nt, nx = normalize_text(title), normalize_text(text)
    if not nt or not nx or len(nt.split()) < 3:
        return 0.0
    if nt in nx:
        return 0.97
    # Sliding-window fuzzy over the head of the document.
    words = nx.split()
    tw = nt.split()
    span = len(tw)
    if span < 3 or len(words) < span:
        return 0.0
    best = 0.0
    limit = min(len(words) - span + 1, 400)
    for i in range(0, limit, max(1, span // 3)):
        window = " ".join(words[i:i + span])
        r = difflib.SequenceMatcher(None, nt, window).ratio()
        if r > best:
            best = r
        if best > 0.98:
            break
    # Cap below the containment score: fuzzy is weaker evidence than exact.
    return min(best, 0.95) if best >= 0.80 else 0.0


def score_pair(target: PaperTarget, fp: FileFingerprint,
               cfg: LibraryConfig = None) -> PairScore:
    """Best evidence for one paper/file pair.

    A file whose text could not be extracted (a scan, an encrypted PDF, a legacy
    .ppt with no LibreOffice) can still be matched on its filename, but never
    automatically: with no text there is nothing to corroborate, so those always
    land in review.
    """
    cfg = cfg or LibraryConfig()
    candidates = []

    if not fp.readable:
        if target.title:
            fn = similarity(target.title, _filename_stem_words(fp.name))
            if fn >= 0.62:
                return PairScore(
                    score=min(fn * 0.90, 0.85), method="title_file",
                    note=(f"filename resembles title ({fn:.2f}); file text "
                          "unreadable, nothing to corroborate"),
                    force_review=True)
        return PairScore()

    tdoi = normalize_doi(target.doi) or normalize_doi(target.url)
    if tdoi and tdoi in fp.dois:
        candidates.append((0.99, "doi", f"DOI in file matches row: {tdoi}"))

    if target.title:
        s = _title_in_text(target.title, fp.text)
        if s:
            candidates.append((s, "title_text", "title found in document text"))

        fn = similarity(target.title, _filename_stem_words(fp.name))
        if fn >= 0.62:
            candidates.append((min(fn * 0.90, 0.85), "title_file",
                               f"filename resembles title ({fn:.2f})"))

        for label, val in (("embedded title", fp.embedded_title),
                           ("first-page title", fp.title_guess)):
            if val:
                s2 = similarity(target.title, val)
                if s2 >= 0.75:
                    candidates.append((min(s2 * 0.95, 0.93), "title_text",
                                       f"{label} matches ({s2:.2f})"))

    if not candidates:
        return PairScore()

    score, method, note = max(candidates, key=lambda c: c[0])
    state, detail = author_agreement(target.authors, fp.author_guess, fp.text)

    if state == "agree":
        # Corroboration can lift a borderline title match, but a DOI match needs
        # no help and nothing is allowed to reach a certainty it hasn't earned.
        score = min(score + cfg.author_bonus, 0.99)
        note = f"{note}; authors agree ({detail})"
    elif state == "conflict":
        # An author conflict blocks auto-accept for title-based matches — the
        # case it exists for is two different papers sharing a title.
        #
        # It does NOT override a DOI match. A DOI printed in the document is a
        # registered identifier; the author string it is being checked against
        # was scraped off a page layout with no font information. Letting the
        # weaker signal veto the stronger one would send correct matches to
        # review whenever author extraction misfires, which trains the reviewer
        # to click through the gate — the opposite of what the gate is for.
        if method == "doi":
            note = (f"{note}; note: extracted authors did not corroborate "
                    f"({detail}) — DOI match retained")
            return PairScore(score=score, method=method, note=note,
                             author_state="conflict_overridden")
        note = f"{note}; AUTHOR CONFLICT — {detail}"
        return PairScore(score=score, method=method, note=note,
                         force_review=True, author_state=state)

    return PairScore(score=score, method=method, note=note, author_state=state)


def verify_file_identity(target: PaperTarget, path: str, cfg: LibraryConfig) -> dict:
    """Check a specific file against what is already known about a paper.

    Used when a CSV file_path is the only source for a row — the file is still
    screened either way (a human named it deliberately), but this catches the
    case where the wrong file got typed into the wrong row rather than staying
    silent about it. Reuses score_pair's DOI/title/author logic rather than
    duplicating it — the only difference here is there is one candidate file to
    check, not a folder of them to rank.

    Returns {"checked": bool, "ok": bool, "note": str}. checked=False means
    verification was not possible (no title/DOI to compare against, or the
    file could not be read) — not that it passed.
    """
    if not target.has_content_key:
        return {"checked": False, "ok": True,
                "note": "no title or DOI known for this row — cannot verify"}

    fp = fingerprint_file(path, cfg)
    if fp.error:
        return {"checked": False, "ok": True,
                "note": f"file unreadable for verification ({fp.error})"}

    ps = score_pair(target, fp, cfg)
    if ps.score <= 0:
        return {"checked": True, "ok": False,
                "note": "file content does not match this row's title/DOI — "
                        "no evidence connecting the two was found"}
    if ps.score < cfg.review_floor or ps.force_review:
        return {"checked": True, "ok": False, "note": ps.note or
                f"best signal {ps.score:.2f}, below the {cfg.review_floor:.2f} floor"}
    return {"checked": True, "ok": True,
            "note": f"confirmed by {ps.method} ({ps.score:.2f}): {ps.note}"}


def match_library(targets: list, fingerprints: list,
                  cfg: LibraryConfig, log=None) -> MatchReport:
    """Assign files to papers one-to-one, bucketed by confidence."""
    def _log(m):
        if log:
            log(m)

    report = MatchReport()
    # Every file participates. Unreadable ones are limited to filename evidence
    # inside score_pair, and are reported as unreadable only if nothing claimed
    # them — a scan named PAPER_014.pdf is a usable match, not a dead end.
    readable = list(fingerprints)

    for t in targets:
        if not t.has_content_key:
            report.skipped_ids.append(t.paper_id)

    # Score every pair, keeping per-file rankings so ties can be detected.
    pairs = []
    by_file: dict = {}
    forced_flags: dict = {}
    for fp in readable:
        ranked = []
        for t in targets:
            ps = score_pair(t, fp, cfg)
            if ps.score > 0:
                ranked.append((ps.score, t.paper_id, ps.method, ps.note))
                forced_flags[(fp.path, t.paper_id)] = (ps.force_review,
                                                       ps.author_state)
        ranked.sort(reverse=True, key=lambda r: r[0])
        by_file[fp.path] = ranked
        for score, pid, method, note in ranked:
            pairs.append((score, fp.path, pid, method, note))

    pairs.sort(reverse=True, key=lambda r: (r[0], r[1], r[2]))

    used_files, used_papers = set(), set()
    fp_by_path = {f.path: f for f in readable}

    for score, path, pid, method, note in pairs:
        if path in used_files or pid in used_papers:
            continue
        if score < cfg.review_floor:
            continue
        used_files.add(path)
        used_papers.add(pid)

        ranked = by_file.get(path, [])
        runner = ""
        forced, author_state = forced_flags.get((path, pid), (False, "unknown"))
        for s2, pid2, m2, _n2 in ranked:
            if pid2 == pid:
                continue
            if score - s2 <= cfg.tie_margin:
                runner = f"{pid2} ({s2:.2f}) via {m2}"
                forced = True
            break

        m = Match(paper_id=pid, path=path, method=method, score=round(score, 3),
                  kind=fp_by_path[path].kind, note=note, runner_up=runner,
                  author_state=author_state)

        if score >= cfg.auto_accept and not forced:
            report.accepted.append(m)
        else:
            if runner:
                m.note = ((m.note + "; ") if m.note else "") + \
                    f"close second: {runner} — confirm before use"
            report.proposals.append(m)

    for fp in readable:
        if fp.path in used_files:
            continue
        if not fp.readable:
            report.unreadable.append((fp.path, fp.error))
            continue
        ranked = by_file.get(fp.path, [])
        if ranked:
            s, pid, meth, _ = ranked[0]
            if pid in used_papers:
                note = f"best guess {pid} ({s:.2f}) already taken by another file"
            else:
                note = f"best guess {pid} scored {s:.2f}, below floor {cfg.review_floor}"
        else:
            note = "no candidate paper matched"
        report.unmatched_files.append((fp.path, note))

    report.unmatched_papers = [t.paper_id for t in targets
                               if t.paper_id not in used_papers]

    _log(report.summary())
    return report


# ══════════════════════════════════════════════════════════════════════════════
# Building targets
# ══════════════════════════════════════════════════════════════════════════════

def targets_from_rows(rows: list, repo_index: dict = None) -> list:
    """Build PaperTargets from batch CSV rows.

    Recognized optional columns: title, authors, doi, year / publication_year.
    Anything typed into the CSV outranks metadata resolved later. A DOI in the
    url column is used when there is no doi column. Titles from an existing
    repository entry for the same paper_id fill remaining gaps.
    """
    repo_index = repo_index or {}
    out = []
    for i, row in enumerate(rows):
        low = {str(k or "").strip().lower(): (v or "") for k, v in row.items()}
        pid = str(low.get("paper_id") or f"PAPER_{i+1}").strip()
        t = PaperTarget(
            paper_id=pid,
            title=str(low.get("title") or "").strip(),
            doi=normalize_doi(low.get("doi") or ""),
            authors=str(low.get("authors") or "").strip(),
            year=str(low.get("year") or low.get("publication_year") or "").strip(),
            url=str(low.get("url") or "").strip(),
            row_index=i,
        )
        if t.title:
            t.title_source = "csv"
        if not t.doi:
            t.doi = normalize_doi(t.url)
        if pid in repo_index:
            if not t.title:
                t.title = str(repo_index[pid].get("title") or "").strip()
                if t.title:
                    t.title_source = "repository"
            if not t.authors:
                t.authors = str(repo_index[pid].get("authors") or "").strip()
            if not t.doi:
                t.doi = normalize_doi(repo_index[pid].get("doi") or "")
        out.append(t)
    return out


def apply_resolution_metadata(targets: list, results: dict, log=None) -> int:
    """Fill target metadata from retrieval attempts.

    results maps paper_id -> {"status": "ok"|"failed", "metadata": {...},
    "doi": "..."}. pdf_resolver accumulates title/authors/year into
    ResolveResult.metadata as it walks the waterfall and keeps them even when no
    PDF is found — so a FAILED retrieval has already paid for the metadata this
    matching needs. Values typed into the CSV are never overwritten.
    """
    filled = 0
    for t in targets:
        r = results.get(t.paper_id) or {}
        t.retrieval_status = r.get("status", t.retrieval_status)
        meta = r.get("metadata") or {}
        if not t.title and meta.get("title"):
            t.title = str(meta["title"]).strip()
            t.title_source = "resolver"
            filled += 1
        if not t.authors and meta.get("authors"):
            t.authors = str(meta["authors"]).strip()
        if not t.year and meta.get("year"):
            t.year = str(meta["year"]).strip()
        if not t.doi and r.get("doi"):
            t.doi = normalize_doi(r["doi"])
        if log and t.title and t.retrieval_status == "failed":
            log(f"{t.paper_id}: {t.title[:60]}"
                + (f" — {t.authors[:40]}" if t.authors else ""))
    return filled


def enrich_targets_online(targets: list, fetch_metadata, log=None) -> int:
    """Second pass for rows the resolver left without a title.

    fetch_metadata(source) -> dict with title/authors keys. Only called for rows
    that still have no title, since a title is what makes content matching
    possible at all.
    """
    filled = 0
    for t in targets:
        if t.title or not (t.doi or t.url):
            continue
        try:
            meta = fetch_metadata(t.doi or t.url) or {}
        except Exception as exc:
            if log:
                log(f"{t.paper_id}: metadata lookup failed ({type(exc).__name__})")
            continue
        title = str(meta.get("title") or "").strip()
        if title:
            t.title = title
            t.title_source = "resolver"
            if not t.authors and meta.get("authors"):
                t.authors = str(meta["authors"]).strip()
            filled += 1
            if log:
                log(f"{t.paper_id}: title resolved — {title[:70]}")
    return filled


# ══════════════════════════════════════════════════════════════════════════════
# Loading a matched file for screening
# ══════════════════════════════════════════════════════════════════════════════

def load_for_screening(path: str, cfg: LibraryConfig, log=None) -> dict:
    """Prepare a local file for the API.

    Returns {payload, basis, transport, source_kind, source, error}:
      payload     bytes (PDF) or str (extracted slide text)
      basis       screening_basis passed to the prompt builder
      transport   pdf | converted_pdf | slide_text
      source_kind pdf | slides — what the evidence actually is

    Decks are screened as full text by default. source_kind is recorded
    separately so deck-sourced verdicts remain filterable in the repository even
    though the prompt treats them like any other document.
    """
    def _log(m):
        if log:
            log(m)

    p = Path(path)
    out = {"payload": None, "basis": "full_text", "transport": "",
           "source_kind": "pdf", "source": "", "error": ""}
    if not p.is_file():
        out["error"] = "file not found"
        return out
    ext = p.suffix.lower()

    if ext in PDF_EXTS:
        try:
            data = p.read_bytes()
        except Exception as exc:
            out["error"] = f"read failed: {exc}"
            return out
        # Check the file really is a PDF before it becomes an API call. A
        # mis-renamed or truncated download would otherwise be base64-encoded,
        # uploaded, and rejected server-side — one wasted call per paper and an
        # error message that points at the API rather than at the file.
        # Only the header is checked: scanned PDFs with no text layer are
        # perfectly valid input, so parseability is not the test.
        if not data[:5].startswith(b"%PDF"):
            head = data[:16].decode("ascii", "replace").strip()
            out["error"] = (f"not a PDF despite the .pdf extension "
                            f"(file begins {head!r})")
            return out
        out["payload"] = data
        out["transport"] = "pdf"
        out["source"] = "local library (pdf)"
        return out

    if ext in SLIDE_EXTS:
        out["source_kind"] = "slides"
        if cfg.slides_conservative_prompt:
            out["basis"] = "slides"
        if cfg.convert_slides_to_pdf:
            data, err = convert_to_pdf(str(p))
            if data:
                out["payload"] = data
                out["transport"] = "converted_pdf"
                out["source"] = "local library (slides → pdf)"
                _log(f"Converted {p.name} to PDF ({len(data)//1024} KB).")
                return out
            _log(f"Conversion unavailable for {p.name} ({err}); using slide text.")
        fp = fingerprint_file(str(p), LibraryConfig(
            max_slides=200, fingerprint_chars=200_000,
            convert_slides_to_pdf=False))
        if fp.error or not fp.text.strip():
            out["error"] = fp.error or "no text extracted from slides"
            return out
        header = (f"[SOURCE: PowerPoint deck '{p.name}' — text extracted from "
                  f"slides. Figures, charts, and images are NOT included.]\n\n")
        out["payload"] = header + fp.text
        out["transport"] = "slide_text"
        out["source"] = "local library (slide text)"
        return out

    out["error"] = f"unsupported file type {ext}"
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Audit trail
# ══════════════════════════════════════════════════════════════════════════════

MAPPING_COLUMNS = ["paper_id", "file_path", "match_method", "match_score",
                   "file_kind", "confirmed_by", "note", "mapped_at"]


def mapping_rows(matches: list, confirmed_by: str = "auto") -> list:
    """Rows for a mapping audit CSV / for pasting into a batch CSV's file_path."""
    import datetime
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    rows = []
    for m in matches:
        rows.append({
            "paper_id": m.paper_id,
            "file_path": m.path,
            "match_method": m.method,
            "match_score": f"{m.score:.3f}",
            "file_kind": m.kind,
            "confirmed_by": confirmed_by,
            "note": m.note,
            "mapped_at": ts,
        })
    return rows


def write_mapping_csv(path, rows: list) -> None:
    import csv as _csv
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=MAPPING_COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in MAPPING_COLUMNS})


def capabilities() -> dict:
    """What this install can actually do — surfaced in the Settings tab."""
    soffice = find_soffice()
    return {
        "pypdf": _HAS_PYPDF,
        "python_pptx": _HAS_PPTX,
        "libreoffice": bool(soffice),
        "libreoffice_path": soffice,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Command-line self-test
# ══════════════════════════════════════════════════════════════════════════════
#
#   python local_library.py --capabilities
#   python local_library.py --folder ~/papers
#   python local_library.py --folder ~/papers --csv batch.csv
#
# Running the matcher outside the GUI is the fastest way to find out why a file
# is not matching: it shows exactly what was read out of each file, which is
# usually where the answer is.

def _cli_fingerprints(files, cfg):
    print(f"\n{'FILE':44} {'KIND':7} DOI / TITLE / AUTHORS")
    print("─" * 100)
    fps = []
    for f in files:
        fp = fingerprint_file(str(f), cfg)
        fps.append(fp)
        name = fp.name if len(fp.name) <= 43 else fp.name[:40] + "…"
        print(f"{name:44} {fp.kind:7} ", end="")
        if fp.error:
            print(f"!! {fp.error}")
            continue
        print(f"doi: {', '.join(fp.dois) or '(none found)'}")
        print(f"{'':52} title:   {fp.title_guess[:60] or '(none)'}")
        print(f"{'':52} authors: {fp.author_guess[:60] or '(none)'}")
    return fps


def _cli_match(fps, csv_path, cfg):
    import csv as _csv
    rows = []
    for enc in ("utf-8-sig", "latin-1", "cp1252"):
        try:
            with open(csv_path, newline="", encoding=enc) as f:
                rows = list(_csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
    if not rows:
        print(f"No rows read from {csv_path}")
        return
    targets = targets_from_rows(rows)
    keyed = [t for t in targets if t.has_content_key]
    print(f"\n{len(targets)} row(s); {len(keyed)} have a title or DOI to match on.")
    if len(keyed) < len(targets):
        print("  Rows with no title and no DOI cannot be matched automatically:")
        print("   ", ", ".join(t.paper_id for t in targets if not t.has_content_key)[:200])
        print("  In the app, a folder scan fills these from the url's metadata first.")

    report = match_library(targets, fps, cfg)
    print(f"\n{report.summary()}\n")
    for label, items in (("AUTO-ACCEPTED", report.accepted),
                         ("NEEDS CONFIRMATION", report.proposals)):
        if items:
            print(f"{label}:")
            for m in items:
                print(f"  {m.paper_id:16} {Path(m.path).name[:44]:46} [{m.label()}]")
                if m.note:
                    print(f"  {'':16} {m.note[:80]}")
    if report.unmatched_files:
        print("UNMATCHED FILES:")
        for path, note in report.unmatched_files:
            print(f"  {Path(path).name[:44]:46} {note[:60]}")
    if report.unreadable:
        print("UNREADABLE:")
        for path, err in report.unreadable:
            print(f"  {Path(path).name[:44]:46} {err[:60]}")
    if report.unmatched_papers:
        print(f"PAPERS WITH NO FILE ({len(report.unmatched_papers)}): "
              + ", ".join(report.unmatched_papers[:25])
              + (" …" if len(report.unmatched_papers) > 25 else ""))


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Inspect and match a folder of local paper files.")
    ap.add_argument("--folder", help="folder of PDF / PowerPoint files")
    ap.add_argument("--csv", help="batch CSV to match against (needs title or doi columns)")
    ap.add_argument("--capabilities", action="store_true",
                    help="report which optional dependencies are installed")
    ap.add_argument("--no-recursive", action="store_true", help="skip subfolders")
    ap.add_argument("--auto", type=float, default=None, help="auto-accept threshold")
    ap.add_argument("--floor", type=float, default=None, help="unmatched floor")
    args = ap.parse_args(argv)

    caps = capabilities()
    if args.capabilities or not args.folder:
        print("Capabilities")
        print(f"  pypdf (read PDFs)          : {'yes' if caps['pypdf'] else 'NO'}")
        print(f"  python-pptx (read .pptx)   : {'yes' if caps['python_pptx'] else 'NO'}")
        print(f"  LibreOffice (decks → PDF)  : "
              f"{caps['libreoffice_path'] or 'NO — decks fall back to text, .ppt unreadable'}")
        if not args.folder:
            print("\nPass --folder to inspect files. --help for all options.")
            return 0

    cfg = LibraryConfig(folder=args.folder, recursive=not args.no_recursive)
    if args.auto is not None:
        cfg.auto_accept = args.auto
    if args.floor is not None:
        cfg.review_floor = args.floor

    files = scan_folder(cfg, log=print)
    if not files:
        print("No supported files found.")
        return 1
    fps = _cli_fingerprints(files, cfg)
    if args.csv:
        _cli_match(fps, args.csv, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
