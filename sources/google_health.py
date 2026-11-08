"""
Google Health API (v4): heart rate and recovery for the cardio panel.

Setup (once):
  1. Create an OAuth client (type "Web application") in Google Cloud with the
     Google Health API enabled, redirect URI http://localhost:8080 (or set
     [google_health] redirect_uri). Publish it "In production": in "Testing"
     Google expires refresh tokens after 7 days and the sync silently dies.
  2. Put GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET in the data
     directory's .env.
  3. Run `python3 -m sources.google_health auth` from the repository root and
     follow the prompts. Tokens are written to [google_health] token_file
     (default: <data dir>/google_health_tokens.json).

Needs `requests` (pip install requests).

Field names come from the v4 REST reference and Google's own CLI
(github.com/Google-Health-API/google-health-cli, pkg/types/registry.go):
  - filters: daily types `<type>.date`, sample types
    `<type>.sample_time.physical_time` (RFC 3339 with Z). Only >= and < are accepted.
  - page size: sleep 25, everything else 10000.
"""
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

import config

TOKEN_FILE = config.path_setting("google_health", "token_file") or config.DATA / "google_health_tokens.json"
BASE = "https://health.googleapis.com/v4/users/me/dataTypes"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    # heart-rate samples live under activity_and_fitness in Google's CLI registry
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
]
# A loopback redirect is allowed for Desktop-type OAuth clients without
# registering it. The browser will fail to load it; the code is in the URL.
REDIRECT_URI = config.setting("google_health", "redirect_uri", "http://localhost:8080")
SHORT_WAKE_WEIGHT = 0.5  # see get_sleep_logs


class GoogleHealthRateLimitError(Exception):
    def __init__(self, retry_after: int = 60):
        super().__init__(f"Google Health rate limit; retry after {retry_after}s")
        self.retry_after = retry_after


def auth_url(client_id: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # without this there is no refresh token
        "prompt": "consent",        # re-issue the refresh token on re-auth
        # NOT include_granted_scopes: mixing in legacy fitness.* grants breaks auth
    })


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Google token exchange failed: {resp.text}")
    tokens = resp.json()
    if "refresh_token" not in tokens:
        raise Exception("No refresh_token returned -- re-run with prompt=consent")
    _write_tokens(tokens)
    return tokens


def _write_tokens(tokens: dict) -> None:
    # Atomic replace: a half-written token file would lock the sync out.
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_FILE)


# --- value helpers -------------------------------------------------------------

