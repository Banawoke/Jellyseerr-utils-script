#!/usr/bin/env python3
"""
SeerrSentinel Cleaner — Detection and cleanup of missing/stalled media.

Can be run directly or via:
    python3 seerr_sentinel.py clean [--dry-run]
"""

import argparse
import requests
import os
import json
from datetime import datetime, timedelta, timezone
import sys

# Config centralisée (charge aussi le .env)
from seerr_sentinel import load_config

_cfg = load_config([
    "JELLYSEER_API_KEY",
    "JELLYSEER_URL",
    "TMDB_API_KEY",
    "RADARR_API_KEY",
    "RADARR_URL",
    "SONARR_API_KEY",
    "SONARR_URL",
])

JELLYSEER_API_KEY = _cfg["JELLYSEER_API_KEY"]
JELLYSEER_URL = _cfg["JELLYSEER_URL"]
TMDB_API_KEY = _cfg["TMDB_API_KEY"]
RADARR_API_KEY = _cfg["RADARR_API_KEY"]
RADARR_URL = _cfg["RADARR_URL"]
SONARR_API_KEY = _cfg["SONARR_API_KEY"]
SONARR_URL = _cfg["SONARR_URL"]
TARGET_TMDB_IDS = []
TMDB_TITLE_CACHE = {}
RELEASE_BUFFER_DAYS = int(os.environ.get("RELEASE_BUFFER_DAYS", "7"))
DELETION_DELAY_DAYS = int(os.environ.get("DELETION_DELAY_DAYS", "2"))
KEEP_REQUESTS_OLDER_THAN_DAYS = int(os.environ.get("KEEP_REQUESTS_OLDER_THAN_DAYS", "14"))
STUCK_DOWNLOAD_MINUTES = float(os.environ.get("STUCK_DOWNLOAD_MINUTES", "20.0"))
MAX_DOWNLOAD_HOURS = float(os.environ.get("MAX_DOWNLOAD_HOURS", "6.0"))
DELETE_NOT_MANAGED = os.environ.get("DELETE_NOT_MANAGED_JELLYSEERR", "true").lower() in ("true", "1", "yes")

PENDING_FILE = "/tmp/jellyseerr_pending_deletions.json"


def format_timedelta(td):
    """Format a timedelta into a human-readable string like '1d 4h' or '36h 20m'."""
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "now"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def normalize_media_type(media_type, default="movie"):  # pragma: no cover - simple helper
    if not media_type:
        return default
    normalized = media_type.lower()
    if normalized in {"tv", "series", "tv_show"}:
        return "tv"
    return "movie"


def normalize_tmdb_id(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def parse_iso_datetime(input_str):
    if not input_str:
        return None
    try:
        normalized = input_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(input_str)
        except ValueError:
            return None


def is_release_due(release_date_str, buffer_days=7):
    release_dt = parse_iso_datetime(release_date_str)
    if not release_dt:
        return True
    now = datetime.now(timezone.utc)
    release_dt_utc = release_dt.astimezone(timezone.utc)
    return release_dt_utc <= now - timedelta(days=buffer_days)


def get_sonarr_next_airing(series):
    next_airing = series.get("nextAiring")
    if next_airing:
        return next_airing

    next_episode = series.get("nextAiringEpisode") or {}
    if next_episode:
        return next_episode.get("airDateUtc")

    for season in series.get("seasons", []):
        stats = season.get("statistics", {})
        candidate = stats.get("nextAiring") or stats.get("nextAiringEpisode")
        if candidate:
            return candidate

    return None

def resolve_media_title(media, tmdb_api_key, media_type_hint=None):
    tmdb_id = media.get("tmdbId")
    title = media.get("title") or media.get("name")
    media_type = normalize_media_type(media_type_hint or media.get("mediaType"))

    if not title and tmdb_id:
        try:
            title = get_tmdb_title(tmdb_id, tmdb_api_key, media_type=media_type)
        except Exception:
            title = None

    return title

def friendly_radarr_title(movie):
    title = resolve_media_title(movie, TMDB_API_KEY, media_type_hint="movie")
    if title:
        return title
    tmdb_id = movie.get("tmdbId")
    if tmdb_id:
        return f"TMDB {tmdb_id}"
    return "Unknown Radarr Movie"

def friendly_sonarr_title(series):
    title = resolve_media_title(series, TMDB_API_KEY, media_type_hint="tv")
    if title:
        return title
    tmdb_id = series.get("tmdbId")
    if tmdb_id:
        return f"TMDB {tmdb_id}"
    return "Unknown Sonarr Series"




def load_pending_deletions():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_pending_deletions(data):
    with open(PENDING_FILE, "w") as f:
        json.dump(data, f, indent=4)


def parse_command_line_arguments():
    parser = argparse.ArgumentParser("Jellyseerr cleaner", description="Syncs Radarr/Sonarr with Jellyseerr requests")
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Only output what would change without modifying any service",
    )
    return parser.parse_args()

def get_tmdb_title(tmdb_id, tmdb_api_key, media_type="movie"):
    cache_key = (tmdb_id, media_type)
    if cache_key in TMDB_TITLE_CACHE:
        return TMDB_TITLE_CACHE[cache_key]

    tmdb_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={tmdb_api_key}"
    response = requests.get(tmdb_url)
    response.raise_for_status()
    data = response.json()
    title = data.get("title") or data.get("name")
    TMDB_TITLE_CACHE[cache_key] = title
    return title

