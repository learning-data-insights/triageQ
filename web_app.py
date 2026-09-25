"""
triageQ — hosted web front end (reference deployment).

    streamlit run web_app.py

A browser front end over the same screening engine the desktop app (app.py)
uses. What changes on the web, and why:

  • Every visitor gets a private, temporary WORKSPACE (a random id carried in
    the page URL). Nothing is shared between visitors. Workspaces are deleted
    after TRIAGEQ_WORKSPACE_TTL_HOURS of inactivity, or at once with
    "Clear my session". Results leave the server by download, not by staying.
  • API keys: visitors paste their own (held in this session's memory only,
    never written to disk — same rule as the desktop), or use the
    organisation's hosted "demo" key, which is capped per visitor and per day.
  • Local-folder scanning is replaced by uploads: a batch CSV's file_path
    column names an uploaded file.
  • Every outbound URL is checked so visitors can't aim the server at private
    addresses (net_guard.py).

Configuration is all environment variables — see .env.example and web_backend.Config.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import streamlit as st

import criteria_profiles as cp
import local_library as lib
import model_providers as mp
import net_guard
import pdf_resolver as pr
import screening
import web_backend as wb

APP_VERSION = "1.2.1-web"
HERE = Path(__file__).parent
EXAMPLES_DIR = HERE / "criteria-profiles"
ASSETS = HERE / "assets"

st.set_page_config(page_title="triageQ", page_icon=str(ASSETS / "icon_256.png"),
                   layout="wide")


# ══════════════════════════════════════════════════════════════════════════════
# Process-wide singletons
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_resource
def _services():
    cfg = wb.Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    pr.URL_GUARD = net_guard.check_url
    jobs = wb.JobManager(cfg)
    quota = wb.Quota(cfg)

    import threading
    import time

    def janitor():
        while True:
            try:
                wb.purge_expired(cfg, busy_ids=jobs.busy_workspaces())
            except Exception:
                pass
            time.sleep(1800)

    threading.Thread(target=janitor, daemon=True, name="triageq-janitor").start()
    return cfg, jobs, quota


CFG, JOBS, QUOTA = _services()
ss = st.session_state


def client_ip() -> str:
    """The visitor's IP. Behind the bundled Caddy proxy that's X-Forwarded-For
    (Caddy overwrites any value the client sent); run directly, it's the
    socket address."""
    try:
        fwd = st.context.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return getattr(st.context, "ip_address", None) or "unknown"
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════════════════════════════
# Workspace
# ══════════════════════════════════════════════════════════════════════════════

def _open_workspace() -> wb.Workspace:
    ws_id = st.query_params.get("ws", "")
    expired = False
    if ws_id and wb.WORKSPACE_ID_RE.match(ws_id):
        ws = wb.Workspace(CFG, ws_id)
        if ws.exists():
            ws.open()
            return ws
        expired = True
    ws = wb.Workspace(CFG, wb.new_workspace_id())
    ws.open()
    st.query_params["ws"] = ws.id
    if expired:
        ss["flash"] = ("info", "That workspace has expired or was cleared, so a new "
                               "empty one was started.")
    return ws


WS = _open_workspace()


def _reset_session_state():
    for k in list(ss.keys()):
        del ss[k]


def flash(kind: str, msg: str):
    ss["flash"] = (kind, msg)


# ══════════════════════════════════════════════════════════════════════════════
# Model provider (sidebar)
# ══════════════════════════════════════════════════════════════════════════════

KEY_OWN = "Use my own API key"
KEY_DEMO = "Use the demo key (limited)"


def _sidebar_provider():
    st.sidebar.subheader("Model")
    options = ([KEY_DEMO] if CFG.hosted_keys else []) + [KEY_OWN]
    src = st.sidebar.radio("API key", options, key="key_source",
                           label_visibility="collapsed")

    if src == KEY_DEMO:
        provs = list(CFG.hosted_keys)
        prov = st.sidebar.selectbox("Provider", provs, key="demo_provider",
                                    format_func=lambda p: mp.PROVIDER_LABELS[p])
        left = QUOTA.remaining(WS.id, client_ip())
        st.sidebar.caption(
            f"Model: **{CFG.hosted_models[prov]}**  \n"
            f"Demo calls left today: **{left}** of {CFG.hosted_daily_per_visitor}")
        return

    provs = ["anthropic", "openai"] + (["openai_compatible"] if CFG.allow_custom_endpoint else [])
    prov = st.sidebar.selectbox("Provider", provs, key="own_provider",
                                format_func=lambda p: mp.PROVIDER_LABELS[p])
    if prov == "openai_compatible":
        st.sidebar.text_input("Base URL", key="own_base_url",
                              placeholder="https://openrouter.ai/api/v1")
    st.sidebar.text_input("API key", type="password", key=f"own_key_{prov}")
    default_model = {"anthropic": "claude-sonnet-5"}.get(prov, "")
    if f"own_model_{prov}" not in ss:
        ss[f"own_model_{prov}"] = default_model
    st.sidebar.text_input("Model", key=f"own_model_{prov}")
    st.sidebar.caption(
        "Your key is kept only in this browser session's memory on the server and "
        "is never saved. Refreshing the page forgets it.")
    if not mp.supports_pdf_vision(prov):
        st.sidebar.caption("This provider has no PDF vision — PDF text is extracted "
                           "and sent instead (tables and figures are lost).")


def provider_setup() -> tuple[mp.ProviderConfig | None, object, str]:
    """(config, quota gate or None, error message). config is None on error."""
    if ss.get("key_source") == KEY_DEMO and CFG.hosted_keys:
        prov = ss.get("demo_provider") or next(iter(CFG.hosted_keys))
        cfg = mp.ProviderConfig(provider=prov, api_key=CFG.hosted_keys[prov],
                                model=CFG.hosted_models[prov])
        ws_id, ip = WS.id, client_ip()
        return cfg, (lambda: QUOTA.reserve(ws_id, ip)), ""

    prov = ss.get("own_provider", "anthropic")
    key = (ss.get(f"own_key_{prov}") or "").strip()
    model = (ss.get(f"own_model_{prov}") or "").strip()
    base = (ss.get("own_base_url") or "").strip() if prov == "openai_compatible" else ""
    if prov != "openai_compatible" and not key:
        return None, None, "Enter an API key in the sidebar (or use the demo key)."
    if not model:
        return None, None, "Enter a model name in the sidebar."
    if prov == "openai_compatible":
        if not base:
            return None, None, "Enter the endpoint's base URL in the sidebar."
        try:
            net_guard.check_url(base)
        except net_guard.BlockedURL as exc:
            return None, None, f"That base URL can't be used from this server: {exc}"
    return mp.ProviderConfig(provider=prov, api_key=key, model=model, base_url=base), None, ""


# ══════════════════════════════════════════════════════════════════════════════
# Workspace controls (sidebar)
# ══════════════════════════════════════════════════════════════════════════════

def _sidebar_workspace():
    st.sidebar.divider()
    st.sidebar.subheader("Your workspace")
    used = WS.size_bytes() / (1024 * 1024)
    exp = WS.expires_at().strftime("%b %d, %H:%M")
    st.sidebar.caption(
        f"Storage: {used:.1f} of {CFG.max_workspace_mb} MB  \n"
        f"Deleted after {CFG.workspace_ttl_hours} h without use (next: {exp} server time).  \n"
        "Bookmark this page's URL to come back to it. **Anyone with the link can "
        "open this workspace.**")
    st.sidebar.download_button(
        "Download all results (.zip)", WS.export_zip(),
        file_name=f"triageq-results-{datetime.date.today()}.zip",
        mime="application/zip", use_container_width=True)

    if not ss.get("confirm_clear"):
        if st.sidebar.button("Clear my session", use_container_width=True):
            ss["confirm_clear"] = True
            st.rerun()
    else:
        st.sidebar.warning("Delete every profile, upload and result in this workspace? "
                           "This can't be undone.")
        c1, c2 = st.sidebar.columns(2)
        if c1.button("Delete", type="primary", use_container_width=True):
            JOBS.forget(WS.id)
            WS.delete()
            _reset_session_state()
            st.query_params.clear()
            flash("success", "Session cleared. This is a fresh, empty workspace.")
            st.rerun()
        if c2.button("Cancel", use_container_width=True):
            ss["confirm_clear"] = False
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Result rendering
# ══════════════════════════════════════════════════════════════════════════════

REC_STYLE = {"INCLUDE": ("✅", "green"), "EXCLUDE": ("⛔", "red"),
             "MANUAL_REVIEW": ("🟡", "orange")}
VERDICT_COLOR = {"YES": "green", "NO": "red", "UNCLEAR": "orange"}


def render_result(result: dict, profile: dict | None, paper_id: str):
    rec = result.get("overall_recommendation", "?")
    icon, color = REC_STYLE.get(rec, ("", "gray"))
    st.markdown(f"### {icon} :{color}[{rec}] &nbsp; · &nbsp; {paper_id}")
    meta = [f"**Confidence:** {result.get('confidence_level', '?')}"]
    if profile:
        meta.append(f"**Criteria:** {profile['profile_name']} v{profile['profile_version']}")
    if result.get("_provider_model"):
        meta.append(f"**Model:** {result['_provider_model']}")
    st.markdown(" &nbsp; | &nbsp; ".join(meta))
    if result.get("_screening_basis") == "abstract_only":
        st.warning("Screened from title and abstract only — full text was unavailable.")
    if result.get("_file_mismatch"):
        st.error(f"Forced to MANUAL_REVIEW: {result['_file_mismatch']}")

    bib = [(k, result.get(key)) for k, key in [
        ("Title", "title"), ("Authors", "authors"), ("Year", "publication_year"),
        ("Venue", "journal_or_venue"), ("DOI", "doi")] if result.get(key)]
    if bib:
        st.markdown("  \n".join(f"**{k}:** {v}" for k, v in bib))

    crit = result.get("criteria", {}) or {}
    criteria_defs = (profile or {}).get("criteria") or [
        {"id": cid, "label": cid} for cid in crit]
    for idx, c in enumerate(criteria_defs, start=1):
        block = crit.get(c["id"], {}) or {}
        v = block.get("verdict", "?")
        opt = "" if c.get("required_for_include", True) else " (optional)"
        with st.container(border=True):
            st.markdown(f"**{idx}. {c.get('label', c['id'])}**{opt} — "
                        f":{VERDICT_COLOR.get(v, 'gray')}[**{v}**]")
            tf = c.get("tag_field")
            if tf and tf.get("name") and block.get(tf["name"]):
                vals = block[tf["name"]]
                shown = ", ".join(vals) if isinstance(vals, list) else str(vals)
                st.markdown(f"*{tf.get('label') or tf['name']}:* {shown}")
            if block.get("reasoning"):
                st.markdown(block["reasoning"])
            if block.get("text_examples"):
                st.caption(f"Evidence: {block['text_examples']}")
            if block.get("location"):
                st.caption(f"Location: {block['location']}")

    if result.get("key_decision_factors"):
        st.markdown(f"**Key decision factors.** {result['key_decision_factors']}")
    if result.get("confidence_rationale"):
        st.markdown(f"**Confidence rationale.** {result['confidence_rationale']}")
    if result.get("additional_notes"):
        st.markdown(f"**Notes.** {result['additional_notes']}")
    if result.get("_local_correction"):
        st.warning(f"Decision-rule correction: {result['_local_correction']}")


# ══════════════════════════════════════════════════════════════════════════════
# Tab: Criteria
# ══════════════════════════════════════════════════════════════════════════════

def _open_editor(profile: dict, is_new: bool, notes=None, source_text: str = ""):
    ss.pop("editor_text", None)     # else the text box keeps the previous profile
    ss["editor"] = {"json": json.dumps(profile, indent=2, ensure_ascii=False),
                    "is_new": is_new, "notes": notes or [], "source_text": source_text}


def _save_from_editor(ed: dict) -> str | None:
    """Validate and save the editor's JSON. Returns an error message or None.
    Mirrors the desktop editor: edits to a version that has already screened
    papers fork a new version instead of rewriting history."""
    try:
        d = json.loads(ss["editor_text"])
    except json.JSONDecodeError as exc:
        return f"Not valid JSON: {exc}"
    ok, errs = cp.validate_profile(d)
    if not ok:
        return "Profile has problems:\n\n" + "\n".join(f"- {e}" for e in errs)
    existing = cp.load_profile(WS.root, d["profile_id"])
    if not ed["is_new"]:
        n = WS.profile_in_use(d["profile_id"], d.get("profile_version"))
        if n and existing and existing != d:
            d["profile_version"] = cp.bump_version(d.get("profile_version", "1.0"))
            flash("info", f"{n} paper(s) were screened with the previous version, so this "
                          f"was saved as v{d['profile_version']}. Their records keep the "
                          "old version stamp.")
    d["builtin"] = False
    if ed.get("source_text"):
        d["source_text"] = ed["source_text"][:20000]
    try:
        saved = WS.save_profile(d)
    except Exception as exc:
        return f"Save failed: {exc}"
    ss.pop("editor", None)
    ss.setdefault("flash", ("success", f"“{saved['profile_name']}” "
                                       f"v{saved['profile_version']} is now active."))
    return None


def _profile_editor():
    ed = ss["editor"]
    st.subheader("Review profile")
    if ed["notes"]:
        st.warning(
            "**Compiler notes — read these.** These are rules the compiler added that "
            "were not in your text. They will shape every screening decision.\n\n"
            + "\n".join(f"- {n}" for n in ed["notes"]))
    if "editor_text" not in ss:
        ss["editor_text"] = ed["json"]
    st.text_area("Profile JSON", height=420, key="editor_text")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Validate"):
        try:
            ok, errs = cp.validate_profile(json.loads(ss["editor_text"]))
            (st.success("Profile is valid.") if ok
             else st.error("\n".join(f"- {e}" for e in errs)))
        except json.JSONDecodeError as exc:
            st.error(f"Not valid JSON: {exc}")
    if c2.button("Preview prompt"):
        try:
            prof = cp.normalize_profile(json.loads(ss["editor_text"]))
            ss["preview"] = cp.build_system_prompt(prof)
        except Exception as exc:
            st.error(f"Can't render: {exc}")
    if c3.button("Save and activate", type="primary"):
        err = _save_from_editor(ed)
        if err:
            st.error(err)
        else:
            st.rerun()
    if c4.button("Discard"):
        ss.pop("editor", None)
        st.rerun()
    if ss.get("preview"):
        with st.expander("System prompt this profile produces", expanded=True):
            st.code(ss.pop("preview"), language=None)


def tab_criteria():
    if ss.get("editor"):
        _profile_editor()
        return

    profiles = WS.profiles()
    active = WS.active_profile()
    if not profiles:
        st.info("triageQ ships with **no criteria of its own**. Start from the example "
                "profile below, import one, or write your criteria in plain text and let "
                "the model compile them.")
    else:
        ids = [p["profile_id"] for p in profiles]
        names = {p["profile_id"]: f"{p['profile_name']}  (v{p['profile_version']})"
                 for p in profiles}
        cur = active["profile_id"] if active and active["profile_id"] in ids else ids[0]
        pick = st.selectbox("Active profile", ids, index=ids.index(cur),
                            format_func=names.get)
        if not active or pick != active["profile_id"]:
            WS.update_settings(active_profile_id=pick)
            active = WS.active_profile()
        if active:
            st.caption(active.get("description") or "")
            c1, c2, c3, c4 = st.columns(4)
            if c1.button("Edit"):
                _open_editor(active, is_new=False)
                st.rerun()
            c2.download_button("Export JSON", json.dumps(active, indent=2, ensure_ascii=False),
                               file_name=f"{active['profile_id']}.json",
                               mime="application/json")
            if c3.button("Preview prompt"):
                ss["preview"] = cp.build_system_prompt(active)
            if c4.button("Delete"):
                ss["confirm_delete_profile"] = active["profile_id"]
            if ss.get("confirm_delete_profile") == active["profile_id"]:
                n = WS.profile_in_use(active["profile_id"])
                msg = f"Delete “{active['profile_name']}”?"
                if n:
                    msg += (f" {n} result(s) were screened with it; they keep their stamp "
                            "but the criteria behind them won't be viewable here any more.")
                st.warning(msg)
                d1, d2 = st.columns(2)
                if d1.button("Yes, delete", type="primary"):
                    cp.delete_profile(WS.root, active["profile_id"])
                    WS.update_settings(active_profile_id="")
                    ss.pop("confirm_delete_profile", None)
                    st.rerun()
                if d2.button("Keep it"):
                    ss.pop("confirm_delete_profile", None)
                    st.rerun()
            if ss.get("preview"):
                with st.expander("System prompt", expanded=True):
                    st.code(ss.pop("preview"), language=None)

    st.divider()
    st.subheader("Add a profile")

    examples = sorted(EXAMPLES_DIR.glob("*.json")) if EXAMPLES_DIR.is_dir() else []
    if examples:
        with st.container(border=True):
            st.markdown("**Start from an example**")
            ex = st.selectbox("Example profile", examples, format_func=lambda p: p.stem,
                              label_visibility="collapsed")
            if st.button("Load example"):
                try:
                    prof = json.loads(ex.read_text(encoding="utf-8"))
                    saved = WS.save_profile(prof)
                    flash("success", f"Loaded “{saved['profile_name']}” and made it active.")
                except Exception as exc:
                    flash("error", f"Could not load example: {exc}")
                st.rerun()

    with st.container(border=True):
        st.markdown("**Compile from your own criteria text**")
        st.caption("Paste a protocol excerpt, PICO statement, or plain prose — or load a "
                   ".txt / .md / .docx file. The model turns it into a structured profile, "
                   "which you then review before it can screen anything.")
        up = st.file_uploader("Criteria file", type=["txt", "md", "docx"],
                              key="criteria_file", label_visibility="collapsed")
        if up is not None and ss.get("criteria_file_seen") != up.file_id:
            ss["criteria_file_seen"] = up.file_id
            tmp = WS.root / f"_criteria_upload{Path(up.name).suffix.lower()}"
            try:
                tmp.write_bytes(up.getvalue())
                ss["criteria_text"], _ = cp.read_criteria_file(tmp)
            except Exception as exc:
                st.error(f"Could not read that file: {exc}")
            finally:
                tmp.unlink(missing_ok=True)
        st.text_area("Criteria text", key="criteria_text", height=180,
                     label_visibility="collapsed",
                     placeholder="Include studies that… Exclude studies that…")
        if st.button("Compile criteria", type="primary"):
            raw = (ss.get("criteria_text") or "").strip()
            pcfg, gate, err = provider_setup()
            if not raw:
                st.error("Paste or load some criteria text first.")
            elif err:
                st.error(err)
            else:
                reason = gate() if gate else None
                if reason:
                    st.error(reason)
                else:
                    with st.spinner("Compiling criteria — usually 20–60 seconds…"):
                        try:
                            prof, notes = cp.compile_criteria(raw, pcfg)
                            _open_editor(prof, is_new=True, notes=notes, source_text=raw)
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Compile failed: {exc}")

    with st.container(border=True):
        st.markdown("**Import a profile JSON**")
        up = st.file_uploader("Profile JSON", type=["json"], key="profile_json",
                              label_visibility="collapsed")
        if up is not None and st.button("Open in editor"):
            try:
                data = json.loads(up.getvalue().decode("utf-8-sig"))
                if not (isinstance(data, dict) and data.get("criteria")):
                    raise ValueError("this JSON has no 'criteria' — it isn't a profile")
                _open_editor(data, is_new=True)
                st.rerun()
            except Exception as exc:
                st.error(f"Can't import: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Tab: Screen one paper
# ══════════════════════════════════════════════════════════════════════════════

def _need_profile() -> dict | None:
    p = WS.active_profile()
    if not p:
        st.info("Set up a criteria profile in the **Criteria** tab first.")
    return p


def tab_single():
    profile = _need_profile()
    if not profile:
        return
    st.caption(f"Screening against **{profile['profile_name']}** v{profile['profile_version']}")

    st.text_input("Paper ID", key="single_pid",
                  help="Your identifier for this paper; results are saved under it.")
    how = st.radio("Source", ["Find by DOI or link", "Upload a file"], horizontal=True)
    st.checkbox("If no PDF can be found, screen from the title and abstract instead",
                key="abstract_fallback")

    if how == "Find by DOI or link":
        c1, c2 = st.columns([4, 1])
        src = c1.text_input("DOI, publisher link, arXiv ID, or PDF URL",
                            placeholder="10.7717/peerj.4375", label_visibility="collapsed")
        if c2.button("Find PDF", use_container_width=True) and src.strip():
            with st.spinner("Looking for an open-access copy…"):
                trail: list[str] = []
                res = pr.resolve_pdf(src.strip(), CFG.resolver_config(), log=trail.append)
                single = None
                if res.ok:
                    single = dict(payload=res.pdf_bytes, basis="full_text", kind="pdf",
                                  pdf_source=res.resolved_via, pdf_url=res.pdf_url,
                                  url=src.strip(),
                                  label=f"{res.resolved_via} — {len(res.pdf_bytes)//1024} KB")
                elif ss.get("abstract_fallback"):
                    meta = pr.fetch_metadata_only(src.strip(), CFG.resolver_config(),
                                                  log=trail.append)
                    if meta.get("abstract"):
                        single = dict(payload=pr.format_metadata_as_text(meta),
                                      basis="abstract_only", kind="pdf",
                                      pdf_source="abstract only", pdf_url="",
                                      url=src.strip(), label="Abstract only — no full text")
                ss["single"] = single
                ss["single_trail"] = trail
                if not single:
                    st.error(f"No open-access PDF found ({res.error}). Upload the file instead.")
    else:
        up = st.file_uploader("PDF or PowerPoint", type=["pdf", "pptx"], key="single_upload")
        if up is not None and ss.get("single_upload_seen") != up.file_id:
            try:
                path = WS.save_upload(up.name, up.getvalue())
                loaded = lib.load_for_screening(str(path), CFG.library_config())
                if loaded["error"]:
                    raise wb.WorkspaceError(loaded["error"])
                ss["single"] = dict(payload=loaded["payload"], basis=loaded["basis"],
                                    kind=loaded.get("source_kind", "pdf"),
                                    pdf_source="upload", pdf_url="", url="",
                                    file_path=path.name, label=path.name)
                ss["single_upload_seen"] = up.file_id
            except wb.WorkspaceError as exc:
                st.error(str(exc))

    single = ss.get("single")
    if single:
        st.success(f"Ready: {single['label']}")
    if ss.get("single_trail"):
        with st.expander("Retrieval trail"):
            st.code("\n".join(ss["single_trail"]), language=None)

    if st.button("Analyze paper", type="primary", disabled=not single):
        pid = (ss.get("single_pid") or "").strip()
        pcfg, gate, err = provider_setup()
        if not pid:
            st.error("Enter a Paper ID.")
        elif err:
            st.error(err)
        else:
            with JOBS.slot(timeout=45) as ok:
                if not ok:
                    st.error("The server is busy with other screenings. Try again in a minute.")
                else:
                    reason = gate() if gate else None
                    if reason:
                        st.error(reason)
                    else:
                        with st.spinner("Screening — usually 20–90 seconds…"):
                            try:
                                result = screening.analyze_paper(
                                    single["payload"], profile, pcfg,
                                    screening_basis=single["basis"])
                                entry = screening.build_repo_entry(
                                    profile, pid, result, basis=single["basis"],
                                    url=single["url"], file_path=single.get("file_path", ""),
                                    pdf_source=single["pdf_source"],
                                    pdf_url=single["pdf_url"], source_kind=single["kind"])
                                WS.repo.save(entry)
                                ss["single_result"] = (pid, result)
                            except Exception as exc:
                                st.error(f"Analysis failed: {exc}")

    if ss.get("single_result"):
        st.divider()
        pid, result = ss["single_result"]
        render_result(result, profile, pid)


# ══════════════════════════════════════════════════════════════════════════════
# Tab: Batch
# ══════════════════════════════════════════════════════════════════════════════

@st.fragment(run_every=2)
def _batch_progress_live():
    _batch_progress()
    job = JOBS.get(WS.id)
    if not job or not job.active:
        st.rerun()          # full rerun, so the static (non-polling) view takes over


def _batch_progress():
    job = JOBS.get(WS.id)
    if not job:
        return
    c = job.counts
    st.progress(job.done / job.total if job.total else 0.0,
                text=(job.current if job.status == "running"
                      else {"queued": "Queued — waiting for a free slot…",
                            "done": "Finished.", "stopped": "Stopped.",
                            "failed": "Failed."}.get(job.status, job.status)))
    m = st.columns(5)
    m[0].metric("Done", f"{job.done}/{job.total}")
    m[1].metric("Include", c["INCLUDE"])
    m[2].metric("Exclude", c["EXCLUDE"])
    m[3].metric("Manual review", c["MANUAL_REVIEW"])
    m[4].metric("Errors", c["error"])
    if job.active and st.button("⏹ Stop after the current paper"):
        JOBS.stop(WS.id)
    log = "".join(t for t, _ in job.log[-400:])
    st.code(log or "…", language=None)


def tab_batch():
    profile = _need_profile()
    if not profile:
        return
    st.caption(f"Screening against **{profile['profile_name']}** v{profile['profile_version']}")

    job = JOBS.get(WS.id)
    if job and job.active:
        st.info("A batch is running. You can leave this page — it keeps going on the "
                "server, and results appear in the **Results** tab as they finish.")
        _batch_progress_live()
        return

    st.markdown(
        f"1. Download the template and fill in one paper per row (up to "
        f"**{CFG.max_batch_rows}** rows).  \n"
        "2. Each row needs a `url` (DOI, link, arXiv ID) and/or a `file_path`. On this "
        "server, `file_path` is the **name of a file you upload below**.  \n"
        "3. Run. The `url` is always tried first; an uploaded file is the fallback.")
    st.download_button("Download CSV template", wb.batch_template_csv(),
                       file_name="batch_template.csv", mime="text/csv")

    up = st.file_uploader("Batch CSV", type=["csv"], key="batch_csv")
    rows = []
    if up is not None:
        rows = [r for r in wb.read_batch_csv(up.getvalue())
                if any((v or "").strip() for v in r.values())]
        if len(rows) > CFG.max_batch_rows:
            st.error(f"This CSV has {len(rows)} rows; the limit here is "
                     f"{CFG.max_batch_rows}. Split it into smaller batches.")
            rows = []
        elif rows:
            st.caption(f"{len(rows)} paper(s) loaded.")

    files = st.file_uploader("Optional: PDFs / .pptx referenced in the file_path column",
                             type=["pdf", "pptx"], accept_multiple_files=True,
                             key="batch_files")
    seen = ss.setdefault("batch_files_seen", set())
    for f in files or []:
        if f.file_id in seen:
            continue
        try:
            WS.save_upload(f.name, f.getvalue())
            seen.add(f.file_id)
        except wb.WorkspaceError as exc:
            st.error(str(exc))

    uploads = WS.uploads()
    if uploads:
        with st.expander(f"Uploaded files in this workspace ({len(uploads)})"):
            st.write(", ".join(p.name for p in uploads))
            if st.button("Remove all uploaded files"):
                for p in uploads:
                    p.unlink(missing_ok=True)
                ss["batch_files_seen"] = set()
                st.rerun()

    missing = [r.get("file_path") for r in rows
               if (r.get("file_path") or "").strip() and not WS.upload_path(r["file_path"])]
    if missing:
        st.warning("Not uploaded yet (these rows will rely on their url): "
                   + ", ".join(sorted(set(missing))[:10]))
    no_source = [r.get("paper_id") for r in rows
                 if not (r.get("url") or "").strip() and not (r.get("file_path") or "").strip()]
    if no_source:
        st.warning(f"{len(no_source)} row(s) have neither url nor file_path and will be skipped.")

    allow_abstract = st.checkbox("Screen from title + abstract when no PDF is found",
                                 key="batch_abstract")

    pcfg, gate, err = provider_setup()
    if gate and rows:
        left = QUOTA.remaining(WS.id, client_ip())
        if left < len(rows):
            st.warning(f"You have {left} demo-key call(s) left today, so only the first "
                       f"{left} paper(s) will be screened. Use your own key for the rest.")

    if st.button("Run batch", type="primary", disabled=not rows):
        if err:
            st.error(err)
        else:
            try:
                JOBS.start_batch(WS, rows, profile, pcfg, allow_abstract, gate=gate)
                st.rerun()
            except wb.WorkspaceError as exc:
                st.error(str(exc))

    if job:   # the last finished batch, still worth seeing
        st.divider()
        st.caption("Last batch")
        _batch_progress()


# ══════════════════════════════════════════════════════════════════════════════
# Tab: Results
# ══════════════════════════════════════════════════════════════════════════════

def tab_results():
    repo = WS.repo.load()
    if not repo:
        st.info("No results yet. Screened papers appear here.")
        return
    counts = {k: sum(1 for r in repo if r.get("recommendation") == k)
              for k in ("INCLUDE", "EXCLUDE", "MANUAL_REVIEW")}
    m = st.columns(4)
    m[0].metric("Papers", len(repo))
    m[1].metric("Include", counts["INCLUDE"])
    m[2].metric("Exclude", counts["EXCLUDE"])
    m[3].metric("Manual review", counts["MANUAL_REVIEW"])

    cols = ["paper_id", "recommendation", "confidence", "title", "publication_year",
            "screening_basis", "profile_id", "profile_version", "analyzed_at"]
    st.dataframe([{c: r.get(c, "") for c in cols} for r in repo],
                 use_container_width=True, hide_index=True)

    d = st.columns(3)
    d[0].download_button("All results (CSV)", WS.repo.csv_path.read_bytes(),
                         file_name="paper_repository.csv", mime="text/csv",
                         use_container_width=True)
    d[1].download_button("Full detail (JSON)", WS.repo.json_path.read_bytes(),
                         file_name="paper_repository.json", mime="application/json",
                         use_container_width=True)
    active = WS.active_profile()
    if active and WS.repo.profile_csv_path(active["profile_id"]).exists():
        p = WS.repo.profile_csv_path(active["profile_id"])
        d[2].download_button(f"Per-criterion CSV ({active['profile_id']})", p.read_bytes(),
                             file_name=p.name, mime="text/csv", use_container_width=True)

    st.divider()
    pids = [r["paper_id"] for r in repo]
    pick = st.selectbox("View a paper", pids)
    entry = next(r for r in repo if r["paper_id"] == pick)
    prof = cp.load_profile(WS.root, entry.get("profile_id", ""))
    render_result(entry.get("_full_result") or {}, prof, pick)


# ══════════════════════════════════════════════════════════════════════════════
# Tab: About
# ══════════════════════════════════════════════════════════════════════════════

def tab_about():
    st.markdown(f"""
