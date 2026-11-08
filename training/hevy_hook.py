#!/usr/bin/env python3
"""Hevy webhook receiver: run the workout chain as soon as a workout is saved.

Hevy POSTs {"workoutId": ...} to the subscribed URL whenever a NEW workout is created
(seen on the first real ping, 2026-10-19; the {"id", "payload": {"workoutId"}} shape we
first assumed is still accepted) (not on edits or deletes -- the nightly 02:00 cron still covers
those). Subscription: POST https://api.hevyapp.com/v1/webhook-subscription
{"url": ..., "authToken": ...}; Hevy sends authToken back in the Authorization header.

Hevy must reach it over HTTPS: put it behind a reverse proxy or tunnel (e.g. Caddy or
Cloudflare Tunnel forwarding https://your-domain/hooks/hevy -> 127.0.0.1:PORT). Run it
as a service (see deploy/).

On a valid ping we answer 200 at once, wait SETTLE_S, then run the same chain as the
02:00 cron (sync_hevy -> push_targets) plus the dashboard rebuild, under the same flock
as the cron line so the two never overlap. Pings that arrive while a run is pending or
running fold into one more run. Stdlib only.

Also serves POST /hooks/nutrition (2026-10-21): an iPhone Shortcut's
Apple Health calories/protein push, handled by nutrition_hook.py under its own token
(NUTRITION_HOOK_TOKEN, so the phone never holds the Hevy one).
"""
from __future__ import annotations

import fcntl
import hmac
import json
import subprocess
import threading
import time
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import nutrition_hook

ROOT = config.REPO
PORT = int(config.setting("hevy", "webhook_port", 8790))
SETTLE_S = 20  # let Hevy finish writing the workout before we fetch it
config.LOGS.mkdir(parents=True, exist_ok=True)
LOCK = config.LOGS / "hevy-chain.lock"  # shared with the nightly cron line (flock)
LOG = config.LOGS / "hevy_hook.log"
MAX_BODY = 64 * 1024
MAX_NUTRITION_BODY = 512 * 1024  # a week of per-food samples is ~20-40 KB
PY = sys.executable
CARDIO_PY = config.setting("cardio", "python") or PY   # the cardio source may need a venv (requests)
CHAIN = [  # (argv, log, fatal) -- a fatal step stops the chain, like the cron's &&
    ([PY, "training/sync_hevy.py"], str(config.TRAINING / "sync.log"), True),
    ([PY, "training/push_targets.py"], str(config.TRAINING / "push_targets.log"), True),
    # cardio needs the fresh Hevy window to mask lifting, else a 4x4 at the end of a
    # session waits for the hourly cron and the nightly rebuild. Non-fatal:
    # a heart-rate source hiccup must not block the dashboard.
    *([(["/usr/bin/flock", str(config.LOGS / "cardio.lock"), CARDIO_PY,
         "training/sync_cardio.py", "--days", "3"], str(config.LOGS / "sync_cardio.log"), False)]
      if config.setting("cardio", "hr_source") else []),
    ([PY, "training/build_dashboard.py"], str(config.LOGS / "dashboard.log"), True),
]


def env(key: str) -> str:
    v = config.read_env().get(key)
    if not v:
        raise SystemExit(f"{key} missing from {config.ENV_FILE}")
    return v


TOKEN = env("HEVY_WEBHOOK_TOKEN")
NUTRITION_TOKEN = env("NUTRITION_HOOK_TOKEN")


def authorized(auth: str, token: str) -> bool:
    return hmac.compare_digest(auth, token) or hmac.compare_digest(auth, f"Bearer {token}")


def log(msg: str) -> None:
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


class Runner:
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.wanted = False
        threading.Thread(target=self._loop, daemon=True).start()

    def kick(self) -> None:
        with self.cv:
            self.wanted = True
            self.cv.notify()

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.wanted:
                    self.cv.wait()
            time.sleep(SETTLE_S)
            with self.cv:
                self.wanted = False  # pings from here on trigger one more run
            self._run()

    def _run(self) -> None:
        t0 = time.monotonic()
        with LOCK.open("w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            for argv, out, fatal in CHAIN:
                with (ROOT / out).open("a") as f:
                    rc = subprocess.run(argv, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, timeout=900).returncode
                step = next(a for a in argv if a.endswith(".py"))
                if rc and fatal:
                    log(f"chain FAILED at {step} (exit {rc}); see {out}")
                    return
                if rc:
                    log(f"chain step {step} failed (exit {rc}), continuing; see {out}")
        log(f"chain ok in {time.monotonic() - t0:.0f}s")


RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # quiet; we log what matters ourselves
        pass

    def _reply(self, code: int, body: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:
        self._reply(200 if self.path.rstrip("/").endswith("/healthz") else 404, "ok\n")

    def do_POST(self) -> None:
        if self.path.rstrip("/").endswith("/nutrition"):
            return self._nutrition()
        if not self.path.rstrip("/").endswith("/hevy"):
            return self._reply(404)
        auth = self.headers.get("Authorization", "")
        if not authorized(auth, TOKEN):
            log(f"rejected POST (bad/missing Authorization; headers: {', '.join(sorted(self.headers.keys()))})")
            return self._reply(401)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._reply(413)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
            wid = body.get("workoutId") or (body.get("payload") or {}).get("workoutId")
        except (ValueError, AttributeError):
            body, wid = None, None
        self._reply(200, "ok\n")
        log(f"ping workout={wid} body={raw[:300].decode(errors='replace')}")
        RUNNER.kick()

    def _nutrition(self) -> None:
        if not authorized(self.headers.get("Authorization", ""), NUTRITION_TOKEN):
            log("rejected nutrition POST (bad/missing Authorization)")
            return self._reply(401, "unauthorized\n")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_NUTRITION_BODY:
            return self._reply(413)
        raw = self.rfile.read(n) if n else b""
        try:
            msg = nutrition_hook.ingest(raw)
        except Exception as e:  # never take the Hevy receiver down with it
            log(f"nutrition push FAILED: {e!r}")
            return self._reply(500, f"error: {e}\n")
        log(f"nutrition push {n} B: {msg}")
        self._reply(200, msg + "\n")


if __name__ == "__main__":
    log(f"listening on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
