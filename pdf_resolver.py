"""
pdf_resolver.py
────────────────────────────────────────────────────────────────────────────────
Turn "a link to a paper" into "the bytes of that paper's PDF", without manual
downloading.

The hard part is not parsing HTML — it is knowing WHERE a free copy lives.
So this is a resolution waterfall, and scraping is the LAST step, not the first:

  1. Direct       — the URL already is a PDF
  2. DOI          — pull a DOI out of the URL or the landing page
  3. Unpaywall    — api.unpaywall.org, free, email only, best_oa_location
  4. OpenAlex     — api.openalex.org, free metadata; optional paid content API
  5. Semantic Scholar — api.semanticscholar.org, openAccessPdf
  6. Europe PMC   — full-text PDFs for anything in PMC
  7. arXiv        — direct /pdf/ pattern for preprints
  8. Landing page — scrape <meta name="citation_pdf_url"> (the Google Scholar
                    convention; most publishers emit it), then PDF-ish <a> hrefs

Every step is optional and individually toggleable.

WHAT THIS DOES NOT DO
  - It does not bypass paywalls. Only open-access copies are retrieved.
  - It does not use Sci-Hub or any shadow library. Expect roughly 50-70%
    coverage on an education-research corpus; the rest still needs a human.
  - Some publishers (Cloudflare-fronted) will refuse automated requests
    regardless of how polite the client is.

BE A GOOD CITIZEN
  Set a contact email. Unpaywall requires one and OpenAlex uses it for the
  "polite pool". Requests are rate limited and results are cached by DOI.
"""

from __future__ import annotations

import io
import json
import re
import time
import threading
from dataclasses import dataclass, field
from urllib.parse import urljoin, quote

import requests

from doi_utils import normalize_doi

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:  # pragma: no cover - optional dependency
    _HAS_BS4 = False


USER_AGENT = (
    "triageQ/1.0 (open-access PDF retrieval for content screening; "
    "mailto:{email}) python-requests"
)

DEFAULT_TIMEOUT = 30
MAX_PDF_BYTES = 40 * 1024 * 1024          # 40 MB guard
MIN_PDF_BYTES = 1024                       # anything smaller is not a paper

ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", re.IGNORECASE)
PMCID_RE = re.compile(r"(PMC\d{6,9})", re.IGNORECASE)


# ══════════════════════════════════════════════════════════════════════════════
# Config & result types
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ResolverConfig:
    enabled: bool = True
    contact_email: str = ""
    openalex_api_key: str = ""          # optional; enables the cached-PDF endpoint
    semantic_scholar_key: str = ""      # optional; raises rate limits

    use_unpaywall: bool = True
    use_openalex: bool = True
    use_openalex_content: bool = False  # paid: ~$0.01/file. Off by default.
    use_semantic_scholar: bool = True
    use_europe_pmc: bool = True
    use_arxiv: bool = True
    use_page_scrape: bool = True

    request_delay: float = 0.5          # seconds between outbound calls
    timeout: int = DEFAULT_TIMEOUT

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> "ResolverConfig":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ResolveResult:
    pdf_bytes: bytes | None = None
    resolved_via: str = ""              # which step succeeded
    pdf_url: str = ""
    doi: str = ""
    landing_url: str = ""
    metadata: dict = field(default_factory=dict)   # title/abstract/year when found
    attempts: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.pdf_bytes is not None

    def summary(self) -> str:
        if self.ok:
            kb = len(self.pdf_bytes) // 1024
            return f"{self.resolved_via} ({kb} KB)"
        return self.error or "not found"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

_last_call = {"t": 0.0}
_rate_lock = threading.Lock()
_doi_cache: dict[str, dict] = {}


def _throttle(delay: float):
    with _rate_lock:
        gap = time.monotonic() - _last_call["t"]
        if gap < delay:
            time.sleep(delay - gap)
        _last_call["t"] = time.monotonic()


def _headers(cfg: ResolverConfig) -> dict:
    email = cfg.contact_email.strip() or "unknown@example.com"
    return {
        "User-Agent": USER_AGENT.format(email=email),
        "Accept": "*/*",
    }


def _is_pdf(content: bytes, content_type: str = "", url: str = "") -> bool:
    if content[:5] == b"%PDF-" or content[:4] == b"%PDF":
        return True
    if "application/pdf" in (content_type or "").lower():
        return content[:4] == b"%PDF"
    return False





