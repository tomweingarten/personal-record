"""Recovery metrics from a sleep-dashboard HTTP API.

An example of a second source: it reads nightly resting HR, HRV and breathing
rate from a separate sleep app's ``/api/ai/trends`` endpoint. It only offers
``recovery``; pair it with another source for heart rate.

    [cardio]
    recovery_source = "sleep_data_api"
    [sleep_data_api]
    url = "http://127.0.0.1:8001"
"""
from __future__ import annotations

import json
import urllib.request

import config


class Source:
    def __init__(self):
        self.url = config.setting("sleep_data_api", "url")
        if not self.url:
            raise SystemExit("set [sleep_data_api] url in settings.toml")

    def recovery(self, days: int = 400) -> list[dict]:
        url = f"{self.url.rstrip('/')}/api/ai/trends?metrics=resting_hr,hrv_rmssd,resp_rate&days={days}"
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)["data"]
        return [{"date": d["date"], "resting_hr": d.get("resting_hr"),
                 "hrv_rmssd": d.get("hrv_rmssd"), "resp_rate": d.get("resp_rate")}
                for d in sorted(data, key=lambda d: d["date"])]