def _num(v):
    """int64 fields arrive as JSON strings."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _civil_date(d: dict | None) -> str | None:
    if not d:
        return None
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _civil_dt(c: dict | None) -> datetime | None:
    """{"date": {y,m,d}, "time": {hours, minutes, seconds}} -> naive local datetime."""
    if not c:
        return None
    d = _civil_date(c.get("date"))
    if not d:
        return None
    t = c.get("time") or {}
    return datetime.fromisoformat(d).replace(
        hour=int(t.get("hours", 0)), minute=int(t.get("minutes", 0)),
        second=int(t.get("seconds", 0)))


def _parse_offset(s: str | None) -> timedelta:
    """Duration-format offsets, e.g. "-14400s"."""
    if not s:
        return timedelta(0)
    try:
        return timedelta(seconds=float(s.rstrip("s")))
    except ValueError:
        return timedelta(0)


def _local(ts: str | None, offset: str | None) -> datetime | None:
    """RFC 3339 physical time + utc offset -> naive local datetime."""
    if not ts:
        return None
    try:
        utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (utc + _parse_offset(offset)).replace(tzinfo=None)


def _first_number(obj: dict, prefer: tuple[str, ...]) -> float | None:
    """First numeric value among `prefer` keys, else the only numeric field.
    Used where the v4 reference does not spell out a field name (respiratory rate)."""
    for k in prefer:
        if k in obj and _num(obj[k]) is not None:
            return _num(obj[k])
    nums = [(k, _num(v)) for k, v in obj.items()
            if not isinstance(v, (dict, list)) and _num(v) is not None]
    return nums[0][1] if len(nums) == 1 else None


class GoogleHealthAPI:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else None

    def is_authenticated(self) -> bool:
        return bool(self.tokens and self.tokens.get("refresh_token"))

    def refresh_token(self):
        resp = requests.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.tokens["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30)
        if resp.status_code != 200:
            # invalid_grant here usually means the OAuth client is in "Testing"
            # (7-day refresh tokens) or the grant was revoked.
            raise Exception(f"Google token refresh failed: {resp.text}")
        new = resp.json()
        # Google normally keeps the refresh token; store a rotated one if sent.
        self.tokens = {**self.tokens, **new}
        _write_tokens(self.tokens)

    def _get(self, url: str, params: dict) -> dict:
        if not self.is_authenticated():
            raise Exception("Not authenticated with Google Health API")
        if "access_token" not in self.tokens:
            self.refresh_token()
        for attempt in range(3):
            headers = {"Authorization": f"Bearer {self.tokens['access_token']}"}
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code == 401:
                self.refresh_token()
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                raise GoogleHealthRateLimitError(int(resp.headers.get("Retry-After", "60") or 60))
            if 500 <= resp.status_code < 600 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"Google Health API Error ({resp.status_code}): {resp.text}")
        raise Exception(f"Google Health API Error ({resp.status_code}): {resp.text}")

    def list_points(self, data_type: str, filt: str, page_size: int = 10000) -> list[dict]:
        out, token = [], None
        while True:
            params = {"filter": filt, "pageSize": page_size}
            if token:
                params["pageToken"] = token
            data = self._get(f"{BASE}/{data_type}/dataPoints", params)
            out += data.get("dataPoints", [])
            token = data.get("nextPageToken")
            if not token:
                return out

    def _daily(self, data_type: str, start_date: str, end_date: str) -> list[dict]:
        """Daily summaries for [start_date, end_date] inclusive -> list of inner objects."""
        f = data_type.replace("-", "_")
        end_excl = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
        key = "".join(w.capitalize() if i else w for i, w in enumerate(data_type.split("-")))
        pts = self.list_points(data_type, f'{f}.date >= "{start_date}" AND {f}.date < "{end_excl}"')
        return [p[key] for p in pts if key in p]

    # --- Data fetching: same rows as FitbitAPI -----------------------------------

    def get_sleep_logs(self, start_date: str, end_date: str) -> list[dict]:
        end_excl = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
        pts = self.list_points(
            "sleep",
            f'sleep.interval.civil_end_time >= "{start_date}T00:00:00" AND '
            f'sleep.interval.civil_end_time < "{end_excl}T00:00:00"',
            page_size=25)
        rows = []
        for p in pts:
            s = p.get("sleep") or {}
            iv = s.get("interval") or {}
            start = _civil_dt(iv.get("civilStartTime")) or _local(iv.get("startTime"), iv.get("startUtcOffset"))
            end = _civil_dt(iv.get("civilEndTime")) or _local(iv.get("endTime"), iv.get("endUtcOffset"))
            if not (start and end):
                continue
            summ = s.get("summary") or {}
            stages = {(st.get("type") or "").upper(): st for st in summ.get("stagesSummary", [])}
            mins = {k: _num(v.get("minutes")) for k, v in stages.items()}
            asleep = _num(summ.get("minutesAsleep"))
            # v4 splits out wakes of <=3 min as `shortAwakenings`, each tagged
            # with the stage it interrupts. Fitbit v1 counted them as wake time
            # and wake count (levels.shortData), so fold them back in to keep
            # wake_min / awakenings / asleep continuous across the cutover.
            # v1 timed these in 30 s epochs; v4 rounds each one out to whole
            # minutes, so full v4 minutes overshoot v1 by ~7 min/night. Weight
            # SHORT_WAKE_WEIGHT was fitted on 21 nights of both APIs (2026-10-14).
            short = s.get("shortAwakenings") or []
            for sa in short:
                a, b = _local(sa.get("startTime"), None), _local(sa.get("endTime"), None)
                m = (b - a).total_seconds() / 60 * SHORT_WAKE_WEIGHT if a and b else 0
                t = (sa.get("type") or "").upper()
                if mins.get(t) is not None:
                    mins[t] -= m
                if mins.get("AWAKE") is not None:
                    mins["AWAKE"] += m
                if asleep is not None:
                    asleep -= m
            mins = {k: (round(v) if v is not None else None) for k, v in mins.items()}
            asleep = round(asleep) if asleep is not None else None
            wake_count = _num(stages.get("AWAKE", {}).get("count"))
            if wake_count is not None:
                wake_count += len(short)
            # Elapsed time from the physical (UTC) instants: civil times repeat
            # or skip an hour across a DST change (2026-11-01: 354 vs 414 min).
            a = _local(iv.get("startTime"), None)
            b = _local(iv.get("endTime"), None)
            in_bed = round(((b - a) if a and b else (end - start)).total_seconds() / 60)
            # Fitbit's `efficiency` is not documented in v4. Use it if present;
            # otherwise asleep / time in bed, flagged so the comparison can see it.
            eff = _num(summ.get("efficiency"))
            eff_derived = eff is None and asleep is not None and in_bed > 0
            if eff_derived:
                eff = round(asleep / in_bed * 100)
            rows.append({
                "date": end.date().isoformat(),          # wake date, like dateOfSleep
                "sleep_duration_min": asleep,
                "sleep_efficiency": eff,
                "deep_sleep_min": mins.get("DEEP"),
                "rem_sleep_min": mins.get("REM"),
                "light_sleep_min": mins.get("LIGHT"),
                "wake_min": mins.get("AWAKE"),
                "time_in_bed_min": in_bed,
                "awakenings": wake_count,
                "sleep_start": start.isoformat(timespec="milliseconds"),
                "sleep_end": end.isoformat(timespec="milliseconds"),
                "sleep_efficiency_derived": eff_derived,
                "_is_nap": (s.get("metadata") or {}).get("mainSleep") is False,
            })
        # Same rule as FitbitAPI: one main sleep per date, the longest.
        seen: dict[str, dict] = {}
        for r in rows:
            if r.pop("_is_nap"):
                continue
            d = r["date"]
            if d not in seen or (r["sleep_duration_min"] or 0) > (seen[d]["sleep_duration_min"] or 0):
                seen[d] = r
        return list(seen.values())

    def get_hrv(self, start_date: str, end_date: str) -> list[dict]:
        rows = []
        for v in self._daily("daily-heart-rate-variability", start_date, end_date):
            rmssd = _num(v.get("averageHeartRateVariabilityMilliseconds"))
            d = _civil_date(v.get("date"))
            if d and rmssd:
                rows.append({"date": d, "hrv_rmssd": rmssd})
        return rows

    def get_spo2(self, start_date: str, end_date: str) -> list[dict]:
        # v4 drops every sample flagged invalid (Fitbit only dropped the 50%
        # sentinel), so averages can differ slightly from the old API.
        rows = []
        for v in self._daily("daily-oxygen-saturation", start_date, end_date):
            d = _civil_date(v.get("date"))
            if d:
                rows.append({"date": d,
                             "spo2_avg": _num(v.get("averagePercentage")),
                             "spo2_min": _num(v.get("lowerBoundPercentage")),
                             "spo2_max": _num(v.get("upperBoundPercentage"))})
        return rows

    def get_temperature(self, start_date: str, end_date: str) -> list[dict]:
        # Fitbit's nightlyRelative == nightly - baseline.
        rows = []
        for v in self._daily("daily-sleep-temperature-derivations", start_date, end_date):
            d = _civil_date(v.get("date"))
            n, b = _num(v.get("nightlyTemperatureCelsius")), _num(v.get("baselineTemperatureCelsius"))
            if d and n is not None and b is not None:
                rows.append({"date": d, "skin_temp_baseline_dev": round(n - b, 2)})
        return rows

    def get_respiratory_rate(self, start_date: str, end_date: str) -> list[dict]:
        rows = []
        for v in self._daily("daily-respiratory-rate", start_date, end_date):
            d = _civil_date(v.get("date"))
            br = _first_number(v, ("breathsPerMinute", "averageBreathsPerMinute",
                                   "respiratoryRateBreathsPerMinute"))
            if d and br:
                rows.append({"date": d, "resp_rate": round(br, 1)})
        return rows

    def get_resting_hr(self, start_date: str, end_date: str) -> list[dict]:
        rows = []
        for v in self._daily("daily-resting-heart-rate", start_date, end_date):
            d = _civil_date(v.get("date"))
            bpm = _num(v.get("beatsPerMinute"))
            if d and bpm is not None:
                rows.append({"date": d, "resting_hr": float(bpm)})
        return rows

    def get_intraday_hr(self, day: str) -> list[tuple[datetime, int]]:
        """1-minute HR for a LOCAL calendar day, as (naive local datetime, bpm).

        v4 returns ~5 s samples. They are averaged per minute so the sleeping-HR
        stats stay comparable with the 1-minute Fitbit series they continue.
        The UTC query window is padded 12 h each side (covers any UTC offset)
        and cut on local time. ~5 s samples make this 3-4 pages per day.
        """
        d0 = date.fromisoformat(day)
        lo = (d0 - timedelta(days=1)).isoformat() + "T12:00:00Z"
        hi = (d0 + timedelta(days=1)).isoformat() + "T12:00:00Z"
        pts = self.list_points(
            "heart-rate",
            f'heart_rate.sample_time.physical_time >= "{lo}" AND '
            f'heart_rate.sample_time.physical_time < "{hi}"')
        per_min: dict[datetime, list[int]] = defaultdict(list)
        for p in pts:
            hr = p.get("heartRate") or {}
            st = hr.get("sampleTime") or {}
            t = _civil_dt(st.get("civilTime")) or _local(st.get("physicalTime"), st.get("utcOffset"))
            bpm = _num(hr.get("beatsPerMinute"))
            if t is None or bpm is None or t.date() != d0:
                continue
            per_min[t.replace(second=0, microsecond=0)].append(int(bpm))
        return sorted((m, round(sum(v) / len(v))) for m, v in per_min.items())


# Client id/secret live in the data dir's .env unless [google_health] env_file points
# elsewhere (e.g. another app that already holds the same OAuth client).
ENV_FILE = config.path_setting("google_health", "env_file") or config.ENV_FILE


def _client() -> GoogleHealthAPI:
    env = config.read_env(ENV_FILE)
    cid, secret = env.get("GOOGLE_HEALTH_CLIENT_ID"), env.get("GOOGLE_HEALTH_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit(f"GOOGLE_HEALTH_CLIENT_ID / GOOGLE_HEALTH_CLIENT_SECRET missing from {ENV_FILE}")
    return GoogleHealthAPI(cid, secret)


class Source:
    """Personal Record cardio source (see sources/__init__.py)."""

    def __init__(self):
        self.api = _client()

    def intraday_hr(self, day: date) -> list[tuple[datetime, int]]:
        return self.api.get_intraday_hr(day.isoformat())

    def recovery(self, days: int = 400) -> list[dict]:
        end = date.today()
        start = (end - timedelta(days=days)).isoformat()
        rows: dict[str, dict] = {}
        for fetch in (self.api.get_resting_hr, self.api.get_hrv, self.api.get_respiratory_rate):
            for r in fetch(start, end.isoformat()):
                rows.setdefault(r["date"], {"date": r["date"], "resting_hr": None,
                                            "hrv_rmssd": None, "resp_rate": None}).update(r)
        return [rows[d] for d in sorted(rows)]


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["auth"]:
        raise SystemExit("usage: python3 -m sources.google_health auth")
    env = config.read_env(ENV_FILE)
    cid, secret = env.get("GOOGLE_HEALTH_CLIENT_ID"), env.get("GOOGLE_HEALTH_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit(f"Add GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET to {ENV_FILE} first")
    print("Open this URL, approve access, then copy the `code` parameter from the address you land on:\n")
    print(auth_url(cid))
    code = input("\ncode: ").strip()
    exchange_code(cid, secret, code)
    print(f"Saved tokens to {TOKEN_FILE}")
