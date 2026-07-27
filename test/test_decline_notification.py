#!/usr/bin/env python3
"""
Test script for the Jellyseerr decline notification feature.

This script:
1. Fetches the list of current 'unavailable' requests from Jellyseerr.
2. Displays them so you can pick one to test against.
3. Sends a REAL decline notification to the chosen request.

WARNING: This sends a real notification to the requester and declines
         the request in Jellyseerr. Use only with a request you intend
         to decline, or restore it manually afterwards.

Usage:
    python3 test_decline_notification.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Load .env from script directory or project root
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

JELLYSEER_API_KEY = os.environ.get("JELLYSEER_API_KEY")
JELLYSEER_URL = os.environ.get("JELLYSEER_URL")
DECLINE_MESSAGE = (
    os.environ.get("JELLYSEER_DECLINE_MESSAGE")
    or os.environ.get("JELLYSEERR_DECLINE_MESSAGE")
    or "The media could not be found or downloaded within the allotted time. The request has been automatically cancelled."
)

def get_headers():
    return {"X-Api-Key": JELLYSEER_API_KEY, "Content-Type": "application/json"}


def fetch_pending_requests(limit=10):
    """Fetch the first N unavailable requests from Jellyseerr."""
    url = f"{JELLYSEER_URL}/api/v1/request?filter=unavailable&sort=added&sortDirection=desc&skip=0&take={limit}"
    resp = requests.get(url, headers=get_headers())
    resp.raise_for_status()
    return resp.json().get("results", [])


def send_decline(request_id, message):
    """Call the Jellyseerr decline endpoint for a given request ID."""
    url = f"{JELLYSEER_URL}/api/v1/request/{request_id}/decline"
    resp = requests.post(url, headers=get_headers(), json={"reason": message})
    return resp


def main():
    if not JELLYSEER_API_KEY or not JELLYSEER_URL:
        print("ERROR: JELLYSEER_API_KEY and JELLYSEER_URL must be set in your .env file.")
        sys.exit(1)

    print("=" * 60)
    print("  SeerrSentinel — Decline Notification Test")
    print("=" * 60)
    print(f"  Jellyseerr: {JELLYSEER_URL}")
    print(f"  Message   : {DECLINE_MESSAGE}")
    print("=" * 60)

    # --- Step 1: List pending requests ---
    print("\nFetching pending (unavailable) requests from Jellyseerr...\n")
    try:
        items = fetch_pending_requests(limit=10)
    except Exception as e:
        print(f"ERROR fetching requests: {e}")
        sys.exit(1)

    if not items:
        print("No unavailable requests found. Nothing to test against.")
        sys.exit(0)

    print(f"{'#':<4} {'Request ID':<12} {'Title':<40} {'Requested By'}")
    print("-" * 80)
    for idx, item in enumerate(items):
        req_id = item.get("id")
        media = item.get("media", {})
        title = media.get("title") or media.get("name") or f"TMDB {media.get('tmdbId', '?')}"
        requester = item.get("requestedBy", {})
        user = requester.get("displayName") or requester.get("email") or requester.get("username") or "Unknown"
        print(f"{idx:<4} {str(req_id):<12} {title[:39]:<40} {user}")

    # --- Step 2: Choose one ---
    print()
    choice = input("Enter the # of the request to send a decline notification to (or 'q' to quit): ").strip()
    if choice.lower() == "q":
        print("Aborted.")
        sys.exit(0)

    try:
        idx = int(choice)
        selected = items[idx]
    except (ValueError, IndexError):
        print("Invalid choice. Aborted.")
        sys.exit(1)

    req_id = selected.get("id")
    media = selected.get("media", {})
    title = media.get("title") or media.get("name") or f"TMDB {media.get('tmdbId', '?')}"
    requester_info = selected.get("requestedBy", {})
    user = requester_info.get("displayName") or requester_info.get("email") or "Unknown"

    print(f"\nSelected: [{req_id}] {title} — requested by {user}")
    confirm = input(f"Send a REAL decline notification to '{user}' for '{title}'? [yes/N]: ").strip().lower()
    if confirm != "yes":
        print("Aborted — no notification sent.")
        sys.exit(0)

    # --- Step 3: Send decline ---
    print(f"\nSending decline notification (request ID: {req_id})...")
    try:
        resp = send_decline(req_id, DECLINE_MESSAGE)
        if resp.status_code in (200, 204):
            print(f"Success! Notification sent and request {req_id} declined.")
        else:
            print(f"Unexpected HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
