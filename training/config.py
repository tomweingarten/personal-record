"""Where Personal Record finds your data, secrets and settings.

Code and data live apart: this repository holds only code. Everything personal
(workout history, body measurements, food, heart rate, the built dashboard, API
keys) lives in the data directory, which is never committed.

Data directory, first match wins:
  1. $PR_DATA_DIR
  2. data_dir in settings.toml
  3. ./data next to this repository

Settings come from settings.toml in the repository root (gitignored; copy
settings.example.toml), or the file named by $PR_SETTINGS.

Layout inside the data directory:
  training/            workouts.csv, exercises.csv, sets.csv (sync_hevy.py),
                       routine_snapshots/, routine_state.json (push_targets.py)
  body_composition.csv scale and tape readings (sync_scale.py, sync_tape.py, sync_renpho.py)
  nutrition/           MacroFactor exports and apple_health_daily.csv (nutrition_hook.py)
  cardio/              hr/ cache, daily.csv, sessions.csv, recovery.csv, overrides.csv
  dashboard/           recomp.html and weekly/ cards (the built output)
  logs/                lock and state files
  .env                 HEVY_API_KEY, NUTRITION_HOOK_TOKEN, GOOGLE_HEALTH_CLIENT_ID/SECRET, ...
  .ha.env              HA_URL, HA_TOKEN (Home Assistant scale and tape)
  .renpho.env          Renpho cloud login (sync_renpho.py)
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_settings_file = Path(os.environ.get("PR_SETTINGS") or REPO / "settings.toml").expanduser()
SETTINGS: dict = tomllib.loads(_settings_file.read_text()) if _settings_file.exists() else {}


def setting(section: str, key: str, default=None):
    """settings.toml value, e.g. setting("body", "target_weight_lb", 150.0)."""
    return SETTINGS.get(section, {}).get(key, default)


def path_setting(section: str, key: str) -> Path | None:
    v = setting(section, key)
    return Path(v).expanduser() if v else None


DATA = Path(os.environ.get("PR_DATA_DIR") or SETTINGS.get("data_dir") or REPO / "data").expanduser()
TRAINING = DATA / "training"
BODY_CSV = DATA / "body_composition.csv"
NUTRITION = DATA / "nutrition"
CARDIO = DATA / "cardio"
DASHBOARD = DATA / "dashboard"
LOGS = DATA / "logs"
ENV_FILE = DATA / ".env"
HA_ENV_FILE = DATA / ".ha.env"
RENPHO_ENV_FILE = DATA / ".renpho.env"
ASSETS = REPO / "assets"


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments ignored. Missing file -> {}."""
    vals: dict[str, str] = {}
    if not path.exists():
        return vals
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals
