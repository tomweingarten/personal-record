#!/usr/bin/env python3
"""Serve the recomp dashboard on the tailnet — always reflects the latest data.

Binds the tailscale IP only (tailnet-private, like sleep.service) and serves
dashboard/recomp.html at `/`. Before serving, it rebuilds the dashboard iff a
source CSV is newer than the built HTML — so a fresh sync (or a mid-day tape
measurement) shows up on next load, without a wasteful rebuild every request.

Run it as a service: see deploy/personal-record-dashboard.service. Stdlib only.
"""
from __future__ import annotations
import http.server
import socketserver
import subprocess
import sys
from glob import glob
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "training"))
import config  # noqa: E402

REPO = config.REPO
DATA = config.DATA
DASH = config.DASHBOARD
HTML = DASH / "recomp.html"
# Bind to localhost or a private address (e.g. your Tailscale IP). Never 0.0.0.0: it is health data.
HOST = config.setting("dashboard", "host", "127.0.0.1")
PORT = int(config.setting("dashboard", "port", 8002))

# source files whose changes should trigger a rebuild
SOURCES = ["body_composition.csv", "training/workouts.csv", "training/sets.csv",
           "training/exercises.csv", "cardio/daily.csv", "cardio/sessions.csv",
           "cardio/recovery.csv"]


def _sources() -> list[Path]:
    paths = [DATA / s for s in SOURCES]
    paths += [Path(p) for p in glob(str(DATA / "nutrition" / "macrofactor_*_daily*.csv"))]
    paths.append(DATA / "nutrition" / "apple_health_daily.csv")   # nutrition_hook.py push
    return [p for p in paths if p.exists()]


def rebuild_if_stale() -> None:
    """Rebuild the dashboard if any source CSV is newer than the built HTML."""
    try:
        if HTML.exists():
            built = HTML.stat().st_mtime
            if not any(p.stat().st_mtime > built for p in _sources()):
                return
        subprocess.run([sys.executable, "training/build_dashboard.py"],
                       cwd=REPO, timeout=60, capture_output=True)
    except Exception:
        pass  # serve whatever HTML exists rather than 500


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(DASH), **k)

    def do_GET(self):
        if self.path in ("/", "/recomp", "/recomp/"):
            rebuild_if_stale()
            self.path = "/recomp.html"
        return super().do_GET()

    def log_message(self, *a):  # keep it quiet
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as httpd:
        print(f"serving the dashboard on http://{HOST}:{PORT}/")
        httpd.serve_forever()
