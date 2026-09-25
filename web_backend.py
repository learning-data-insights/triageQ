"""
Server-side plumbing for the hosted (web) build of triageQ — everything the
web front end needs that the desktop app never did, because the desktop app
has exactly one user and one disk:

  Config       every limit and secret, read from environment variables
  Workspace    one visitor's private folder: repository, profiles, uploads
  Quota        daily call caps for the organisation's hosted ("demo") keys
  JobManager   background batch runs, with a server-wide concurrency cap
  janitor      deletes idle workspaces after a TTL

No Streamlit imports here, so all of it can be exercised from a plain Python
shell. The screening itself is screening.py — the same code the desktop uses.

Run `python web_backend.py cleanup` to purge expired workspaces on demand
(the web app also does this on its own every 30 minutes).
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import criteria_profiles as cp
import local_library as lib
import model_providers as mp
import pdf_resolver as pr
import screening


# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


# Used when the operator enables a hosted key without naming a model. OpenAI has
# no default on purpose, matching the desktop app: model availability changes
# independently of this tool, so the operator must choose one.
DEFAULT_HOSTED_MODELS = {"anthropic": "claude-sonnet-5", "openai": ""}


@dataclass
class Config:
    data_dir: Path
    contact_email: str
    max_batch_rows: int
    max_upload_mb: int
    max_workspace_mb: int
    max_concurrent_jobs: int
    workspace_ttl_hours: int
    min_free_disk_pct: int
    allow_custom_endpoint: bool
    hosted_keys: dict          # provider -> api key
    hosted_models: dict        # provider -> model name
    hosted_daily_total: int
    hosted_daily_per_visitor: int

    @classmethod
    def from_env(cls) -> "Config":
        hosted_keys, hosted_models = {}, {}
        for prov in ("anthropic", "openai"):
            key = os.environ.get(f"TRIAGEQ_HOSTED_{prov.upper()}_KEY", "").strip()
            model = (os.environ.get(f"TRIAGEQ_HOSTED_{prov.upper()}_MODEL", "").strip()
                     or DEFAULT_HOSTED_MODELS[prov])
            if key and model:
                hosted_keys[prov] = key
                hosted_models[prov] = model
        return cls(
            data_dir=Path(os.environ.get("TRIAGEQ_DATA_DIR", "").strip()
                          or Path(__file__).parent / "web_data"),
            contact_email=os.environ.get("TRIAGEQ_CONTACT_EMAIL", "").strip(),
            max_batch_rows=_env_int("TRIAGEQ_MAX_BATCH_ROWS", 10),
            max_upload_mb=_env_int("TRIAGEQ_MAX_UPLOAD_MB", 25),
            max_workspace_mb=_env_int("TRIAGEQ_MAX_WORKSPACE_MB", 30),
            max_concurrent_jobs=_env_int("TRIAGEQ_MAX_CONCURRENT_JOBS", 3),
            workspace_ttl_hours=_env_int("TRIAGEQ_WORKSPACE_TTL_HOURS", 24),
            min_free_disk_pct=_env_int("TRIAGEQ_MIN_FREE_DISK_PCT", 15),
            allow_custom_endpoint=_env_bool("TRIAGEQ_ALLOW_CUSTOM_ENDPOINT", True),
            hosted_keys=hosted_keys,
            hosted_models=hosted_models,
            hosted_daily_total=_env_int("TRIAGEQ_HOSTED_DAILY_TOTAL", 100),
            hosted_daily_per_visitor=_env_int("TRIAGEQ_HOSTED_DAILY_PER_VISITOR", 5),
        )

    def resolver_config(self) -> pr.ResolverConfig:
        # One server-wide resolver setup. The paid OpenAlex content endpoint
        # stays off: visitors would be spending the operator's money.
        return pr.ResolverConfig(contact_email=self.contact_email,
                                 use_openalex_content=False)

    def library_config(self) -> lib.LibraryConfig:
        return lib.LibraryConfig()

    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def usage_dir(self) -> Path:
        return self.data_dir / "usage"

    def disk_ok(self) -> bool:
        try:
            du = shutil.disk_usage(self.data_dir)
        except OSError:
            return True
        return du.free / du.total * 100 >= self.min_free_disk_pct


# ══════════════════════════════════════════════════════════════════════════════
# Workspaces
# ══════════════════════════════════════════════════════════════════════════════

WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_. ()-]")
UPLOAD_EXTS = lib.PDF_EXTS | {".pptx", ".pptm"}   # .ppt needs LibreOffice


def new_workspace_id() -> str:
    return secrets.token_urlsafe(18)


def safe_filename(name: str) -> str:
    """Basename only, stripped to a conservative character set. Uploaded files
    are addressed by this name from a batch CSV's file_path column, so it must
    never be able to climb out of the uploads folder."""
    base = Path(name.replace("\\", "/")).name
    base = _UNSAFE_NAME_RE.sub("_", base).strip(" .")
    return base[:120] or "file"


class WorkspaceError(Exception):
    """A refusal with a message safe to show the visitor."""


class Workspace:
    """One visitor's folder. Its id is the only credential: anyone holding the
    id (it rides in the page URL) can open the workspace, nobody else can."""

    def __init__(self, cfg: Config, ws_id: str):
        if not WORKSPACE_ID_RE.match(ws_id or ""):
            raise WorkspaceError("invalid workspace id")
        self.cfg = cfg
        self.id = ws_id
        self.root = cfg.workspaces_dir() / ws_id
        self.repo = screening.Repository(self.root)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def exists(self) -> bool:
        return self.root.is_dir()

    def open(self):
        """Create if needed and mark as recently used (resets the TTL clock)."""
        self.repo.ensure()
        self.uploads_dir.mkdir(exist_ok=True)
        self._marker.touch()

    def delete(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def _marker(self) -> Path:
        return self.root / ".last_seen"

    def last_seen(self) -> float:
        try:
            return self._marker.stat().st_mtime
        except OSError:
            try:
                return self.root.stat().st_mtime
            except OSError:
                return 0.0

    def expires_at(self) -> datetime.datetime:
        return (datetime.datetime.fromtimestamp(self.last_seen())
                + datetime.timedelta(hours=self.cfg.workspace_ttl_hours))

    def size_bytes(self) -> int:
        total = 0
        for p in self.root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    # ── settings (just the active profile, for now) ───────────────────────────

    @property
    def _settings_path(self) -> Path:
        return self.root / "workspace.json"

    def settings(self) -> dict:
        try:
            return json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def update_settings(self, **kw):
        s = self.settings()
        s.update(kw)
        self._settings_path.write_text(json.dumps(s, indent=2), encoding="utf-8")

    # ── profiles ──────────────────────────────────────────────────────────────

    def profiles(self) -> list[dict]:
        return cp.list_profiles(self.root)

    def active_profile(self) -> dict | None:
        pid = self.settings().get("active_profile_id", "")
        return cp.load_profile(self.root, pid) if pid else None

    def save_profile(self, profile: dict, activate: bool = True) -> dict:
        profile = cp.normalize_profile(profile)
        cp.save_profile(self.root, profile)
        if activate:
            self.update_settings(active_profile_id=profile["profile_id"])
        return profile

    def profile_in_use(self, profile_id: str, version: str | None = None) -> int:
        return sum(1 for r in self.repo.load()
                   if r.get("profile_id") == profile_id
                   and (version is None or r.get("profile_version") == version))

    # ── uploads ───────────────────────────────────────────────────────────────

    @property
    def uploads_dir(self) -> Path:
        return self.root / "uploads"

    def check_room(self, incoming_bytes: int):
        if not self.cfg.disk_ok():
            raise WorkspaceError(
                "The server is low on disk space right now. Please try again later.")
        cap = self.cfg.max_workspace_mb * 1024 * 1024
        if self.size_bytes() + incoming_bytes > cap:
            raise WorkspaceError(
                f"This workspace is limited to {self.cfg.max_workspace_mb} MB. "
                "Remove some uploads or clear the session to make room.")

    def save_upload(self, name: str, data: bytes) -> Path:
        name = safe_filename(name)
        if Path(name).suffix.lower() not in UPLOAD_EXTS:
            raise WorkspaceError(f"{name}: only PDF and PowerPoint (.pptx) files are accepted.")
        if len(data) > self.cfg.max_upload_mb * 1024 * 1024:
            raise WorkspaceError(f"{name} is larger than {self.cfg.max_upload_mb} MB.")
        self.check_room(len(data))
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        path = self.uploads_dir / name
        path.write_bytes(data)
        return path

    def uploads(self) -> list[Path]:
        if not self.uploads_dir.is_dir():
            return []
        return sorted(p for p in self.uploads_dir.iterdir() if p.is_file())

    def upload_path(self, name: str) -> Path | None:
        """Resolve a batch CSV's file_path to an uploaded file. Only the basename
        counts — a visitor can never name a path elsewhere on the server."""
        if not name:
            return None
        p = self.uploads_dir / safe_filename(name)
        return p if p.is_file() else None

    def remove_upload(self, name: str):
        p = self.upload_path(name)
        if p:
            p.unlink()

    # ── export ────────────────────────────────────────────────────────────────

    def export_zip(self) -> bytes:
        """Repository JSON, every CSV mirror, and every profile — everything a
        visitor needs to keep their work after the workspace expires. Uploaded
        source files are not included; the visitor already has them."""
        self.repo.ensure()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(self.root.glob("*.json")) + sorted(self.root.glob("*.csv")):
                if p.name == "workspace.json":
                    continue
                z.write(p, p.name)
            for p in sorted(cp.profiles_dir(self.root).glob("*.json")):
                z.write(p, f"criteria_profiles/{p.name}")
        return buf.getvalue()


def purge_expired(cfg: Config, busy_ids: set | None = None) -> list[str]:
    """Delete workspaces idle for longer than the TTL. Workspaces with a batch
    still running are skipped, however old."""
    busy_ids = busy_ids or set()
    cutoff = time.time() - cfg.workspace_ttl_hours * 3600
    removed = []
    root = cfg.workspaces_dir()
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir() or d.name in busy_ids:
                continue
            try:
                ws = Workspace(cfg, d.name)
            except WorkspaceError:
                shutil.rmtree(d, ignore_errors=True)   # not ours; don't keep it
                continue
            if ws.last_seen() < cutoff:
                ws.delete()
                removed.append(d.name)
    # Usage counters are tiny, but there is no reason to keep them forever.
    udir = cfg.usage_dir()
    if udir.is_dir():
        old = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
        for f in udir.glob("*.json"):
            if f.stem < old:
                f.unlink(missing_ok=True)
    return removed


# ══════════════════════════════════════════════════════════════════════════════
# Hosted-key quota
# ══════════════════════════════════════════════════════════════════════════════

class Quota:
    """Daily model-call caps for the organisation's own keys.

    Counted per server, per workspace, and per client IP — the IP cap is what
    stops "clear session, get a fresh allowance" from working. A call is
    counted when it is reserved, before it is made, and a failed call is not
    refunded: the provider may still have billed it. Counters reset at
    midnight UTC. These are a courtesy limit, not a billing control; the hard
    ceiling is the spending limit set in the provider's own console.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()

    def _path(self) -> Path:
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return self.cfg.usage_dir() / f"{today}.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._path().read_text(encoding="utf-8"))
        except Exception:
            return {"total": 0, "workspace": {}, "ip": {}}

    def remaining(self, ws_id: str, ip: str) -> int:
        with self._lock:
            u = self._load()
        return max(0, min(
            self.cfg.hosted_daily_total - u["total"],
            self.cfg.hosted_daily_per_visitor - u["workspace"].get(ws_id, 0),
            self.cfg.hosted_daily_per_visitor - u["ip"].get(ip, 0),
        ))

    def reserve(self, ws_id: str, ip: str) -> str | None:
        """Count one call. Returns None if allowed, else a reason to show."""
        with self._lock:
            u = self._load()
            if u["total"] >= self.cfg.hosted_daily_total:
                return ("The shared demo key has reached today's limit for everyone. "
                        "Use your own API key, or try again tomorrow (resets at 00:00 UTC).")
            if (u["workspace"].get(ws_id, 0) >= self.cfg.hosted_daily_per_visitor
                    or u["ip"].get(ip, 0) >= self.cfg.hosted_daily_per_visitor):
                return (f"You've used today's {self.cfg.hosted_daily_per_visitor} demo-key "
                        "calls. Use your own API key to keep going, or come back tomorrow.")
            u["total"] += 1
            u["workspace"][ws_id] = u["workspace"].get(ws_id, 0) + 1
            u["ip"][ip] = u["ip"].get(ip, 0) + 1
            self.cfg.usage_dir().mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps(u), encoding="utf-8")
            return None


