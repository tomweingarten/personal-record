#!/usr/bin/env python3
"""Daily calories + protein from Apple Health, pushed by an iPhone Shortcut.

MacroFactor writes every food entry to Apple Health as Dietary Energy / Protein
samples. A Shortcut on the phone finds the last week of those samples and POSTs
them to https://<your-domain>/hooks/nutrition, which hevy_hook.py routes here. This replaced hand-sent MacroFactor exports as the
day-to-day nutrition source on 2026-10-21: the exports only came in when Tom
remembered to send one, and the dashboard's protein section had been stuck on
the 09-18 export for a month.

Body: one sample per line, `kind|start|value`, either raw or as {"lines": "..."}
JSON (both are easy to build in Shortcuts):

    energy|2026-10-20T08:12:00-04:00|412
    protein|2026-10-20T08:12:00-04:00|31.5

`kind` is energy (kcal) or protein (g). `start` is the sample's start date; the
local calendar day is its first 10 characters when ISO 8601, and the Shortcuts
default "Oct 20, 2026 at 8:12 AM" is accepted too. Values may carry thousands
separators or a unit suffix.

Each POST REPLACES the stored totals for every day it mentions, so re-sending the
same week is idempotent and later edits in MacroFactor overwrite earlier pushes.
A day absent from the payload is left alone. The current day is stored but is
partial; build_dashboard.load_nutrition() ignores today.

What Apple Health does NOT carry: MacroFactor's trend weight, expenditure and
protein target. The dashboard derives trend weight from the HA scale and carries
the last exported protein target forward; expenditure needs an occasional export.

    python3 training/nutrition_hook.py --check   # nightly: ntfy if pushes stopped
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import config

ROOT = config.DATA
CSV_PATH = config.NUTRITION / "apple_health_daily.csv"
COLS = ["date", "kcal", "protein_g", "energy_samples", "protein_samples", "received_at"]
KINDS = {"energy": "kcal", "dietaryenergy": "kcal", "calories": "kcal",
         "protein": "protein_g"}
STALE_DAYS = 2   # --check alerts when no push has arrived for this long
NTFY = config.setting("alerts", "ntfy_url")   # optional; None = print only
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _day(s: str) -> date | None:
    s = s.strip().replace(" ", " ").replace("\xa0", " ")
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    head = s.split(" at ")[0].strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _num(s: str) -> float | None:
    m = _NUM.search(s.replace(",", ""))
    return float(m.group()) if m else None


def parse(raw: bytes) -> tuple[dict[date, dict], list[str]]:
    """-> ({day: {"kcal": sum, "protein_g": sum, "energy_samples": n, ...}}, bad lines)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            text = body.get("lines") or ""
            if isinstance(text, list):
                text = "\n".join(map(str, text))
    except ValueError:
        pass                                  # plain-text body
    days: dict[date, dict] = {}
    bad: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        kind = KINDS.get(re.sub(r"[^a-z]", "", parts[0].lower())) if parts else None
        d = _day(parts[1]) if len(parts) == 3 else None
        v = _num(parts[2]) if len(parts) == 3 else None
        if kind is None or d is None or v is None:
            bad.append(line[:120])
            continue
        r = days.setdefault(d, {"kcal": 0.0, "protein_g": 0.0,
                                "energy_samples": 0, "protein_samples": 0})
        r[kind] += v
        r["energy_samples" if kind == "kcal" else "protein_samples"] += 1
    return days, bad


def load() -> dict[str, dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open() as f:
        return {r["date"]: r for r in csv.DictReader(f) if r.get("date")}


def merge(days: dict[date, dict]) -> None:
    rows = load()
    now = datetime.now().isoformat(timespec="seconds")
    # A kind absent from the WHOLE payload (one Find step failed) keeps its stored days.
    sent = {k for k in ("energy", "protein")
            if any(r[f"{k}_samples"] for r in days.values())}
    for d, r in days.items():
        old = rows.get(d.isoformat(), {})
        row = {"date": d.isoformat(), "received_at": now}
        for kind, col, fmt in (("energy", "kcal", "{:.0f}"), ("protein", "protein_g", "{:.1f}")):
            if kind in sent:
                # no samples that day stays blank, not 0: "not logged" != "ate nothing"
                row[col] = fmt.format(r[col]) if r[f"{kind}_samples"] else ""
                row[f"{kind}_samples"] = r[f"{kind}_samples"]
            else:
                row[col] = old.get(col, "")
                row[f"{kind}_samples"] = old.get(f"{kind}_samples", "")
        rows[d.isoformat()] = row
    tmp = CSV_PATH.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for k in sorted(rows):
            w.writerow({c: rows[k].get(c, "") for c in COLS})
    tmp.replace(CSV_PATH)


def ingest(raw: bytes) -> str:
    """Parse + store one push; returns the reply shown in the Shortcut."""
    days, bad = parse(raw)
    if days:
        merge(days)
    span = f"{min(days)}..{max(days)}" if days else "nothing"
    msg = f"stored {len(days)} day(s) {span}"
    if bad:
        msg += f"; {len(bad)} unreadable line(s), first: {bad[0]!r}"
    return msg


def check() -> int:
    """Nightly: alert once a day while pushes are missing (the failure is otherwise silent)."""
    rows = load()
    last = max((r.get("received_at") or "" for r in rows.values()), default="")
    when = datetime.fromisoformat(last) if last else None
    if when and datetime.now() - when < timedelta(days=STALE_DAYS):
        print(f"{datetime.now():%Y-%m-%d %H:%M} nutrition push ok, last {last}")
        return 0
    msg = (f"No Apple Health nutrition push since {last or 'ever'}. Run the "
           "Apple Health Shortcut on the iPhone (it needs the phone unlocked).")
    print(f"{datetime.now():%Y-%m-%d %H:%M} STALE: {msg}")
    if not NTFY:
        return 0
    req = urllib.request.Request(NTFY, data=msg.encode(), headers={
        "Title": "Nutrition sync stale", "Tags": "fork_and_knife"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as e:
        print(f"  ntfy failed: {e}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("file", nargs="?", help="ingest a saved payload by hand")
    a = ap.parse_args()
    if a.check:
        raise SystemExit(check())
    if a.file:
        print(ingest(Path(a.file).read_bytes()))
