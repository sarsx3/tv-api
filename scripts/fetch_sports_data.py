#!/usr/bin/env python3
"""
fetch_sports_data.py
=====================
Collects upcoming/live Football and Cricket match schedules and writes
them to sports/football.json and sports/cricket.json.

This script does NOT collect ball-by-ball or live goal scores — only
match date, time, teams, venue, competition, and current status
(Upcoming / Live / Finished / Postponed / Cancelled / Abandoned).

APIs used:
  - Football : API-Football (api-sports.io)      -> https://www.api-football.com
  - Cricket  : CricketData.org (formerly CricAPI) -> https://cricketdata.org

This script never deletes a previously valid JSON file. If an API call
fails, that sport's old file is left untouched and the reason is
appended to sports/error_log.txt.

All output JSON fields are plain English only — no localized text is
written into football.json / cricket.json, since these files are
consumed directly by the app.
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
# Configuration
# ------------------------------------------------------------------

BD_TZ = ZoneInfo("Asia/Dhaka")               # Bangladesh Standard Time (UTC+6)
UTC = timezone.utc

# Writes into the existing "sports" folder in this repo. Does not touch
# any other file in the repo.
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sports")
FOOTBALL_JSON_PATH = os.path.join(DATA_DIR, "football.json")
CRICKET_JSON_PATH = os.path.join(DATA_DIR, "cricket.json")
ERROR_LOG_PATH = os.path.join(DATA_DIR, "error_log.txt")

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "").strip()
CRICKET_API_KEY = os.environ.get("CRICKET_API_KEY", "").strip()

FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
CRICKET_BASE_URL = "https://api.cricapi.com/v1"

REQUEST_TIMEOUT = 15          # seconds
MAX_RETRIES = 2               # retries per API call
RETRY_DELAY_SECONDS = 4

HOURS_AHEAD = 48               # how far ahead to show matches (hours)

# CricketData.org free tier: 100 hits/day. Running every 30 min = 48
# runs/day. 2 hits/run (2 pages of /currentMatches) = 96/day, same
# safety margin the football side already uses.
CRICKET_PAGES_PER_RUN = 2
CRICKET_PAGE_SIZE_OFFSET_STEP = 25   # CricAPI returns ~25 results per page


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def now_utc():
    return datetime.now(UTC)


def now_bd_str():
    return datetime.now(BD_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log_error(message: str):
    """Appends a timestamped error line to error_log.txt (keeps last 100 lines)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    line = f"[{now_bd_str()} BDT] {message}\n"
    print(f"ERROR: {message}", file=sys.stderr)

    old_lines = []
    if os.path.exists(ERROR_LOG_PATH):
        with open(ERROR_LOG_PATH, "r", encoding="utf-8") as f:
            old_lines = f.readlines()

    old_lines.append(line)
    old_lines = old_lines[-100:]

    with open(ERROR_LOG_PATH, "w", encoding="utf-8") as f:
        f.writelines(old_lines)


