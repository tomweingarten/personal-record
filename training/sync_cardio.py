#!/usr/bin/env python3
"""
Cardio tracker: minute heart rate → cardio minutes, sessions and recovery trends.

Feeds the "Am I getting my cardio in?" section of the recomp dashboard. The
targets it grades against are in programming.md, *Cardio — for stress first*:
one moderate session (30–40 min, 110–130 bpm) and one 4x4 (intervals reaching
152+) per Mon–Sun week, plus 10–15 min walk boosts as a free extra.

Writes into <data dir>/cardio/:
  - hr/YYYY-MM-DD.csv — cached 1-minute HR (HH:MM,bpm). Bulky and re-fetchable.
  - daily.csv         — per day: moderate / hard minutes outside lifting
  - sessions.csv      — detected sessions: moderate, 4x4, walk boost
  - recovery.csv      — nightly resting HR, HRV and deep-sleep breathing rate,
                        from the recovery source (the "is it working" panel)
  - overrides.csv     — HAND-EDITED. Corrections to detection, see apply_overrides.

Lifting is masked out. A Hevy workout window is lifting, minus any logged
cardio exercise (bike, treadmill, elliptical, stairs): that exercise's duration
is carved off the end (or start) of the window, depending on where it sits in
the workout. Without the mask, lifting spikes (100–126 bpm on a Pull day) would
count as moderate cardio.

Heart rate and recovery come from the sources named in settings.toml [cardio]
(see sources/__init__.py). Run with a Python that has what the source needs:
  python3 training/sync_cardio.py [--days N]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import config  # noqa: E402

sys.path.insert(0, str(config.REPO))
import sources  # noqa: E402

ROOT = config.REPO
OUT = config.CARDIO
HR_DIR = OUT / "hr"

# Bands (programming.md heart-rate anchors). "Moderate" is counted from 105 to
# match the weekly-minutes metric there; sessions are judged on 110–130.
MOD_LO = 105
HARD_LO = 132          # above moderate: 4x4 territory
INTERVAL_HIT = 152     # a 4x4 interval should reach this by minute 2

MOD_SESSION_MIN = 20   # sustained block this long = a moderate session (target 30–40)
MOD_SESSION_AVG = 110  # ...averaging at least this; a 20-min brisk walk at 108 is a boost
BOOST_MIN = 10         # 10–19 min sustained = a walk boost
GAP_MERGE = 3          # dips below MOD_LO this short don't end a block

CARDIO_WORDS = ("cycling", "bike", "treadmill", "elliptical", "stair", "rowing",
                "rower", "spinning", "running", "walking")

DEFAULT_DAYS = 3       # today + 2 back: Google Health can backfill a late watch sync


# ---------------------------------------------------------------- heart rate

def fetch_hr(src, day: date) -> list[tuple[datetime, int]]:
    rows = src.intraday_hr(day)
    HR_DIR.mkdir(parents=True, exist_ok=True)
    (HR_DIR / f"{day}.csv").write_text("".join(f"{m:%H:%M},{b}\n" for m, b in rows))
    return rows


def load_hr(day: date) -> list[tuple[datetime, int]]:
    p = HR_DIR / f"{day}.csv"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        hm, bpm = line.split(",")
        h, m = hm.split(":")
        out.append((datetime(day.year, day.month, day.day, int(h), int(m)), int(bpm)))
    return out


# ---------------------------------------------------------------- Hevy masking

def hevy_windows() -> dict[date, list[dict]]:
    """Per local day: [{start, end, title, lift_start, lift_end, cardio}] with the
    logged-cardio portion carved off the lifting window."""
    tdir = config.TRAINING
    ex_title = {}
    ex_order = {}
    with (tdir / "exercises.csv").open() as f:
        for r in csv.DictReader(f):
            ex_title[(r["workout_id"], r["order"])] = r["title"]
            ex_order.setdefault(r["workout_id"], []).append(int(r["order"]))
    cardio_secs: dict[str, dict[int, int]] = {}
    with (tdir / "sets.csv").open() as f:
        for r in csv.DictReader(f):
            t = ex_title.get((r["workout_id"], r["exercise_order"]), "").lower()
            if any(w in t for w in CARDIO_WORDS) and r["duration_seconds"]:
                d = cardio_secs.setdefault(r["workout_id"], {})
                k = int(r["exercise_order"])
                d[k] = d.get(k, 0) + int(float(r["duration_seconds"]))
    out: dict[date, list[dict]] = {}
    with (tdir / "workouts.csv").open() as f:
        for r in csv.DictReader(f):
            if not r["start_time"] or not r["end_time"]:
                continue
            s = datetime.fromisoformat(r["start_time"]).astimezone().replace(tzinfo=None)
            e = datetime.fromisoformat(r["end_time"]).astimezone().replace(tzinfo=None)
            ls, le, cardio = s, e, None
            orders = sorted(ex_order.get(r["id"], []))
            for k, secs in (cardio_secs.get(r["id"]) or {}).items():
                dur = timedelta(seconds=secs)
                if orders and k == orders[-1]:
                    le = e - dur
                    cardio = (le, e)
                elif orders and k == orders[0]:
                    ls = s + dur
                    cardio = (s, ls)
            out.setdefault(s.date(), []).append(
                {"start": s, "end": e, "title": r["title"], "lift_start": ls,
                 "lift_end": le, "cardio": cardio})
    return out


def lift_kind(title: str) -> str:
    t = title.lower()
    if "don't push" in t or "dont push" in t:
        return "Pull"
    if "push" in t:
        return "Push"
    if "home" in t:
        return "Home"
    if "travel" in t or "hotel" in t:
        return "Travel"
    return "Lift"


# ---------------------------------------------------------------- detection

def blocks(series: list[tuple[datetime, int]]) -> list[list[tuple[datetime, int]]]:
    """Sustained runs at >= MOD_LO, bridging dips of <= GAP_MERGE minutes."""
    out, cur, gap = [], [], 0
    prev_t = None
    for t, b in series:
        if prev_t is not None and (t - prev_t) > timedelta(minutes=GAP_MERGE + 1):
            if cur:
                out.append(cur)
            cur, gap = [], 0
        prev_t = t
        if b >= MOD_LO:
            cur.append((t, b))
            gap = 0
        elif cur:
            gap += 1
            if gap > GAP_MERGE:
                out.append(cur)
                cur, gap = [], 0
    if cur:
        out.append(cur)
    return out


def interval_peaks(block: list[tuple[datetime, int]]) -> list[int]:
    """Peaks of an interval session: local maxima >= 128 that stand >= 10 bpm above
    the low point within 4 min on at least one side, >= 5 min apart."""
    vals = [b for _, b in block]
    peaks, last_i = [], -99
    for i, v in enumerate(vals):
        if v < 128:
            continue
        lo, hi = max(0, i - 3), min(len(vals), i + 4)
        if v < max(vals[lo:hi]):
            continue
        left = min(vals[max(0, i - 4):i] or [v])
        right = min(vals[i + 1:i + 5] or [v])
        if v - left < 10 and v - right < 10:
            continue
        if i - last_i < 5:
            if v > vals[last_i]:
                peaks[-1], last_i = v, i
            continue
        peaks.append(v)
        last_i = i
    return peaks


def detect(day: date, hr, windows) -> tuple[dict, list[dict]]:
    lifts = windows.get(day, [])

    def lifting(t):
        return any(w["lift_start"] <= t < w["lift_end"] for w in lifts)

    def in_logged_cardio(t):
        return any(w["cardio"] and w["cardio"][0] <= t < w["cardio"][1] for w in lifts)

    free = [(t, b) for t, b in hr if not lifting(t)]
    mod = sum(1 for _, b in free if MOD_LO <= b < HARD_LO)
    hard = sum(1 for _, b in free if b >= HARD_LO)
    daily = {"date": day.isoformat(), "mod_min": mod, "hard_min": hard,
             "hr_min": len(hr), "lift_min": len(hr) - len(free),
             "lifts": "+".join(lift_kind(w["title"]) for w in lifts)}

    sessions = []
    for blk in blocks(free):
        # Length is the block's span: short dips inside a session still count.
        n = int((blk[-1][0] - blk[0][0]).total_seconds() // 60) + 1
        if n < BOOST_MIN:
            continue
        vals = [b for _, b in blk]
        peaks = interval_peaks(blk)
        logged = sum(in_logged_cardio(t) for t, _ in blk) >= len(blk) / 2
        s = {"date": day.isoformat(), "start": f"{blk[0][0]:%H:%M}",
             "end": f"{blk[-1][0] + timedelta(minutes=1):%H:%M}", "minutes": n,
             "avg_hr": round(sum(vals) / len(vals)), "peak_hr": max(vals),
             "hard_min": sum(v >= HARD_LO for v in vals),
             "peaks": " ".join(map(str, peaks)), "source": "hevy+hr" if logged else "hr"}
        if len(peaks) >= 3:
            hits = sum(p >= INTERVAL_HIT for p in peaks)
            s |= {"kind": "4x4", "grade": "hit" if hits >= 3 else "too easy"}
        elif n >= MOD_SESSION_MIN and sum(vals) / len(vals) >= MOD_SESSION_AVG:
            s |= {"kind": "moderate", "grade": "hit"}
        else:
            s |= {"kind": "boost", "grade": "hit"}
        sessions.append(s)
    return daily, sessions


# ---------------------------------------------------------------- overrides

OVERRIDE_HELP = """\
# Hand corrections to cardio detection. One row per fix; newest wins.
# kind:  4x4 | moderate | boost | none
# grade: hit | too easy   (blank = keep detected grade)
# start: HH:MM of the detected session to change, or blank to ADD a session
#        (minutes then required). kind=none with a start deletes that session.
# Typical: a bike 4x4 that felt hard but the wrist read low ->
#   2026-10-31,08:10,4x4,hit,,felt hard; wrist reads low on the bike
"""
OVERRIDE_COLS = ["date", "start", "kind", "grade", "minutes", "note"]


def apply_overrides(sessions: list[dict]) -> list[dict]:
    p = OUT / "overrides.csv"
    if not p.exists():
        p.write_text(OVERRIDE_HELP + ",".join(OVERRIDE_COLS) + "\n")
        return sessions
    lines = [l for l in p.read_text().splitlines() if l and not l.startswith("#")]
    for o in csv.DictReader(lines):
        d, st = o.get("date", ""), (o.get("start") or "").strip()
        if not d:
            continue
        match = [s for s in sessions if s["date"] == d and st and s["start"] == st]
        if match:
            s = match[0]
            if o["kind"] == "none":
                sessions.remove(s)
                continue
            s["kind"] = o["kind"] or s["kind"]
            s["grade"] = o["grade"] or s["grade"]
            s["source"] = "override"
            s["note"] = o.get("note", "")
        elif not st and o.get("minutes"):
            sessions.append({"date": d, "start": "", "end": "", "minutes": int(o["minutes"]),
                             "avg_hr": "", "peak_hr": "", "hard_min": "", "peaks": "",
                             "kind": o["kind"], "grade": o["grade"] or "hit",
                             "source": "override", "note": o.get("note", "")})
    return sessions


# ---------------------------------------------------------------- recovery

def fetch_recovery(days: int = 400) -> list[dict]:
    name = config.setting("cardio", "recovery_source")
    return sources.load(name).recovery(days) if name else []


# ---------------------------------------------------------------- io

def write_if_changed(path: Path, cols: list[str], rows: list[dict]) -> bool:
    """Only touch the file when content changed: serve_dashboard.py rebuilds on
    any source mtime newer than the HTML (see the sync_scale fix, 2026-10-27)."""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    if path.exists() and path.read_text(newline="") == buf.getvalue():
        return False
    path.write_text(buf.getvalue(), newline="")
    return True


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


DAILY_COLS = ["date", "mod_min", "hard_min", "hr_min", "lift_min", "lifts"]
SESSION_COLS = ["date", "start", "end", "kind", "grade", "minutes", "avg_hr", "peak_hr",
                "hard_min", "peaks", "source", "note"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="re-fetch this many days back from today (older days come from the cache)")
    ap.add_argument("--no-fetch", action="store_true", help="recompute from cached HR only")
    ap.add_argument("--no-build", action="store_true", help="don't rebuild the dashboard")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    today = date.today()

    if not args.no_fetch:
        name = config.setting("cardio", "hr_source")
        if not name:
            raise SystemExit("no heart-rate source: set [cardio] hr_source in settings.toml")
        src = sources.load(name)
        for i in range(args.days - 1, -1, -1):
            fetch_hr(src, today - timedelta(days=i))

    windows = hevy_windows()
    cached = sorted(date.fromisoformat(p.stem) for p in HR_DIR.glob("*.csv"))
    daily, sessions = [], []
    for d in cached:
        hr = load_hr(d)
        if not hr:
            continue
        dd, ss = detect(d, hr, windows)
        daily.append(dd)
        sessions += ss
    sessions = apply_overrides(sessions)
    sessions.sort(key=lambda s: (s["date"], s["start"]))

    changed = []
    if write_if_changed(OUT / "daily.csv", DAILY_COLS, daily):
        changed.append("daily")
    if write_if_changed(OUT / "sessions.csv", SESSION_COLS, sessions):
        changed.append("sessions")
    if not args.no_fetch:
        try:
            rec = fetch_recovery()
            if rec and write_if_changed(OUT / "recovery.csv",
                                ["date", "resting_hr", "hrv_rmssd", "resp_rate"], rec):
                changed.append("recovery")
        except Exception as e:  # source down: keep the last copy
            print(f"recovery fetch failed: {e}")

    if changed and not args.no_build:
        # serve_dashboard.py only learns about new SOURCES on a service restart,
        # so rebuild here rather than rely on it.
        import subprocess
        subprocess.run([sys.executable, "training/build_dashboard.py"], cwd=ROOT,
                       timeout=120, capture_output=True)

    wk = [s for s in sessions if date.fromisoformat(s["date"]) >= today - timedelta(days=today.weekday())]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"{stamp} sync_cardio: {len(cached)} day(s) cached, "
          f"changed: {', '.join(changed) or 'nothing'}; this week: "
          + (", ".join(f"{s['date'][5:]} {s['kind']} {s['minutes']}m {s['grade']}" for s in wk) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
