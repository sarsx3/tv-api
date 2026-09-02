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
  - Football : football-data.org (v4)             -> https://www.football-data.org
  - Cricket  : CricketData.org (formerly CricAPI)  -> https://cricketdata.org

NOTE ON THE FOOTBALL PROVIDER (v3 of this script)
---------------------------------------------------
This script previously used API-Football (api-sports.io). That
provider's free tier repeatedly auto-suspended this project's account
for making requests from a shared/cloud IP (GitHub Actions runners
use datacenter IPs, which its anti-abuse system flags as "hosting
provider traffic") — a suspension unrelated to quota usage or code
correctness. football-data.org has none of that: it's been free since
2013, is widely run from CI/cron by hobby projects, and uses a simple
per-minute rate limit (10 req/min on the free tier) instead of a
fragile daily counter. The trade-off is coverage: the free tier only
includes ~12 major competitions (Premier League, La Liga, Bundesliga,
Serie A, Ligue 1, Champions League, Eredivisie, Primeira Liga, the
Championship, Brazilian Série A, the World Cup, and the Euros) rather
than API-Football's 1000+ leagues.

DESIGN GOALS
------------
1. Practically impossible to run into a rate limit.
   - Football makes exactly ONE request per run (a single dateFrom/
     dateTo range covering the next two days), against a 10-req/min
     budget — nowhere close, even with retries.
   - Cricket (CricketData.org) still has a real daily quota (100/day
     on the free tier), so it keeps the persistent quota-tracker file
     (sports/.meta/cricket_quota.json, resets at UTC midnight) and a
     safety cap (default 90/100) that stops new calls before the real
     limit is ever reached. Configurable via CRICKET_DAILY_LIMIT /
     CRICKET_SAFE_CAP env vars.

2. Never mistakes "API failed" for "no matches today".
   - football-data.org uses standard HTTP error codes (400/403/429),
     which safe_get() already treats as failures — no embedded-error
     quirk to work around here.
   - CricketData.org's "status" field is checked explicitly, since it
     can return HTTP 200 with a "failure" status in the body.

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
import math
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

CRICKET_QUOTA_PATH = os.path.join(META_DIR, "cricket_quota.json")

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "").strip()
CRICKET_API_KEY = os.environ.get("CRICKET_API_KEY", "").strip()

FOOTBALL_BASE_URL = "https://api.football-data.org/v4"
CRICKET_BASE_URL = "https://api.cricapi.com/v1"

# football-data.org free tier: 10 requests/minute (no daily counter).
# We make exactly 1 request per run, so this is purely informational —
# it's surfaced in the output JSON for transparency, not enforced.
FOOTBALL_RATE_LIMIT_PER_MINUTE = int(os.environ.get("FOOTBALL_RATE_LIMIT_PER_MINUTE", "10"))

# CricketData.org free tier: 100 requests/day. We stop at 90 by default
# so retries / manual "Run workflow" clicks never push us over the
# real limit.
CRICKET_DAILY_LIMIT = int(os.environ.get("CRICKET_DAILY_LIMIT", "100"))
CRICKET_SAFE_CAP = int(os.environ.get("CRICKET_SAFE_CAP", "90"))

REQUEST_TIMEOUT = 15          # seconds
MAX_RETRIES = 2               # retries per API call (on transient errors only)
RETRY_BASE_DELAY = 3          # seconds, exponential backoff base

HOURS_AHEAD = 168              # how far ahead to show matches (hours) — 7 days
COVERAGE_DAYS_LABEL = f"{HOURS_AHEAD // 24} দিনের" if HOURS_AHEAD % 24 == 0 else f"{HOURS_AHEAD} ঘণ্টার"
COVERAGE_DAYS_LABEL_EN = f"{HOURS_AHEAD // 24}-day" if HOURS_AHEAD % 24 == 0 else f"{HOURS_AHEAD}-hour"

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

