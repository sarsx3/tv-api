#!/usr/bin/env python3
"""
FlashiFly Sports — Schedule Enrichment Script
=============================================
কাজ: sports/football.json + sports/cricket.json + channels/mapping.json + channels/streams.json
      → output/football.json + output/cricket.json (enriched, Flutter app এটা পড়বে)
"""

import json
import os
from datetime import datetime, timezone

# ─── File Paths ────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOOTBALL_JSON   = os.path.join(BASE_DIR, "sports", "football.json")
CRICKET_JSON    = os.path.join(BASE_DIR, "sports", "cricket.json")
MAPPING_JSON    = os.path.join(BASE_DIR, "channels", "mapping.json")
STREAMS_JSON    = os.path.join(BASE_DIR, "channels", "streams.json")
OUTPUT_DIR      = os.path.join(BASE_DIR, "output")

# ─── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ Saved: {path}")

def build_streams_map(streams_data: dict) -> dict:
    """channel_id → full channel object (name, logo, sources)"""
    result = {}
    for ch in streams_data.get("channels", []):
        result[ch["channel_id"]] = {
            "channelId"  : ch["channel_id"],
            "name"       : ch["name"],
            "logo"       : ch.get("logo", ""),
            "country"    : ch.get("country", ""),
            "sources"    : ch.get("sources", [])
        }
    return result

def match_keywords(text: str, keywords: list) -> bool:
    """Case-insensitive keyword match"""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)

def find_channels(identifier: str, sport: str, mapping: dict, streams_map: dict) -> list:
    """
    identifier: football এ = league.name, cricket এ = matchTitle
    Returns: list of enriched channel objects
    """
    rules = mapping.get(sport, [])
    matched_channel_ids = []

    for rule in rules:
        if match_keywords(identifier, rule["keywords"]):
            matched_channel_ids = rule["channels"]
            break  # প্রথম match নেওয়া হবে

    # fallback
    if not matched_channel_ids:
        matched_channel_ids = mapping.get("fallback", {}).get(sport, [])

    # streams_map থেকে full channel data বের করো
    channels = []
    for ch_id in matched_channel_ids:
        if ch_id in streams_map:
            channels.append(streams_map[ch_id])
        else:
            print(f"  ⚠️  Channel ID '{ch_id}' streams.json এ নেই, skip করা হলো।")

    return channels

def has_live_source(channels: list) -> bool:
    """কোনো channel এ অন্তত একটি non-empty source URL আছে কিনা"""
    for ch in channels:
        for src in ch.get("sources", []):
            if src.get("url", "").strip():
                return True
    return False

# ─── Enrich Football ───────────────────────────────────────────────────────────

def enrich_football(football_data: dict, mapping: dict, streams_map: dict) -> dict:
    print("\n⚽ Football enrichment শুরু...")
    enriched_matches = []

    for match in football_data.get("matches", []):
        league_name  = match.get("league", {}).get("name", "")
        country      = match.get("league", {}).get("country", "")

        # Brazil Serie A আলাদা করতে country check
        identifier = league_name
        if league_name == "Serie A" and country == "Brazil":
            identifier = "Campeonato Brasileiro Serie A"

        channels = find_channels(identifier, "football", mapping, streams_map)

        enriched_match = dict(match)
        enriched_match["channels"]      = channels
        enriched_match["hasLiveSource"] = has_live_source(channels)
        enriched_matches.append(enriched_match)

        status = match.get("status", {}).get("category", "")
        print(f"  [{status:10}] {league_name} — "
              f"{match.get('homeTeam',{}).get('name','')} vs "
              f"{match.get('awayTeam',{}).get('name','')} "
              f"→ {len(channels)} channel(s)")

    result = dict(football_data)
    result["matches"]     = enriched_matches
    result["enrichedAt"]  = datetime.now(timezone.utc).isoformat()
    print(f"  ✅ Football: {len(enriched_matches)} matches enriched.")
    return result

# ─── Enrich Cricket ────────────────────────────────────────────────────────────

def enrich_cricket(cricket_data: dict, mapping: dict, streams_map: dict) -> dict:
    print("\n🏏 Cricket enrichment শুরু...")
    enriched_matches = []

    for match in cricket_data.get("matches", []):
        match_title = match.get("matchTitle", "")
        channels    = find_channels(match_title, "cricket", mapping, streams_map)

        enriched_match = dict(match)
        enriched_match["channels"]      = channels
        enriched_match["hasLiveSource"] = has_live_source(channels)
        enriched_matches.append(enriched_match)

        status = match.get("status", {}).get("category", "")
        print(f"  [{status:10}] {match_title[:55]:55} → {len(channels)} channel(s)")

    result = dict(cricket_data)
    result["matches"]    = enriched_matches
    result["enrichedAt"] = datetime.now(timezone.utc).isoformat()
    print(f"  ✅ Cricket: {len(enriched_matches)} matches enriched.")
    return result

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  FlashiFly — Sports Schedule Enrichment")
    print("=" * 60)

    # Load files
    print("\n📂 Files লোড হচ্ছে...")
    mapping      = load_json(MAPPING_JSON)
    streams_data = load_json(STREAMS_JSON)
    streams_map  = build_streams_map(streams_data)
    print(f"  Channels loaded: {len(streams_map)}")

    # Football
    football_data    = load_json(FOOTBALL_JSON)
    enriched_football = enrich_football(football_data, mapping, streams_map)
    save_json(os.path.join(OUTPUT_DIR, "football.json"), enriched_football)

    # Cricket
    cricket_data    = load_json(CRICKET_JSON)
    enriched_cricket = enrich_cricket(cricket_data, mapping, streams_map)
    save_json(os.path.join(OUTPUT_DIR, "cricket.json"), enriched_cricket)

    print("\n" + "=" * 60)
    print("  ✅ সব ঠিকঠাক! output/ ফোল্ডারে দেখুন।")
    print("=" * 60)

if __name__ == "__main__":
    main()