def _get(cfg: ResolverConfig, url: str, *, stream: bool = False,
         accept_pdf: bool = False) -> requests.Response:
    _throttle(cfg.request_delay)
    h = _headers(cfg)
    if accept_pdf:
        h["Accept"] = "application/pdf,*/*"
    return requests.get(url, headers=h, timeout=cfg.timeout,
                        allow_redirects=True, stream=stream)


def _download_pdf(cfg: ResolverConfig, url: str) -> tuple[bytes | None, str]:
    """Download a URL and confirm it is really a PDF. Returns (bytes, error)."""
    if not url:
        return None, "empty url"
    try:
        resp = _get(cfg, url, stream=True, accept_pdf=True)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")

        buf = io.BytesIO()
        size = 0
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            buf.write(chunk)
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                return None, f"file exceeds {MAX_PDF_BYTES // (1024*1024)} MB"
        data = buf.getvalue()

        if len(data) < MIN_PDF_BYTES:
            return None, f"response too small ({len(data)} bytes)"
        if not _is_pdf(data, ct, url):
            snippet = ct or "unknown content-type"
            return None, f"not a PDF ({snippet})"
        return data, ""
    except requests.exceptions.RequestException as exc:
        return None, _short_err(exc)


def _short_err(exc: Exception) -> str:
    s = str(exc)
    return (s[:120] + "…") if len(s) > 120 else s


# ══════════════════════════════════════════════════════════════════════════════
# Landing-page parsing
# ══════════════════════════════════════════════════════════════════════════════

_META_PDF_RE = re.compile(
    r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE)
_META_PDF_RE_ALT = re.compile(
    r"""<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""",
    re.IGNORECASE)
_META_DOI_RE = re.compile(
    r"""<meta[^>]+name=["'](?:citation_doi|dc\.identifier)["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE)
_META_TITLE_RE = re.compile(
    r"""<meta[^>]+name=["']citation_title["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE)


def parse_landing_page(html: str, base_url: str) -> dict:
    """Pull citation_pdf_url / citation_doi / citation_title out of a landing page.

    Uses BeautifulSoup when available, regex otherwise, so the app still works
    if bs4 isn't installed.
    """
    out = {"pdf_url": "", "doi": "", "title": "", "candidates": []}
    if not html:
        return out

    if _HAS_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all("meta"):
                name = (tag.get("name") or tag.get("property") or "").lower()
                content = (tag.get("content") or "").strip()
                if not content:
                    continue
                if name == "citation_pdf_url" and not out["pdf_url"]:
                    out["pdf_url"] = urljoin(base_url, content)
                elif name in ("citation_doi", "dc.identifier") and not out["doi"]:
                    out["doi"] = normalize_doi(content)
                elif name == "citation_title" and not out["title"]:
                    out["title"] = content

            # Fallback: anchors that look like a PDF link
            for a in soup.find_all("a", href=True):
                href = a["href"]
                blob = (href + " " + a.get_text(" ", strip=True)).lower()
                if ".pdf" in href.lower() or "/pdf" in href.lower() or "download pdf" in blob:
                    full = urljoin(base_url, href)
                    if full not in out["candidates"]:
                        out["candidates"].append(full)
        except Exception:
            pass

    if not out["pdf_url"]:
        m = _META_PDF_RE.search(html) or _META_PDF_RE_ALT.search(html)
        if m:
            out["pdf_url"] = urljoin(base_url, m.group(1).strip())
    if not out["doi"]:
        m = _META_DOI_RE.search(html)
        if m:
            out["doi"] = normalize_doi(m.group(1))
    if not out["title"]:
        m = _META_TITLE_RE.search(html)
        if m:
            out["title"] = m.group(1).strip()
    if not out["doi"]:
        out["doi"] = normalize_doi(html[:200000])

    out["candidates"] = out["candidates"][:5]
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Individual sources
# ══════════════════════════════════════════════════════════════════════════════

def lookup_unpaywall(cfg: ResolverConfig, doi: str) -> dict:
    """https://api.unpaywall.org/v2/{DOI}?email=  →  pdf urls + metadata."""
    email = cfg.contact_email.strip()
    if not doi or not email:
        return {}
    key = f"unpaywall:{doi}"
    if key in _doi_cache:
        return _doi_cache[key]
    try:
        url = f"https://api.unpaywall.org/v2/{quote(doi)}?email={quote(email)}"
        r = _get(cfg, url)
        if r.status_code != 200:
            return {}
        d = r.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError):
        return {}

    urls: list[str] = []
    best = d.get("best_oa_location") or {}
    for candidate in (best.get("url_for_pdf"), best.get("url")):
        if candidate and candidate not in urls:
            urls.append(candidate)
    for loc in (d.get("oa_locations") or []):
        for candidate in (loc.get("url_for_pdf"), loc.get("url")):
            if candidate and candidate not in urls:
                urls.append(candidate)

    out = {
        "pdf_urls": urls,
        "is_oa": bool(d.get("is_oa")),
        "oa_status": d.get("oa_status") or "",
        "title": d.get("title") or "",
        "year": d.get("year") or "",
        "journal": d.get("journal_name") or "",
        "authors": "; ".join(
            f"{a.get('family','')}, {a.get('given','')}".strip(", ")
            for a in (d.get("z_authors") or []) if a
        ),
    }
    _doi_cache[key] = out
    return out