def _check_rate_limit_headers(resp, label):
    """
    Surfaces any rate-limit headers a provider sends back (football-data.org
    sends X-Requests-Available-Minute / X-RequestCounter-Reset; many others
    use the X-RateLimit-* convention). Logged to stdout always, and escalated
    to error_log.txt when we're down to the last request or two, so problems
    are visible before they turn into a 429.
    """
    headers = resp.headers
    remaining = headers.get("X-Requests-Available-Minute") or headers.get("X-RateLimit-Remaining")
    limit = headers.get("X-RateLimit-Limit")
    if remaining is None:
        return
    note = f"{label}: rate limit remaining = {remaining}" + (f"/{limit}" if limit else "")
    print(note)
    try:
        if int(remaining) <= 1:
            log_error(f"{note} (running low — consider spacing out requests further).")
    except ValueError:
        pass


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
            _check_rate_limit_headers(resp, label)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                suffix = f" (Retry-After: {retry_after}s)" if retry_after else ""
                log_error(f"{label}: Rate limit exceeded (HTTP 429){suffix}.")
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
            "bn": f"আগামী {COVERAGE_DAYS_LABEL} মধ্যে বড় লীগগুলোতে কোনো ফুটবল ম্যাচ নেই।",
            "en": f"No football matches in the next {COVERAGE_DAYS_LABEL_EN} window across the covered leagues.",
        },
        "quota_paused": {
            # Not used for football (no daily quota with this provider) but
            # kept so build_result() has a message for every possible state.
            "bn": "ফুটবল ডাটা আপডেট সাময়িকভাবে বন্ধ আছে। আগের সংগৃহীত তথ্য দেখানো হচ্ছে।",
            "en": "Football data updates are paused for now. Showing the last known data.",
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
            "bn": f"এই মুহূর্তে কোনো লাইভ বা আসন্ন ({COVERAGE_DAYS_LABEL} মধ্যে) ক্রিকেট ম্যাচ নেই।",
            "en": f"No live or upcoming cricket matches within the next {COVERAGE_DAYS_LABEL_EN} window.",
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


def build_result(sport, state, existing, matches=None, extra_meta=None):
    """
    Assembles the final JSON structure written to disk, for either sport.
    - state: "ok" | "no_matches" | "quota_paused" | "error" | "no_key"
    - existing: previously written JSON (or None) — used as fallback data
      when state indicates the fresh fetch didn't happen / failed.
    - matches: freshly parsed matches list, only provided when state is
      "ok" or "no_matches" (a genuinely successful fetch).
    - extra_meta: sport-specific meta fields (quota usage, rate limit,
      coverage notes, etc.) merged into the "meta" object.
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

    meta = {
        "updatedAt": now.isoformat(),
        "lastUpdated": now_bd_str() + " (Bangladesh Time)",
        "timezone": "Asia/Dhaka (UTC+6)",
        "coverageHours": HOURS_AHEAD,
        "isStale": is_stale,
        "staleSince": stale_since,
    }
    if extra_meta:
        meta.update(extra_meta)

    return {
        "sport": sport,
        "status": {
            "state": display_state,
            "message": message,
        },
        "meta": meta,
        "totalMatches": len(final_matches),
        "nextMatch": pick_next_match(final_matches),
        "matches": final_matches,
    }


# ------------------------------------------------------------------
# FOOTBALL — football-data.org (v4)
# ------------------------------------------------------------------

FOOTBALL_COVERAGE_NOTE = {
    "bn": "শুধু প্রধান লীগ ও টুর্নামেন্ট কভার করা হয়: প্রিমিয়ার লীগ, লা লিগা, বুন্দেসলিগা, "
          "সিরি আ, লিগ ১, চ্যাম্পিয়নস লীগ, এরেডিভিজি, প্রিমেইরা লিগা, চ্যাম্পিয়নশিপ, "
          "ব্রাজিল সিরি আ, বিশ্বকাপ ও ইউরো।",
    "en": "Covers major leagues/tournaments only: Premier League, La Liga, Bundesliga, "
          "Serie A, Ligue 1, Champions League, Eredivisie, Primeira Liga, the Championship, "
          "Brazilian Série A, the World Cup, and the Euros.",
}

# football-data.org v4 status values -> our normalized categories.
FOOTBALL_STATUS_MAP = {
    "SCHEDULED":         ("Upcoming", "Scheduled"),
    "TIMED":             ("Upcoming", "Time Confirmed"),
    "IN_PLAY":           ("Live", "In Play"),
    "PAUSED":            ("Live", "Half Time"),
    "EXTRA_TIME":        ("Live", "Extra Time"),
    "PENALTY_SHOOTOUT":  ("Live", "Penalty Shootout"),
    "SUSPENDED":         ("Live", "Suspended"),
    "FINISHED":          ("Finished", "Full Time"),
    "AWARDED":           ("Finished", "Awarded"),
    "POSTPONED":         ("Postponed", "Postponed"),
    "CANCELLED":         ("Cancelled", "Cancelled"),
}


def map_football_status(status_word):
    return FOOTBALL_STATUS_MAP.get(status_word, ("Unknown", status_word or "Unknown"))


def _parse_iso(dt_str):
    # football-data.org returns e.g. "2026-09-02T16:05:00Z". Normalize the
    # trailing "Z" defensively instead of relying on the Python version's
    # fromisoformat() to understand it.
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def parse_football_matches(raw_matches):
    window_end = now_utc() + timedelta(hours=HOURS_AHEAD)
    matches = []

    for fx in raw_matches:
        try:
            utc_date = fx.get("utcDate")
            if not utc_date:
                continue
            match_dt_utc = _parse_iso(utc_date)
            if match_dt_utc.tzinfo is None:
                match_dt_utc = match_dt_utc.replace(tzinfo=UTC)

            # Keep matches within HOURS_AHEAD, plus recently-started ones
            # (so live matches still show).
            if match_dt_utc < (now_utc() - timedelta(hours=4)) or match_dt_utc > window_end:
                continue

            match_dt_bd = match_dt_utc.astimezone(BD_TZ)
            status_word = fx.get("status")
            category, label = map_football_status(status_word)

            competition = fx.get("competition", {}) or {}
            area = fx.get("area", {}) or {}
            home = fx.get("homeTeam", {}) or {}
            away = fx.get("awayTeam", {}) or {}
            score = fx.get("score", {}) or {}
            full_time = score.get("fullTime", {}) or {}
            half_time = score.get("halfTime", {}) or {}
            season = fx.get("season", {}) or {}

            matchday = fx.get("matchday")

            matches.append({
                "matchId": fx.get("id"),
                "sport": "football",
                "league": {
                    "id": competition.get("id"),
                    "name": competition.get("name"),
                    "country": area.get("name"),
                    "logo": competition.get("emblem"),
                    "round": f"Matchday {matchday}" if matchday else fx.get("stage"),
                    "season": (season.get("startDate") or "")[:4] or None,
                },
                "homeTeam": {
                    "id": home.get("id"),
                    "name": home.get("name"),
                    "logo": home.get("crest"),
                },
                "awayTeam": {
                    "id": away.get("id"),
                    "name": away.get("name"),
                    "logo": away.get("crest"),
                },
                "venue": fx.get("venue"),
                "date": match_dt_bd.strftime("%Y-%m-%d"),
                "time": match_dt_bd.strftime("%H:%M"),
                "bdDate": match_dt_bd.strftime("%Y-%m-%d"),
                "bdTime": match_dt_bd.strftime("%H:%M"),
                "status": {
                    "code": status_word,
                    "category": category,       # Upcoming / Live / Finished / Postponed / Cancelled
                    "label": label,
                },
                "halfTimeScore": half_time if half_time else None,
                "homeScore": full_time.get("home"),
                "awayScore": full_time.get("away"),
            })
        except Exception as e:  # noqa: BLE001
            log_error(f"Failed to parse a football match: {e}")
            continue

    matches.sort(key=lambda m: (m["date"], m["time"]))
    return matches


def build_football_json(existing):
    extra_meta = {
        "provider": "football-data.org",
        "rateLimitPerMinute": FOOTBALL_RATE_LIMIT_PER_MINUTE,
        "coverageNote": FOOTBALL_COVERAGE_NOTE,
    }

    if not FOOTBALL_API_KEY:
        log_error("FOOTBALL_API_KEY not found. Check GitHub Secrets configuration.")
        return build_result("football", "no_key", existing, extra_meta=extra_meta)

    bd_now = now_bd()
    # football-data.org excludes the dateTo day itself, so add an extra day
    # of buffer on top of the HOURS_AHEAD window to make sure the last day
    # is fully included regardless of what time of day this runs.
    coverage_days = math.ceil(HOURS_AHEAD / 24) + 1
    date_from = bd_now.strftime("%Y-%m-%d")
    date_to = (bd_now + timedelta(days=coverage_days)).strftime("%Y-%m-%d")

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    params = {"dateFrom": date_from, "dateTo": date_to}

    data, _permanent = safe_get(
        f"{FOOTBALL_BASE_URL}/matches",
        params=params,
        headers=headers,
        label=f"Football matches ({date_from} to {date_to})",
    )

    if data is None:
        return build_result("football", "error", existing, extra_meta=extra_meta)

    raw_matches = data.get("matches")
    if raw_matches is None:
        # Standard HTTP errors are already caught by safe_get(); this
        # covers the unlikely case of a 200 with an unexpected body shape.
        log_error(f"Football matches: unexpected response shape -> {str(data)[:300]}")
        return build_result("football", "error", existing, extra_meta=extra_meta)

    matches = parse_football_matches(raw_matches)

    state = "ok" if matches else "no_matches"
    return build_result("football", state, existing, matches=matches, extra_meta=extra_meta)


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

    def meta_with_usage(final_used):
        return {
            "provider": "CricketData.org",
            "apiRequestsUsedToday": final_used,
            "apiRequestsLimitToday": CRICKET_DAILY_LIMIT,
        }

    if not CRICKET_API_KEY:
        log_error("CRICKET_API_KEY not found. Check GitHub Secrets configuration.")
        return build_result("cricket", "no_key", existing, extra_meta=meta_with_usage(used))

    if used >= CRICKET_SAFE_CAP:
        log_error(
            f"Cricket: daily safety cap reached ({used}/{CRICKET_SAFE_CAP} of "
            f"{CRICKET_DAILY_LIMIT}/day quota). Skipping this run's API calls."
        )
        return build_result("cricket", "quota_paused", existing, extra_meta=meta_with_usage(used))

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
        return build_result("cricket", "error", existing, extra_meta=meta_with_usage(used))

    matches = parse_cricket_matches(all_matches)
    state = "ok" if matches else "no_matches"
    return build_result("cricket", state, existing, matches=matches, extra_meta=meta_with_usage(used))


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
        football_result = build_result(
            "football", "error", existing_football,
            extra_meta={"provider": "football-data.org", "rateLimitPerMinute": FOOTBALL_RATE_LIMIT_PER_MINUTE},
        )

    write_json(FOOTBALL_JSON_PATH, football_result)

    try:
        cricket_result = build_cricket_json(existing_cricket)
    except Exception as e:  # noqa: BLE001
        log_error(f"Unexpected error building cricket data: {e}\n{traceback.format_exc()}")
        _, used = load_quota(CRICKET_QUOTA_PATH)
        cricket_result = build_result(
            "cricket", "error", existing_cricket,
            extra_meta={"provider": "CricketData.org", "apiRequestsUsedToday": used, "apiRequestsLimitToday": CRICKET_DAILY_LIMIT},
        )

    write_json(CRICKET_JSON_PATH, cricket_result)

    status_summary = build_status_summary(football_result, cricket_result)
    write_json(STATUS_JSON_PATH, status_summary)

    # Friendly one-line-per-sport summary in the Action logs.
    def label_for(result):
        s = result["status"]["state"]
        n = result["totalMatches"]
        meta = result["meta"]
        icon = {"ok": "✅", "no_matches": "💤", "error": "⚠️", "quota_paused": "⏸️"}.get(s, "❓")

        if "apiRequestsUsedToday" in meta:
            quota_note = f" | requests_used_today={meta['apiRequestsUsedToday']}/{meta['apiRequestsLimitToday']}"
        elif "rateLimitPerMinute" in meta:
            quota_note = f" | rate_limit={meta['rateLimitPerMinute']}/min (1 call made)"
        else:
            quota_note = ""

        return f"{icon} {s} | matches={n}{quota_note}"

    print(f"Football -> {label_for(football_result)}")
    print(f"Cricket  -> {label_for(cricket_result)}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
