#!/usr/bin/env python3
"""
fetch_sports_data.py
=====================
এই script Football ও Cricket-এর ম্যাচের সময়সূচি (schedule) সংগ্রহ করে
এবং data/football.json ও data/cricket.json ফাইলে সেভ করে।

গুরুত্বপূর্ণ: এই system স্কোর (score) সংগ্রহ করে না — শুধু ম্যাচ কবে,
কখন, কার সাথে কার, কোথায়, এবং এখন ম্যাচের status কী
(Upcoming / Live / Finished) — এই তথ্যগুলো সংগ্রহ করে।

ব্যবহৃত API:
  - Football : API-Football (api-sports.io)      -> https://www.api-football.com
  - Cricket  : CricketData.org (আগের নাম CricAPI) -> https://cricketdata.org

এই script কখনোই আগের valid JSON মুছে ফেলে না। কোনো API call ব্যর্থ হলে,
সেই sport-এর পুরোনো ফাইল অপরিবর্তিত থাকে এবং error log-এ কারণ লেখা হয়।
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------
# কনফিগারেশন
# ------------------------------------------------------------------

BD_TZ = ZoneInfo("Asia/Dhaka")               # Bangladesh Standard Time (UTC+6)
UTC = timezone.utc

# এই repo-র আগে থেকেই থাকা "sports" ফোল্ডারের ভেতরেই football.json ও
# cricket.json তৈরি/আপডেট হবে (repo-এর অন্য কোনো ফাইলে হাত দেওয়া হয় না)।
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sports")
FOOTBALL_JSON_PATH = os.path.join(DATA_DIR, "football.json")
CRICKET_JSON_PATH = os.path.join(DATA_DIR, "cricket.json")
ERROR_LOG_PATH = os.path.join(DATA_DIR, "error_log.txt")

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "").strip()
CRICKET_API_KEY = os.environ.get("CRICKET_API_KEY", "").strip()

FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
CRICKET_BASE_URL = "https://api.cricapi.com/v1"

REQUEST_TIMEOUT = 15          # সেকেন্ড
MAX_RETRIES = 2               # প্রতিটি API call সর্বোচ্চ কতবার আবার চেষ্টা করবে
RETRY_DELAY_SECONDS = 4

HOURS_AHEAD = 48               # কতক্ষণ সামনের ম্যাচ দেখাবে (ঘন্টা)


# ------------------------------------------------------------------
# সাধারণ Helper function
# ------------------------------------------------------------------

def now_utc():
    return datetime.now(UTC)


def now_bd_str():
    return datetime.now(BD_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log_error(message: str):
    """error_log.txt-তে সময়সহ error লিখে রাখে (শেষ ১০০ লাইন রাখে)।"""
    os.makedirs(DATA_DIR, exist_ok=True)
    line = f"[{now_bd_str()} BDT] {message}\n"
    print(f"ERROR: {message}", file=sys.stderr)

    old_lines = []
    if os.path.exists(ERROR_LOG_PATH):
        with open(ERROR_LOG_PATH, "r", encoding="utf-8") as f:
            old_lines = f.readlines()

    old_lines.append(line)
    old_lines = old_lines[-100:]  # শুধু সর্বশেষ ১০০ লাইন রাখা হবে

    with open(ERROR_LOG_PATH, "w", encoding="utf-8") as f:
        f.writelines(old_lines)


def safe_get(url, params=None, headers=None, label="request"):
    """
    Retry সহ একটি safe GET request।
    ব্যর্থ হলে None রিটার্ন করে (Exception raise করে না), যাতে
    একটি sport ব্যর্থ হলেও অন্য sport-এর কাজ থেমে না যায়।
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 2):  # প্রথম চেষ্টা + MAX_RETRIES বার retry
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                last_error = f"{label}: Rate limit শেষ হয়ে গেছে (HTTP 429)।"
                log_error(last_error)
                return None
            if resp.status_code >= 500:
                last_error = f"{label}: সার্ভার সমস্যা (HTTP {resp.status_code})। Attempt {attempt}."
                log_error(last_error)
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            if resp.status_code != 200:
                last_error = f"{label}: অপ্রত্যাশিত HTTP status {resp.status_code} -> {resp.text[:300]}"
                log_error(last_error)
                return None

            try:
                return resp.json()
            except json.JSONDecodeError:
                last_error = f"{label}: JSON parse করতে ব্যর্থ হয়েছে।"
                log_error(last_error)
                return None

        except requests.exceptions.Timeout:
            last_error = f"{label}: Timeout হয়েছে (attempt {attempt})।"
            log_error(last_error)
            time.sleep(RETRY_DELAY_SECONDS)
        except requests.exceptions.ConnectionError:
            last_error = f"{label}: Internet/connection সমস্যা (attempt {attempt})।"
            log_error(last_error)
            time.sleep(RETRY_DELAY_SECONDS)
        except Exception as e:  # noqa: BLE001
            last_error = f"{label}: অপ্রত্যাশিত error -> {e}"
            log_error(last_error)
            return None

    log_error(f"{label}: সব retry ব্যর্থ হয়েছে। শেষ error -> {last_error}")
    return None