def _openalex_abstract(inverted: dict | None) -> str:
    """OpenAlex stores abstracts as an inverted index; rebuild the text."""
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs or []:
            positions.append((i, word))
    if not positions:
        return ""
    positions.sort(key=lambda t: t[0])
    return " ".join(w for _, w in positions)


def lookup_openalex(cfg: ResolverConfig, doi: str) -> dict:
    """OpenAlex work record: OA locations, plus title/abstract for metadata-only mode."""
    if not doi:
        return {}
    key = f"openalex:{doi}"
    if key in _doi_cache:
        return _doi_cache[key]
    try:
        url = f"https://api.openalex.org/works/doi:{quote(doi)}"
        params = []
        if cfg.contact_email.strip():
            params.append(f"mailto={quote(cfg.contact_email.strip())}")
        if cfg.openalex_api_key.strip():
            params.append(f"api_key={quote(cfg.openalex_api_key.strip())}")
        if params:
            url += "?" + "&".join(params)
        r = _get(cfg, url)
        if r.status_code != 200:
            return {}
        d = r.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError):
        return {}

    urls: list[str] = []
    for loc in ([d.get("best_oa_location"), d.get("primary_location")]
                + (d.get("locations") or [])):
        if not loc:
            continue
        for candidate in (loc.get("pdf_url"), loc.get("landing_page_url")):
            if candidate and candidate not in urls:
                urls.append(candidate)
    oa_url = (d.get("open_access") or {}).get("oa_url")
    if oa_url and oa_url not in urls:
        urls.append(oa_url)

    src = ((d.get("primary_location") or {}).get("source") or {})
    out = {
        "pdf_urls": urls,
        "work_id": (d.get("id") or "").rsplit("/", 1)[-1],
        "content_url": d.get("content_url") or "",
        "is_oa": bool((d.get("open_access") or {}).get("is_oa")),
        "title": d.get("title") or d.get("display_name") or "",
        "year": d.get("publication_year") or "",
        "journal": src.get("display_name") or "",
        "abstract": _openalex_abstract(d.get("abstract_inverted_index")),
        "authors": "; ".join(
            (a.get("author") or {}).get("display_name", "")
            for a in (d.get("authorships") or [])
        ).strip("; "),
    }
    _doi_cache[key] = out
    return out


def lookup_semantic_scholar(cfg: ResolverConfig, doi: str) -> dict:
    if not doi:
        return {}
    key = f"s2:{doi}"
    if key in _doi_cache:
        return _doi_cache[key]
    fields = "title,abstract,year,venue,authors,openAccessPdf,externalIds"
    url = (f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi)}"
           f"?fields={fields}")
    try:
        _throttle(max(cfg.request_delay, 1.0))   # S2 is stricter without a key
        h = _headers(cfg)
        if cfg.semantic_scholar_key.strip():
            h["x-api-key"] = cfg.semantic_scholar_key.strip()
        r = requests.get(url, headers=h, timeout=cfg.timeout)
        if r.status_code != 200:
            return {}
        d = r.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError):
        return {}

    pdf = (d.get("openAccessPdf") or {}).get("url") or ""
    out = {
        "pdf_urls": [pdf] if pdf else [],
        "title": d.get("title") or "",
        "abstract": d.get("abstract") or "",
        "year": d.get("year") or "",
        "journal": d.get("venue") or "",
        "authors": "; ".join(a.get("name", "") for a in (d.get("authors") or [])),
        "pmcid": (d.get("externalIds") or {}).get("PubMedCentral") or "",
    }
    _doi_cache[key] = out
    return out


