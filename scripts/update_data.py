"""Fetch hourly PM2.5 readings from data.gov.sg and save them to data.json.

Runs in GitHub Actions every hour. Uses the DATA_GOV_API_KEY secret when set
(higher rate limits); works without it too. Only standard library is used.
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
START = dt.date(2026, 10, 1)
REGIONS = ["north", "south", "east", "west", "central"]
OUT = os.path.join(os.path.dirname(__file__), "..", "data.json")
SGT = dt.timezone(dt.timedelta(hours=8))
KEY = os.environ.get("DATA_GOV_API_KEY", "").strip()


def get_json(url):
    headers = {"User-Agent": "haze-watch (GitHub Actions)"}
    if KEY:
        headers["x-api-key"] = KEY
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Gave up on {url}")


def fetch_day(day):
    rows, token = {}, None
    while True:
        q = {"date": day.isoformat()}
        if token:
            q["paginationToken"] = token
        j = get_json(f"{API}?{urllib.parse.urlencode(q)}")
        if j.get("code") != 0:
            raise RuntimeError(j.get("errorMsg") or "API error")
        for it in j["data"].get("items", []):
            vals = [it["readings"]["pm25_one_hourly"].get(r) for r in REGIONS]
            if all(isinstance(v, (int, float)) for v in vals):
                rows[it["timestamp"]] = vals
        token = j["data"].get("paginationToken")
        if not token:
            return rows


def main():
    rows = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = json.load(f)
        rows = dict(zip(old.get("t", []), old.get("v", [])))

    today = dt.datetime.now(SGT).date()
    have = {}
    for t in rows:
        have[t[:10]] = have.get(t[:10], 0) + 1

    day = START
    while day <= today:
        # Refetch today and yesterday every run; other days only if incomplete.
        if day >= today - dt.timedelta(days=1) or have.get(day.isoformat(), 0) < 24:
            rows.update(fetch_day(day))
            time.sleep(0.3)
        day += dt.timedelta(days=1)

    ts = sorted(t for t in rows if t[:10] >= START.isoformat())
    data = {"regions": REGIONS, "t": ts, "v": [rows[t] for t in ts]}
    new_text = json.dumps(data, separators=(",", ":"))
    old_text = open(OUT).read().strip() if os.path.exists(OUT) else ""
    if new_text != old_text:
        with open(OUT, "w") as f:
            f.write(new_text + "\n")
        print(f"Updated: {len(ts)} hours, latest {ts[-1] if ts else 'none'}")
    else:
        print("No new readings.")


if __name__ == "__main__":
    main()