**triageQ {APP_VERSION}** — an open, human-in-the-loop tool for sorting large volumes of
content against custom criteria. This hosted copy is a **reference deployment**.

It returns INCLUDE / EXCLUDE / MANUAL_REVIEW recommendations with per-criterion verdicts,
quoted evidence, and a confidence level. It is a screening assistant for human reviewers,
not a replacement for them.

**Your data on this server**
- Everything you add lives in a private workspace tied to this page's link. It is deleted
  after {CFG.workspace_ttl_hours} hours without use, or immediately with **Clear my session**.
- Use **Download all results** to keep your work.
- Papers you screen are sent to the model provider you choose. Don't upload anything
  confidential.
- Your own API key is held in memory for this session only and is never written to disk.

**Limits here:** {CFG.max_batch_rows} papers per batch · {CFG.max_upload_mb} MB per file ·
{CFG.max_workspace_mb} MB per workspace.

*This application was developed with AI assistance (Claude Sonnet 5). It is experimental
software intended for initial triage only; recommendations should be verified by a human
reviewer. Licensed under Apache 2.0.* AI-assisted application built by Learning Data Insights.
""")


# ══════════════════════════════════════════════════════════════════════════════
# Page
# ══════════════════════════════════════════════════════════════════════════════

st.logo(str(ASSETS / "logo.png"), icon_image=str(ASSETS / "icon_256.png"))
_sidebar_provider()
_sidebar_workspace()

active = WS.active_profile()
st.title("triageQ")
st.caption("Screen papers against your own criteria — every recommendation is for a "
           "human to review. "
           + (f"Active criteria: **{active['profile_name']}** v{active['profile_version']}"
              if active else "No criteria profile yet."))

if ss.get("flash"):
    kind, msg = ss.pop("flash")
    getattr(st, kind)(msg)

t_crit, t_single, t_batch, t_results, t_about = st.tabs(
    ["Criteria", "Screen one paper", "Batch", "Results", "About"])
with t_crit:
    tab_criteria()
with t_single:
    tab_single()
with t_batch:
    tab_batch()
with t_results:
    tab_results()
with t_about:
    tab_about()