def lookup_europe_pmc(cfg: ResolverConfig, doi: str = "", pmcid: str = "") -> dict:
    """Europe PMC covers the PMC open-access subset and links out to publishers."""
    if not doi and not pmcid:
        return {}
    try:
        if not pmcid:
            q = f'DOI:"{doi}"'
            url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                   f"?query={quote(q)}&format=json&resultType=core&pageSize=1")
            r = _get(cfg, url)
            if r.status_code != 200:
                return {}
            results = ((r.json().get("resultList") or {}).get("result") or [])
            if not results:
                return {}
            rec = results[0]
            pmcid = rec.get("pmcid") or ""
            meta = {
                "title": rec.get("title") or "",
                "abstract": rec.get("abstractText") or "",
                "year": rec.get("pubYear") or "",
                "journal": ((rec.get("journalInfo") or {}).get("journal") or {})
                           .get("title", ""),
                "authors": rec.get("authorString") or "",
            }
        else:
            meta = {}

        urls = []
        if pmcid:
            urls.append("https://www.ebi.ac.uk/europepmc/webservices/rest/"
                        f"{pmcid}/fullTextPDF")
            urls.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/")
        meta["pdf_urls"] = urls
        meta["pmcid"] = pmcid
        return meta
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError):
        return {}


def arxiv_pdf_urls(source: str) -> list[str]:
    m = ARXIV_RE.search(source or "")
    if m:
        return [f"https://arxiv.org/pdf/{m.group(1)}"]
    m = re.search(r"\barXiv[:\s]+([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", source or "", re.IGNORECASE)
    if m:
        return [f"https://arxiv.org/pdf/{m.group(1)}"]
    return []


# ══════════════════════════════════════════════════════════════════════════════
# The waterfall
# ══════════════════════════════════════════════════════════════════════════════

