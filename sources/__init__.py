"""Heart-rate and recovery sources for the cardio panel.

A source is a module in this folder with a ``Source`` class offering either or
both of:

    intraday_hr(day: date) -> list[tuple[datetime, int]]
        1-minute heart rate for one LOCAL calendar day, as (naive local time, bpm).
    recovery(days: int) -> list[dict]
        nightly rows {"date": "YYYY-MM-DD", "resting_hr", "hrv_rmssd", "resp_rate"};
        any value may be None.

settings.toml picks them:

    [cardio]
    hr_source = "google_health"         # module name in sources/
    recovery_source = "google_health"   # optional; may differ from hr_source

To add a provider (Garmin, Oura, Strava, a CSV export...), copy
google_health.py's ``Source`` shape into a new module and name it in settings.
Nothing else in the code needs to change.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Sources import `config`, which lives in training/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training"))


def load(name: str):
    """Instantiate sources/<name>.py's Source."""
    try:
        mod = importlib.import_module(f"sources.{name}")
    except ModuleNotFoundError as e:
        raise SystemExit(f"Unknown cardio source {name!r}: no sources/{name}.py ({e})")
    return mod.Source()
