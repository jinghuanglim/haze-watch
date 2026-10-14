"""Fetch hourly PM2.5 readings from data.gov.sg and save them to data.json.

Runs in GitHub Actions every hour. Uses the DATA_GOV_API_KEY secret when set
(higher rate limits); works without it too. Only standard library is used.
"""
import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
PSI_API = "https://api-open.data.gov.sg/v2/real-time/api/psi"
WIND_API = "https://api-open.data.gov.sg/v2/real-time/api/wind-{kind}"
WIND_LIMIT = int(os.environ.get("WIND_LIMIT", "150"))   # max hours of wind to backfill per run
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


def fetch_psi_day(day):
    """24-hour PSI for each region, for every hour of one day."""
    rows, token = {}, None
    while True:
        q = {"date": day.isoformat()}
        if token:
            q["paginationToken"] = token
        j = get_json(f"{PSI_API}?{urllib.parse.urlencode(q)}")
        if j.get("code") != 0:
            raise RuntimeError(j.get("errorMsg") or "API error")
        for it in j["data"].get("items", []):
            p = it["readings"].get("psi_twenty_four_hourly", {})
            vals = [p.get(r) for r in REGIONS]
            if all(isinstance(v, (int, float)) for v in vals):
                rows[it["timestamp"]] = vals
        token = j["data"].get("paginationToken")
        if not token:
            return rows


def update_psi(old, today):
    p = old.get("psi") or {"t": [], "v": []}
    rows = dict(zip(p["t"], p["v"]))
    have = {}
    for t in rows:
        have[t[:10]] = have.get(t[:10], 0) + 1
    day = START
    while day <= today:
        if day >= today - dt.timedelta(days=1) or have.get(day.isoformat(), 0) < 24:
            rows.update(fetch_psi_day(day))
            time.sleep(0.2)
        day += dt.timedelta(days=1)
    ts = sorted(t for t in rows if t[:10] >= START.isoformat())
    return {"t": ts, "v": [rows[t] for t in ts]}


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


def wind_at(ts):
    """Island-wide wind at one hour: vector-mean direction (where it blows FROM, degrees) and mean speed (km/h)."""
    q = urllib.parse.urlencode({"date": ts[:19]})
    d = get_json(WIND_API.format(kind="direction") + "?" + q)["data"].get("readings", [])
    sp = get_json(WIND_API.format(kind="speed") + "?" + q)["data"].get("readings", [])
    if not d or not sp:
        return None
    dirs = {x["stationId"]: x["value"] for x in d[0]["data"] if isinstance(x.get("value"), (int, float))}
    spds = {x["stationId"]: x["value"] for x in sp[0]["data"] if isinstance(x.get("value"), (int, float))}
    both = [k for k in dirs if k in spds]
    if not both:
        return None
    u = sum(-spds[k] * math.sin(math.radians(dirs[k])) for k in both) / len(both)
    v = sum(-spds[k] * math.cos(math.radians(dirs[k])) for k in both) / len(both)
    frm = (math.degrees(math.atan2(-u, -v)) + 360) % 360
    kmh = sum(spds[k] for k in both) / len(both) * 1.852
    return [round(frm), round(kmh, 1)]


def update_wind(old, hours):
    w = old.get("wind") or {"t": [], "d": [], "s": []}
    have = {t: [d, s] for t, d, s in zip(w["t"], w["d"], w["s"])}
    missing = [t for t in hours if t not in have][-WIND_LIMIT:]   # newest first priority
    if missing:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for t, res in zip(missing, ex.map(lambda t: wind_at(t), missing)):
                if res:
                    have[t] = res
    ts = sorted(t for t in have if t in set(hours))
    return {"t": ts, "d": [have[t][0] for t in ts], "s": [have[t][1] for t in ts]}


def main():
    rows, old = {}, {}
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
    try:
        data["psi"] = update_psi(old, today)
    except Exception as e:                        # PSI is extra; never block PM2.5 updates
        print("PSI update skipped:", e)
        if old.get("psi"):
            data["psi"] = old["psi"]
    try:
        data["wind"] = update_wind(old, ts)
    except Exception as e:                        # wind is a bonus; never block PM2.5 updates
        print("Wind update skipped:", e)
        if old.get("wind"):
            data["wind"] = old["wind"]
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