def resolve_pdf(source: str, cfg: ResolverConfig, log=None) -> ResolveResult:
    """Resolve a URL, DOI, or arXiv id to PDF bytes.

    `log` is an optional callable(str) for progress messages.
    """
    res = ResolveResult()
    source = (source or "").strip()
    if not source:
        res.error = "no source given"
        return res

    def note(msg: str):
        res.attempts.append(msg)
        if log:
            log(msg)

    is_url = source.lower().startswith(("http://", "https://"))
    res.landing_url = source if is_url else ""
    res.doi = normalize_doi(source)

    # ── Step 1: the URL is already a PDF ──────────────────────────────────────
    if is_url:
        looks_pdf = source.lower().endswith(".pdf") or "/pdf" in source.lower()
        if looks_pdf:
            data, err = _download_pdf(cfg, source)
            if data:
                res.pdf_bytes, res.resolved_via, res.pdf_url = data, "direct link", source
                note("direct link → ok")
                return res
            note(f"direct link → {err}")

    if not cfg.enabled:
        res.error = "auto-resolution disabled"
        return res

    # ── Step 2: arXiv shortcut ────────────────────────────────────────────────
    if cfg.use_arxiv:
        for u in arxiv_pdf_urls(source):
            data, err = _download_pdf(cfg, u)
            if data:
                res.pdf_bytes, res.resolved_via, res.pdf_url = data, "arXiv", u
                note("arXiv → ok")
                return res
            note(f"arXiv → {err}")

    # ── Step 3: fetch the landing page for a DOI and citation_pdf_url ─────────
    page_info = {}
    if is_url:
        try:
            r = _get(cfg, source)
            ct = r.headers.get("content-type", "")
            if _is_pdf(r.content[:8], ct, source):
                res.pdf_bytes = r.content
                res.resolved_via, res.pdf_url = "direct (content-type)", source
                note("landing page was itself a PDF → ok")
                return res
            if "html" in ct.lower() or r.text.lstrip()[:100].lower().startswith(("<!doct", "<html")):
                page_info = parse_landing_page(r.text, str(r.url))
                if page_info.get("doi") and not res.doi:
                    res.doi = page_info["doi"]
                    note(f"landing page → DOI {res.doi}")
                if page_info.get("title"):
                    res.metadata.setdefault("title", page_info["title"])
        except requests.exceptions.RequestException as exc:
            note(f"landing page → {_short_err(exc)}")

    doi = res.doi

    # ── Step 4: Unpaywall ─────────────────────────────────────────────────────
    if doi and cfg.use_unpaywall:
        if not cfg.contact_email.strip():
            note("Unpaywall → skipped (contact email required)")
        else:
            info = lookup_unpaywall(cfg, doi)
            _merge_meta(res, info)
            if not info:
                note("Unpaywall → no record")
            elif not info.get("pdf_urls"):
                note(f"Unpaywall → no OA copy (status: {info.get('oa_status') or 'unknown'})")
            else:
                for u in info["pdf_urls"][:4]:
                    data, err = _download_pdf(cfg, u)
                    if data:
                        res.pdf_bytes, res.resolved_via, res.pdf_url = data, "Unpaywall", u
                        note("Unpaywall → ok")
                        return res
                note(f"Unpaywall → {len(info['pdf_urls'])} link(s), none downloadable")

    # ── Step 5: OpenAlex ──────────────────────────────────────────────────────
    oa_info = {}
    if doi and cfg.use_openalex:
        oa_info = lookup_openalex(cfg, doi)
        _merge_meta(res, oa_info)
        if not oa_info:
            note("OpenAlex → no record")
        else:
            for u in (oa_info.get("pdf_urls") or [])[:4]:
                data, err = _download_pdf(cfg, u)
                if data:
                    res.pdf_bytes, res.resolved_via, res.pdf_url = data, "OpenAlex", u
                    note("OpenAlex → ok")
                    return res
            note("OpenAlex → no downloadable OA copy")

    # ── Step 5b: OpenAlex cached content API (paid, opt-in) ───────────────────
    if (cfg.use_openalex_content and cfg.openalex_api_key.strip()
            and oa_info.get("work_id")):
        u = (f"https://content.openalex.org/works/{oa_info['work_id']}.pdf"
             f"?api_key={quote(cfg.openalex_api_key.strip())}")
        data, err = _download_pdf(cfg, u)
        if data:
            res.pdf_bytes = data
            res.resolved_via = "OpenAlex content API (billed)"
            res.pdf_url = f"https://content.openalex.org/works/{oa_info['work_id']}.pdf"
            note("OpenAlex content API → ok")
            return res
        note(f"OpenAlex content API → {err}")

    # ── Step 6: Semantic Scholar ──────────────────────────────────────────────
    s2 = {}
    if doi and cfg.use_semantic_scholar:
        s2 = lookup_semantic_scholar(cfg, doi)
        _merge_meta(res, s2)
        if s2.get("pdf_urls"):
            for u in s2["pdf_urls"]:
                data, err = _download_pdf(cfg, u)
                if data:
                    res.pdf_bytes = data
                    res.resolved_via, res.pdf_url = "Semantic Scholar", u
                    note("Semantic Scholar → ok")
                    return res
            note("Semantic Scholar → OA link not downloadable")
        else:
            note("Semantic Scholar → no OA PDF")

    # ── Step 7: Europe PMC ────────────────────────────────────────────────────
    if cfg.use_europe_pmc:
        pmcid = s2.get("pmcid") or ""
        if not pmcid:
            m = PMCID_RE.search(source)
            pmcid = m.group(1).upper() if m else ""
        epmc = lookup_europe_pmc(cfg, doi=doi, pmcid=pmcid)
        _merge_meta(res, epmc)
        if epmc.get("pdf_urls"):
            for u in epmc["pdf_urls"]:
                data, err = _download_pdf(cfg, u)
                if data:
                    res.pdf_bytes, res.resolved_via, res.pdf_url = data, "Europe PMC", u
                    note("Europe PMC → ok")
                    return res
            note("Europe PMC → record found, PDF not retrievable")
        else:
            note("Europe PMC → no record")

    # ── Step 8: scrape the landing page (last resort) ─────────────────────────
    if cfg.use_page_scrape:
        # Refetch via doi.org if we never saw a landing page
        if not page_info and doi:
            try:
                r = _get(cfg, f"https://doi.org/{quote(doi)}")
                ct = r.headers.get("content-type", "")
                if _is_pdf(r.content[:8], ct):
                    res.pdf_bytes = r.content
                    res.resolved_via, res.pdf_url = "doi.org redirect", str(r.url)
                    note("doi.org → ok")
                    return res
                page_info = parse_landing_page(r.text, str(r.url))
                res.landing_url = str(r.url)
            except requests.exceptions.RequestException as exc:
                note(f"doi.org → {_short_err(exc)}")

        tried = []
        if page_info.get("pdf_url"):
            tried.append(page_info["pdf_url"])
        tried.extend(page_info.get("candidates") or [])
        for u in tried[:5]:
            data, err = _download_pdf(cfg, u)
            if data:
                res.pdf_bytes = data
                res.resolved_via = ("citation_pdf_url meta tag"
                                    if u == page_info.get("pdf_url") else "page link")
                res.pdf_url = u
                note(f"{res.resolved_via} → ok")
                return res
        if tried:
            note(f"page scrape → {len(tried)} candidate(s), none downloadable")
        else:
            note("page scrape → no PDF link on page")

    res.error = "no open-access PDF found"
    return res


