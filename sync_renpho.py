#!/usr/bin/env python3
"""Pull Renpho smart-tape girths into body_composition.csv (weight: see sync_scale.py).

Uses the unofficial `renpho-api` client (Renpho Health API, cloud.renpho.com) for
auth + scale data, plus the girth endpoint discovered by decompiling the app:

    RenphoHealth/renpho/girth/queryAllGirthsDataList      <- tape circumferences

(The library only implements scale data; the girth path has that sneaky extra
`renpho/` segment, which is why naive `RenphoHealth/girth/...` guesses 404'd.)

Run under the venv that has renpho-api installed:
    .venv/bin/python sync_renpho.py              # sync into body_composition.csv
    .venv/bin/python sync_renpho.py --discover   # raw dump for debugging

Credentials come from .renpho.env (gitignored): RENPHO_EMAIL, RENPHO_PASSWORD.
Girth values are stored by Renpho in cm; we convert to inches. Weight is kg -> lb.
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "training"))
import config  # noqa: E402

ROOT = config.DATA
ENV = config.RENPHO_ENV_FILE
CSV_PATH = config.BODY_CSV
KG_TO_LB = 2.2046226218
CM_TO_IN = 1 / 2.54

GIRTH_ENDPOINT = "RenphoHealth/renpho/girth/queryAllGirthsDataList"

CSV_COLUMNS = ["date", "weight_lb", "bf_pct", "neck_in", "shoulder_in", "chest_in",
               "arm_l_in", "arm_r_in", "waist_in", "abdomen_in", "hips_in",
               "thigh_l_in", "thigh_r_in", "calf_l_in", "calf_r_in", "source", "note"]

# Renpho girth field (cm) -> csv column. Single-side values (armValue etc.) fall
# back to the right side; explicit left/right win if both present.
GIRTH_MAP = {
    "neckValue": "neck_in", "shoulderValue": "shoulder_in", "chestValue": "chest_in",
    "armValue": "arm_r_in", "rightArmValue": "arm_r_in", "leftArmValue": "arm_l_in",
    "waistValue": "waist_in", "abdomenValue": "abdomen_in", "hipValue": "hips_in",
    "thighValue": "thigh_r_in", "rightThighValue": "thigh_r_in", "leftThighValue": "thigh_l_in",
    "calfValue": "calf_r_in", "rightCalfValue": "calf_r_in", "leftCalfValue": "calf_l_in",
}

# Data correction: through 2026-07-25 Tom recorded LEFT-side limbs into Renpho's
# RIGHT-side slots (confirmed 2026-07-26). Remap those records to the left columns.
# He's fixing the labels in the app going forward, so this only applies on/before
# the cutoff — later records are trusted as-is. Applied in the sync (not by hand-
# editing the CSV) because sync rewrites the CSV from the API on every nightly run.
SIDE_SWAP_THROUGH = "2026-07-25"
SIDE_SWAP = {"arm_r_in": "arm_l_in", "thigh_r_in": "thigh_l_in", "calf_r_in": "calf_l_in"}


def load_env() -> tuple[str, str]:
    if not ENV.exists():
        sys.exit(f"Missing {ENV}. Copy .renpho.env.example to .renpho.env and fill it in.")
    vals = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip()
    email, pw = vals.get("RENPHO_EMAIL"), vals.get("RENPHO_PASSWORD")
    if not email or not pw:
        sys.exit("RENPHO_EMAIL / RENPHO_PASSWORD not set in .renpho.env")
    return email, pw


def client():
    try:
        from renpho import RenphoClient
    except ImportError:
        sys.exit("renpho-api not installed. Run: .venv/bin/pip install renpho-api "
                 "(and use .venv/bin/python to run this script).")
    email, pw = load_env()
    return RenphoClient(email, pw)


def _local_date(rec: dict) -> str | None:
    """Local calendar date of a record, from epoch timeStamp + its timeZone offset."""
    ts = rec.get("timeStamp") or rec.get("timestamp")
    if not ts:
        ca = rec.get("createdAt")
        return str(ca)[:10] if ca else None
    try:
        off = int(str(rec.get("timeZone", "0")).split(".")[0])
    except ValueError:
        off = 0
    return (datetime.fromtimestamp(int(ts), tz=timezone.utc) + timedelta(hours=off)).date().isoformat()


def fetch_girths(c) -> list[dict]:
    """Fetch tape circumference records via the decompiled girth endpoint."""
    from renpho.crypto import encrypt_request, decrypt_response
    if not getattr(c, "token", None):
        c.login()
    res = c._post(GIRTH_ENDPOINT,
                  encrypt_request({"pageNum": 1, "pageSize": 500, "userIds": [str(c.user_id)]}))
    data = res.get("data")
    if not data:
        return []
    recs = decrypt_response(data)
    return recs if isinstance(recs, list) else []


def _blank_row() -> dict:
    return {c: "" for c in CSV_COLUMNS}


def sync() -> None:
    c = client()
    c.login()
    # Tape only. Weight is NOT read from Renpho (2026-10-08): Tom owns no Renpho
    # scale and the account held one stray weight record. Weight comes from the
    # Etekcity scale via Home Assistant -- see sync_scale.py.
    girths = fetch_girths(c)              # tape circumferences (our endpoint)

    # existing rows keyed by date (merge so a scale weigh-in and tape session on
    # the same day land in one row)
    rows: dict[str, dict] = {}
    if CSV_PATH.exists():
        with CSV_PATH.open() as f:
            for r in csv.DictReader(f):
                if r.get("date"):
                    r = {**_blank_row(), **r}
                    # Scrub what the retired Renpho scale path wrote (one row).
                    srcs = (r.get("source") or "").split("+")
                    if "renpho-scale" in srcs:
                        r["weight_lb"] = r["bf_pct"] = ""
                        r["source"] = "+".join(x for x in srcs if x and x != "renpho-scale")
                    rows[r["date"]] = r

    def touch(date):
        return rows.setdefault(date, {**_blank_row(), "date": date})

    def mark(row, src):
        srcs = set(filter(None, (row.get("source", "").split("+") if row.get("source") else [])))
        srcs.add(src)
        row["source"] = "+".join(sorted(srcs))

    n_girth = 0
    # Every saved tape session is its own API record, so a site measured three
    # times in a day arrives as three records. AVERAGE them per site (added
    # 2026-09-22 for the repeat-reading protocol in training/programming.md). It
    # used to be last-write-wins, which would have silently kept only the final
    # reading. No history changed: before 09-22 no site had two readings on one
    # local date. (createdAt is server time, so dates come from timeStamp +
    # timeZone.) The API returns the full history on every call, so recomputing
    # the mean each night is idempotent.
    by_date: dict[str, dict[str, list[float]]] = {}
    for g in girths:
        date = _local_date(g)
        if not date:
            continue
        swap = date <= SIDE_SWAP_THROUGH   # see SIDE_SWAP note above
        vals = by_date.setdefault(date, {})
        for field, col in GIRTH_MAP.items():
            v = g.get(field)
            if v:  # nonzero
                if swap and col in SIDE_SWAP:
                    # move to the correct side AND clear the mis-sided value, else a
                    # previously-synced (wrong-side) entry lingers on merge.
                    touch(date)[col] = ""
                    col = SIDE_SWAP[col]
                vals.setdefault(col, []).append(float(v) * CM_TO_IN)
        n_girth += 1

    for date, vals in by_date.items():
        if not vals:
            continue
        row = touch(date)
        for col, xs in vals.items():
            row[col] = f'{sum(xs) / len(xs):.2f}'
        mark(row, "renpho-tape")
        reps = max(len(xs) for xs in vals.values())
        if reps > 1 and (not row.get("note") or row["note"].startswith("tape: mean")):
            row["note"] = f"tape: mean of up to {reps} readings/site"

    ordered = [rows[d] for d in sorted(rows)]
    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in ordered:
            w.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
    print(f"wrote {len(ordered)} dated rows to {CSV_PATH.name} "
          f"(tape records: {n_girth})")
    print("Regenerate the dashboard: python3 training/build_dashboard.py")


def discover() -> None:
    c = client(); c.login()
    print(f"user_id={c.user_id}")
    print("\n=== scale (library get_all_measurements) ===")
    for m in c.get_all_measurements()[:3]:
        print(" ", {k: m[k] for k in ("timeStamp", "weight", "bodyfat") if k in m})
    print("\n=== girths (our endpoint) ===")
    for g in fetch_girths(c):
        nz = {k: v for k, v in g.items() if k.endswith("Value") and v}
        print(f"  {_local_date(g)}: {nz}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="raw dump for debugging")
    args = ap.parse_args()
    (discover if args.discover else sync)()
