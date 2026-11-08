#!/usr/bin/env python3
"""Pull daily scale weights from Home Assistant into body_composition.csv.

Source: the Etekcity EFS-A591S (Apex HR) smart scale, read over BLE by the HA
custom integration `etekcity_fitness_scale_ble`; entity WEIGHT_ENTITY below is
the per-user ("Tom's") weight sensor. This replaced the Renpho scale path on
2026-10-08: Tom does not own a Renpho scale, and the Renpho account only ever
held one stray weight record. The HA device itself had been disabled (by
accident) from ~2026-08-23 to 10-08, so there is no HA weight history before
10-08 -- earlier daily weights live only in MacroFactor exports.

Runs nightly after sync_renpho.py (which rewrites the CSV but preserves rows and
columns it does not own). Stdlib only.

    python3 sync_scale.py            # merge the last LOOKBACK_DAYS into the csv
    python3 sync_scale.py --days 10  # wider lookback (HA's recorder keeps ~10 days)

One value per local day: the median of the readings within WEIGH_WINDOW of the
day's FIRST reading. The first weigh-in is the protocol one (post-void,
pre-food), and a BLE scale emits several updates while the reading settles.
Readings outside PLAUSIBLE_LB are dropped -- pets get weighed too.

HA restarts (nightly at 04:30, and any manual restart) re-post the last weight
with a fresh last_changed. Those are NOT weigh-ins: any state stamped between a
"Home Assistant stopped" and RESTART_GRACE after the next "started" is dropped.
Before this filter (fixed 2026-10-14) a day with no weigh-in before 04:30
recorded the previous day's weight, e.g. 10-13 was 147.5 instead of 142.3.

Also writes the latest day's weigh-in to LATEST_JSON (tailnet-only launcher
server), which an iPhone Shortcut reads to log weight into Apple Health.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "training"))
import config  # noqa: E402

ROOT = config.DATA
ENV = config.HA_ENV_FILE
CSV_PATH = config.BODY_CSV
WEIGHT_ENTITY = config.setting("scale", "weight_entity", "")
SOURCE = "ha-scale"
LOOKBACK_DAYS = 3
RESTART_GRACE = timedelta(seconds=90)
LATEST_JSON = config.path_setting("scale", "latest_json")   # optional; None = off
WEIGH_WINDOW = timedelta(minutes=10)
PLAUSIBLE_LB = tuple(config.setting("scale", "plausible_lb", [80.0, 400.0]))
KG_TO_LB = 2.2046226218


def load_env() -> tuple[str, str]:
    vals = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals["HA_URL"].rstrip("/"), vals["HA_TOKEN"]


def ha_get(base: str, token: str, path: str):
    req = urllib.request.Request(base + path, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def restart_windows(base: str, token: str, start: str, end: str) -> list[tuple[datetime, datetime]]:
    """(stopped, started + RESTART_GRACE) for each HA restart in the window."""
    q = urllib.parse.urlencode({"end_time": end})
    events = ha_get(base, token, f"/api/logbook/{urllib.parse.quote(start)}?{q}")
    out, stopped = [], None
    for e in events:
        if e.get("domain") != "homeassistant":
            continue
        when = datetime.fromisoformat(e["when"])
        msg = e.get("message") or ""
        if "stopped" in msg:
            stopped = when
        elif "started" in msg:
            out.append((stopped or when - RESTART_GRACE, when + RESTART_GRACE))
            stopped = None
    return out


def fetch(days: int) -> list[tuple[datetime, float]]:
    """(local datetime, lb) for every plausible reading in the window.

    end_time is passed EXPLICITLY: /api/history/period defaults it to
    start + 24 h, which silently truncates a longer lookback.
    """
    base, token = load_env()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()
    # skip_initial_state: otherwise HA prepends the state as of `start`,
    # stamped with `start` itself -- a fake reading whose time moves every run.
    q = urllib.parse.urlencode({"filter_entity_id": WEIGHT_ENTITY,
                                "end_time": now.isoformat(),
                                "skip_initial_state": ""})
    hist = ha_get(base, token, f"/api/history/period/{urllib.parse.quote(start)}?{q}")
    restarts = restart_windows(base, token, start, now.isoformat())
    states = hist[0] if hist else []
    unit = next((s.get("attributes", {}).get("unit_of_measurement")
                 for s in states if s.get("attributes")), "lb")
    out = []
    for s in states:
        try:
            v = float(s["state"])
        except (TypeError, ValueError):      # unavailable / unknown
            continue
        lb = v * KG_TO_LB if unit == "kg" else v
        if not PLAUSIBLE_LB[0] <= lb <= PLAUSIBLE_LB[1]:
            continue
        t = datetime.fromisoformat(s["last_changed"]).astimezone()   # local time
        if any(a <= t <= b for a, b in restarts):   # restored, not weighed
            continue
        out.append((t, lb))
    return sorted(out)


def daily(readings: list[tuple[datetime, float]]) -> dict[str, tuple[datetime, float]]:
    """day -> (time of the first weigh-in, median lb within WEIGH_WINDOW of it)."""
    by_day: dict[str, list[tuple[datetime, float]]] = {}
    for t, lb in readings:
        by_day.setdefault(t.date().isoformat(), []).append((t, lb))
    out = {}
    for day, rs in by_day.items():
        first = rs[0][0]
        out[day] = (first, statistics.median(lb for t, lb in rs if t - first <= WEIGH_WINDOW))
    return out


def write_latest(days: dict[str, tuple[datetime, float]]) -> None:
    """Latest weigh-in for the Apple Health Shortcut. `id` is what the phone
    dedupes on, so it must stay stable for a given weigh-in."""
    if not days:
        return
    day = max(days)
    t, lb = days[day]
    if LATEST_JSON is None:
        return
    LATEST_JSON.parent.mkdir(exist_ok=True)
    tmp = LATEST_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "id": t.isoformat(timespec="seconds"),
        "date": day,
        "measured_at": t.isoformat(timespec="seconds"),
        "weight_lb": round(lb, 1),
    }) + "\n")
    tmp.replace(LATEST_JSON)


def merge(days: dict[str, float]) -> int:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        rows = {r["date"]: r for r in reader if r.get("date")}
    for day, (_, lb) in days.items():
        row = rows.setdefault(day, {c: "" for c in cols} | {"date": day})
        row["weight_lb"] = f"{lb:.1f}"
        srcs = set(filter(None, (row.get("source") or "").split("+")))
        srcs.add(SOURCE)
        row["source"] = "+".join(sorted(srcs))
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for d in sorted(rows):
        w.writerow({c: rows[d].get(c, "") for c in cols})
    # Only touch the file when the data changed: this runs every 15 min, and
    # serve_dashboard.py rebuilds on any source mtime newer than the HTML, so an
    # unconditional rewrite made every page load rebuild and stamp "generated" = now.
    if buf.getvalue() != CSV_PATH.read_text(newline=""):
        with CSV_PATH.open("w", newline="") as f:
            f.write(buf.getvalue())
    return len(days)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    args = ap.parse_args()
    days = daily(fetch(args.days))
    n = merge(days)
    write_latest(days)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"{stamp} sync_scale: {n} day(s) from HA "
          + ", ".join(f"{d}={v:.1f}" for d, (_, v) in sorted(days.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
