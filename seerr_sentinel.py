#!/usr/bin/env python3
"""
SeerrSentinel — Central orchestrator and configuration loader.

Usage:
    python3 seerr_sentinel.py --health-check
    python3 seerr_sentinel.py search
    python3 seerr_sentinel.py clean [--dry-run]
    python3 seerr_sentinel.py import [--radarr] [--sonarr] [--force-id N]
    python3 seerr_sentinel.py all [--dry-run]
    python3 seerr_sentinel.py daemon [--dry-run] [--interval N]
"""

import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration — chargement du .env
# ---------------------------------------------------------------------------

# Locate .env: look next to this script first, then fall back to /cronjob/.env (Docker)
_SCRIPT_DIR = Path(__file__).parent
_ENV_CANDIDATES = [
    _SCRIPT_DIR / ".env",
    Path("/cronjob/.env"),
]
_ENV_PATH = next((p for p in _ENV_CANDIDATES if p.exists()), _SCRIPT_DIR / ".env")
load_dotenv(dotenv_path=_ENV_PATH)

# All known variables across the suite
_ALL_REQUIRED_VARS = [
    "JELLYSEER_API_KEY",
    "JELLYSEER_URL",
    "TMDB_API_KEY",
    "RADARR_API_KEY",
    "RADARR_URL",
    "SONARR_API_KEY",
    "SONARR_URL",
    "DOWNLOADS_PATH",
]

_ALL_OPTIONAL_VARS = {
    "RELEASE_BUFFER_DAYS": "7",
    "DELETION_DELAY_DAYS": "2",
    "KEEP_REQUESTS_OLDER_THAN_DAYS": "14",
    "DELETE_NOT_MANAGED_JELLYSEERR": "true",
    "PARTIAL_CLEANUP_ENABLED": "true",
    "STUCK_DOWNLOAD_MINUTES": "20.0",
    "MAX_DOWNLOAD_HOURS": "6.0",
    "DAEMON_INTERVAL_SECONDS": "60",
    "SEARCH_INTERVAL_MINUTES": "15",
    "CLEAN_INTERVAL_MINUTES": "240",
    "IMPORT_INTERVAL_MINUTES": "30",
    "MOVIE_CYCLE_HOURS": "12",
    "MOVIE_MAX_SEARCHES": "2",
    "SEASON_CYCLE_HOURS": "12",
    "SEASON_MAX_SEARCHES": "2",
    "EPISODE_CYCLE_HOURS": "12",
    "EPISODE_MAX_SEARCHES": "1",
}


def load_config(required: list) -> dict:
    """
    Validates that all variables listed in `required` are set in the environment
    (loaded from .env).

    Returns a dict {var_name: value}.
    Calls sys.exit(1) if any variable is missing.
    """
    config = {}
    missing = []
    for var in required:
        value = os.environ.get(var, "").strip()
        if not value:
            missing.append(var)
        else:
            config[var] = value

    if missing:
        print(
            f"[SeerrSentinel] Missing variables in .env: {', '.join(missing)}\n"
            f"                Expected .env file: {_ENV_PATH}"
        )
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CLI — Commands
# ---------------------------------------------------------------------------

