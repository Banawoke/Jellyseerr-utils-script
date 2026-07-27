#!/usr/bin/env python3
"""
Test & simulation script for stale queue import cleanup on Sonarr / Radarr.

This script allows you to:
1. Run Unit Tests (using unittest + mocks) offline.
2. Inject a REAL import error into Sonarr activity queue and LEAVE IT THERE
   so you can inspect it in your Sonarr Web UI (http://10.2.1.9:8989/activity/queue).
3. Test clearing stale queue items with SeerrSentinel.

Usage:
    # Run interactive menu:
    python3 test/test_stale_queue_import.py

    # Inject an import error into Sonarr and leave it in the Web UI:
    python3 test/test_stale_queue_import.py --inject

    # Run offline unit tests:
    python3 test/test_stale_queue_import.py --unittest
"""

import os
import sys
import time
import base64
import subprocess
import unittest
from unittest.mock import patch, MagicMock
import requests
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sentinel_cleaner import clean_stuck_downloads

# Load environment
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SONARR_URL = os.environ.get("SONARR_URL", "http://10.2.1.9:8989")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY")


class TestStaleQueueImportCleanup(unittest.TestCase):
    """Unit tests for stale queue item cleanup logic."""

    @patch("sentinel_cleaner.requests.delete")
    @patch("sentinel_cleaner.requests.get")
    def test_already_imported_item_cleared_without_blocklist(self, mock_get, mock_delete):
        """Verify that queue items whose media hasFile=True are cleared WITHOUT blocklisting."""
        mock_queue_resp = MagicMock()
        mock_queue_resp.status_code = 200
        mock_queue_resp.json.return_value = {
            "records": [
                {
                    "id": 101,
                    "seriesId": 12,
                    "episodeId": 501,
                    "title": "Test.Show.S01E01.720p-GROUP",
                    "added": "2026-07-27T10:00:00Z",
                    "size": 1000000000,
                    "sizeleft": 0,
                    "status": "completed",
                    "trackedDownloadState": "importPending",
                    "statusMessages": [
                        {
                            "title": "Test.Show.S01E01.720p-GROUP",
                            "messages": ["No files found are eligible for import in /downloads/..."]
                        }
                    ]
                }
            ]
        }

        mock_ep_resp = MagicMock()
        mock_ep_resp.status_code = 200
        mock_ep_resp.json.return_value = [
            {"id": 501, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True}
        ]

        def get_side_effect(url, **kwargs):
            if "/api/v3/queue" in url:
                return mock_queue_resp
            elif "/api/v3/episode" in url:
                return mock_ep_resp
            return MagicMock(status_code=404)

        mock_get.side_effect = get_side_effect
        mock_delete.return_value = MagicMock(status_code=200)

        clean_stuck_downloads("test_key", "http://test-sonarr:8989", "Sonarr", dry_run=False)

        mock_delete.assert_called_once()
        call_kwargs = mock_delete.call_args.kwargs
        params = call_kwargs.get("params", {})
        
        self.assertEqual(params.get("removeFromClient"), "true")
        self.assertEqual(params.get("blocklist"), "false", "Item already in library MUST NOT be blocklisted!")