def safe_get(url, params=None, headers=None, label="request"):
    """
    GET request with retries. Returns None on failure instead of raising,
    so one sport failing never blocks the other sport.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                last_error = f"{label}: Rate limit exceeded (HTTP 429)."
                log_error(last_error)
                return None
            if resp.status_code >= 500:
                last_error = f"{label}: Server error (HTTP {resp.status_code}). Attempt {attempt}."
                log_error(last_error)
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            if resp.status_code != 200:
                last_error = f"{label}: Unexpected HTTP status {resp.status_code} -> {resp.text[:300]}"
                log_error(last_error)
                return None

            try:
                return resp.json()
            except json.JSONDecodeError:
                last_error = f"{label}: Failed to parse JSON response."
                log_error(last_error)
                return None

        except requests.exceptions.Timeout:
            last_error = f"{label}: Request timed out (attempt {attempt})."
            log_error(last_error)
            time.sleep(RETRY_DELAY_SECONDS)
        except requests.exceptions.ConnectionError:
            last_error = f"{label}: Connection error (attempt {attempt})."
            log_error(last_error)
            time.sleep(RETRY_DELAY_SECONDS)
        except Exception as e:  # noqa: BLE001
            last_error = f"{label}: Unexpected error -> {e}"
            log_error(last_error)
            return None

    log_error(f"{label}: All retries failed. Last error -> {last_error}")
    return None


def load_existing(path):
    """Loads the previous JSON file so it can be preserved if the API fails."""
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
    print(f"Written: {path}")


def empty_placeholder(sport):
    """Placeholder written only if this is the very first run AND the API failed."""
    return {
        "sport": sport,
        "timezone": "Asia/Dhaka (UTC+6)",
        "updatedAt": now_utc().isoformat(),
        "lastUpdated": now_bd_str() + " (Bangladesh Time)",
        "coverageHours": HOURS_AHEAD,
        "totalMatches": 0,
        "matches": [],
        "note": "Initial fetch failed. Check error_log.txt for details.",
    }


# ------------------------------------------------------------------
# FOOTBALL — API-Football (api-sports.io)
# ------------------------------------------------------------------

# category, English label — no localized text goes into the JSON output.
FOOTBALL_STATUS_MAP = {
    "TBD":  ("Upcoming", "Time To Be Defined"),
    "NS":   ("Upcoming", "Not Started"),
    "1H":   ("Live", "First Half"),
    "HT":   ("Live", "Half Time"),
    "2H":   ("Live", "Second Half"),
    "ET":   ("Live", "Extra Time"),
    "BT":   ("Live", "Break Time (Extra Time)"),
    "P":    ("Live", "Penalty Shootout"),
    "SUSP": ("Live", "Match Suspended"),
    "INT":  ("Live", "Match Interrupted"),
    "LIVE": ("Live", "Live"),
    "FT":   ("Finished", "Full Time"),
    "AET":  ("Finished", "Finished After Extra Time"),
    "PEN":  ("Finished", "Finished After Penalties"),
    "PST":  ("Postponed", "Postponed"),
    "CANC": ("Cancelled", "Cancelled"),
    "ABD":  ("Abandoned", "Abandoned"),
    "AWD":  ("Finished", "Technical Loss / Award"),
    "WO":   ("Finished", "Walkover"),
}


def map_football_status(short_code):
    return FOOTBALL_STATUS_MAP.get(short_code, ("Unknown", short_code or "Unknown"))


def fetch_football_fixtures_for_date(date_str):
    """Fetches all-league fixtures for a specific date (in Bangladesh time)."""
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    params = {
        "date": date_str,
        "timezone": "Asia/Dhaka",
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
        log_error("FOOTBALL_API_KEY not found. Check GitHub Secrets configuration.")
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
        return None

    window_end = now_utc() + timedelta(hours=HOURS_AHEAD)
    matches = []

    for fx in all_fixtures:
        try:
            fixture = fx.get("fixture", {})
            league = fx.get("league", {})
            teams = fx.get("teams", {})

            iso_date = fixture.get("date")
            if not iso_date:
                continue
            match_dt = datetime.fromisoformat(iso_date)
            match_dt_utc = match_dt.astimezone(UTC)

            # Keep matches within HOURS_AHEAD, plus recently-started ones
            # (so live matches still show).
            if match_dt_utc < (now_utc() - timedelta(hours=4)) or match_dt_utc > window_end:
                continue

            match_dt_bd = match_dt.astimezone(BD_TZ)
            status_short = fixture.get("status", {}).get("short")
            category, label = map_football_status(status_short)

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
                    "label": label,
                },
                "halfTimeScore": score.get("halftime"),
                "homeScore": goals.get("home"),
                "awayScore": goals.get("away"),
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"Failed to parse a football fixture: {e}")
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
# IMPORTANT FIX: uses /currentMatches instead of /matches.
#
# /matches is a general browse/search endpoint (returns an arbitrary
# ~25-per-page slice of ALL matches in their database — finished,
# domestic, youth, far-future — not scoped to "soon"). On any given
# day, none of those 25 results may fall inside a 48h window, which
# silently produced an empty matches[] list even though real upcoming
# matches existed.
#
# /currentMatches is CricketData.org's purpose-built endpoint for
# live + imminently upcoming matches, which is what this app needs.

CRICKET_STATUS_LABELS = {
    "Upcoming": "Upcoming",
    "Live": "Live",
    "Finished": "Finished",
}


def map_cricket_status(match):
    if match.get("matchEnded"):
        return "Finished"
    if match.get("matchStarted") and not match.get("matchEnded"):
        return "Live"
    return "Upcoming"


def fetch_cricket_matches():
    if not CRICKET_API_KEY:
        log_error("CRICKET_API_KEY not found. Check GitHub Secrets configuration.")
        return None

    all_matches = []
    any_success = False

    for page in range(CRICKET_PAGES_PER_RUN):
        offset = page * CRICKET_PAGE_SIZE_OFFSET_STEP
        params = {
            "apikey": CRICKET_API_KEY,
            "offset": offset,
        }
        data = safe_get(
            f"{CRICKET_BASE_URL}/currentMatches",
            params=params,
            label=f"Cricket currentMatches (offset {offset})",
        )
        if data is None:
            continue
        if data.get("status") != "success":
            log_error(f"Cricket API returned an error status: {data.get('status')} - {data.get('reason')}")
            continue

        any_success = True
        page_matches = data.get("data", [])
        all_matches.extend(page_matches)

        # Stop early if this page came back short (means no more pages).
        if len(page_matches) < CRICKET_PAGE_SIZE_OFFSET_STEP:
            break

    if not any_success:
        return None

    return all_matches


def build_cricket_json():
    raw_matches = fetch_cricket_matches()
    if raw_matches is None:
        return None

    window_end = now_utc() + timedelta(hours=HOURS_AHEAD)
    matches = []
    seen_ids = set()

    for m in raw_matches:
        try:
            match_id = m.get("id")
            if match_id in seen_ids:
                continue  # de-dupe across pages
            seen_ids.add(match_id)

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

            category = map_cricket_status(m)

            matches.append({
                "matchId": match_id,
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
                    "category": category,   # Upcoming / Live / Finished
                    "label": m.get("status") or CRICKET_STATUS_LABELS.get(category, category),
                },
                "result": m.get("status") if m.get("matchEnded") else None,
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"Failed to parse a cricket match: {e}")
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
    print(f"=== Sports Data Update starting | {now_bd_str()} BDT ===")

    # ---------- Football ----------
    football_data = None
    try:
        football_data = build_football_json()
    except Exception as e:  # noqa: BLE001
        log_error(f"Unexpected error building football data: {e}\n{traceback.format_exc()}")

    if football_data is not None:
        write_json(FOOTBALL_JSON_PATH, football_data)
    else:
        existing = load_existing(FOOTBALL_JSON_PATH)
        if existing is None:
            write_json(FOOTBALL_JSON_PATH, empty_placeholder("football"))
        else:
            print("Football: API failed, keeping previous JSON unchanged.")

    # ---------- Cricket ----------
    cricket_data = None
    try:
        cricket_data = build_cricket_json()
    except Exception as e:  # noqa: BLE001
        log_error(f"Unexpected error building cricket data: {e}\n{traceback.format_exc()}")

    if cricket_data is not None:
        write_json(CRICKET_JSON_PATH, cricket_data)
    else:
        existing = load_existing(CRICKET_JSON_PATH)
        if existing is None:
            write_json(CRICKET_JSON_PATH, empty_placeholder("cricket"))
        else:
            print("Cricket: API failed, keeping previous JSON unchanged.")

    print("=== Done ===")


if __name__ == "__main__":
    main()
