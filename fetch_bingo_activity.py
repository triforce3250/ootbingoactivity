#!/usr/bin/env python3
"""
OoT Bingo Activity Fetcher for Racetime.gg

Queries the category-level endpoint (https://racetime.gg/oot/races/data)
instead of per-user endpoints so it captures ALL players (including new and
returning summer tournament players) without duplicate race-detail fetches.

Usage examples:
  # Fetch full history since the first OoT Bingo race on 2020-05-05 (default)
  python fetch_bingo_activity.py

  # Fetch an arbitrary date range
  python fetch_bingo_activity.py --start-date 2025-05-01 --end-date 2025-10-01

  # Incrementally update an existing bingo_activity.json with new races
  python fetch_bingo_activity.py --incremental

  # Include all Bingo modes (not just mode=normal)
  python fetch_bingo_activity.py --include-all-modes
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://racetime.gg"
CATEGORY_RACES_URL = f"{BASE_URL}/oot/races/data"
HEADERS = {
    "User-Agent": "OoT-Bingo-Activity-Tracker/2.0 (Contact: triforce3250)"
}


def parse_iso_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    # Handle trailing 'Z' for Python < 3.11 compatibility
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def parse_iso_duration_seconds(duration_str: Optional[str]) -> Optional[float]:
    """Parse ISO-8601 duration like 'P0DT01H12M34.567890S' into seconds."""
    if not duration_str:
        return None
    match = re.match(
        r"^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$",
        duration_str,
    )
    if not match:
        return None
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = float(match.group(4) or 0.0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def extract_bingo_metadata(info: str) -> Dict[str, str]:
    info_lower = (info or "").lower()
    version_match = re.search(r"/v([\d.]+)/|version=([\d.]+)", info_lower)
    bingo_version = (
        version_match.group(1) or version_match.group(2)
        if version_match
        else "Unknown"
    )
    mode_match = re.search(r"mode=([a-z0-9_-]+)", info_lower)
    if mode_match:
        bingo_mode = mode_match.group(1)
    elif "blackout" in info_lower:
        bingo_mode = "blackout"
    elif "anti-bingo" in info_lower:
        bingo_mode = "anti-bingo"
    else:
        bingo_mode = "unknown"
    return {"bingo_version": bingo_version, "bingo_mode": bingo_mode}


def request_json(
    session: requests.Session,
    url: str,
    max_retries: int = 4,
) -> Optional[Dict[str, Any]]:
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"\n  [429 Rate Limited] Sleeping {retry_after:.1f}s...")
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            print(f"\n  [HTTP {resp.status_code}] Skipping {url}")
            return None
        except requests.RequestException as exc:
            wait_s = 2 ** attempt
            print(f"\n  [Network Error] {exc} — retrying in {wait_s}s...")
            time.sleep(wait_s)
    return None


def is_valid_bingo_race(race: Dict[str, Any], include_all_modes: bool) -> bool:
    goal = race.get("goal") or {}
    if (goal.get("name") or "").lower() != "bingo":
        return False
    if not race.get("recorded", False):
        return False
    info = (race.get("info") or "").lower()
    if "anti-bingo" in info:
        return False
    if include_all_modes:
        return "ootbingo.github.io/bingo" in info or "bingo" in (goal.get("name") or "").lower()
    return (
        "ootbingo.github.io/bingo" in info
        and "mode=normal" in info
        and "blackout" not in info
        and "short" not in info
    )


def fetch_races(
    start_dt: dt.datetime,
    end_dt: dt.datetime,
    include_all_modes: bool = False,
    existing_races_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
    incremental_cutoff: Optional[dt.datetime] = None,
    delay: float = 0.15,
    max_pages: int = 5000,
) -> Dict[str, Dict[str, Any]]:
    races_by_name: Dict[str, Dict[str, Any]] = dict(existing_races_by_name or {})
    session = requests.Session()
    page = 1
    reached_older_than_start = False

    print(
        f"Querying {CATEGORY_RACES_URL} from "
        f"{start_dt.date().isoformat()} to {end_dt.date().isoformat()}..."
    )

    while page <= max_pages and not reached_older_than_start:
        page_url = f"{CATEGORY_RACES_URL}?page={page}"
        data = request_json(session, page_url)
        if not data:
            break

        races = data.get("races") or []
        if not races:
            break

        num_pages = data.get("num_pages", 1)
        print(f"Page {page}/{num_pages} ({len(races)} races)...", end="", flush=True)
        matched_on_page = 0

        for race in races:
            race_name = race.get("name")
            race_dt = parse_iso_datetime(race.get("ended_at") or race.get("opened_at"))
            if not race_dt or not race_name:
                continue

            # Category races are ordered newest-first; once we pass start_dt, stop.
            if race_dt < start_dt:
                reached_older_than_start = True
                break

            if incremental_cutoff and race_dt <= incremental_cutoff and race_name in races_by_name:
                reached_older_than_start = True
                break

            if race_dt > end_dt:
                continue

            if not is_valid_bingo_race(race, include_all_modes):
                continue

            if race_name in races_by_name:
                continue

            detail_url = f"{BASE_URL}{race['data_url']}"
            details = request_json(session, detail_url)
            if not details:
                continue

            meta = extract_bingo_metadata(race.get("info") or "")
            entrants_out: List[Dict[str, Any]] = []
            finished_count = 0

            for entrant in details.get("entrants") or []:
                user = entrant.get("user") or {}
                status_val = (entrant.get("status") or {}).get("value", "")
                finish_time = entrant.get("finish_time")
                if status_val == "done" and finish_time:
                    finished_count += 1
                entrants_out.append(
                    {
                        "user_id": user.get("id"),
                        "username": user.get("name"),
                        "full_name": user.get("full_name"),
                        "status": status_val,
                        "finish_time": finish_time,
                        "finish_seconds": parse_iso_duration_seconds(finish_time),
                        "place": entrant.get("place"),
                    }
                )

            races_by_name[race_name] = {
                "name": race_name,
                "url": f"{BASE_URL}{race.get('url', '')}",
                "opened_at": race.get("opened_at"),
                "ended_at": race.get("ended_at"),
                "info": race.get("info") or "",
                "bingo_version": meta["bingo_version"],
                "bingo_mode": meta["bingo_mode"],
                "entrant_count": len(entrants_out),
                "finished_count": finished_count,
                "entrants": entrants_out,
            }
            matched_on_page += 1
            time.sleep(delay)

        print(f" +{matched_on_page} Bingo races")
        if page >= num_pages:
            break
        page += 1
        time.sleep(delay)

    return races_by_name


def build_output_payload(
    races_by_name: Dict[str, Dict[str, Any]],
    start_dt: dt.datetime,
    end_dt: dt.datetime,
) -> Dict[str, Any]:
    races_list = sorted(
        races_by_name.values(),
        key=lambda r: r.get("ended_at") or r.get("opened_at") or "",
        reverse=True,
    )
    unique_players = set()
    for race in races_list:
        for entrant in race.get("entrants") or []:
            if entrant.get("user_id"):
                unique_players.add(entrant["user_id"])

    return {
        "metadata": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "category": "oot",
            "goal": "Bingo",
            "start_date": start_dt.date().isoformat(),
            "end_date": end_dt.date().isoformat(),
            "total_races": len(races_list),
            "unique_players": len(unique_players),
        },
        "races": races_list,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query and store OoT Bingo races from Racetime.gg."
    )
    today = dt.datetime.now(dt.timezone.utc).date()
    default_start = "2020-05-05"
    default_end = today.isoformat()

    parser.add_argument(
        "--start-date",
        default=default_start,
        help=f"Inclusive UTC start date YYYY-MM-DD (default: {default_start})",
    )
    parser.add_argument(
        "--end-date",
        default=default_end,
        help=f"Inclusive UTC end date YYYY-MM-DD (default: {default_end})",
    )
    parser.add_argument(
        "--output",
        default="bingo_activity.json",
        help="Output JSON file path (default: bingo_activity.json)",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Merge new races into an existing output JSON file.",
    )
    parser.add_argument(
        "--include-all-modes",
        action="store_true",
        help="Include non-normal Bingo modes (e.g. blackout) in addition to mode=normal.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Delay in seconds between API requests (default: 0.15).",
    )
    args = parser.parse_args()

    start_dt = dt.datetime.fromisoformat(args.start_date).replace(
        hour=0, minute=0, second=0, tzinfo=dt.timezone.utc
    )
    end_dt = dt.datetime.fromisoformat(args.end_date).replace(
        hour=23, minute=59, second=59, tzinfo=dt.timezone.utc
    )

    existing_races: Dict[str, Dict[str, Any]] = {}
    incremental_cutoff: Optional[dt.datetime] = None

    if args.incremental and os.path.exists(args.output):
        with open(args.output, "r", encoding="utf-8") as f:
            prev = json.load(f)
        for race in prev.get("races") or []:
            if race.get("name"):
                existing_races[race["name"]] = race
        if existing_races:
            timestamps = [
                parse_iso_datetime(r.get("ended_at") or r.get("opened_at"))
                for r in existing_races.values()
            ]
            valid_ts = [t for t in timestamps if t is not None]
            if valid_ts:
                incremental_cutoff = max(valid_ts)
                print(
                    f"Loaded {len(existing_races)} existing races. "
                    f"Fetching races newer than {incremental_cutoff.isoformat()}."
                )

    races_by_name = fetch_races(
        start_dt=start_dt,
        end_dt=end_dt,
        include_all_modes=args.include_all_modes,
        existing_races_by_name=existing_races,
        incremental_cutoff=incremental_cutoff,
        delay=args.delay,
    )

    payload = build_output_payload(races_by_name, start_dt, end_dt)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(
        f"\nSaved {payload['metadata']['total_races']} races "
        f"({payload['metadata']['unique_players']} unique players) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())