#!/usr/bin/env python3
"""
Hevy API → CSV sync. Run nightly via cron.

Writes three files into health/training/:
  - workouts.csv  — one row per workout (id, start_time, end_time, title)
  - exercises.csv — one row per exercise instance (workout, exercise, order)
  - sets.csv      — one row per set (workout, exercise, set#, weight, reps, rpe, type)

Uses only Python stdlib (urllib.request for HTTP, custom .env parser) so no
venv or pip install needed — just `python3 sync_hevy.py`.

Prereqs:
  - Hevy Pro subscription + API key from https://hevy.com/settings?developer
  - API key in health/.env as HEVY_API_KEY=...
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

ROOT = config.TRAINING
ROOT.mkdir(parents=True, exist_ok=True)
ENV_PATH = config.ENV_FILE


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env parser — KEY=VALUE lines, ignores # comments and blanks."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


env = load_env(ENV_PATH)
API_KEY = env.get("HEVY_API_KEY") or os.environ.get("HEVY_API_KEY")
if not API_KEY:
    sys.exit(f"HEVY_API_KEY missing. Add to {ENV_PATH}")

API_BASE = "https://api.hevyapp.com/v1"
PAGE_SIZE = 10


MAX_TRIES = 5
RETRY_CODES = (429, 500, 502, 503, 504)


def api_get(path: str, params: dict | None = None) -> dict:
    """GET with backoff on throttling.

    Hevy began 429-ing this key around 2026-08-14 and a single 429 used to abort
    the whole nightly sync, because every HTTPError was fatal. Throttling and
    5xx are transient; retry them and only give up after MAX_TRIES.
    """
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{API_BASE}{path}{qs}"
    for attempt in range(MAX_TRIES):
        req = urllib.request.Request(
            url,
            headers={"api-key": API_KEY, "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = attempt == MAX_TRIES - 1
            if e.code in RETRY_CODES and not last:
                hdr = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
                delay = float(hdr) if hdr.isdigit() else 2.0 * (2 ** attempt)
                print(f"  HTTP {e.code} on page {params.get('page') if params else '?'}"
                      f" — retrying in {delay:.0f}s ({attempt + 1}/{MAX_TRIES - 1})",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            body = e.read().decode("utf-8", errors="replace")
            sys.exit(f"HTTP {e.code} on {url}\n{body}")
        except urllib.error.URLError as e:
            if attempt == MAX_TRIES - 1:
                sys.exit(f"network error on {url}: {e.reason}")
            delay = 2.0 * (2 ** attempt)
            print(f"  network error ({e.reason}) — retrying in {delay:.0f}s",
                  file=sys.stderr)
            time.sleep(delay)
    sys.exit(f"exhausted {MAX_TRIES} attempts on {url}")


def fetch_all_workouts() -> list[dict]:
    all_workouts: list[dict] = []
    page = 1
    while True:
        data = api_get("/workouts", {"page": page, "pageSize": PAGE_SIZE})
        workouts = data.get("workouts", [])
        if not workouts:
            break
        all_workouts.extend(workouts)
        # Stop on the page count the API itself reports. The short-page check
        # below never fires when the total is an exact multiple of PAGE_SIZE:
        # the loop asks for one page past the end and Hevy answers
        # `HTTP 404 Page not found`, which killed the sync. That fired at 250,
        # 260, 270, 280, 290 (each self-healing on the next logged workout) and
        # then stuck at 300 from 2026-08-13. push_targets.all_routines() has
        # always handled the same 404; this is the matching guard.
        page_count = data.get("page_count")
        if isinstance(page_count, int) and page >= page_count:
            break
        if len(workouts) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.5)
    return all_workouts


def _read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def report_changes(new_workouts: list[dict], new_sets: list[dict]) -> None:
    """Say what moved since the last sync — in particular EDITS to old workouts.

    The sync is a full re-fetch + full overwrite, so retroactive edits in Hevy are
    picked up automatically; without this they'd land silently and a metric could
    shift with no visible cause.
    """
    old_w = {w["id"]: w for w in _read_existing(ROOT / "workouts.csv")}
    old_s: dict[str, list] = {}
    for s in _read_existing(ROOT / "sets.csv"):
        old_s.setdefault(s["workout_id"], []).append(
            (s["exercise_order"], s["set_index"], s["type"],
             s["weight_kg"], s["reps"], s["rpe"]))
    if not old_w:
        return
    new_s: dict[str, list] = {}
    for s in new_sets:
        new_s.setdefault(str(s["workout_id"]), []).append(
            (str(s["exercise_order"]), str(s["set_index"]), str(s["type"] or ""),
             str(s["weight_kg"] if s["weight_kg"] is not None else ""),
             str(s["reps"] if s["reps"] is not None else ""),
             str(s["rpe"] if s["rpe"] is not None else "")))

    new_ids = {str(w.get("id")) for w in new_workouts}
    added = new_ids - set(old_w)
    removed = set(old_w) - new_ids
    edited = []
    for w in new_workouts:
        wid = str(w.get("id"))
        if wid in added or wid not in old_w:
            continue
        if new_s.get(wid, []) != old_s.get(wid, []):
            edited.append((w.get("start_time", "")[:10], w.get("title", "")))

    if added:
        print(f"  +{len(added)} new workout(s)")
    if removed:
        print(f"  -{len(removed)} workout(s) removed in Hevy: {sorted(removed)}")
    for date, title in sorted(edited):
        print(f"  ~ EDITED (retroactive): {date}  {title}")
    if not (added or removed or edited):
        print("  no changes")


def write_csvs(workouts: list[dict]) -> None:
    workouts_rows: list[dict] = []
    exercises_rows: list[dict] = []
    sets_rows: list[dict] = []

    for w in workouts:
        workouts_rows.append({
            "id": w.get("id"),
            "start_time": w.get("start_time"),
            "end_time": w.get("end_time"),
            "title": w.get("title"),
            "description": w.get("description", ""),
        })
        for ex_idx, ex in enumerate(w.get("exercises", []) or []):
            exercises_rows.append({
                "workout_id": w.get("id"),
                "order": ex_idx,
                "exercise_template_id": ex.get("exercise_template_id"),
                "title": ex.get("title"),
                "notes": ex.get("notes", ""),
                "superset_id": ex.get("superset_id"),
            })
            for set_idx, s in enumerate(ex.get("sets", []) or []):
                sets_rows.append({
                    "workout_id": w.get("id"),
                    "exercise_order": ex_idx,
                    "set_index": set_idx,
                    "type": s.get("type"),
                    "weight_kg": s.get("weight_kg"),
                    "reps": s.get("reps"),
                    "distance_meters": s.get("distance_meters"),
                    "duration_seconds": s.get("duration_seconds"),
                    "rpe": s.get("rpe"),
                })

    # Guard against silent data loss: this is a full overwrite, so a partial fetch
    # (transient API error mid-pagination returning an early empty page) would
    # quietly delete history. Deleting a couple of workouts in Hevy is normal;
    # losing a chunk is not. --force overrides.
    old_count = len(_read_existing(ROOT / "workouts.csv"))
    if old_count and len(workouts_rows) < old_count * 0.95 and "--force" not in sys.argv:
        sys.exit(f"ABORT: fetched {len(workouts_rows)} workouts but {old_count} are on disk "
                 f"(<95%). Refusing to overwrite — likely a partial fetch. "
                 f"Re-run when the API is healthy, or pass --force if the deletion is real.")

    report_changes(workouts, sets_rows)
    _write(ROOT / "workouts.csv", workouts_rows)
    _write(ROOT / "exercises.csv", exercises_rows)
    _write(ROOT / "sets.csv", sets_rows)


def _write(path: Path, rows: list[dict]) -> None:
    if not rows:
        print(f"no rows for {path.name}", file=sys.stderr)
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows → {path.name}")


def _republish_strength_export() -> None:
    """Refresh the world-readable strength snapshot after a successful sync.

    The nightly sync is what makes the CSVs newer than the dashboard, so without
    this the export would sit stale until the next dashboard build. Import is
    local and the whole thing is best-effort: a broken export must never fail or
    slow the sync, which is the job that actually matters.
    """
    try:
        sys.path.insert(0, str(ROOT))
        import build_dashboard
        if build_dashboard.export_strength_snapshot():
            print(f"wrote {build_dashboard.STRENGTH_EXPORT}")
    except Exception as e:                       # noqa: BLE001 - side channel
        print(f"strength export skipped: {e}", file=sys.stderr)


if __name__ == "__main__":
    # timestamp so the appended cron log (training/sync.log) stays auditable —
    # you can trace exactly which nightly run picked up a retroactive edit
    print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} sync ===")
    workouts = fetch_all_workouts()
    print(f"fetched {len(workouts)} workouts")
    write_csvs(workouts)
    _republish_strength_export()