class QuotaExceeded(Exception):
    pass


def gated(gate):
    """Turn a gate (callable → None | reason) into a raise-on-refusal check."""
    def check():
        if gate:
            reason = gate()
            if reason:
                raise QuotaExceeded(reason)
    return check


# ══════════════════════════════════════════════════════════════════════════════
# Batch jobs
# ══════════════════════════════════════════════════════════════════════════════

def read_batch_csv(data: bytes) -> list[dict]:
    for enc in ("utf-8-sig", "latin-1", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def batch_template_csv() -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=screening.BATCH_CSV_COLUMNS)
    w.writeheader()
    w.writerow(screening.BATCH_TEMPLATE_ROW)
    return buf.getvalue().encode("utf-8")


@dataclass
class Job:
    id: str
    workspace_id: str
    total: int
    status: str = "queued"      # queued | running | done | stopped | failed
    done: int = 0
    counts: dict = field(default_factory=lambda: {
        "INCLUDE": 0, "EXCLUDE": 0, "MANUAL_REVIEW": 0, "error": 0})
    log: list = field(default_factory=list)       # (text, tag)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    current: str = ""
    stop_requested: bool = False

    def write(self, text: str, tag: str = ""):
        self.log.append((text, tag))

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")


class JobManager:
    """At most one batch per workspace, and at most max_concurrent_jobs
    screening at once across the server; the rest wait in line. Single-paper
    analyses share the same slots (see slot())."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._slots = threading.BoundedSemaphore(max(1, cfg.max_concurrent_jobs))
        self._jobs: dict[str, Job] = {}         # workspace id -> latest job
        self._lock = threading.Lock()

    def get(self, ws_id: str) -> Job | None:
        return self._jobs.get(ws_id)

    def busy_workspaces(self) -> set:
        with self._lock:
            return {w for w, j in self._jobs.items() if j.active}

    def stop(self, ws_id: str):
        j = self._jobs.get(ws_id)
        if j and j.active:
            j.stop_requested = True

    def forget(self, ws_id: str):
        self.stop(ws_id)
        with self._lock:
            self._jobs.pop(ws_id, None)

    class _Slot:
        def __init__(self, sem, timeout):
            self.sem, self.timeout, self.ok = sem, timeout, False

        def __enter__(self):
            self.ok = self.sem.acquire(timeout=self.timeout)
            return self.ok

        def __exit__(self, *exc):
            if self.ok:
                self.sem.release()

    def slot(self, timeout: float = 60):
        """`with jobs.slot() as ok:` — ok is False if the server stayed busy."""
        return self._Slot(self._slots, timeout)

    def start_batch(self, ws: Workspace, rows: list[dict], profile: dict,
                    provider_cfg: mp.ProviderConfig, allow_abstract: bool,
                    gate=None) -> Job:
        with self._lock:
            cur = self._jobs.get(ws.id)
            if cur and cur.active:
                raise WorkspaceError("A batch is already running in this workspace.")
            job = Job(id=secrets.token_hex(6), workspace_id=ws.id, total=len(rows))
            self._jobs[ws.id] = job
        t = threading.Thread(
            target=self._run, daemon=True,
            args=(job, ws, rows, profile, provider_cfg, allow_abstract, gate))
        t.start()
        return job

    def _run(self, job, ws, rows, profile, provider_cfg, allow_abstract, gate):
        job.write("Waiting for a free screening slot…\n", "info")
        while not self._slots.acquire(timeout=2):
            if job.stop_requested:
                job.status, job.finished_at = "stopped", time.time()
                job.write("⏹ Stopped before starting.\n", "info")
                return
        try:
            job.status = "running"
            run_batch(job, ws, rows, profile, provider_cfg, allow_abstract,
                      resolver_cfg=self.cfg.resolver_config(),
                      library_cfg=self.cfg.library_config(), gate=gate)
            if job.status == "running":
                job.status = "done"
        except Exception as exc:                    # never leave a job "running"
            job.status = "failed"
            job.write(f"\nBatch failed: {exc}\n", "err")
        finally:
            job.finished_at = time.time()
            job.current = ""
            self._slots.release()


def run_batch(job: Job, ws: Workspace, rows: list[dict], profile: dict,
              provider_cfg: mp.ProviderConfig, allow_abstract: bool, *,
              resolver_cfg: pr.ResolverConfig, library_cfg: lib.LibraryConfig,
              gate=None):
    """The desktop batch loop (app.py _batch_worker), minus the folder-scan
    step: on the web, local files are uploads, named in the CSV's file_path
    column. Same source precedence — url first, then file_path verified
    against the row's title/DOI, then abstract-only if enabled."""
    check_quota = gated(gate)
    total = len(rows)
    job.write(f"Loaded {total} papers.\n", "info")
    job.write(f"Criteria: {profile['profile_name']} v{profile['profile_version']}\n", "info")
    if allow_abstract:
        job.write("Abstract-only fallback: ON\n", "info")

    targets = lib.targets_from_rows(rows, {r.get("paper_id"): r for r in ws.repo.load()})
    target_by_pid = {t.paper_id: t for t in targets}
    n_abstract = n_mismatch = 0

    for i, row in enumerate(rows):
        if job.stop_requested:
            job.write(f"\n⏹ Batch stopped after {i} of {total} papers.\n", "info")
            job.status = "stopped"
            break
        ws.open()   # a long batch keeps its workspace alive

        pid = (row.get("paper_id") or f"PAPER_{i+1}").strip()
        url = (row.get("url") or "").strip()
        fp_name = (row.get("file_path") or "").strip()
        target = target_by_pid.get(pid)
        job.current = f"Paper {i+1}/{total}: {pid}"
        job.write(f"\n[{i+1}/{total}] {pid} — ", "info")

        basis, pdf_source, pdf_url, payload = "full_text", "", "", None
        source_kind, mismatch_note, online_meta, online_doi = "pdf", "", {}, ""
        file_used = ""

        # 1. Online retrieval, always first.
        if url:
            job.write("locating PDF… ", "info")
            trail: list[str] = []
            res = pr.resolve_pdf(url, resolver_cfg, log=trail.append)
            online_doi, online_meta = res.doi or "", dict(res.metadata or {})
            if res.ok:
                payload, pdf_source, pdf_url = res.pdf_bytes, res.resolved_via, res.pdf_url
                job.write(f"{res.resolved_via}. ", "ok")
            else:
                job.write(f"not found online ({res.error}). ", "info")
                for t in trail[-4:]:
                    job.write(f"\n      · {t}", "trail")
                job.write("\n      ", "")

        # 2. An uploaded file named in file_path, verified before it's trusted.
        path = ws.upload_path(fp_name) if payload is None else None
        if payload is None and fp_name and not path:
            job.write(f"file_path '{fp_name}' was not uploaded. ", "err")
        if path:
            if target and online_meta:
                lib.apply_resolution_metadata(
                    [target], {pid: {"status": "failed", "metadata": online_meta,
                                     "doi": online_doi}})
            loaded = lib.load_for_screening(str(path), library_cfg)
            if loaded["error"]:
                job.write(f"file unusable ({loaded['error']}). ", "err")
            else:
                payload, basis = loaded["payload"], loaded["basis"]
                pdf_source = f"upload ({loaded['transport']})"
                source_kind = loaded.get("source_kind", "pdf")
                file_used = path.name
                job.write(f"using uploaded {path.name}. ", "ok")
                if target:
                    v = lib.verify_file_identity(target, str(path), library_cfg)
                    if v["checked"] and not v["ok"]:
                        mismatch_note = f"file_path mismatch: {v['note']}"
                        n_mismatch += 1
                        job.write(f"MISMATCH ({v['note']}). ", "err")

        # 3. Abstract-only fallback.
        if payload is None and allow_abstract and online_meta.get("abstract"):
            payload = pr.format_metadata_as_text(online_meta)
            basis, pdf_source = "abstract_only", "abstract only"
            n_abstract += 1
            job.write("no PDF — using abstract. ", "info")

        if payload is None:
            job.write("No source. Skipping.\n", "err")
            job.counts["error"] += 1
            job.done = i + 1
            continue

        t0 = time.monotonic()
        try:
            check_quota()
            job.write("Analyzing… ", "info")
            result = screening.analyze_paper(payload, profile, provider_cfg,
                                             screening_basis=basis)
            if mismatch_note:
                result["overall_recommendation"] = "MANUAL_REVIEW"
                result["_file_mismatch"] = mismatch_note
            entry = screening.build_repo_entry(
                profile, pid, result, basis=basis, url=url, file_path=file_used,
                pdf_source=pdf_source, pdf_url=pdf_url, source_kind=source_kind)
            ws.repo.save(entry)
            rec = result.get("overall_recommendation", "?")
            if rec in job.counts:
                job.counts[rec] += 1
            suffix = "  [abstract only]" if basis == "abstract_only" else ""
            tag = {"INCLUDE": "ok", "EXCLUDE": "err"}.get(rec, "info")
            job.write(f"→ {rec}{suffix}  ({time.monotonic() - t0:.0f}s)\n", tag)
            if result.get("_local_correction"):
                job.write(f"      {result['_local_correction']}\n", "info")
        except QuotaExceeded as exc:
            job.write(f"\n{exc}\n", "err")
            job.counts["error"] += 1
            job.done = i + 1
            job.status = "stopped"
            break
        except Exception as exc:
            job.write(f"ERROR: {exc}\n", "err")
            job.counts["error"] += 1
        job.done = i + 1

    c = job.counts
    job.write(f"\nDone — {c['INCLUDE']} included, {c['EXCLUDE']} excluded, "
              f"{c['MANUAL_REVIEW']} manual review, {c['error']} errors.\n", "ok")
    if n_abstract:
        job.write(f"{n_abstract} paper(s) screened from abstract only — review these "
                  "before treating their verdicts as final.\n", "info")
    if n_mismatch:
        job.write(f"{n_mismatch} paper(s) forced to MANUAL_REVIEW because the uploaded "
                  "file did not match the row's title/DOI.\n", "err")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["cleanup"]:
        gone = purge_expired(Config.from_env())
        print(f"Removed {len(gone)} expired workspace(s).")
    else:
        print("usage: python web_backend.py cleanup")