def _cmd_health_check(compact: bool = False) -> int:
    """Actively test connectivity to all configured services.

    compact=True: only print failures/warnings (silent when all OK).
    compact=False (default): full report with all OK lines.
    """
    OK    = "  [  OK  ]"
    WARN  = "  [ WARN ]"
    FAIL  = "  [ FAIL ]"

    errors   = []
    warnings = []

    if not compact:
        print("SeerrSentinel \u2014 Health Check & Environment")
        print("=" * 52)

        print("\n  Environment variables:")
        all_ok = True
        for var in _ALL_REQUIRED_VARS:
            value = os.environ.get(var, "").strip()
            if value:
                display = value[:10] + "…" if "KEY" in var else value
                print(f"     {var:<35} = {display}")
            else:
                print(f"     {var:<35} — {FAIL} MISSING")
                all_ok = False
                errors.append(f"Missing required environment variable: {var}")

        for var, default in _ALL_OPTIONAL_VARS.items():
            value = os.environ.get(var, "")
            if value:
                print(f"     {var:<35} = {value}")
            else:
                print(f"     {var:<35} — (default: {default})")


    def _get(url, api_key=None, timeout=5):
        headers = {"X-Api-Key": api_key} if api_key else {}
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            return r
        except requests.exceptions.ConnectionError:
            return None
        except requests.exceptions.Timeout:
            return "timeout"
        except Exception as e:
            return str(e)

    def _check_arr(name, base_url, api_key, endpoint="/api/v3/system/status"):
        """Test an *arr API and return (ok, detail_str)."""
        if not base_url or not api_key:
            return False, "URL or API key not configured"
        r = _get(base_url.rstrip("/") + endpoint, api_key=api_key)
        if r is None:
            return False, f"Connection refused — is {name} running at {base_url}?"
        if r == "timeout":
            return False, f"Timeout after 5 s — {base_url} may be unreachable"
        if isinstance(r, str):
            return False, f"Unexpected error: {r}"
        if r.status_code == 401:
            return False, f"Authentication failed (HTTP 401) — check your API key"
        if r.status_code == 403:
            return False, f"Forbidden (HTTP 403) — API key may have wrong permissions"
        if r.status_code == 200:
            try:
                data = r.json()
                version = data.get("version", "?")
                return True, f"Connected   (version {version})  at {base_url}"
            except Exception:
                return True, f"Connected  at {base_url}"
        return False, f"Unexpected HTTP {r.status_code} from {base_url}"

    if not compact:
        print("\n  Services reachability:")

    # ── Radarr ──────────────────────────────────────────────
    radarr_url = os.environ.get("RADARR_URL", "").strip()
    radarr_key = os.environ.get("RADARR_API_KEY", "").strip()
    ok, detail = _check_arr("Radarr", radarr_url, radarr_key)
    if ok:
        if not compact: print(f"{OK}  Radarr  — {detail}")
    else:
        print(f"{FAIL}  Radarr  — {detail}")
        errors.append(f"Radarr: {detail}")

    # ── Sonarr ──────────────────────────────────────────────
    sonarr_url = os.environ.get("SONARR_URL", "").strip()
    sonarr_key = os.environ.get("SONARR_API_KEY", "").strip()
    ok, detail = _check_arr("Sonarr", sonarr_url, sonarr_key)
    if ok:
        if not compact: print(f"{OK}  Sonarr  — {detail}")
    else:
        print(f"{FAIL}  Sonarr  — {detail}")
        errors.append(f"Sonarr: {detail}")

    # ── Jellyseerr ──────────────────────────────────────────
    seerr_url = os.environ.get("JELLYSEER_URL", "").strip()
    seerr_key = os.environ.get("JELLYSEER_API_KEY", "").strip()
    ok, detail = _check_arr("Jellyseerr", seerr_url, seerr_key, endpoint="/api/v1/settings/main")
    if ok:
        if not compact: print(f"{OK}  Jellyseerr — {detail}")
    else:
        print(f"{FAIL}  Jellyseerr — {detail}")
        errors.append(f"Jellyseerr: {detail}")

    # ── TMDB ────────────────────────────────────────────────
    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not tmdb_key:
        print(f"{FAIL}  TMDB    — API key not configured")
        errors.append("TMDB: API key not configured")
    else:
        r = _get(f"https://api.themoviedb.org/3/configuration?api_key={tmdb_key}")
        if r is None:
            print(f"{FAIL}  TMDB    — Cannot reach api.themoviedb.org (no internet?)")
            errors.append("TMDB: Cannot reach api.themoviedb.org")
        elif r == "timeout":
            print(f"{FAIL}  TMDB    — Timeout (>5 s) reaching api.themoviedb.org")
            errors.append("TMDB: Request timed out")
        elif isinstance(r, str):
            print(f"{FAIL}  TMDB    — {r}")
            errors.append(f"TMDB: {r}")
        elif r.status_code == 401:
            print(f"{FAIL}  TMDB    — Invalid API key (HTTP 401)")
            errors.append("TMDB: Invalid API key")
        elif r.status_code == 200:
            if not compact: print(f"{OK}  TMDB    — API key valid, TMDB reachable")
        else:
            print(f"{FAIL}  TMDB    — Unexpected HTTP {r.status_code}")
            errors.append(f"TMDB: HTTP {r.status_code}")

    # ── Downloads folder ────────────────────────────────────
    if not compact:
        print("\n  Downloads folder:")
    
    dl_path = os.environ.get("DOWNLOADS_PATH", "").strip()
    if not dl_path:
        print(f"{FAIL}  DOWNLOADS_PATH — not configured")
        errors.append("DOWNLOADS_PATH not configured")
    elif not os.path.exists(dl_path):
        print(f"{FAIL}  {dl_path} — directory does not exist")
        errors.append(f"Downloads path does not exist: {dl_path}")
    elif not os.access(dl_path, os.R_OK):
        print(f"{FAIL}  {dl_path} — directory exists but is not readable")
        errors.append(f"Downloads path not readable: {dl_path}")
    else:
        contents = list(Path(dl_path).iterdir())
        if not contents:
            print(f"{WARN}  {dl_path} — directory is empty (nothing to import yet)")
            warnings.append(f"Downloads path is empty: {dl_path}")
        else:
            if not compact: print(f"{OK}  {dl_path} — OK ({len(contents)} entries)")

    # ── Summary ─────────────────────────────────────────────
    if errors or warnings or not compact:
        print("\n" + "=" * 52)
        
    if errors:
        print(f"  RESULT: {len(errors)} error(s) detected — SeerrSentinel may not work correctly\n")
        for e in errors:
            print(f"    ✗ {e}")
        if warnings:
            print()
            for w in warnings:
                print(f"    ⚠ {w}")
        print()
        return 1
    elif warnings:
        print(f"  RESULT: All services reachable — {len(warnings)} warning(s)\n")
        for w in warnings:
            print(f"    ⚠ {w}")
        print()
        return 2
    else:
        if not compact:
            print("  RESULT: All checks passed — SeerrSentinel is ready ✓\n")
        return 0