def _merge_meta(res: ResolveResult, info: dict):
    """Keep the first non-empty value seen for each metadata field."""
    if not info:
        return
    for k in ("title", "abstract", "year", "journal", "authors"):
        v = info.get(k)
        if v and not res.metadata.get(k):
            res.metadata[k] = v
    if info.get("doi") and not res.doi:
        res.doi = info["doi"]


# ══════════════════════════════════════════════════════════════════════════════
# Abstract-only fallback
# ══════════════════════════════════════════════════════════════════════════════

def fetch_metadata_only(source: str, cfg: ResolverConfig, log=None) -> dict:
    """Title/abstract/venue for a paper whose PDF could not be retrieved.

    Enables stage-1 (title/abstract) screening without a full text. Returns {}
    when no abstract could be found — screening on a title alone is not useful.
    """
    doi = normalize_doi(source)
    meta: dict = {}

    if not doi and source.lower().startswith(("http://", "https://")):
        try:
            r = _get(cfg, source)
            info = parse_landing_page(r.text, str(r.url))
            doi = info.get("doi", "")
            if info.get("title"):
                meta["title"] = info["title"]
        except requests.exceptions.RequestException:
            pass

    if not doi:
        return meta

    for fn in (lookup_openalex, lookup_semantic_scholar):
        try:
            info = fn(cfg, doi)
        except Exception:
            continue
        for k in ("title", "abstract", "year", "journal", "authors"):
            if info.get(k) and not meta.get(k):
                meta[k] = info[k]
        if meta.get("abstract"):
            break

    if not meta.get("abstract"):
        try:
            info = lookup_europe_pmc(cfg, doi=doi)
            for k in ("title", "abstract", "year", "journal", "authors"):
                if info.get(k) and not meta.get(k):
                    meta[k] = info[k]
        except Exception:
            pass

    if meta:
        meta["doi"] = doi
    if log and meta.get("abstract"):
        log(f"metadata fallback → abstract found ({len(meta['abstract'])} chars)")
    return meta


def format_metadata_as_text(meta: dict) -> str:
    """Render metadata into the text block sent in place of a PDF."""
    parts = [
        "BIBLIOGRAPHIC RECORD (full text unavailable)",
        "",
        f"Title: {meta.get('title') or '(not found)'}",
        f"Authors: {meta.get('authors') or '(not found)'}",
        f"Year: {meta.get('year') or '(not found)'}",
        f"Journal/Venue: {meta.get('journal') or '(not found)'}",
        f"DOI: {meta.get('doi') or '(not found)'}",
        "",
        "Abstract:",
        meta.get("abstract") or "(no abstract available)",
    ]
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Self-test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    cfg = ResolverConfig(contact_email=(sys.argv[2] if len(sys.argv) > 2 else ""))
    target = sys.argv[1] if len(sys.argv) > 1 else "10.7717/peerj.4375"
    print(f"Resolving: {target}\n")
    out = resolve_pdf(target, cfg, log=lambda m: print("  ·", m))
    print()
    print("Result:", out.summary())
    print("DOI:", out.doi or "(none)")
    print("PDF URL:", out.pdf_url or "(none)")
    if out.metadata:
        print("Metadata:", json.dumps(out.metadata, indent=2)[:600])