def inject_import_error_into_sonarr():
    """
    Creates a completed torrent in Transmission containing non-video files and triggers Sonarr
    to create an import error ('No files found are eligible for import') in Sonarr Activity Queue,
    and LEAVES IT THERE in Sonarr.
    """
    print("\n" + "=" * 70)
    print("  INJECTING IMPORT ERROR INTO SONARR ACTIVITY QUEUE")
    print("=" * 70)

    if not SONARR_URL or not SONARR_API_KEY:
        print("ERROR: SONARR_URL and SONARR_API_KEY must be configured in .env")
        return

    folder_name = "Shadowhunters.The.Mortal.Instruments.S03.FRENCH.720p.WEB.x264-TEST_IMPORT"
    target_dir = os.path.join("/mnt/data/Telechargements/transmission/tv-sonarr", folder_name)
    os.makedirs(target_dir, exist_ok=True)

    with open(os.path.join(target_dir, "sample.nfo"), "w") as f:
        f.write("Sample NFO non-video file for import warning test.")
    with open(os.path.join(target_dir, "readme.txt"), "w") as f:
        f.write("Readme text file for import warning test.")

    print(f"1. Created target directory with non-video files:\n   {target_dir}")

    # Generate .torrent matching the local files using transmission-create
    try:
        subprocess.run(
            f"docker exec transmission transmission-create -o /tmp/test_import.torrent /downloads/transmission/tv-sonarr/{folder_name}",
            shell=True,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        b64_metainfo = subprocess.check_output(
            "docker exec transmission base64 /tmp/test_import.torrent",
            shell=True
        ).decode("ascii").replace("\n", "")
        print("2. Generated matching torrent metainfo inside Transmission container.")
    except Exception as e:
        print(f"ERROR generating torrent file: {e}")
        return

    # Add to Transmission via RPC
    tr_url = "http://172.17.0.1:804/transmission/rpc"
    auth = ("antoine", "spartane118")

    try:
        r1 = requests.post(tr_url, auth=auth)
        sid = r1.headers.get("X-Transmission-Session-Id")
        headers = {"X-Transmission-Session-Id": sid}

        body = {
            "method": "torrent-add",
            "arguments": {
                "metainfo": b64_metainfo,
                "download-dir": "/downloads/transmission/tv-sonarr",
                "paused": False
            }
        }
        r2 = requests.post(tr_url, auth=auth, headers=headers, json=body)
        res_data = r2.json()
        added_info = res_data.get("arguments", {}).get("torrent-added") or res_data.get("arguments", {}).get("torrent-duplicate")
        t_id = added_info.get("id") if added_info else None
        print(f"3. Added torrent to Transmission (Torrent ID: {t_id}).")

        if t_id:
            # Verify local data to set percentDone = 1.0
            requests.post(tr_url, auth=auth, headers=headers, json={"method": "torrent-verify", "arguments": {"ids": [t_id]}})
            time.sleep(2)
    except Exception as e:
        print(f"ERROR sending RPC to Transmission: {e}")

    # Trigger Sonarr CheckForFinishedDownload
    s_headers = {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}
    print("4. Triggering Sonarr CheckForFinishedDownload command...")
    requests.post(f"{SONARR_URL}/api/v3/command", json={"name": "CheckForFinishedDownload"}, headers=s_headers)

    time.sleep(3)

    # Check Sonarr queue
    q_resp = requests.get(f"{SONARR_URL}/api/v3/queue", headers=s_headers)
    records = q_resp.json().get("records", []) if q_resp.status_code == 200 else []

    print("\n" + "=" * 70)
    print("  RESULT: SONARR ACTIVITY QUEUE HAS BEEN INJECTED")
    print("=" * 70)
    print(f"Active items in Sonarr Queue: {len(records)}")

    for rec in records[:3]:
        print(f" • Title         : {rec.get('title')}")
        print(f"   Status        : {rec.get('status')} | TrackedState: {rec.get('trackedDownloadState')}")
        print(f"   StatusMsgs    : {rec.get('statusMessages')}")

    print("\n" + "!" * 70)
    print(f"👉 OPEN YOUR SONARR WEB UI NOW TO SEE THE IMPORT ERROR LIVE:")
    print(f"   {SONARR_URL}/activity/queue")
    print(f"   Item: '{folder_name}'")
    print(f"   Warning: 'No files found are eligible for import in...'")
    print("!" * 70)


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--unittest":
            sys.argv.pop(1)
            unittest.main()
            return
        elif sys.argv[1] == "--inject":
            inject_import_error_into_sonarr()
            return

    print("=" * 70)
    print("  SeerrSentinel — Stale Queue Import Simulation & Test Tool")
    print("=" * 70)
    print("1. Inject a REAL import error into Sonarr Activity Queue (and LEAVE IT THERE)")
    print("2. Run SeerrSentinel cleaner to purge queue (without blocklisting)")
    print("3. Run offline unit tests")
    print("4. Exit")

    choice = input("\nSelect an option [1-4]: ").strip()
    if choice == "1":
        inject_import_error_into_sonarr()
    elif choice == "2":
        clean_stuck_downloads(SONARR_API_KEY, SONARR_URL, "Sonarr", dry_run=False)
    elif choice == "3":
        suite = unittest.TestLoader().loadTestsFromTestCase(TestStaleQueueImportCleanup)
        runner = unittest.TextTestRunner(verbosity=1)
        runner.run(suite)
    else:
        print("Bye!")


if __name__ == "__main__":
    main()