def fetch_jellyseerr_requests(api_key, base_url):
    take = 50
    skip = 0
    headers = {"X-Api-Key": api_key}
    requests_list = []
    seen_tmdb = set()
    total_results = None

    while True:
        url = f"{base_url}/api/v1/request?filter=unavailable&sort=added&sortDirection=desc&skip={skip}&take={take}"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        page_info = data.get("pageInfo", {})
        if total_results is None:
            total_results = page_info.get("results", 0)

        results = data.get("results", [])
        if not results:
            break

        for item in results:
            media = item.get("media", {})
            tmdb_id = media.get("tmdbId")
            if not tmdb_id or tmdb_id in seen_tmdb:
                continue
            media_type = normalize_media_type(media.get("mediaType"))
            title = media.get("title") or media.get("name")
            media_id = media.get("id")
            request_id = item.get("id")  # ID of the request itself (used for decline notification)

            # Extract new fields
            created_at = item.get("createdAt")
            requested_by = item.get("requestedBy", {})
            requester = requested_by.get("displayName") or requested_by.get("email") or requested_by.get("username") or "Unknown"

            if not title and tmdb_id:
                try:
                    title = get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type=media_type)
                except Exception:
                    title = "Unknown Title"
            seen_tmdb.add(tmdb_id)
            requests_list.append(
                {
                    "title": title or "Unknown Title",
                    "media_id": media_id,
                    "request_id": request_id,
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "request_date": created_at,
                    "requested_by": requester,
                }
            )
            
        skip += take
        if skip >= total_results:
            break

    return requests_list

def decline_jellyseerr_requests(api_key, base_url, requests_to_decline):
    """Decline Jellyseerr requests via the API.
    Jellyseerr automatically notifies the requester via its configured notification agents.
    The declined request stays visible so the user can re-request with one click.
    """
    if not requests_to_decline:
        print("No Jellyseerr requests to decline.")
        return 0

    headers = {"X-Api-Key": api_key}
    declined_count = 0
    for request in requests_to_decline:
        request_id = request.get("request_id")
        tmdb_id = request.get("tmdb_id")
        title = request.get("title") or (f"TMDB {tmdb_id}" if tmdb_id else "Unknown Jellyseerr Media")
        requester = request.get("requested_by", "Unknown")
        if not request_id:
            print(f"  [WARN] No request_id for '{title}', skipping.")
            continue
        decline_url = f"{base_url}/api/v1/request/{request_id}/decline"
        try:
            resp = requests.post(decline_url, headers=headers)
            resp.raise_for_status()
            print(f"  [✓] Declined '{title}' (request ID: {request_id}, by: {requester})")
            declined_count += 1
        except Exception as e:
            print(f"  [WARN] Could not decline '{title}' (request ID: {request_id}): {e}")

    print(f"Declined {declined_count} Jellyseerr request(s).")
    return declined_count


def resolve_jellyseerr_delete_requests(jellyseerr_entries, api_key, base_url):
    if not jellyseerr_entries:
        return []

    if all(isinstance(entry, dict) for entry in jellyseerr_entries):
        return jellyseerr_entries

    request_candidates = {
        req.get("tmdb_id"): req
        for req in fetch_jellyseerr_requests(api_key, base_url)
        if req.get("tmdb_id")
    }

    normalized_requests = []
    for entry in jellyseerr_entries:
        tmdb_id = entry
        if isinstance(entry, dict):
            tmdb_id = entry.get("tmdb_id")
        if tmdb_id is None:
            continue
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            pass
        request = request_candidates.get(tmdb_id)
        if request:
            normalized_requests.append(request)
        else:
            print(f"Warning: Jellyseerr request for TMDB ID {tmdb_id} not found in current requests.")

    return normalized_requests

def get_all_radarr_movies_with_status(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/movie"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    movies = response.json()
    return {movie.get("tmdbId"): movie for movie in movies if "tmdbId" in movie}

def get_all_sonarr_series_with_status(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/series"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    series_list = response.json()
    return {series.get("tmdbId"): series for series in series_list if "tmdbId" in series}

def get_radarr_missing_movies(api_key, base_url):
    all_movies = get_all_radarr_movies_with_status(api_key, base_url)
    missing_movies = {}
    for tmdb_id, movie in all_movies.items():
        if tmdb_id is None:
            continue
        has_file = movie.get("hasFile")

        # If API says file exists, but sizeOnDisk is 0, the file is gone
        if has_file is True:
            size_on_disk = movie.get("sizeOnDisk", 0) or movie.get("movieFile", {}).get("size", 0)
            if size_on_disk == 0:
                title = movie.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="movie")
                print(f"  [GHOST] {title} (TMDB {tmdb_id}): hasFile=True but sizeOnDisk=0!")
                has_file = False

        if has_file is False:
            release_date = movie.get("releaseDate") or movie.get("inCinemas")
            if not is_release_due(release_date):
                continue
            title = movie.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="movie")
            missing_movies[tmdb_id] = title or f"TMDB {tmdb_id}"
    return missing_movies

