#!/usr/bin/env python3
"""triageQ usage report (standard library only).

Reads:
  * Caddy JSON access logs (access.log and rolled access-*.log.gz) in --log-dir.
  * App usage counters <usage-dir>/<YYYY-MM-DD>.json ("total", "runs_workspace").

Prints, per UTC day, counts only: unique visitors, page loads, sessions
(Streamlit websocket connections), peak concurrent sessions, median session
length in minutes, demo runs and model calls. Client IPs are used in memory
to count distinct visitors and are never printed or written; user agents are
never read.

With --append PATH, writes one CSV row per UTC day for the last --settle-days
days ending yesterday, adding missing days and updating rows whose numbers
changed. Streamlit sessions are logged only when they close and count toward
the day they started, so a session still open at the nightly run shows up in
a later run; the trailing window lets that late close land in the right row.
Rows older than the window are never touched.
"""
import argparse
import csv
import datetime as dt
import glob
import gzip
import json
import os
import statistics
import sys

DEFAULT_LOG_DIR = "/var/lib/docker/volumes/triageq_caddy-data/_data"
DEFAULT_USAGE_DIR = "/var/lib/docker/volumes/triageq_triageq-data/_data/usage"
CSV_COLUMNS = ["date", "unique_visitors", "page_loads", "sessions",
               "peak_concurrent", "median_session_minutes", "demo_runs", "model_calls"]


def utc_day(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def open_log(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def log_files(log_dir):
    files = sorted(glob.glob(os.path.join(log_dir, "access-*.log.gz")))
    plain = os.path.join(log_dir, "access.log")
    if os.path.exists(plain):
        files.append(plain)
    return files


def parse_line(line):
    """Return (ts, ip, method, uri, duration) or None for unusable lines."""
    try:
        obj = json.loads(line)
        req = obj["request"]
        ts = float(obj["ts"])
        ip = req.get("client_ip") or req.get("remote_ip")
        if not ip:
            return None
        return ts, ip, req.get("method", ""), req.get("uri", ""), float(obj.get("duration") or 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def new_day():
    return {"ips": set(), "page_loads": 0, "sessions": []}


def collect(log_dir):
    """Aggregate all logs into {date: {ips, page_loads, sessions[(start, end)]}}."""
    days = {}
    for path in log_files(log_dir):
        try:
            fh = open_log(path)
        except OSError:
            continue
        with fh:
            try:
                for line in fh:
                    rec = parse_line(line)
                    if rec is None:
                        continue
                    ts, ip, method, uri, duration = rec
                    day = days.setdefault(utc_day(ts), new_day())
                    day["ips"].add(ip)
                    if method == "GET" and (uri == "/" or uri.startswith("/?")):
                        day["page_loads"] += 1
                    if uri.startswith("/_stcore/stream"):
                        start = ts - duration
                        # A session belongs to the day it started.
                        days.setdefault(utc_day(start), new_day())["sessions"].append((start, ts))
            except (OSError, EOFError):
                continue  # truncated gzip: keep what was read
    return days


def peak_concurrent(sessions):
    events = []
    for start, end in sessions:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()  # at equal times, -1 sorts first: back-to-back sessions do not overlap
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def read_usage(usage_dir, date):
    try:
        with open(os.path.join(usage_dir, date + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        runs = data.get("runs_workspace") or {}
        return sum(int(v) for v in runs.values()), int(data.get("total") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0


def day_row(days, usage_dir, date):
    day = days.get(date) or new_day()
    sessions = day["sessions"]
    lengths = [(e - s) / 60 for s, e in sessions]
    median = round(statistics.median(lengths), 1) if lengths else 0.0
    demo_runs, model_calls = read_usage(usage_dir, date)
    return {
        "date": date,
        "unique_visitors": len(day["ips"]),
        "page_loads": day["page_loads"],
        "sessions": len(sessions),
        "peak_concurrent": peak_concurrent(sessions),
        "median_session_minutes": f"{median:.1f}",
        "demo_runs": demo_runs,
        "model_calls": model_calls,
    }


def format_table(rows):
    headers = ["date", "visitors", "page_loads", "sessions", "peak_concurrent",
               "median_session_min", "demo_runs", "model_calls"]
    body = [[str(r[c]) for c in CSV_COLUMNS] for r in rows]
    widths = [max(len(h), *(len(b[i]) for b in body)) if body else len(h)
              for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(b, widths)) for b in body]
    return "\n".join(lines)


def upsert_rows(path, rows):
    """Add or update rows (keyed by date) in the CSV at path. Returns an exit code."""
    existing = {}
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != CSV_COLUMNS:
                print(f"error: {path} has an unexpected header; refusing to modify it",
                      file=sys.stderr)
                return 1
            for r in reader:
                existing[r["date"]] = r
    for row in sorted(rows, key=lambda r: r["date"]):
        new = {c: str(row[c]) for c in CSV_COLUMNS}
        old = existing.get(new["date"])
        if old is None:
            existing[new["date"]] = new
            print(f"appended {new['date']}")
        elif all(old[c] == new[c] for c in CSV_COLUMNS):
            print(f"unchanged {new['date']}")
        else:
            changes = ", ".join(f"{c} {old[c]}->{new[c]}" for c in CSV_COLUMNS
                                if old[c] != new[c])
            existing[new["date"]] = new
            print(f"updated {new['date']} ({changes})")
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for d in sorted(existing):
            writer.writerow(existing[d])
    os.replace(tmp, path)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="triageQ usage report (counts only).")
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--usage-dir", default=DEFAULT_USAGE_DIR)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--date", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--append", metavar="PATH",
                    help="upsert one CSV row per day for the last --settle-days days "
                         "ending yesterday (UTC); sessions are logged when they close, "
                         "so re-computing the window lets a late close update its day")
    ap.add_argument("--settle-days", type=int, default=7,
                    help="with --append, how many days ending yesterday to recompute "
                         "(default 7); --date limits it to that one day")
    args = ap.parse_args(argv)

    today = dt.datetime.now(dt.timezone.utc).date()
    if args.date:
        try:
            dt.date.fromisoformat(args.date)
        except ValueError:
            ap.error("--date must be YYYY-MM-DD")
    if args.append:
        if args.date:
            dates = [args.date]
        else:
            dates = [(today - dt.timedelta(days=i)).isoformat()
                     for i in range(args.settle_days, 0, -1)]
    elif args.date:
        dates = [args.date]
    else:
        dates = [(today - dt.timedelta(days=i)).isoformat()
                 for i in range(args.days - 1, -1, -1)]

    days = collect(args.log_dir)
    rows = [day_row(days, args.usage_dir, d) for d in dates]
    if args.append:
        return upsert_rows(args.append, rows)
    print(format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