def load_existing(path):
    """আগের JSON file load করে, যাতে API ব্যর্থ হলে সেটা রক্ষা করা যায়।"""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None
    return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"লেখা হয়েছে: {path}")


# ------------------------------------------------------------------
# FOOTBALL — API-Football (api-sports.io)
# ------------------------------------------------------------------

FOOTBALL_STATUS_MAP = {
    # short_code : (category, বাংলা-বান্ধব লেবেল)
    "TBD": ("Upcoming", "সময় এখনো নির্ধারিত হয়নি"),
    "NS":  ("Upcoming", "শুরু হয়নি"),
    "1H":  ("Live", "প্রথমার্ধ চলছে"),
    "HT":  ("Live", "বিরতি"),
    "2H":  ("Live", "দ্বিতীয়ার্ধ চলছে"),
    "ET":  ("Live", "অতিরিক্ত সময়"),
    "BT":  ("Live", "অতিরিক্ত সময়ের বিরতি"),
    "P":   ("Live", "পেনাল্টি শুটআউট"),
    "SUSP": ("Live", "স্থগিত (সাময়িক)"),
    "INT": ("Live", "বাধাপ্রাপ্ত"),
    "LIVE": ("Live", "লাইভ"),
    "FT":  ("Finished", "শেষ"),
    "AET": ("Finished", "অতিরিক্ত সময়ে শেষ"),
    "PEN": ("Finished", "পেনাল্টিতে শেষ"),
    "PST": ("Postponed", "স্থগিত"),
    "CANC": ("Cancelled", "বাতিল"),
    "ABD": ("Abandoned", "পরিত্যক্ত"),
    "AWD": ("Finished", "ওয়াকওভারে সিদ্ধান্ত"),
    "WO":  ("Finished", "ওয়াকওভার"),
}


def map_football_status(short_code):
    return FOOTBALL_STATUS_MAP.get(short_code, ("Unknown", short_code or "অজানা"))


def fetch_football_fixtures_for_date(date_str):
    """একটি নির্দিষ্ট তারিখের সব লিগের ম্যাচ fetch করে (Bangladesh সময়ে)।"""
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    params = {
        "date": date_str,
        "timezone": "Asia/Dhaka",   # API নিজেই Bangladesh সময়ে সময় দেবে
    }
    data = safe_get(
        f"{FOOTBALL_BASE_URL}/fixtures",
        params=params,
        headers=headers,
        label=f"Football fixtures ({date_str})",
    )
    if data is None:
        return None
    return data.get("response", [])


