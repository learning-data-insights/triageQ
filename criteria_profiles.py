"""
criteria_profiles.py
────────────────────────────────────────────────────────────────────────────────
Configurable screening criteria for triageQ.

A "criteria profile" is a structured JSON object that fully describes one
review's inclusion/exclusion logic. The system prompt sent to the model is
RENDERED from the profile — nothing about the criteria is hardcoded in the app.

Two layers, by design:

  Layer 1  RAW CRITERIA   — whatever a team already has (protocol text, a Word
                            doc, a pasted paragraph).
  Layer 2  COMPILED PROFILE — structured, with explicit include_if / exclude_if
                            boundaries, reviewed and approved by a human before
                            it is ever used to screen a paper.

The compiler (compile_criteria) turns Layer 1 into a draft of
Layer 2. The human review step is not optional — free-text criteria without
explicit boundary rules are the known cause of false INCLUDEs.

Profiles are stored as one JSON file each in <repo_dir>/criteria_profiles/.
"""

from __future__ import annotations

import json
import re
import datetime
from pathlib import Path

PROFILE_SCHEMA_VERSION = 1

VERDICT_VALUES = ("YES", "NO", "UNCLEAR")
RECOMMENDATIONS = ("INCLUDE", "EXCLUDE", "MANUAL_REVIEW")

DEFAULT_LANGUAGE_RULE = (
    "All reasoning, text_examples, key_decision_factors, and additional_notes fields must "
    "contain ONLY verifiable facts stated in the paper: model names, task descriptions, "
    "reported metrics, dataset names, sample sizes, and direct quotes. Do NOT include "
    'evaluative language such as "well-documented," "high-quality," "strong example," '
    '"impressive," "thorough," or any other qualitative judgment about the paper\'s merit. '
    "Describe what the paper does, not how good it is."
)

