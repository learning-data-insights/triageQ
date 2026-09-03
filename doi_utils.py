"""
Shared DOI parsing used by both pdf_resolver.py (online) and local_library.py
(offline). Stdlib-only (just `re`) so importing it adds no dependency beyond
what both modules already require, and all files can still be copied and run
independently — see the "Files" section of the README.
"""

from __future__ import annotations

import re

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)
_DOI_TRAILING = ".,;:)]}>\"'"


def normalize_doi(raw: str) -> str:
    """Strip prefixes and trailing punctuation from anything DOI-shaped."""
    s = str(raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\s*(doi:|https?://(dx\.)?doi\.org/)", "", s, flags=re.IGNORECASE)
    m = DOI_RE.search(s)
    if not m:
        return ""
    return m.group(1).rstrip(_DOI_TRAILING).lower()


def extract_doi_from_text(text: str) -> str:
    """First DOI found anywhere in free text, normalized. Empty if none."""
    return normalize_doi(text or "")


def extract_dois(text: str) -> list:
    """All distinct DOIs in free text, normalized, in order of first appearance."""
    seen, out = set(), []
    for m in DOI_RE.finditer(text or ""):
        d = m.group(1).rstrip(_DOI_TRAILING).lower()
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out