def get_sonarr_missing_episodes(api_key, base_url):
    all_series = get_all_sonarr_series_with_status(api_key, base_url)
    missing_items = {}
    for tmdb_id, series in all_series.items():
        if tmdb_id is None:
            continue
        
        # Check series-level safeguards first
        next_air_date = get_sonarr_next_airing(series)
        if not is_release_due(next_air_date, RELEASE_BUFFER_DAYS):
            continue
        
        previous_airing = series.get("previousAiring")
        if previous_airing and not is_release_due(previous_airing, RELEASE_BUFFER_DAYS):
            continue

        # Series-level ghost detection via sizeOnDisk
        series_stats = series.get("statistics", {})
        series_size_on_disk = series_stats.get("sizeOnDisk", 0)
        api_total_files = series_stats.get("episodeFileCount", 0)
        is_ghost_series = api_total_files > 0 and series_size_on_disk == 0

        if is_ghost_series:
            title = series.get("title") or f"TMDB {tmdb_id}"
            print(f"  [GHOST] {title} (TMDB {tmdb_id}): API says {api_total_files} files but sizeOnDisk=0!")

        # Now check seasons
        seasons = series.get("seasons", [])
        monitored_seasons_count = 0
        actionable_seasons = [] # Seasons with 0 files (to be unmonitored)
        report_seasons = []     # All seasons with missing files (for logging)
        
        for season in seasons:
            if not season.get("monitored"):
                continue
            monitored_seasons_count += 1
            
            stats = season.get("statistics", {})
            file_count = stats.get("episodeFileCount", 0)
            total_count = stats.get("episodeCount", 0)
            season_size = stats.get("sizeOnDisk", 0)

            # Ghost detection: API says files exist but sizeOnDisk is 0
            if file_count > 0 and season_size == 0:
                print(f"  [GHOST] Season {season.get('seasonNumber')}: {file_count} files but sizeOnDisk=0")
                file_count = 0
            
            # If ANY files are missing, we report it
            if file_count < total_count and total_count > 0:
                report_seasons.append(season.get("seasonNumber"))
                
                # We only take ACTION (unmonitor/delete) if 0 files exist
                if file_count == 0:
                    actionable_seasons.append(season.get("seasonNumber"))
        
        # Empty series detection: all monitored seasons have 0 episodes AND 0 files
        # This catches series like "The Hunting Wives" where Sonarr has no episode data
        if not report_seasons and monitored_seasons_count > 0:
            total_episodes = series_stats.get("episodeCount", 0)
            total_files = series_stats.get("episodeFileCount", 0)
            if total_episodes == 0 and total_files == 0:
                title = series.get("title") or f"TMDB {tmdb_id}"
                print(f"  [EMPTY] {title} (TMDB {tmdb_id}): {monitored_seasons_count} monitored season(s) but 0 episodes tracked")
                report_seasons = [s.get("seasonNumber") for s in seasons if s.get("monitored")]
                actionable_seasons = report_seasons[:]

        if not report_seasons:
            continue
            
        title = series.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="tv")
        
        # Determine action based on ACTIONABLE seasons
        if actionable_seasons and len(actionable_seasons) == monitored_seasons_count:
             action = "delete_series"
        elif actionable_seasons:
             action = "unmonitor_seasons"
        else:
             action = "report_only"
             
        missing_items[tmdb_id] = {
            "title": title or f"TMDB {tmdb_id}",
            "tmdb_id": tmdb_id,
            "series_id": series.get("id"),
            "action": action,
            "seasons": actionable_seasons, # For action logic
            "report_seasons": report_seasons # For display logic
        }
            
    return missing_items

def delete_radarr_movie(api_key, base_url, tmdb_id):
    headers = {"X-Api-Key": api_key}
    # First, find the movie in Radarr by tmdbId
    lookup_url = f"{base_url}/api/v3/movie?tmdbId={tmdb_id}"
    response = requests.get(lookup_url, headers=headers)
    response.raise_for_status()
    movies = response.json()

    if not movies:
        print(f"Movie with TMDB ID {tmdb_id} not found in Radarr.")
        return

    # Assuming the first result is the correct one
    movie_id = movies[0]["id"]
    delete_url = f"{base_url}/api/v3/movie/{movie_id}"
    # Set deleteFiles to true to remove associated files and add exclusion to prevent re-adding
    params = {"deleteFiles": "true", "addExclusion": "true"}
    delete_response = requests.delete(delete_url, headers=headers, params=params)
    delete_response.raise_for_status()
    print(f"Successfully deleted movie with TMDB ID {tmdb_id} from Radarr.")

def unmonitor_sonarr_seasons(api_key, base_url, series_id, seasons_to_unmonitor):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    
    # Get series first to have current state
    get_url = f"{base_url}/api/v3/series/{series_id}"
    resp = requests.get(get_url, headers=headers)
    resp.raise_for_status()
    series_data = resp.json()
    
    updated = False
    for season in series_data.get("seasons", []):
        if season["seasonNumber"] in seasons_to_unmonitor:
            if season["monitored"]:
                season["monitored"] = False
                updated = True
    
    if updated:
        put_url = f"{base_url}/api/v3/series/{series_id}"
        resp = requests.put(put_url, headers=headers, json=series_data)
        resp.raise_for_status()
        print(f"Unmonitored seasons {seasons_to_unmonitor} for series {series_data.get('title')}")
    else:
        print(f"Seasons {seasons_to_unmonitor} were already unmonitored for {series_data.get('title')}")

