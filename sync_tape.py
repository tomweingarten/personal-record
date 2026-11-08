#!/usr/bin/env python3
"""Pull Renpho smart-tape sessions from Home Assistant into body_composition.csv.

Source: the `renpho_tape` custom integration in HA (code in ha/renpho_tape/, deployed to
/config/custom_components/ on the HA VM). It talks to the R-Y002 tape over HA's
Bluetooth and keeps every measuring session it has received; the tape itself forgets a
session once HA has downloaded it. This replaced the Renpho app + cloud path
(sync_renpho.py) on 2026-10-17.

We fetch the full session list with the integration's `renpho_tape.get_sessions`
service on every run, so there is no recorder-retention or HA-restart re-post problem
(contrast sync_scale.py), and recomputing is idempotent.

One row per local date. A site measured more than once on a date is AVERAGED (every
raw reading, so lock-lock-lock-Save in one session counts as three; with more than 3,
only the 3 that agree most closely, so a retake replaces the outlier it was taken for), the
same repeat-reading rule as sync_renpho.py (training/programming.md). The tape reports
cm; the CSV is in inches. Runs nightly at 02:20 after sync_scale.py. sync_renpho.py
left the nightly chain on 2026-10-17 (HA clears sessions off the tape, so the cloud
gets nothing new) and is kept only for a manual backfill; if run, run this after it so
HA's tape values win on any shared date.

    python3 sync_tape.py            # merge into the csv
    python3 sync_tape.py --dry-run  # print what would change
Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from datetime import datetime
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "training"))
import config  # noqa: E402

ROOT = config.DATA
ENV = config.HA_ENV_FILE
CSV_PATH = config.BODY_CSV
SOURCE = "ha-tape"
CM_TO_IN = 1 / 2.54

# integration part key -> csv column
PART_COLUMNS = {
    "neck": "neck_in", "shoulder": "shoulder_in", "chest": "chest_in",
    "arm_left": "arm_l_in", "arm_right": "arm_r_in", "waist": "waist_in",
    "abdomen": "abdomen_in", "hip": "hips_in",
    "thigh_left": "thigh_l_in", "thigh_right": "thigh_r_in",
    "calf_left": "calf_l_in", "calf_right": "calf_r_in",
}

# Sessions that are not real measurements (by session id). Add here rather than editing
# the HA store, so the raw record survives.
EXCLUDE_SESSIONS: set[str] = set()

# Junk the tape replays from its memory (seen 2026-10-28, when it dumped old records):
#   * 0xFFFF "no value" slots, which decode as 655.35 cm at every site;
#   * records with an unset clock (dated 1890-12-28), whose values are also slot-shifted
#     (neck 30.8 cm = an arm, arm 76.2 cm = a waist);
#   * the same records again, re-stamped with the download time (identical values).
SENTINEL_CM = 600       # any real girth is far below this
MIN_YEAR = 2025         # the tape was bought in 2026; earlier dates mean the clock was unset


def junk_filter(sessions: list[dict]) -> list[dict]:
    """Drop clockless sessions, sentinel values, and replays of an earlier session's values."""
    seen, out = set(), []
    for s in sorted(sessions, key=lambda s: s["measured_at"]):
        if s.get("junk"):   # the integration marks these itself since 2026-10-31
            seen.add(tuple(sorted((k, round(v, 1)) for k, v in s["values_cm"].items())))
            continue
        vals = {k: v for k, v in s["values_cm"].items() if v and v < SENTINEL_CM}
        key = tuple(sorted((k, round(v, 1)) for k, v in vals.items()))
        if not vals or key in seen:
            continue    # all-sentinel, or a replay (the first copy decides, and is judged below)
        seen.add(key)
        if datetime.fromisoformat(s["measured_at"]).year < MIN_YEAR:
            continue
        out.append(s | {"values_cm": vals})
    return out


def load_env() -> tuple[str, str]:
    vals = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals["HA_URL"].rstrip("/"), vals["HA_TOKEN"]


def fetch_sessions() -> list[dict]:
    base, token = load_env()
    req = urllib.request.Request(
        f"{base}/api/services/renpho_tape/get_sessions?return_response", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["service_response"]["sessions"]


def daily(sessions: list[dict]) -> dict[str, dict[str, list[float]]]:
    """local date -> csv column -> readings in inches."""
    out: dict[str, dict[str, list[float]]] = {}
    for s in junk_filter(sessions):
        if s["id"] in EXCLUDE_SESSIONS:
            continue
        date = datetime.fromisoformat(s["measured_at"]).date().isoformat()  # stored in local time
        raw = s.get("readings_cm", {})  # live sessions keep every locked reading
        for part, cm in s["values_cm"].items():
            col = PART_COLUMNS.get(part)
            if col and cm:
                xs = [x for x in raw.get(part) or [cm] if x and x < SENTINEL_CM]
                out.setdefault(date, {}).setdefault(col, []).extend(x * CM_TO_IN for x in xs)
    return out


def tightest(xs: list[float], k: int = 3) -> list[float]:
    """The k readings closest together; same rule as the integration's retake check."""
    xs = sorted(xs)
    if len(xs) <= k:
        return xs
    i = min(range(len(xs) - k + 1), key=lambda i: xs[i + k - 1] - xs[i])
    return xs[i:i + k]


def merge(days: dict[str, dict[str, list[float]]], dry_run: bool) -> int:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = {r["date"]: r for r in reader if r.get("date")}
    changed = 0
    for date, cols in sorted(days.items()):
        row = rows.setdefault(date, {c: "" for c in fields} | {"date": date})
        before = dict(row)
        for col, xs in cols.items():
            xs = tightest(xs)
            row[col] = f"{sum(xs) / len(xs):.2f}"
        srcs = set(filter(None, (row.get("source") or "").split("+")))
        srcs.add(SOURCE)
        row["source"] = "+".join(sorted(srcs))
        reps = max(len(xs) for xs in cols.values())
        if reps > 1 and (not row.get("note") or row["note"].startswith("tape: mean")):
            row["note"] = (f"tape: mean of up to {reps} readings/site" if reps <= 3
                           else f"tape: mean of the closest 3 of up to {reps} readings/site")
        if row != before:
            changed += 1
            print(f"{date}: " + ", ".join(f"{c}={row[c]}" for c in sorted(cols)))
    if not dry_run:
        with CSV_PATH.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for d in sorted(rows):
                w.writerow({c: rows[d].get(c, "") for c in fields})
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sessions = fetch_sessions()
    days = daily(sessions)
    n = merge(days, args.dry_run)
    print(f"{datetime.now():%Y-%m-%d %H:%M} {len(sessions)} session(s) from HA, "
          f"{len(days)} day(s), {n} row(s) {'would change' if args.dry_run else 'updated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