def build_football_json():
    if not FOOTBALL_API_KEY:
        log_error("FOOTBALL_API_KEY পাওয়া যায়নি। GitHub Secrets ঠিকভাবে সেট করা হয়েছে কিনা দেখুন।")
        return None

    now_bd = datetime.now(BD_TZ)
    dates_to_fetch = [
        now_bd.strftime("%Y-%m-%d"),
        (now_bd + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]

    all_fixtures = []
    any_success = False
    for d in dates_to_fetch:
        fixtures = fetch_football_fixtures_for_date(d)
        if fixtures is not None:
            any_success = True
            all_fixtures.extend(fixtures)

    if not any_success:
        # দুটো call-ই ব্যর্থ হয়েছে -> পুরোনো ফাইল অপরিবর্তিত রাখো
        return None

    window_end = now_utc() + timedelta(hours=HOURS_AHEAD)
    matches = []

    for fx in all_fixtures:
        try:
            fixture = fx.get("fixture", {})
            league = fx.get("league", {})
            teams = fx.get("teams", {})

            iso_date = fixture.get("date")  # যেমন: 2026-08-16T20:00:00+06:00
            if not iso_date:
                continue
            match_dt = datetime.fromisoformat(iso_date)
            match_dt_utc = match_dt.astimezone(UTC)

            # শুধু "এখন থেকে HOURS_AHEAD ঘন্টার মধ্যে" এবং "গত কয়েক ঘণ্টার মধ্যে
            # শুরু হওয়া" ম্যাচ রাখা হচ্ছে, যাতে সদ্য চলমান ম্যাচও দেখা যায়
            if match_dt_utc < (now_utc() - timedelta(hours=4)) or match_dt_utc > window_end:
                continue

            match_dt_bd = match_dt.astimezone(BD_TZ)
            status_short = fixture.get("status", {}).get("short")
            category, label_bn = map_football_status(status_short)

            score = fx.get("score", {}) or {}
            goals = fx.get("goals", {}) or {}

            matches.append({
                "matchId": fixture.get("id"),
                "sport": "football",
                "league": {
                    "id": league.get("id"),
                    "name": league.get("name"),
                    "country": league.get("country"),
                    "logo": league.get("logo"),
                    "round": league.get("round"),
                    "season": league.get("season"),
                },
                "homeTeam": {
                    "id": teams.get("home", {}).get("id"),
                    "name": teams.get("home", {}).get("name"),
                    "logo": teams.get("home", {}).get("logo"),
                },
                "awayTeam": {
                    "id": teams.get("away", {}).get("id"),
                    "name": teams.get("away", {}).get("name"),
                    "logo": teams.get("away", {}).get("logo"),
                },
                "venue": fixture.get("venue", {}).get("name"),
                "date": match_dt_bd.strftime("%Y-%m-%d"),
                "time": match_dt_bd.strftime("%H:%M"),
                "bdDate": match_dt_bd.strftime("%Y-%m-%d"),
                "bdTime": match_dt_bd.strftime("%H:%M"),
                "status": {
                    "code": status_short,
                    "category": category,       # Upcoming / Live / Finished / Postponed / Cancelled / Abandoned
                    "label": label_bn,
                },
                # score/goals ঐচ্ছিকভাবে রাখা হলো (ব্যবহার না করলে App-এ ignore করা যাবে)
                "halfTimeScore": score.get("halftime"),
                "homeScore": goals.get("home"),
                "awayScore": goals.get("away"),
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"একটি football fixture parse করতে সমস্যা হয়েছে: {e}")
            continue

    matches.sort(key=lambda m: (m["date"], m["time"]))

    return {
        "sport": "football",
        "timezone": "Asia/Dhaka (UTC+6)",
        "updatedAt": now_utc().isoformat(),
        "lastUpdated": now_bd_str() + " (Bangladesh Time)",
        "coverageHours": HOURS_AHEAD,
        "totalMatches": len(matches),
        "matches": matches,
    }


# ------------------------------------------------------------------
# CRICKET — CricketData.org (CricAPI v1)
# ------------------------------------------------------------------

def map_cricket_status(match):
    if match.get("matchEnded"):
        return "Finished"
    if match.get("matchStarted") and not match.get("matchEnded"):
        return "Live"
    return "Upcoming"


def fetch_cricket_matches():
    if not CRICKET_API_KEY:
        log_error("CRICKET_API_KEY পাওয়া যায়নি। GitHub Secrets ঠিকভাবে সেট করা হয়েছে কিনা দেখুন।")
        return None

    params = {
        "apikey": CRICKET_API_KEY,
        "offset": 0,
    }
    data = safe_get(
        f"{CRICKET_BASE_URL}/matches",
        params=params,
        label="Cricket matches",
    )
    if data is None:
        return None
    if data.get("status") != "success":
        log_error(f"Cricket API থেকে error status এসেছে: {data.get('status')} - {data.get('reason')}")
        return None
    return data.get("data", [])


def build_cricket_json():
    raw_matches = fetch_cricket_matches()
    if raw_matches is None:
        return None

    window_end = now_utc() + timedelta(hours=HOURS_AHEAD)
    matches = []

    for m in raw_matches:
        try:
            date_gmt_str = m.get("dateTimeGMT")
            if not date_gmt_str:
                continue
            match_dt_utc = datetime.fromisoformat(date_gmt_str).replace(tzinfo=UTC)

            if match_dt_utc < (now_utc() - timedelta(hours=6)) or match_dt_utc > window_end:
                continue

            match_dt_bd = match_dt_utc.astimezone(BD_TZ)
            team_info = m.get("teamInfo", []) or []
            teams = m.get("teams", []) or []

            def team_logo(team_name):
                for t in team_info:
                    if t.get("name") == team_name:
                        return t.get("img")
                return None

            team1_name = teams[0] if len(teams) > 0 else None
            team2_name = teams[1] if len(teams) > 1 else None

            matches.append({
                "matchId": m.get("id"),
                "sport": "cricket",
                "series": m.get("series_id"),
                "matchTitle": m.get("name"),
                "matchType": m.get("matchType"),
                "team1": {"name": team1_name, "logo": team_logo(team1_name)},
                "team2": {"name": team2_name, "logo": team_logo(team2_name)},
                "venue": m.get("venue"),
                "date": match_dt_bd.strftime("%Y-%m-%d"),
                "time": match_dt_bd.strftime("%H:%M"),
                "bdDate": match_dt_bd.strftime("%Y-%m-%d"),
                "bdTime": match_dt_bd.strftime("%H:%M"),
                "status": {
                    "category": map_cricket_status(m),   # Upcoming / Live / Finished
                    "label": m.get("status"),
                },
                "result": m.get("status") if m.get("matchEnded") else None,
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"একটি cricket match parse করতে সমস্যা হয়েছে: {e}")
            continue

    matches.sort(key=lambda m: (m["date"], m["time"]))

    return {
        "sport": "cricket",
        "timezone": "Asia/Dhaka (UTC+6)",
        "updatedAt": now_utc().isoformat(),
        "lastUpdated": now_bd_str() + " (Bangladesh Time)",
        "coverageHours": HOURS_AHEAD,
        "totalMatches": len(matches),
        "matches": matches,
    }


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    print(f"=== Sports Data Update শুরু হচ্ছে | {now_bd_str()} BDT ===")

    # ---------- Football ----------
    football_data = None
    try:
        football_data = build_football_json()
    except Exception as e:  # noqa: BLE001
        log_error(f"Football data তৈরি করতে অপ্রত্যাশিত error: {e}\n{traceback.format_exc()}")

    if football_data is not None:
        write_json(FOOTBALL_JSON_PATH, football_data)
    else:
        existing = load_existing(FOOTBALL_JSON_PATH)
        if existing is None:
            # প্রথমবার এবং API-ও ব্যর্থ -> খালি কিন্তু valid JSON রাখা হচ্ছে
            write_json(FOOTBALL_JSON_PATH, {
                "sport": "football",
                "timezone": "Asia/Dhaka (UTC+6)",
                "updatedAt": now_utc().isoformat(),
                "lastUpdated": now_bd_str() + " (Bangladesh Time)",
                "coverageHours": HOURS_AHEAD,
                "totalMatches": 0,
                "matches": [],
                "note": "প্রথম fetch ব্যর্থ হয়েছে। error_log.txt দেখুন।",
            })
        else:
            print("Football: API ব্যর্থ হয়েছে, তাই আগের JSON অপরিবর্তিত রাখা হলো।")

    # ---------- Cricket ----------
    cricket_data = None
    try:
        cricket_data = build_cricket_json()
    except Exception as e:  # noqa: BLE001
        log_error(f"Cricket data তৈরি করতে অপ্রত্যাশিত error: {e}\n{traceback.format_exc()}")

    if cricket_data is not None:
        write_json(CRICKET_JSON_PATH, cricket_data)
    else:
        existing = load_existing(CRICKET_JSON_PATH)
        if existing is None:
            write_json(CRICKET_JSON_PATH, {
                "sport": "cricket",
                "timezone": "Asia/Dhaka (UTC+6)",
                "updatedAt": now_utc().isoformat(),
                "lastUpdated": now_bd_str() + " (Bangladesh Time)",
                "coverageHours": HOURS_AHEAD,
                "totalMatches": 0,
                "matches": [],
                "note": "প্রথম fetch ব্যর্থ হয়েছে। error_log.txt দেখুন।",
            })
        else:
            print("Cricket: API ব্যর্থ হয়েছে, তাই আগের JSON অপরিবর্তিত রাখা হলো।")

    print("=== সম্পন্ন ===")


if __name__ == "__main__":
    main()