def delete_sonarr_series(api_key, base_url, tmdb_id):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    # First, find the series in Sonarr by tmdbId
    lookup_url = f"{base_url}/api/v3/series?tmdbId={tmdb_id}"
    response = requests.get(lookup_url, headers={"X-Api-Key": api_key})
    response.raise_for_status()
    series_list = response.json()

    if not series_list:
        print(f"Series with TMDB ID {tmdb_id} not found in Sonarr.")
        return
    target_tmdb = normalize_tmdb_id(tmdb_id)
    matching_series = next(
        (
            series
            for series in series_list
            if normalize_tmdb_id(series.get("tmdbId")) == target_tmdb
        ),
        None,
    )
    if not matching_series:
        print(
            f"Series with TMDB ID {tmdb_id} was not among the lookup results for Sonarr; skipping."
        )
        return
    series_id = matching_series["id"]
    delete_url = f"{base_url}/api/v3/series/editor"
    payload = {
        "seriesIds": [series_id],
        "moveFiles": True,
        "deleteFiles": True,
        "addImportListExclusion": True,
    }

    delete_response = requests.delete(delete_url, headers=headers, json=payload)
    delete_response.raise_for_status()
    print(f"Successfully deleted series with TMDB ID {tmdb_id} from Sonarr.")

def clean_stuck_downloads(api_key, base_url, app_name, dry_run=False):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/queue?pageSize=200&includeUnknownSeriesItems=true&includeUnknownMovieItems=true"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    records = data.get("records", [])
    
    stuck_records = []
    now = datetime.now(timezone.utc)
    
    # Count occurrences of downloadId in queue to detect season packs (multiple episodes sharing the same download)
    download_id_counts = {}
    for r in records:
        dl_id = r.get("downloadId")
        if dl_id:
            download_id_counts[dl_id] = download_id_counts.get(dl_id, 0) + 1

    # Pre-fetch library state (hasFile) for items in queue to avoid blocklisting already-imported media
    episodes_has_file = {}
    movies_has_file = {}
    
    if app_name.lower() == "sonarr":
        series_ids = set(r.get("seriesId") for r in records if r.get("seriesId"))
        for s_id in series_ids:
            try:
                ep_resp = requests.get(f"{base_url}/api/v3/episode?seriesId={s_id}", headers=headers, timeout=10)
                if ep_resp.status_code == 200:
                    for ep in ep_resp.json():
                        episodes_has_file[ep["id"]] = ep.get("hasFile", False)
            except Exception:
                pass
    elif app_name.lower() == "radarr":
        movie_ids = set(r.get("movieId") for r in records if r.get("movieId"))
        for m_id in movie_ids:
            try:
                m_resp = requests.get(f"{base_url}/api/v3/movie/{m_id}", headers=headers, timeout=10)
                if m_resp.status_code == 200:
                    movies_has_file[m_id] = m_resp.json().get("hasFile", False)
            except Exception:
                pass

    print(f"\nEvaluating downloads in {app_name}...")
    for r in records:
        size = r.get("size", 0)
        sizeleft = r.get("sizeleft", 0)
        title = r.get("title", "Unknown")
        
        if size > 0:
            progress = 1.0 - (sizeleft / float(size))
        else:
            progress = 0.0
            
        added_str = r.get("added")
        added_dt = parse_iso_datetime(added_str)
        if added_dt and added_dt.tzinfo is None:
            added_dt = added_dt.replace(tzinfo=timezone.utc)
            
        age_hours = (now - added_dt).total_seconds() / 3600.0 if added_dt else 0.0
        age_minutes = (now - added_dt).total_seconds() / 60.0 if added_dt else 0.0
        
        # Check if media is ALREADY in the library (hasFile=True)
        already_imported = False
        if app_name.lower() == "sonarr":
            ep_ids = r.get("episodeIds", [])
            if not ep_ids and "episodeId" in r:
                ep_ids = [r["episodeId"]]
            if ep_ids and all(episodes_has_file.get(eid, False) for eid in ep_ids):
                already_imported = True
        elif app_name.lower() == "radarr":
            m_id = r.get("movieId")
            if m_id and movies_has_file.get(m_id, False):
                already_imported = True

        reason = None
        should_blocklist = True

        if already_imported:
            reason = f"Media is already imported in {app_name} library (hasFile=True). Clearing stale queue entry."
            should_blocklist = False
        else:
            ep_ids = r.get("episodeIds", [])
            if not ep_ids and "episodeId" in r:
                ep_ids = [r["episodeId"]]
                
            dl_id = r.get("downloadId")
            is_season_pack = (
                len(ep_ids) > 1 
                or r.get("fullSeason", False) 
                or (dl_id is not None and download_id_counts.get(dl_id, 0) > 1)
            )

            tracked_state = r.get("trackedDownloadState", "")
            tracked_status = r.get("trackedDownloadStatus", "")
            status_msgs = []
            for m in r.get("statusMessages", []):
                if isinstance(m, dict):
                    if "title" in m:
                        status_msgs.append(str(m["title"]))
                    for msg in m.get("messages", []):
                        status_msgs.append(str(msg))
                elif isinstance(m, str):
                    status_msgs.append(m)
            
            msg_text = " ".join(status_msgs).lower()
            is_unimportable_release = (
                (tracked_status == "warning" and (progress >= 0.99 or r.get("status") == "completed"))
                or any(k in msg_text for k in [
                    "not imported", "missing from", "no files", "eligible for import",
                    "no video", "failed to import", "sample", "title mismatch",
                    "not possible", "mismatch", "unknown series", "unknown movie", "rejected",
                    "unable to import", "found multiple", "multiple movies", "multiple series"
                ])
            )

            # Blocklist if release is blocked/unimportable (no eligible files) or incomplete season pack
            if tracked_state in ("importBlocked", "importFailed") or is_unimportable_release:
                reason = "Release is blocked or cannot be automatically imported by Radarr/Sonarr."
                should_blocklist = True
            elif is_season_pack and (tracked_state == "importBlocked" or is_unimportable_release):
                reason = "Season pack release is incomplete or blocked from import."
                should_blocklist = True
            elif added_dt and progress <= 0.05 and age_minutes >= STUCK_DOWNLOAD_MINUTES:
                reason = f"Progress is low ({progress*100:.2f}% <= 5%) after {age_minutes:.1f} minutes (limit: {STUCK_DOWNLOAD_MINUTES}m)."
                should_blocklist = True
            elif added_dt and age_hours >= MAX_DOWNLOAD_HOURS:
                reason = f"Download taking too long ({age_hours:.1f}h >= {MAX_DOWNLOAD_HOURS}h), currently at {progress*100:.2f}%."
                should_blocklist = True
            
        if reason:
            action_desc = "Mark for cleanup (no blocklist)" if not should_blocklist else "Mark for removal & blocklist"
            print(f" {action_desc}: {title}")
            print(f" Reason: {reason}")
            stuck_records.append((r, reason, should_blocklist))
        else:
            print(f"Keep: {title} | Progress: {progress*100:.2f}% | Age: {age_minutes:.1f} minutes | Status: {r.get('status')}")
            
    if not stuck_records:
        return

    print(f"\n--- Cleaning {len(stuck_records)} queue entries in {app_name} ---")
    for r, reason, do_blocklist in stuck_records:
        title = r.get("title", "Unknown")
        record_id = r.get("id")
        blocklist_str = "true" if do_blocklist else "false"
        
        if not dry_run:
            delete_url = f"{base_url}/api/v3/queue/{record_id}"
            params = {"removeFromClient": "true", "blocklist": blocklist_str}
            try:
                delete_response = requests.delete(delete_url, headers=headers, params=params)
                delete_response.raise_for_status()
                status_act = "removed and blocklisted" if do_blocklist else "cleared from queue (already in library)"
                print(f"  -> Successfully {status_act}: {title}")
            except requests.exceptions.HTTPError as he:
                if he.response is not None and he.response.status_code == 404:
                    # Item was already removed as part of a batch/season pack removal
                    pass
                else:
                    print(f"  -> Failed to remove {title}: {he}")
            except Exception as e:
                print(f"  -> Failed to remove {title}: {e}")
        else:
            status_act = "remove and blocklist" if do_blocklist else "clear from queue without blocklisting (already in library)"
            print(f"  -> [DRY RUN] Would {status_act}: {title}")



