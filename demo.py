#!/usr/bin/env python3
"""Build the dashboard from the bundled sample data, so you can see it before connecting anything.

The sample is 10 fixed weeks of an invented lifter. The dashboard works relative
to today, so this copies sample-data/ to demo-data/ (gitignored) with every date
shifted to end today, then builds demo-data/dashboard/recomp.html.

    python3 demo.py            # build, then open the printed path in a browser
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = REPO / "sample-data"
OUT = REPO / "demo-data"
SAMPLE_END = date(2026, 6, 28)
SHIFT = timedelta(days=(date.today() - SAMPLE_END).days)

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def shift(v: str) -> str:
    if ISO.match(v):
        if len(v) == 10:
            return (date.fromisoformat(v) + SHIFT).isoformat()
        return (datetime.fromisoformat(v) + SHIFT).isoformat()
    m = US.match(v)
    if m:
        d = date(int(m[3]), int(m[1]), int(m[2])) + SHIFT
        return f"{d.month}/{d.day}/{d.year}"
    return v


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    for src in SRC.rglob("*.csv"):
        rel = src.relative_to(SRC)
        if src.parent.name == "nutrition":       # export files are named by their end date
            rel = rel.with_name(re.sub(r"\d{4}-\d{2}-\d{2}", (SAMPLE_END + SHIFT).isoformat(), rel.name))
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with src.open() as f, dst.open("w", newline="") as g:
            r = csv.reader(f)
            w = csv.writer(g)
            w.writerow(next(r))
            for row in r:
                w.writerow([shift(c) for c in row])
    env = {**os.environ, "PR_DATA_DIR": str(OUT), "PR_SETTINGS": os.devnull}
    rc = subprocess.run([sys.executable, "training/build_dashboard.py"], cwd=REPO, env=env).returncode
    if rc == 0:
        print(f"\nOpen {OUT / 'dashboard' / 'recomp.html'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