def _run_script(script_name: str, extra_args: list = None) -> int:
    """Run a sub-script from the same directory via subprocess."""
    script_path = Path(__file__).parent / script_name
    env = os.environ.copy()
    env["_SEERRSENTINEL_INTERNAL"] = "1"
    cmd = [sys.executable, str(script_path)] + (extra_args or [])
    result = subprocess.run(cmd, env=env)
    return result.returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seerr_sentinel",
        description="SeerrSentinel — Jellyseerr / Radarr / Sonarr management suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  seerr_sentinel.py --health-check       # Test connectivity and show env variables\n"
            "  seerr_sentinel.py all --dry-run        # Run all steps once for testing (no changes made)\n"
            "  seerr_sentinel.py daemon --interval 60 # Run as a background daemon all steps\n"
            "  seerr_sentinel.py all                  # Run all steps once\n"
            "  seerr_sentinel.py search               # Trigger missing media search\n"
            "  seerr_sentinel.py clean --dry-run      # Preview cleanup without deleting\n"
            "  seerr_sentinel.py import               # Inject downloaded files\n"        ),
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Test connectivity to all services (Radarr, Sonarr, TMDB, Jellyseerr) and show environment variables and exit",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # search
    subparsers.add_parser(
        "search",
        help="Trigger Radarr/Sonarr searches for missing media",
    )

    # clean
    clean_p = subparsers.add_parser(
        "clean",
        help="Clean up missing/stalled media in Radarr, Sonarr and Jellyseerr",
    )
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulation without making any changes",
    )

    # import
    import_p = subparsers.add_parser(
        "import",
        help="Inject video files from the downloads folder",
    )
    import_p.add_argument("--radarr", action="store_true", help="Radarr only")
    import_p.add_argument("--sonarr", action="store_true", help="Sonarr only")
    import_p.add_argument(
        "--force-id",
        type=int,
        metavar="ID",
        help="Force a specific Series/Movie ID",
    )

    all_p = subparsers.add_parser("all", help="Run all operations in sequence")
    all_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode for the clean step",
    )

    # daemon
    daemon_p = subparsers.add_parser("daemon", help="Run continuously in the background")
    daemon_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode for the clean step",
    )
    daemon_p.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("DAEMON_INTERVAL_SECONDS", "60")),
        help="Wait time in seconds between loops",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.health_check:
        sys.exit(_cmd_health_check())

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # En-tête
    separator = "=" * 52
    print(f"\n{separator}")
    print(f"  SeerrSentinel  ›  {args.command.upper()}")
    print(f"{separator}\n")

    if args.command == "search":
        rc = _cmd_health_check(compact=True)
        if rc == 1: sys.exit(1)
        sys.exit(_run_script("sentinel_search.py"))

    elif args.command == "clean":
        rc = _cmd_health_check(compact=True)
        if rc == 1: sys.exit(1)
        extra = ["--dry-run"] if args.dry_run else []
        sys.exit(_run_script("sentinel_cleaner.py", extra))

    elif args.command == "import":
        rc = _cmd_health_check(compact=True)
        if rc == 1: sys.exit(1)
        extra = []
        if args.radarr:
            extra.append("--radarr")
        if args.sonarr:
            extra.append("--sonarr")
        if args.force_id:
            extra += ["--force-id", str(args.force_id)]
        sys.exit(_run_script("sentinel_import.py", extra))

    elif args.command in ("all", "daemon"):
        def _should_run(job: str, interval_minutes: int) -> bool:
            fpath = Path("/tmp/seerr_sentinel_schedule.json")
            if not fpath.exists(): 
                return True
            try:
                with open(fpath, "r") as f:
                    sched = json.load(f)
            except Exception:
                return True
            last_run_str = sched.get(job)
            if not last_run_str: 
                return True
            try:
                last_run = datetime.fromisoformat(last_run_str)
                now = datetime.now(timezone.utc)
                if (now - last_run) >= timedelta(minutes=interval_minutes):
                    return True
            except Exception:
                return True
            return False

        def _update_run(job: str):
            fpath = Path("/tmp/seerr_sentinel_schedule.json")
            sched = {}
            if fpath.exists():
                try:
                    with open(fpath, "r") as f:
                        sched = json.load(f)
                except Exception:
                    pass
            sched[job] = datetime.now(timezone.utc).isoformat()
            try:
                with open(fpath, "w") as f:
                    json.dump(sched, f)
            except Exception:
                pass

        def _run_pass(is_daemon=False):
            rc1 = rc2 = rc3 = 0

            s_interval = int(os.environ.get("SEARCH_INTERVAL_MINUTES", "15"))
            c_interval = int(os.environ.get("CLEAN_INTERVAL_MINUTES", "240"))
            i_interval = int(os.environ.get("IMPORT_INTERVAL_MINUTES", "30"))

            # In 'all' (manual / cron), SEARCH runs unconditionally.
            # In 'daemon', SEARCH runs based on the loaded interval.
            run_search = not is_daemon or _should_run("search", s_interval)
            run_clean = _should_run("clean", c_interval)
            run_import = _should_run("import", i_interval)

            # Silently skip if there's nothing to do in daemon mode
            if is_daemon and not (run_search or run_clean or run_import):
                return 0, False

            if is_daemon:
                print(f"\n{separator}")
                print(f"  SeerrSentinel  ›  DAEMON ACTIVATED AT {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{separator}\n")

            if run_search:
                print(f"▶  [1/3] SEARCH (Executed)")
                _update_run("search")
                rc1 = _run_script("sentinel_search.py")
            elif not is_daemon:
                print(f"▶  [1/3] SEARCH (Skipped - Interval {s_interval}m not reached)")

            if run_clean:
                print(f"\n▶  [2/3] CLEAN (Executed)")
                _update_run("clean")
                clean_args = ["--dry-run"] if args.dry_run else []
                rc2 = _run_script("sentinel_cleaner.py", clean_args)
            elif not is_daemon:
                print(f"\n▶  [2/3] CLEAN (Skipped - Interval {c_interval}m not reached)")

            if run_import:
                print(f"\n▶  [3/3] IMPORT (Executed)")
                _update_run("import")
                rc3 = _run_script("sentinel_import.py")
            elif not is_daemon:
                print(f"\n▶  [3/3] IMPORT (Skipped - Interval {i_interval}m not reached)")

            return max(rc1, rc2, rc3), True

        if args.command == "all":
            rc = _cmd_health_check(compact=True)
            if rc == 1: sys.exit(1)
            worst, _ = _run_pass(is_daemon=False)
            print(f"\n{separator}")
            print(f"  SeerrSentinel  ›  ALL done (exit codes up to {worst})")
            print(f"{separator}")
            sys.exit(worst)
        elif args.command == "daemon":
            print("Starting SeerrSentinel in daemon mode...")
            print(f"Interval: {args.interval}s between cycles. Press Ctrl+C to stop.\n")
            # Run health check once at startup — abort on critical errors
            print("--- Startup Health Check ---")
            hc_rc = _cmd_health_check(compact=True)
            if hc_rc == 1:
                print("\nCritical health check failure. Daemon will not start.")
                sys.exit(1)
            print("--- Health Check OK. Daemon started.\n")
            try:
                while True:
                    _run_pass(is_daemon=True)
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nDaemon stopped by user.")
                sys.exit(0)


if __name__ == "__main__":
    main()