def get_all_radarr_movies(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/movie"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    movies = response.json()
    # Filter for movies that are 'missing'
    missing_movies = {}
    for movie in movies:
        tmdb_id = movie.get("tmdbId")
        has_file = movie.get("hasFile")
        title = movie.get("title") or (get_tmdb_title(tmdb_id, TMDB_API_KEY) if tmdb_id else None)
        print(f"Radarr Movie: {title}, Status: {movie.get('status')}, HasFile: {has_file}, TMDB ID: {tmdb_id}")
        if tmdb_id and has_file is False:
            missing_movies[tmdb_id] = title or f"TMDB {tmdb_id}"
    return missing_movies

def get_all_sonarr_series(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/series"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    series_list = response.json()
    missing_series = {}
    for series in series_list:
        tmdb_id = series.get("tmdbId")
        stats = series.get("statistics", {})
        episode_file_count = stats.get("episodeFileCount", 0)
        episode_count = stats.get("episodeCount", 0)
        title = series.get("title") or (get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="tv") if tmdb_id else None)
        print(f"Sonarr Series: {title}, Status: {series.get('status')}, EpisodeFileCount: {episode_file_count}/{episode_count}, TMDB ID: {tmdb_id}")
        if tmdb_id and episode_file_count < episode_count:
            missing_series[tmdb_id] = title or f"TMDB {tmdb_id}"
    return missing_series

def get_jellyseerr_library_media(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v1/media?"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    library_media = {}
    for item in data["results"]:
        tmdb_id = item.get("tmdbId")
        if not tmdb_id:
            continue
        title = resolve_media_title(item, TMDB_API_KEY, media_type_hint=item.get("mediaType"))
        library_media[tmdb_id] = title or f"TMDB {tmdb_id}"
    return library_media

def perform_deletions_list(radarr_api_key, radarr_url, sonarr_api_key, sonarr_url, deletion_data):
    if deletion_data["radarr"]:
        print(f"Deleting movies from Radarr: {deletion_data['radarr']}")
        for tmdb_id in deletion_data["radarr"]:
            delete_radarr_movie(radarr_api_key, radarr_url, tmdb_id)
    else:
        print("Nothing to delete from Radarr.")

    if deletion_data["sonarr"]:
        print(f"Processing series in Sonarr: {deletion_data['sonarr']}")
        for item in deletion_data["sonarr"]:
            if isinstance(item, dict):
                tmdb_id = item["tmdb_id"]
                action = item.get("action", "delete_series")
                if action == "delete_series":
                    delete_sonarr_series(sonarr_api_key, sonarr_url, tmdb_id)
                elif action == "unmonitor_seasons":
                    series_id = item.get("series_id")
                    seasons = item.get("seasons", [])
                    if series_id and seasons:
                        unmonitor_sonarr_seasons(sonarr_api_key, sonarr_url, series_id, seasons)
            else:
                 delete_sonarr_series(sonarr_api_key, sonarr_url, item)
    else:
        print("Nothing to delete from Sonarr.")

    jellyseerr_requests_to_decline = deletion_data.get("jellyseerr", []) or []
    if jellyseerr_requests_to_decline:
        print("Declining Jellyseerr requests (users will be notified and can re-request):")
        decline_jellyseerr_requests(JELLYSEER_API_KEY, JELLYSEER_URL, jellyseerr_requests_to_decline)
    else:
        print("Nothing to decline from Jellyseerr.")



def get_jellyseerr_media_info(api_key, base_url, tmdb_id, media_type):
    """
    Lookup media information in Jellyseerr by TMDB ID.
    Returns the JSON response which includes mediaInfo and requests if they exist.
    """
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v1/{media_type}/{tmdb_id}"
    response = requests.get(url, headers=headers)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return response.json()

def generate_missing_media_report(dry_run=False):
    radarr_missing = get_radarr_missing_movies(RADARR_API_KEY, RADARR_URL)
    sonarr_missing = get_sonarr_missing_episodes(SONARR_API_KEY, SONARR_URL)
    jellyseerr_requests = fetch_jellyseerr_requests(JELLYSEER_API_KEY, JELLYSEER_URL)
    jellyseerr_library = get_jellyseerr_library_media(JELLYSEER_API_KEY, JELLYSEER_URL)

    jellyseerr_request_map = {
        movie.get("tmdb_id"): movie
        for movie in jellyseerr_requests
        if movie.get("tmdb_id") and movie.get("media_id")
    }

    all_radarr_movies = get_all_radarr_movies_with_status(RADARR_API_KEY, RADARR_URL)
    all_sonarr_series = get_all_sonarr_series_with_status(SONARR_API_KEY, SONARR_URL)

    # --- Compact Radarr Summary ---
    radarr_total = len(all_radarr_movies)
    radarr_has_file = 0
    radarr_not_released = 0
    for tmdb_id, movie in all_radarr_movies.items():
        if movie.get("hasFile"):
            radarr_has_file += 1
        release_date = movie.get("releaseDate") or movie.get("inCinemas")
        if release_date and not is_release_due(release_date, RELEASE_BUFFER_DAYS):
            radarr_not_released += 1
    radarr_missing_count = radarr_total - radarr_has_file
    not_rel_str = f" ({radarr_not_released} not yet released)" if radarr_not_released else ""
    print(f"\n--- Radarr Movies: {radarr_has_file}/{radarr_total} files{not_rel_str} ---")

    # --- Compact Sonarr Summary ---
    sonarr_total = len(all_sonarr_series)
    sonarr_complete = 0
    sonarr_not_aired = 0
    for tmdb_id, series in all_sonarr_series.items():
        stats = series.get("statistics", {})
        ep_file = stats.get("episodeFileCount", 0)
        ep_total = stats.get("episodeCount", 0)
        if ep_total > 0 and ep_file >= ep_total:
            sonarr_complete += 1
        next_air = get_sonarr_next_airing(series)
        if next_air and not is_release_due(next_air, RELEASE_BUFFER_DAYS):
            sonarr_not_aired += 1
    not_air_str = f" ({sonarr_not_aired} not yet aired)" if sonarr_not_aired else ""
    print(f"--- Sonarr Series: {sonarr_complete}/{sonarr_total} complete{not_air_str} ---")

    # --- Compact Jellyseerr Summary ---
    jelly_not_released = 0
    jelly_truly_unfulfilled = 0
    jelly_not_in_arr = 0
    for req in jellyseerr_requests:
        tid = req.get("tmdb_id")
        mtype = req.get("media_type", "movie")
        if mtype == "movie" and tid in all_radarr_movies:
            movie = all_radarr_movies[tid]
            # Skip if movie already has its file
            if movie.get("hasFile"):
                continue
            release_date = movie.get("releaseDate") or movie.get("inCinemas")
            if release_date and not is_release_due(release_date, RELEASE_BUFFER_DAYS):
                jelly_not_released += 1
            jelly_truly_unfulfilled += 1
        elif mtype == "tv" and tid in all_sonarr_series:
            series = all_sonarr_series[tid]
            stats = series.get("statistics", {})
            ep_file = stats.get("episodeFileCount", 0)
            ep_total = stats.get("episodeCount", 0)
            # Skip if all episodes already have files
            if ep_total > 0 and ep_file >= ep_total:
                continue
            next_air = get_sonarr_next_airing(series)
            if next_air and not is_release_due(next_air, RELEASE_BUFFER_DAYS):
                jelly_not_released += 1
            jelly_truly_unfulfilled += 1
        else:
            jelly_not_in_arr += 1
            continue  # Not in *arr → skip from count
    jelly_nr_str = f" ({jelly_not_released} not yet released)" if jelly_not_released else ""
    print(f"--- Jellyseerr: {jelly_truly_unfulfilled} unfulfilled request(s){jelly_nr_str} | {len(jellyseerr_library)} in library ---")

    if radarr_missing:
        print("\nRadarr movies to be considered missing:")
        for tmdb_id, title in radarr_missing.items():
            print(f"- {title} (TMDB ID: {tmdb_id})")
    else:
        print("\nNo Radarr movies considered missing.")

    if sonarr_missing:
        print("Sonarr series to be considered missing/incomplete:")
        for tmdb_id, info in sonarr_missing.items():
            action = info['action']
            seasons = info.get('report_seasons', info['seasons'])
            print(f"- {info['title']} (TMDB ID: {tmdb_id}) -> Action: {action}, Missing Seasons: {seasons}")
    else:
        print("No Sonarr series considered missing.")

    # Identify all current candidates for deletion
    # Candidates are items missing in Radarr/Sonarr. 
    # We look up their status in Jellyseerr directly to find request info.
    
    current_candidates_tmdb_ids = set(radarr_missing) | set(sonarr_missing)
    
    now = datetime.now(timezone.utc)
    existing_pending = load_pending_deletions()
    updated_pending = {}
    
    ready_radarr = []
    ready_sonarr = []
    ready_jellyseerr = []
    
    # Counters for final summary
    pending_radarr_count = 0
    pending_sonarr_count = 0
    ready_radarr_count = 0
    ready_sonarr_count = 0
    rollback_count = 0
    rollback_soonest_td = None
    
    _sep = '═' * 55
    print(f"\n{_sep}")
    print(f"  Deletion Queue ({len(current_candidates_tmdb_ids)} candidate(s))")
    print(f"{_sep}")
    
    # Process current candidates
    for tmdb_id in current_candidates_tmdb_ids:
        # Determine media type for lookup
        if tmdb_id in radarr_missing:
            media_type = "movie"
        else:
            media_type = "tv"

        # Lookup media in Jellyseerr directly
        try:
             jellyseerr_data = get_jellyseerr_media_info(JELLYSEER_API_KEY, JELLYSEER_URL, tmdb_id, media_type)
        except Exception as e:
             print(f"Error looking up TMDB ID {tmdb_id} in Jellyseerr: {e}")
             continue
             
        media_info = jellyseerr_data.get("mediaInfo")
        if not media_info:
            if DELETE_NOT_MANAGED:
                # Resolve title for display
                if tmdb_id in radarr_missing:
                    title_nm = radarr_missing[tmdb_id]
                    ready_radarr.append(tmdb_id)
                    ready_radarr_count += 1
                elif tmdb_id in sonarr_missing:
                    title_nm = sonarr_missing[tmdb_id].get("title", f"TMDB {tmdb_id}")
                    ready_sonarr.append(sonarr_missing[tmdb_id])
                    ready_sonarr_count += 1
                else:
                    title_nm = f"TMDB {tmdb_id}"
                print(f"  🗑 {title_nm} | Not in Jellyseerr → will be deleted")
            else:
                print(f"  NOT IN JELLYSEERR TMDB {tmdb_id} — Not managed by Jellyseerr")
            continue
            
        requests_list = media_info.get("requests", [])
        has_request = bool(requests_list)
        
        if requests_list:
            # Sort requests by date descending to get the latest
            requests_list.sort(key=lambda x: x.get("createdAt"), reverse=True)
            latest_request = requests_list[0]
            request_date_str = latest_request.get("createdAt")
            requested_by_user = latest_request.get("requestedBy", {})
            requester = requested_by_user.get("displayName") or requested_by_user.get("email") or "Unknown"
        else:
            # Fallback to media creation date if no requests exist
            request_date_str = media_info.get("createdAt")
            if not request_date_str:
                print(f"  NOT IN JELLYSEERR TMDB {tmdb_id} — No request or creation date found")
                continue
            requester = "Media-Only (No Request)"
        
        media_id = media_info.get("id")
        title = jellyseerr_data.get("title") or jellyseerr_data.get("name") or f"TMDB {tmdb_id}"

        request_date = parse_iso_datetime(request_date_str)
        
        # Entry for pending file (store based on latest request)
        tmdb_id_str = str(tmdb_id)
        
        # Preserve first_seen from existing pending data for rollback tracking
        prev_entry = existing_pending.get(tmdb_id_str, {})
        first_seen_str = prev_entry.get("first_seen", now.isoformat())
        
        entry = {
            "tmdb_id": tmdb_id,
            "title": title,
            "request_date": request_date_str,
            "requested_by": requester,
            "media_id": media_id,
            "first_seen": first_seen_str,
        }
        
        # Add to pending list
        updated_pending[tmdb_id_str] = entry
        
        # --- Rollback check for no-request candidates ---
        if not has_request:
            first_seen_dt = parse_iso_datetime(first_seen_str)
            if first_seen_dt:
                if first_seen_dt.tzinfo is None:
                    first_seen_dt = first_seen_dt.replace(tzinfo=timezone.utc)
                rollback_deadline = first_seen_dt + timedelta(days=DELETION_DELAY_DAYS)
                remaining_rollback = rollback_deadline - now
                
                if remaining_rollback.total_seconds() > 0:
                    rollback_count += 1
                    if rollback_soonest_td is None or remaining_rollback < rollback_soonest_td:
                        rollback_soonest_td = remaining_rollback
                    print(f"  {title} | No request — rollback in: {format_timedelta(remaining_rollback)}")
                    continue  # Skip deletion, still in rollback window
                else:
                    print(f"  {title} | No request — rollback expired, proceeding")
        
        # Check if ready to delete
        if request_date:
            # Ensure request_date is UTC aware if 'now' is
            if request_date.tzinfo is None:
                request_date = request_date.replace(tzinfo=timezone.utc)
            
            age = now - request_date
            age_days = age.days
            deletion_deadline = request_date + timedelta(days=DELETION_DELAY_DAYS)
            remaining = deletion_deadline - now
            age_str = format_timedelta(age)
            
            if age_days >= DELETION_DELAY_DAYS:

                # Skip report_only sonarr entries early — they are incomplete but should NOT be deleted
                if tmdb_id in sonarr_missing and sonarr_missing[tmdb_id].get("action") == "report_only":
                    print(f"  PARTIAL {title} | {requester} | Skipped — has files, only partial missing")
                    continue

                # Ready for deletion
                marker = "→"
                print(f"  {marker} {title} | By: {requester} | READY — will be deleted now | Requested: {age_str} ago")
                
                # Add to execution lists
                if tmdb_id in radarr_missing:
                    ready_radarr.append(tmdb_id)
                    ready_radarr_count += 1
                elif tmdb_id in sonarr_missing:
                    ready_sonarr.append(sonarr_missing[tmdb_id])
                    ready_sonarr_count += 1
                 
                # Check if we should delete the Jellyseerr request
                if age_days < KEEP_REQUESTS_OLDER_THAN_DAYS:
                    # Extract the request_id from the latest Jellyseerr request (for decline notification)
                    latest_req = requests_list[0] if requests_list else {}
                    # Prepare jellyseerr deletion object
                    req_obj = {
                        "title": title,
                        "media_id": media_id,
                        "request_id": latest_req.get("id"),
                        "tmdb_id": tmdb_id,
                        "media_type": media_type,
                        "request_date": request_date_str,
                        "requested_by": requester,
                    }
                    ready_jellyseerr.append(req_obj)
                else:
                    pass  # Keep old Jellyseerr request
            else:
                remaining_str = format_timedelta(remaining)
                if tmdb_id in radarr_missing:
                    pending_radarr_count += 1
                elif tmdb_id in sonarr_missing:
                    pending_sonarr_count += 1
                print(f"  WAITING... {title} | By: {requester} | Deletion in: {remaining_str} | Requested: {age_str} ago")
        else:
             print(f"  NOT IN JELLYSEERR {title} — No request date found")

    # Save updated pending state
    save_pending_deletions(updated_pending)

    # --- Final Summary Block ---
    # Pending = total missing minus those ready to delete
    total_pending_radarr = len(radarr_missing) - ready_radarr_count
    total_pending_sonarr = len(sonarr_missing) - ready_sonarr_count
    print(f"\n{_sep}")
    print("  Cleaner Summary")
    print(f"  Radarr: {radarr_has_file}/{radarr_total} files | {total_pending_radarr} pending | {ready_radarr_count} ready")
    print(f"  Sonarr: {sonarr_complete}/{sonarr_total} complete | {total_pending_sonarr} pending | {ready_sonarr_count} ready")
    if rollback_count > 0 and rollback_soonest_td:
        print(f"  Rollback: {rollback_count} candidate(s) without request (soonest in {format_timedelta(rollback_soonest_td)})")
    print(f"{_sep}")

    # Perform Deletions
    if dry_run:
        print("\n--- DRY RUN ---")
        print(f"Would delete {len(ready_radarr)} movies from Radarr")
        print(f"Would delete {len(ready_sonarr)} series from Sonarr")
        print(f"Would delete {len(ready_jellyseerr)} requests from Jellyseerr")
        if ready_jellyseerr:
             for r in ready_jellyseerr:
                 print(f"  - {r['title']} (Requested by {r['requested_by']} on {r['request_date']})")
    else:
        if ready_radarr or ready_sonarr or ready_jellyseerr:
            print("\nPerforming deletions...")
            deletion_data = {
                "radarr": ready_radarr,
                "sonarr": ready_sonarr,
                "jellyseerr": ready_jellyseerr
            }
            perform_deletions_list(RADARR_API_KEY, RADARR_URL, SONARR_API_KEY, SONARR_URL, deletion_data)
        else:
            print("\nNo media ready for deletion.")

if __name__ == "__main__":
    if not os.environ.get("_SEERRSENTINEL_INTERNAL"):
        print("Error: This script cannot be run directly.")
        print("Use:  python3 seerr_sentinel.py clean [--dry-run]")
        sys.exit(1)

    args = parse_command_line_arguments()
    
    print("--- Cleaning Stuck Downloads ---")
    clean_stuck_downloads(RADARR_API_KEY, RADARR_URL, "Radarr", dry_run=args.dry_run)
    clean_stuck_downloads(SONARR_API_KEY, SONARR_URL, "Sonarr", dry_run=args.dry_run)
    
    generate_missing_media_report(dry_run=args.dry_run)