DEFAULT_CONFIDENCE_RULES = {
    "high": (
        "All criteria are unambiguously met or unambiguously not met based on explicit "
        "statements in the paper. No boundary judgment was required."
    ),
    "medium": (
        "At least one criterion required meaningful interpretation — e.g. the topic is "
        "adjacent to but not clearly within scope; the role of the intervention is secondary "
        "or unclear; metrics are reported but for a proxy task rather than the main outcome."
    ),
    "low": (
        "The paper sits on a genuine boundary; key information is missing or contradictory; "
        "the study could be interpreted either way by a reasonable reviewer."
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# No profile ships built in. triageQ is criteria-agnostic by design: the first
# thing a new install needs is a profile the user defines, either by pasting
# raw criteria for the compiler to structure (see compile_criteria
# below) or by writing one directly to this schema. EMPTY_PROFILE exists only
# as an in-memory placeholder the app can point to before that first profile
# is created — it is never written to disk and can never be used to screen a
# paper (validate_profile rejects it: zero criteria is invalid on purpose).
# ══════════════════════════════════════════════════════════════════════════════

EMPTY_PROFILE = {
    "schema_version": PROFILE_SCHEMA_VERSION,
    "profile_id": "",
    "profile_name": "No criteria profile yet",
    "profile_version": "0",
    "description": "Create a profile to begin screening.",
    "created_at": "",
    "locked": False,
    "builtin": True,
    "language_requirement": "English",
    "min_publication_year": None,
    "language_rule": DEFAULT_LANGUAGE_RULE,
    "confidence_rules": dict(DEFAULT_CONFIDENCE_RULES),
    "global_exclusions": [],
    "criteria": [],
}



# ══════════════════════════════════════════════════════════════════════════════
# Storage
# ══════════════════════════════════════════════════════════════════════════════

def profiles_dir(repo_dir: Path) -> Path:
    d = Path(repo_dir) / "criteria_profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(repo_dir: Path, profile_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", profile_id)
    return profiles_dir(repo_dir) / f"{safe}.json"


def list_profiles(repo_dir: Path) -> list[dict]:
    """All profiles on disk, sorted by name. Corrupt files are skipped."""
    out = []
    for f in sorted(profiles_dir(repo_dir).glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("profile_id"):
                d["_path"] = str(f)
                out.append(d)
        except Exception:
            continue
    out.sort(key=lambda d: (d.get("profile_name") or "").lower())
    return out


def load_profile(repo_dir: Path, profile_id: str) -> dict | None:
    p = _profile_path(repo_dir, profile_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_profile(repo_dir: Path, profile: dict) -> Path:
    ok, errors = validate_profile(profile)
    if not ok:
        raise ValueError("Invalid profile:\n  - " + "\n  - ".join(errors))
    profile.setdefault("schema_version", PROFILE_SCHEMA_VERSION)
    profile.setdefault("created_at", datetime.datetime.now().isoformat(timespec="seconds"))
    profile["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    profile.pop("_path", None)
    p = _profile_path(repo_dir, profile["profile_id"])
    p.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def delete_profile(repo_dir: Path, profile_id: str) -> bool:
    p = _profile_path(repo_dir, profile_id)
    if p.exists():
        p.unlink()
        return True
    return False


def bump_version(version: str) -> str:
    """1.0 -> 1.1, 1.9 -> 1.10, 'draft' -> 'draft.1'."""
    m = re.match(r"^(\d+)\.(\d+)$", str(version).strip())
    if m:
        return f"{m.group(1)}.{int(m.group(2)) + 1}"
    m = re.match(r"^(\d+)$", str(version).strip())
    if m:
        return f"{m.group(1)}.1"
    return f"{version}.1"


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (name or "profile")).strip("-").lower()
    return s or "profile"


# ══════════════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════════════

def validate_profile(profile: dict) -> tuple[bool, list[str]]:
    """Structural validation. Returns (ok, [error strings])."""
    e: list[str] = []
    if not isinstance(profile, dict):
        return False, ["Profile must be a JSON object."]

    for field in ("profile_id", "profile_name", "profile_version"):
        if not str(profile.get(field) or "").strip():
            e.append(f"Missing required field: {field}")

    pid = str(profile.get("profile_id") or "")
    if pid and not re.match(r"^[A-Za-z0-9_.-]+$", pid):
        e.append("profile_id may only contain letters, numbers, hyphens, underscores, periods.")

    crits = profile.get("criteria")
    if not isinstance(crits, list) or not crits:
        e.append("Profile must define at least one criterion in 'criteria'.")
        return False, e

    seen_ids: set[str] = set()
    n_required = 0
    for i, c in enumerate(crits, start=1):
        tag = f"criteria[{i}]"
        if not isinstance(c, dict):
            e.append(f"{tag} must be an object.")
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            e.append(f"{tag} missing 'id'.")
        elif not re.match(r"^[a-z][a-z0-9_]*$", cid):
            e.append(f"{tag} id '{cid}' must be lowercase snake_case (letters, digits, _).")
        elif cid in seen_ids:
            e.append(f"{tag} duplicate id '{cid}'.")
        else:
            seen_ids.add(cid)

        if not str(c.get("label") or "").strip():
            e.append(f"{tag} missing 'label'.")
        if not str(c.get("definition") or "").strip():
            e.append(f"{tag} missing 'definition'.")

        has_inc = bool(c.get("include_if")) or bool(c.get("include_if_groups")) \
            or bool(c.get("categories"))
        if not has_inc:
            e.append(
                f"{tag} ('{cid}') has no include_if, include_if_groups, or categories. "
                "Every criterion needs explicit YES conditions."
            )
        if not c.get("exclude_if") and not c.get("categories"):
            e.append(
                f"{tag} ('{cid}') has no exclude_if rules. Explicit NO conditions are what "
                "prevent liberal interpretation — add at least one."
            )
        if c.get("required_for_include", True):
            n_required += 1

        tf = c.get("tag_field")
        if tf is not None:
            if not isinstance(tf, dict):
                e.append(f"{tag} tag_field must be an object.")
            else:
                if not str(tf.get("name") or "").strip():
                    e.append(f"{tag} tag_field missing 'name'.")
                elif not re.match(r"^[a-z][a-z0-9_]*$", str(tf["name"])):
                    e.append(f"{tag} tag_field name must be lowercase snake_case.")
                av = tf.get("allowed_values")
                if av is not None and not isinstance(av, list):
                    e.append(f"{tag} tag_field allowed_values must be a list.")

        cats = c.get("categories")
        if cats is not None:
            if not isinstance(cats, list):
                e.append(f"{tag} categories must be a list.")
            else:
                for j, cat in enumerate(cats, start=1):
                    if not isinstance(cat, dict) or not str(cat.get("name") or "").strip():
                        e.append(f"{tag} categories[{j}] needs a 'name'.")

    if n_required == 0:
        e.append("At least one criterion must have required_for_include = true.")

    yr = profile.get("min_publication_year")
    if yr not in (None, "") and not (isinstance(yr, int) and 1900 <= yr <= 2100):
        e.append("min_publication_year must be a 4-digit integer year, or null.")

    conf = profile.get("confidence_rules")
    if conf is not None and not isinstance(conf, dict):
        e.append("confidence_rules must be an object with high/medium/low keys.")

    return (len(e) == 0), e


def normalize_profile(profile: dict) -> dict:
    """Fill in optional fields so downstream code can assume they exist."""
    p = json.loads(json.dumps(profile))
    p.setdefault("schema_version", PROFILE_SCHEMA_VERSION)
    p.setdefault("description", "")
    p.setdefault("locked", False)
    p.setdefault("builtin", False)
    p.setdefault("language_requirement", "English")
    p.setdefault("min_publication_year", None)
    p.setdefault("language_rule", DEFAULT_LANGUAGE_RULE)
    p.setdefault("global_exclusions", [])
    cr = p.get("confidence_rules") or {}
    p["confidence_rules"] = {
        "high": cr.get("high") or DEFAULT_CONFIDENCE_RULES["high"],
        "medium": cr.get("medium") or DEFAULT_CONFIDENCE_RULES["medium"],
        "low": cr.get("low") or DEFAULT_CONFIDENCE_RULES["low"],
    }
    for c in p.get("criteria", []):
        c.setdefault("include_if", [])
        c.setdefault("exclude_if", [])
        c.setdefault("notes", [])
        c.setdefault("required_for_include", True)
        c.setdefault("tag_field", None)
        c.setdefault("categories", None)
        c.setdefault("include_if_groups", None)
        c.setdefault("groups_note", "")
    return p


# ══════════════════════════════════════════════════════════════════════════════
# Derived shapes: column names, expected JSON schema
# ══════════════════════════════════════════════════════════════════════════════

def criterion_ids(profile: dict) -> list[str]:
    return [c["id"] for c in profile.get("criteria", []) if c.get("id")]


def tag_fields(profile: dict) -> list[tuple[str, str]]:
    """[(criterion_id, tag_field_name), ...] for criteria that carry a tag field."""
    out = []
    for c in profile.get("criteria", []):
        tf = c.get("tag_field")
        if tf and tf.get("name"):
            out.append((c["id"], tf["name"]))
    return out


def profile_csv_columns(profile: dict, base_columns: list[str]) -> list[str]:
    """Base columns + one verdict column per criterion + one column per tag field."""
    cols = list(base_columns)
    for c in profile.get("criteria", []):
        cid = c.get("id")
        if cid and cid not in cols:
            cols.append(cid)
        tf = c.get("tag_field")
        if tf and tf.get("name") and tf["name"] not in cols:
            cols.append(tf["name"])
    return cols


def example_output_json(profile: dict) -> str:
    """The JSON skeleton shown to the model, derived from the profile."""
    crit_block = {}
    for c in profile.get("criteria", []):
        block = {
            "verdict": "YES",
            "reasoning": f"Factual description of how the paper does or does not meet "
                         f"'{c.get('label')}'",
            "text_examples": "Direct quote or close paraphrase from the paper",
            "location": "Page/section reference",
        }
        tf = c.get("tag_field")
        if tf and tf.get("name"):
            av = tf.get("allowed_values") or []
            block[tf["name"]] = [av[0]] if av else ["Example value", "Another value"]
        crit_block[c["id"]] = block

    obj = {
        "title": "Full paper title",
        "authors": "Last, First; Last, First",
        "publication_year": "2024",
        "journal_or_venue": "Computers and Education: Artificial Intelligence",
        "doi": "10.1016/j.compedu.2024.01.001",
        "abstract": "Full abstract text copied verbatim from the paper.",
        "criteria": crit_block,
        "confidence_level": "High",
        "confidence_rationale": "Specific statement of which criterion required interpretation, "
                                "or confirmation that all criteria were unambiguous",
        "key_decision_factors": "Factual list of the specific evidence that determined the "
                                "recommendation",
        "additional_notes": "Factual observations relevant for human reviewers: boundary issues, "
                            "missing information, or conflicting signals in the paper",
        "overall_recommendation": "INCLUDE",
    }
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# Prompt rendering
# ══════════════════════════════════════════════════════════════════════════════

def _bullets(items, indent="  ") -> str:
    return "\n".join(f"{indent}- {str(i).strip()}" for i in items if str(i).strip())


def build_system_prompt(profile: dict, screening_basis: str = "full_text") -> str:
    """Render the complete system prompt from a criteria profile.

    screening_basis:
      "full_text"     a PDF of the paper itself is attached
      "abstract_only" only title/abstract metadata is available
      "slides"        the source is a presentation deck about the study, not the
                      study — treat omissions as UNCLEAR, not as NO
    Both non-full-text modes push verdicts toward UNCLEAR, for the same reason:
    the evidence is a summary of the work, and a summary's silence is not a
    finding about the work.
    """
    p = normalize_profile(profile)
    crits = p["criteria"]
    required_ids = [c["id"] for c in crits if c.get("required_for_include", True)]

    L: list[str] = []
    A = L.append

    name = p.get("profile_name") or "this systematic review"
    desc = (p.get("description") or "").strip()
    A(f"You are a systematic literature review screener for {name}.")
    if desc:
        A(desc)
    A("Your job is to evaluate whether a research paper meets the inclusion criteria")
    A("for this review.")
    A("")

    A("## CRITICAL LANGUAGE RULE")
    A(p["language_rule"])
    A("")

    # ── Criteria ──
    for idx, c in enumerate(crits, start=1):
        opt = "" if c.get("required_for_include", True) else "  (OPTIONAL — informational only)"
        A(f"## Criterion {idx}: {c['label']} — verdict: YES / NO / UNCLEAR{opt}")
        A(str(c.get("definition") or "").strip())

        tf = c.get("tag_field")
        if tf and tf.get("name"):
            av = tf.get("allowed_values") or []
            if tf.get("required"):
                A("")
                A(f"The {tf['name']} field is REQUIRED and must always contain at least one value.")
            if av:
                A(f"Valid {tf['name']} values (use exact spelling):")
                for v in av:
                    A(f'  "{v}"')
            else:
                A(f"The {tf['name']} field is free-form: list the specific values found in "
                  f"the paper.")
            if tf.get("fallback_rule"):
                A("")
                A(str(tf["fallback_rule"]).strip())

        if c.get("include_if"):
            A("")
            A("INCLUDE (verdict YES) if:")
            A(_bullets(c["include_if"]))

        for grp in (c.get("include_if_groups") or []):
            A("")
            A(str(grp.get("label") or "Path").strip() + ":")
            A(_bullets(grp.get("items") or []))
        if c.get("include_if_groups") and c.get("groups_note"):
            A("")
            A("IMPORTANT: " + str(c["groups_note"]).strip())

        for cat in (c.get("categories") or []):
            A("")
            A(f"{str(cat.get('name','')).upper()} (YES if):")
            A(_bullets(cat.get("yes_if") or []))
            if cat.get("no_if"):
                A("  (NO if): " + "; ".join(str(x).strip() for x in cat["no_if"]))

        if c.get("exclude_if"):
            A("")
            A("EXCLUDE (verdict NO) if:")
            A(_bullets(c["exclude_if"]))

        for n in (c.get("notes") or []):
            A(f"- {str(n).strip()}")
        A("")

    if p.get("global_exclusions"):
        A("## Review-Wide Exclusions")
        A("If any of the following apply, the paper is EXCLUDED regardless of the "
          "criterion verdicts above:")
        A(_bullets(p["global_exclusions"]))
        A("")

    # ── Confidence ──
    conf = p["confidence_rules"]
    A("## Confidence Calibration")
    A('You MUST assign one of exactly three values — "High", "Medium", or "Low" — to '
      "confidence_level.")
    A("This field is required and must never be null, empty, or omitted.")
    A("Assign based on how much interpretation was required:")
    A(f"- High: {conf['high']}")
    A(f"- Medium: {conf['medium']}")
    A(f"- Low: {conf['low']}")
    A("")

    # ── Decision rules ──
    req_list = ", ".join(required_ids)
    A("## Decision Rules — STRICTLY DETERMINISTIC")
    A("Apply these rules in order, based solely on the verdict values assigned above.")
    A("")
    A(f"The required criteria are: {req_list}")
    A("")
    A("1. Every required criterion is YES → overall_recommendation = \"INCLUDE\"")
    disq = ["Any required verdict is NO"]
    if p.get("language_requirement"):
        disq.append(f"paper is not in {p['language_requirement']}")
    if p.get("min_publication_year"):
        disq.append(f"paper published before {p['min_publication_year']}")
    A(f"2. {', OR '.join(disq)} → overall_recommendation = \"EXCLUDE\"")
    A('3. Any required verdict is UNCLEAR (and none are NO) → '
      'overall_recommendation = "MANUAL_REVIEW"')
    A("")
    A("CRITICAL CONSTRAINTS:")
    A("- If all required verdicts are YES, the recommendation MUST be \"INCLUDE\". Always.")
    A("  Never return \"MANUAL_REVIEW\" when all required verdicts are YES, even if confidence")
    A("  is Low. Low confidence is recorded in confidence_level — it does not change the")
    A("  recommendation.")
    A('- "MANUAL_REVIEW" is only valid when at least one required verdict is "UNCLEAR".')
    A("- These rules are not guidelines — they are the complete and only logic for")
    A("  overall_recommendation.")
    A("")

    # ── Metadata ──
    A("## Publication Metadata Extraction")
    A("Extract the following fields directly from the paper. Report null for any field not found.")
    A("- title: Full paper title as printed")
    A('- authors: All author names as listed, in order, separated by "; "')
    A("- publication_year: 4-digit year from header, footer, copyright notice, or journal metadata")
    A('- journal_or_venue: Journal name, conference name, or preprint server (e.g. "arXiv")')
    A('- doi: DOI string if present, without the "https://doi.org/" prefix')
    A("- abstract: The full abstract text, copied verbatim from the paper")
    A("")

    # ── Abstract-only mode ──
    if screening_basis == "abstract_only":
        A("## SCREENING BASIS: TITLE AND ABSTRACT ONLY")
        A("The full text of this paper was NOT available. You are screening from bibliographic")
        A("metadata (title, abstract, venue) only. This changes how you must assign verdicts:")
        A('- Assign "YES" or "NO" ONLY when the abstract states the fact explicitly.')
        A('- Assign "UNCLEAR" whenever the abstract is silent on a criterion. Absence of')
        A("  evidence in an abstract is NOT evidence of absence — abstracts routinely omit")
        A("  metrics, model names, and study design details that would satisfy a criterion.")
        A('- confidence_level must not be "High" unless every criterion was explicitly stated.')
        A("- In additional_notes, state which criteria could not be assessed from the abstract.")
        A("- Do NOT infer, guess, or extrapolate from the title or venue.")
        A("The expected outcome of abstract-only screening is a higher MANUAL_REVIEW rate.")
        A("That is correct behavior, not a failure.")
        A("")

    # ── Slide-deck mode ──
    if screening_basis == "slides":
        A("## SCREENING BASIS: PRESENTATION SLIDES")
        A("The attached source is a presentation deck (conference talk, seminar, or")
        A("similar) about this study — it is NOT the paper. A deck is a compressed")
        A("summary written for an audience that can ask questions, so it routinely")
        A("omits sample sizes, metrics, model names, comparison conditions, and")
        A("design detail that a criterion depends on. Therefore:")
        A('- Assign "YES" or "NO" ONLY for what the slides state or clearly show.')
        A('- Assign "UNCLEAR" whenever the deck does not address a criterion. A deck')
        A("  not mentioning something is NOT evidence that the study lacked it.")
        A('- confidence_level must not be "High" unless every criterion was')
        A("  explicitly addressed in the slides.")
        A("- In additional_notes, state which criteria could not be assessed from the")
        A("  deck and note that the underlying paper should be obtained.")
        A("- Speaker notes and dense text-only slides may carry more detail than the")
        A("  titles; read everything present before deciding.")
        A("A higher MANUAL_REVIEW rate is the expected and correct outcome here.")
        A("")

    # ── Output ──
    A("## Required Output Format")
    A("Respond ONLY with valid JSON in this exact structure (no markdown fences, no preamble).")
    for c in crits:
        tf = c.get("tag_field")
        if tf and tf.get("required") and tf.get("name"):
            A(f"{tf['name']} is REQUIRED — always include at least one value.")
    A("overall_recommendation must follow the deterministic rules above exactly.")
    A("NOTE: overall_recommendation appears LAST so you complete all analysis before deciding.")
    A(example_output_json(p))

    return "\n".join(L)


def build_user_message(profile: dict, screening_basis: str = "full_text") -> str:
    name = profile.get("profile_name") or "the review"
    if screening_basis == "abstract_only":
        return (
            f"Evaluate this paper against the {name} inclusion criteria using ONLY the "
            "bibliographic metadata below. The full text is not available. Use UNCLEAR for "
            "any criterion the abstract does not explicitly address. "
            "Return ONLY valid JSON — no markdown, no preamble."
        )
    if screening_basis == "slides":
        return (
            f"Evaluate the study described in these presentation slides against the "
            f"{name} inclusion criteria. The slides are a summary, not the paper: use "
            "UNCLEAR for any criterion the deck does not explicitly address. "
            "Return ONLY valid JSON — no markdown, no preamble."
        )
    return (
        f"Evaluate this paper against the {name} inclusion criteria. "
        "Read the full paper carefully before deciding. "
        "Return ONLY valid JSON — no markdown, no preamble."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Deterministic decision check (belt-and-braces against model drift)
# ══════════════════════════════════════════════════════════════════════════════

def apply_decision_rules(profile: dict, result: dict) -> tuple[str, str | None]:
    """Recompute overall_recommendation locally from the verdicts.

    Returns (recommendation, correction_note). correction_note is non-None when
    the model's own recommendation disagreed with the profile's decision rules.
    """
    p = normalize_profile(profile)
    crits = {c["id"]: c for c in p["criteria"]}
    verdicts = {}
    for cid, c in crits.items():
        v = str((result.get("criteria", {}).get(cid) or {}).get("verdict", "")).strip().upper()
        verdicts[cid] = v if v in VERDICT_VALUES else "UNCLEAR"

    required = [cid for cid, c in crits.items() if c.get("required_for_include", True)]

    computed = "INCLUDE"
    if any(verdicts.get(cid) == "NO" for cid in required):
        computed = "EXCLUDE"
    elif any(verdicts.get(cid) == "UNCLEAR" for cid in required):
        computed = "MANUAL_REVIEW"

    # Year / language gates
    if computed != "EXCLUDE":
        min_year = p.get("min_publication_year")
        if min_year:
            raw_year = str(result.get("publication_year") or "").strip()
            m = re.search(r"(19|20)\d{2}", raw_year)
            if m and int(m.group(0)) < int(min_year):
                computed = "EXCLUDE"

    model_rec = str(result.get("overall_recommendation", "")).strip().upper()
    if model_rec in RECOMMENDATIONS and model_rec != computed:
        note = (f"Recommendation corrected from {model_rec} to {computed} by local decision "
                f"rules. Verdicts: "
                + ", ".join(f"{k}={v}" for k, v in verdicts.items()))
        return computed, note
    return computed, None


# ══════════════════════════════════════════════════════════════════════════════
# The compiler: raw text → draft profile
# ══════════════════════════════════════════════════════════════════════════════

COMPILER_SYSTEM_PROMPT = """You convert free-text systematic review inclusion/exclusion criteria
into a structured screening profile used by an automated paper screener.

Your single most important job is to make IMPLICIT BOUNDARIES EXPLICIT.

Free-text criteria almost always state what to include and leave what to exclude unstated.
An automated screener with no exclusion rules over-includes: it accepts adjacent topics,
papers that merely discuss the topic rather than study it, and papers where the intervention
of interest is incidental. You must anticipate those failure modes and write exclude_if rules
that block them, even when the source text never mentions them.

For every criterion you produce:
  - include_if: concrete, observable conditions for a YES verdict.
  - exclude_if: concrete conditions for a NO verdict. NEVER leave this empty. At minimum,
    include the mirror-image of each include_if condition, plus these classic near-misses
    where relevant: the topic is only discussed/reviewed rather than performed; the topic
    appears only in the literature review or motivation; the study population or setting is
    adjacent but out of scope; a related-but-different task is performed instead.
  - Prefer conditions a reader could verify from the paper's method section.

Rules:
  - Split compound criteria into separate criteria. One concept per criterion.
  - criterion ids must be lowercase snake_case.
  - Set required_for_include=true for criteria that must be YES to include a paper.
  - Add a tag_field only when the criterion asks the screener to categorize the paper
    (e.g. which sub-domain, which metrics). Use allowed_values when the source text names a
    closed list; otherwise leave allowed_values empty for free-form values.
  - Use categories only when a criterion has named sub-types each with their own YES/NO rules.
  - Infer min_publication_year and language_requirement if the source text states them,
    otherwise use null and "English" respectively.
  - Write confidence_rules tailored to this review's specific boundary risks.
  - In compiler_notes, list: (a) every exclusion rule you inferred that was NOT in the source
    text, and (b) every genuine ambiguity a human must resolve. Be specific and brief.

Respond with ONLY a valid JSON object matching this schema. No markdown fences, no preamble.

{
  "schema_version": 1,
  "profile_id": "lowercase-hyphenated-id",
  "profile_name": "Human readable name",
  "profile_version": "1.0",
  "description": "One sentence describing the review.",
  "language_requirement": "English",
  "min_publication_year": null,
  "language_rule": "<copy the CRITICAL LANGUAGE RULE text given to you verbatim>",
  "global_exclusions": [],
  "confidence_rules": { "high": "...", "medium": "...", "low": "..." },
  "criteria": [
    {
      "id": "snake_case_id",
      "label": "Human Readable Label",
      "definition": "What this criterion requires, in one or two sentences.",
      "include_if": ["...", "..."],
      "exclude_if": ["...", "..."],
      "notes": [],
      "required_for_include": true,
      "tag_field": {
        "name": "snake_case_name",
        "label": "Human Readable",
        "required": true,
        "allowed_values": ["Value A", "Value B", "Unknown"],
        "fallback_value": "Unknown",
        "fallback_rule": "Use \\"Unknown\\" only when certain no named value applies."
      },
      "categories": [
        { "name": "Value A", "yes_if": ["..."], "no_if": ["..."] }
      ]
    }
  ],
  "compiler_notes": ["..."]
}

Omit tag_field and categories entirely (or set null) when they do not apply."""


def build_compile_user_message(raw_text: str) -> str:
    return (
        "Convert the following inclusion/exclusion criteria into a structured screening "
        "profile. Make all implicit boundaries explicit.\n\n"
        "Use exactly this text for the language_rule field:\n"
        f"---\n{DEFAULT_LANGUAGE_RULE}\n---\n\n"
        "SOURCE CRITERIA:\n"
        "---\n"
        f"{raw_text.strip()}\n"
        "---"
    )


def clean_json_response(raw: str) -> dict:
    """Tolerant JSON extraction — fences, trailing commas, trailing prose."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    start = raw.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found in response", raw, 0)
    obj, _ = json.JSONDecoder().raw_decode(raw, start)
    return obj


def compile_criteria(raw_text: str, provider_cfg: "mp.ProviderConfig",
                     progress=None) -> tuple[dict, list[str]]:
    """Turn raw criteria text into a draft profile. Returns (profile, compiler_notes).

    Works with any configured provider — the compiler prompt is plain text,
    same as the screening prompt. The returned profile is a DRAFT. It must be
    reviewed by a human before use.
    """
    import model_providers as mp_  # local import avoids a module-load-order dependency

    provider = mp_.build_provider(provider_cfg)
    if progress:
        progress(f"Compiling criteria with {provider.display_name}…")

    text = provider.complete(
        COMPILER_SYSTEM_PROMPT, build_compile_user_message(raw_text), max_tokens=8000)
    draft = clean_json_response(text)

    notes = draft.pop("compiler_notes", []) or []
    if isinstance(notes, str):
        notes = [notes]

    draft.setdefault("schema_version", PROFILE_SCHEMA_VERSION)
    draft.setdefault("profile_version", "1.0")
    if not draft.get("profile_id"):
        draft["profile_id"] = slugify(draft.get("profile_name", "imported-profile"))
    draft["profile_id"] = slugify(draft["profile_id"])
    draft.setdefault("language_rule", DEFAULT_LANGUAGE_RULE)
    draft["created_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    draft["locked"] = False
    draft["builtin"] = False
    draft["source"] = "compiled_from_text"

    return normalize_profile(draft), [str(n) for n in notes]


# ══════════════════════════════════════════════════════════════════════════════
# Raw criteria file readers
# ══════════════════════════════════════════════════════════════════════════════

def read_criteria_file(path: str | Path) -> tuple[str, dict | None]:
    """Read a criteria source file.

    Returns (raw_text, profile_or_None). If the file is already a valid profile
    JSON, profile is returned and raw_text is empty.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("criteria"):
            return "", data
        # A JSON file that isn't a profile — treat its text as raw criteria
        return json.dumps(data, indent=2, ensure_ascii=False), None

    if suffix == ".docx":
        return _read_docx_text(path), None

    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return path.read_text(encoding=enc), None
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace"), None


def _read_docx_text(path: Path) -> str:
    """Extract plain text from a .docx without external dependencies."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
