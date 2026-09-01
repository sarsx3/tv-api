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

DESIGN GOALS (v2)
------------------
1. Practically impossible to run out of daily API quota.
   - Football now uses a single "from/to" date-range request per run
     instead of two separate per-date requests (half the calls).
   - Every request made against each API is counted in a small,
     git-committed quota-tracker file (sports/.meta/*_quota.json) that
     resets automatically at UTC midnight (when these providers reset
     their own counters). Once usage reaches a safety cap (default 90
     out of 100/day), the script stops calling that API for the rest
     of the day instead of risking a hard failure or, worse, a
     provider that returns HTTP 200 with an empty body once quota is
     blown (see point 2).
   - The safety cap is configurable via FOOTBALL_DAILY_LIMIT /
     CRICKET_DAILY_LIMIT env vars if your plan has a different quota.

2. Never mistakes "API failed" for "no matches today".
   - api-sports.io (football) can return HTTP 200 with an "errors"
     object embedded in the JSON body when the key/plan/quota is
     invalid. That is treated as a hard failure, not as zero matches.
   - CricketData.org's "status" field is checked the same way.

3. Every output file always has a clear, explicit status the app can
   branch on directly — no more guessing why "matches" is empty:
     - "ok"            -> fresh data, matches may or may not be present
     - "no_matches"     -> fresh data, genuinely zero matches right now
     - "quota_paused"   -> daily request budget reached; showing last
                            known-good data (marked stale)
     - "error"          -> the API call failed; showing last known-good
                            data (marded stale)
   Each status carries a ready-to-display message in both Bangla and
   English (status.message.bn / status.message.en).

4. Old good data is NEVER silently overwritten with an empty result
   unless the API explicitly, successfully reported zero matches.

5. A small "nextMatch" preview and a combined sports/status.json
   summary are included for a nicer at-a-glance UI.
"""

import json
import os
import random
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
META_DIR = os.path.join(DATA_DIR, ".meta")

FOOTBALL_JSON_PATH = os.path.join(DATA_DIR, "football.json")
CRICKET_JSON_PATH = os.path.join(DATA_DIR, "cricket.json")
STATUS_JSON_PATH = os.path.join(DATA_DIR, "status.json")
ERROR_LOG_PATH = os.path.join(DATA_DIR, "error_log.txt")

FOOTBALL_QUOTA_PATH = os.path.join(META_DIR, "football_quota.json")
CRICKET_QUOTA_PATH = os.path.join(META_DIR, "cricket_quota.json")

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "").strip()
CRICKET_API_KEY = os.environ.get("CRICKET_API_KEY", "").strip()

FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
CRICKET_BASE_URL = "https://api.cricapi.com/v1"

# Daily quota + safety margin. Both providers' free tiers are 100/day;
# we stop at 90 by default so retries / manual "Run workflow" clicks /
# clock-skew near midnight never push us over the real limit.
FOOTBALL_DAILY_LIMIT = int(os.environ.get("FOOTBALL_DAILY_LIMIT", "100"))
FOOTBALL_SAFE_CAP = int(os.environ.get("FOOTBALL_SAFE_CAP", "90"))
CRICKET_DAILY_LIMIT = int(os.environ.get("CRICKET_DAILY_LIMIT", "100"))
CRICKET_SAFE_CAP = int(os.environ.get("CRICKET_SAFE_CAP", "90"))

REQUEST_TIMEOUT = 15          # seconds
MAX_RETRIES = 2               # retries per API call (on transient errors only)
RETRY_BASE_DELAY = 3          # seconds, exponential backoff base

HOURS_AHEAD = 48               # how far ahead to show matches (hours)

# CricketData.org returns ~25 results per page. We only fetch a 2nd
# page if the 1st page came back completely full (a strong signal
# there may be more), so most runs use just 1 request.
CRICKET_MAX_PAGES_PER_RUN = 2
CRICKET_PAGE_SIZE = 25


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------

def now_utc():
    return datetime.now(UTC)


def now_bd():
    return datetime.now(BD_TZ)


def now_bd_str():
    return now_bd().strftime("%Y-%m-%d %H:%M:%S")


def log_error(message: str):
    """Appends a timestamped error line to error_log.txt (keeps last 150 lines)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    line = f"[{now_bd_str()} BDT] {message}\n"
    print(f"ERROR: {message}", file=sys.stderr)

    old_lines = []
    if os.path.exists(ERROR_LOG_PATH):
        with open(ERROR_LOG_PATH, "r", encoding="utf-8") as f:
            old_lines = f.readlines()

    old_lines.append(line)
    old_lines = old_lines[-150:]

    with open(ERROR_LOG_PATH, "w", encoding="utf-8") as f:
        f.writelines(old_lines)


def load_json(path):
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


# ------------------------------------------------------------------
# Daily quota tracker (persisted to a small JSON file, committed by
# the workflow, so usage survives across scheduled runs).
# ------------------------------------------------------------------

def _quota_today_key():
    # Both providers reset their daily counters at UTC midnight.
    return now_utc().strftime("%Y-%m-%d")


def load_quota(path):
    today = _quota_today_key()
    data = load_json(path)
    if data and data.get("date") == today:
        return today, int(data.get("used", 0))
    return today, 0


def save_quota(path, today, used):
    write_json_quiet(path, {"date": today, "used": used})


def write_json_quiet(path, data):
    """Like write_json but without the console 'Written:' noise for meta files."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def bump_quota(path, today, used, by=1):
    used += by
    save_quota(path, today, used)
    return used


# ------------------------------------------------------------------
# HTTP with retries + exponential backoff (transient errors only)
# ------------------------------------------------------------------

def safe_get(url, params=None, headers=None, label="request"):
    """
    GET request with retries. Returns (json_or_none, permanent_failure)
    - json_or_none: parsed JSON body, or None if the call ultimately failed
    - permanent_failure: True if retrying is pointless (e.g. bad key,
      429, 4xx client errors) so callers can avoid burning quota
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                log_error(f"{label}: Rate limit exceeded (HTTP 429).")
                return None, True

            if 400 <= resp.status_code < 500:
                log_error(f"{label}: Client error (HTTP {resp.status_code}) -> {resp.text[:300]}")
                return None, True

            if resp.status_code >= 500:
                last_error = f"{label}: Server error (HTTP {resp.status_code}), attempt {attempt}."
                log_error(last_error)
                _backoff_sleep(attempt)
                continue

            try:
                return resp.json(), False
            except json.JSONDecodeError:
                log_error(f"{label}: Failed to parse JSON response.")
                return None, False

        except requests.exceptions.Timeout:
            last_error = f"{label}: Request timed out (attempt {attempt})."
            log_error(last_error)
            _backoff_sleep(attempt)
        except requests.exceptions.ConnectionError:
            last_error = f"{label}: Connection error (attempt {attempt})."
            log_error(last_error)
            _backoff_sleep(attempt)
        except Exception as e:  # noqa: BLE001
            log_error(f"{label}: Unexpected error -> {e}")
            return None, False

    log_error(f"{label}: All retries failed. Last error -> {last_error}")
    return None, False


def _backoff_sleep(attempt):
    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
    time.sleep(delay)


# ------------------------------------------------------------------
# Status / message helpers (bilingual, ready for the app to render)
# ------------------------------------------------------------------

MESSAGES = {
    "football": {
        "ok": {
            "bn": "সময়সূচি সফলভাবে আপডেট হয়েছে।",
            "en": "Schedule updated successfully.",
        },
        "no_matches": {
            "bn": f"আগামী {HOURS_AHEAD} ঘণ্টার মধ্যে কোনো ফুটবল ম্যাচ নেই।",
            "en": f"No football matches in the next {HOURS_AHEAD} hours.",
        },
        "quota_paused": {
            "bn": "আজকের ফুটবল ডাটা রিকোয়েস্ট সীমা প্রায় শেষ, তাই সাময়িকভাবে আপডেট বন্ধ আছে। আগের সংগৃহীত তথ্য দেখানো হচ্ছে।",
            "en": "Today's football API request budget is nearly used up, so updates are paused for now. Showing the last known data.",
        },
        "error": {
            "bn": "ফুটবল ডাটা আনতে সমস্যা হয়েছে। আগের সংগৃহীত তথ্য দেখানো হচ্ছে।",
            "en": "Couldn't fetch football data right now. Showing the last known data.",
        },
        "no_key": {
            "bn": "ফুটবল API Key কনফিগার করা নেই। GitHub Secrets চেক করুন।",
            "en": "Football API key is not configured. Check GitHub Secrets.",
        },
    },
    "cricket": {
        "ok": {
            "bn": "সময়সূচি সফলভাবে আপডেট হয়েছে।",
            "en": "Schedule updated successfully.",
        },
        "no_matches": {
            "bn": f"এই মুহূর্তে কোনো লাইভ বা আসন্ন ({HOURS_AHEAD} ঘণ্টার মধ্যে) ক্রিকেট ম্যাচ নেই।",
            "en": f"No live or upcoming cricket matches within the next {HOURS_AHEAD} hours.",
        },
        "quota_paused": {
            "bn": "আজকের ক্রিকেট ডাটা রিকোয়েস্ট সীমা প্রায় শেষ, তাই সাময়িকভাবে আপডেট বন্ধ আছে। আগের সংগৃহীত তথ্য দেখানো হচ্ছে।",
            "en": "Today's cricket API request budget is nearly used up, so updates are paused for now. Showing the last known data.",
        },
        "error": {
            "bn": "ক্রিকেট ডাটা আনতে সমস্যা হয়েছে। আগের সংগৃহীত তথ্য দেখানো হচ্ছে।",
            "en": "Couldn't fetch cricket data right now. Showing the last known data.",
        },
        "no_key": {
            "bn": "ক্রিকেট API Key কনফিগার করা নেই। GitHub Secrets চেক করুন।",
            "en": "Cricket API key is not configured. Check GitHub Secrets.",
        },
    },
}


def pick_next_match(matches):
    """Small preview of the soonest Live/Upcoming match for quick UI display."""
    for m in matches:
        category = m.get("status", {}).get("category")
        if category in ("Live", "Upcoming"):
            return {
                "matchId": m.get("matchId"),
                "date": m.get("date"),
                "time": m.get("time"),
                "status": category,
            }
    return None


def build_result(sport, state, existing, used, limit, matches=None):
    """
    Assembles the final JSON structure written to disk, for either sport.
    - state: "ok" | "no_matches" | "quota_paused" | "error" | "no_key"
    - existing: previously written JSON (or None) — used as fallback data
      when state indicates the fresh fetch didn't happen / failed.
    - matches: freshly parsed matches list, only provided when state is
      "ok" or "no_matches" (a genuinely successful fetch).
    """
    is_stale = state in ("quota_paused", "error", "no_key")
    now = now_utc()

    if matches is not None:
        final_matches = matches
    elif existing:
        final_matches = existing.get("matches", [])
    else:
        final_matches = []

    stale_since = None
    if is_stale:
        if existing:
            stale_since = existing.get("meta", {}).get("updatedAt") or existing.get("updatedAt")
        if not stale_since:
            stale_since = now.isoformat()

    display_state = state if state != "no_key" else "error"
    message = MESSAGES[sport].get(state, MESSAGES[sport]["error"])

    return {
        "sport": sport,
        "status": {
            "state": display_state,
            "message": message,
        },
        "meta": {
            "updatedAt": now.isoformat(),
            "lastUpdated": now_bd_str() + " (Bangladesh Time)",
            "timezone": "Asia/Dhaka (UTC+6)",
            "coverageHours": HOURS_AHEAD,
            "isStale": is_stale,
            "staleSince": stale_since,
            "apiRequestsUsedToday": used,
            "apiRequestsLimitToday": limit,
        },
        "totalMatches": len(final_matches),
        "nextMatch": pick_next_match(final_matches),
        "matches": final_matches,
    }


# ------------------------------------------------------------------
# FOOTBALL — API-Football (api-sports.io)
# ------------------------------------------------------------------

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


def parse_football_fixtures(raw_fixtures):
    window_end = now_utc() + timedelta(hours=HOURS_AHEAD)
    matches = []

    for fx in raw_fixtures:
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
    return matches


def build_football_json(existing):
    today, used = load_quota(FOOTBALL_QUOTA_PATH)

    if not FOOTBALL_API_KEY:
        log_error("FOOTBALL_API_KEY not found. Check GitHub Secrets configuration.")
        return build_result("football", "no_key", existing, used, FOOTBALL_DAILY_LIMIT)

    if used >= FOOTBALL_SAFE_CAP:
        log_error(
            f"Football: daily safety cap reached ({used}/{FOOTBALL_SAFE_CAP} of "
            f"{FOOTBALL_DAILY_LIMIT}/day quota). Skipping this run's API call."
        )
        return build_result("football", "quota_paused", existing, used, FOOTBALL_DAILY_LIMIT)

    bd_now = now_bd()
    date_from = bd_now.strftime("%Y-%m-%d")
    date_to = (bd_now + timedelta(days=1)).strftime("%Y-%m-%d")

    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    params = {"from": date_from, "to": date_to, "timezone": "Asia/Dhaka"}

    data, _permanent = safe_get(
        f"{FOOTBALL_BASE_URL}/fixtures",
        params=params,
        headers=headers,
        label=f"Football fixtures ({date_from} to {date_to})",
    )
    used = bump_quota(FOOTBALL_QUOTA_PATH, today, used, by=1)

    if data is None:
        return build_result("football", "error", existing, used, FOOTBALL_DAILY_LIMIT)

    # IMPORTANT: api-sports.io returns HTTP 200 even when the daily quota
    # is exceeded or the key/plan is invalid — the real error is embedded
    # inside the JSON body as a non-empty "errors" object, alongside an
    # empty "response": []. Without this check, a quota-exceeded response
    # looks identical to "no matches today" and would silently overwrite
    # the previous good file with an empty list.
    errors = data.get("errors")
    if errors:
        log_error(f"Football fixtures: API returned errors -> {errors}")
        return build_result("football", "error", existing, used, FOOTBALL_DAILY_LIMIT)

    raw_fixtures = data.get("response", [])
    matches = parse_football_fixtures(raw_fixtures)

    state = "ok" if matches else "no_matches"
    return build_result("football", state, existing, used, FOOTBALL_DAILY_LIMIT, matches=matches)


# ------------------------------------------------------------------
# CRICKET — CricketData.org (CricAPI v1)
# ------------------------------------------------------------------
# Uses /currentMatches (live + imminently upcoming matches) rather than
# the general /matches browse endpoint, which returns an arbitrary
# slice of ALL matches in their database and can miss what's "soon".

def map_cricket_status(match):
    if match.get("matchEnded"):
        return "Finished"
    if match.get("matchStarted") and not match.get("matchEnded"):
        return "Live"
    return "Upcoming"


def parse_cricket_matches(raw_matches):
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
                    "label": m.get("status") or category,
                },
                "result": m.get("status") if m.get("matchEnded") else None,
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"Failed to parse a cricket match: {e}")
            continue

    matches.sort(key=lambda m: (m["date"], m["time"]))
    return matches


def build_cricket_json(existing):
    today, used = load_quota(CRICKET_QUOTA_PATH)

    if not CRICKET_API_KEY:
        log_error("CRICKET_API_KEY not found. Check GitHub Secrets configuration.")
        return build_result("cricket", "no_key", existing, used, CRICKET_DAILY_LIMIT)

    if used >= CRICKET_SAFE_CAP:
        log_error(
            f"Cricket: daily safety cap reached ({used}/{CRICKET_SAFE_CAP} of "
            f"{CRICKET_DAILY_LIMIT}/day quota). Skipping this run's API calls."
        )
        return build_result("cricket", "quota_paused", existing, used, CRICKET_DAILY_LIMIT)

    all_matches = []
    any_success = False
    any_hard_failure = False

    for page in range(CRICKET_MAX_PAGES_PER_RUN):
        if used >= CRICKET_SAFE_CAP:
            log_error("Cricket: safety cap reached mid-run, stopping further pagination.")
            break

        offset = page * CRICKET_PAGE_SIZE
        params = {"apikey": CRICKET_API_KEY, "offset": offset}
        data, permanent = safe_get(
            f"{CRICKET_BASE_URL}/currentMatches",
            params=params,
            label=f"Cricket currentMatches (offset {offset})",
        )
        used = bump_quota(CRICKET_QUOTA_PATH, today, used, by=1)

        if data is None:
            any_hard_failure = any_hard_failure or permanent
            break

        if data.get("status") != "success":
            log_error(f"Cricket API returned an error status: {data.get('status')} - {data.get('reason')}")
            any_hard_failure = True
            break

        any_success = True
        page_matches = data.get("data", [])
        all_matches.extend(page_matches)

        # Stop early if this page came back short (means no more pages).
        if len(page_matches) < CRICKET_PAGE_SIZE:
            break

    if not any_success:
        state = "error" if any_hard_failure else "error"
        return build_result("cricket", state, existing, used, CRICKET_DAILY_LIMIT)

    matches = parse_cricket_matches(all_matches)
    state = "ok" if matches else "no_matches"
    return build_result("cricket", state, existing, used, CRICKET_DAILY_LIMIT, matches=matches)


# ------------------------------------------------------------------
# Combined status.json — quick, unified snapshot for dashboards
# ------------------------------------------------------------------

def build_status_summary(football_result, cricket_result):
    def summarize(result):
        return {
            "state": result["status"]["state"],
            "message": result["status"]["message"],
            "totalMatches": result["totalMatches"],
            "nextMatch": result["nextMatch"],
            "isStale": result["meta"]["isStale"],
        }

    return {
        "generatedAt": now_utc().isoformat(),
        "lastUpdated": now_bd_str() + " (Bangladesh Time)",
        "football": summarize(football_result),
        "cricket": summarize(cricket_result),
    }


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    print(f"=== Sports Data Update starting | {now_bd_str()} BDT ===")

    existing_football = load_json(FOOTBALL_JSON_PATH)
    existing_cricket = load_json(CRICKET_JSON_PATH)

    try:
        football_result = build_football_json(existing_football)
    except Exception as e:  # noqa: BLE001
        log_error(f"Unexpected error building football data: {e}\n{traceback.format_exc()}")
        _, used = load_quota(FOOTBALL_QUOTA_PATH)
        football_result = build_result("football", "error", existing_football, used, FOOTBALL_DAILY_LIMIT)

    write_json(FOOTBALL_JSON_PATH, football_result)

    try:
        cricket_result = build_cricket_json(existing_cricket)
    except Exception as e:  # noqa: BLE001
        log_error(f"Unexpected error building cricket data: {e}\n{traceback.format_exc()}")
        _, used = load_quota(CRICKET_QUOTA_PATH)
        cricket_result = build_result("cricket", "error", existing_cricket, used, CRICKET_DAILY_LIMIT)

    write_json(CRICKET_JSON_PATH, cricket_result)

    status_summary = build_status_summary(football_result, cricket_result)
    write_json(STATUS_JSON_PATH, status_summary)

    # Friendly one-line-per-sport summary in the Action logs.
    def label_for(result):
        s = result["status"]["state"]
        n = result["totalMatches"]
        used = result["meta"]["apiRequestsUsedToday"]
        limit = result["meta"]["apiRequestsLimitToday"]
        icon = {"ok": "✅", "no_matches": "💤", "error": "⚠️", "quota_paused": "⏸️"}.get(s, "❓")
        return f"{icon} {s} | matches={n} | requests_used_today={used}/{limit}"

    print(f"Football -> {label_for(football_result)}")
    print(f"Cricket  -> {label_for(cricket_result)}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
