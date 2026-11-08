#!/usr/bin/env python3
"""tape_watch.py - rebuild the recomp dashboard within a minute of a new tape session.

Runs every minute from the apps crontab (2026-10-31). Reads one HA entity,
sensor.renpho_tape_last_session, and compares its session_id with the one it saw
last time (logs/tape_watch.state). Only when it changes does it run
sync_tape.py -> training/build_dashboard.py, under the same flock as the Hevy chain
and the nightly cron so two dashboard builds never overlap. Quiet when nothing
changed; HA being down is not an error (the 02:20 cron is the fallback).

A poll rather than a push from HA: a push needs a new LAN listener or Caddy route
(root), and a 1-minute lag doesn't matter for a body-tape sitting.
Stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import sync_tape

import config  # noqa: E402  (sync_tape put training/ on sys.path)

ROOT = config.REPO
config.LOGS.mkdir(parents=True, exist_ok=True)
STATE = config.LOGS / "tape_watch.state"
LOCK = config.LOGS / "hevy-chain.lock"
ENTITY = config.setting("tape", "session_entity", "sensor.renpho_tape_last_session")


def current_session() -> str | None:
    base, token = sync_tape.load_env()
    req = urllib.request.Request(f"{base}/api/states/{ENTITY}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return (json.load(r).get("attributes") or {}).get("session_id")
    except OSError:
        return None   # HA restarting or unreachable: try again next minute


def main() -> int:
    sid = current_session()
    if not sid:
        return 0
    seen = STATE.read_text().strip() if STATE.exists() else None
    if sid == seen:
        return 0
    if seen is None:   # first run: record, don't rebuild
        STATE.write_text(sid)
        return 0
    stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
    with LOCK.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        for cmd in (["sync_tape.py"], ["training/build_dashboard.py"]):
            r = subprocess.run([sys.executable, *cmd], cwd=ROOT, capture_output=True, text=True)
            if r.returncode:
                print(f"{stamp} new session {sid}: {cmd[0]} FAILED\n{r.stderr[-1500:]}")
                return 1   # state not advanced, so the next minute retries
    STATE.write_text(sid)
    print(f"{stamp} new session {sid}: synced + dashboard rebuilt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
