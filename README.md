<p align="center">
  <img src=".logo.png" alt="SeerrSentinel Logo" width="100" height="100">
</p>
<h1 align="center">SeerrSentinel</h1>

Automation suite for managing **Seerr**, **Radarr**, and **Sonarr**. I was struggling to keep my library clean so i made this script. The goal of SeerrSentinel is to automate media discovery, library cleanup, and file imports for Seerr, Radarr, and Sonarr.

>[!IMPORTANT]
> Any help will be very welcome! It works with my use case but may need some tweaks for yours. Report any bug or feature request 😊.

## Scripts

| File | Role |
|---|---|
| `seerr_sentinel.py` | **Main entry point** — orchestrator + config validation |
| `sentinel_search.py` | Automated searches for missing media |
| `sentinel_cleaner.py` | Detection and deletion of stalled/missing media |
| `sentinel_import.py` | Video file injection from the downloads folder |

## Installation

It is highly recommended to use **Docker** or **Docker Compose**. A continuously updated image is available.

### Using Docker Compose (Recommended)

1. Create a `docker-compose.yml` file:
```yaml
services:
  seerr-sentinel:
    image: ghcr.io/banawoke/seerrsentinel:latest
    container_name: seerr-sentinel
    restart: unless-stopped
    user: "1000:1000" # Run as your local user (recommend)
    environment:
      - JELLYSEER_API_KEY=your_api_key
      - JELLYSEER_URL=http://your-Seerr:5055
      - TMDB_API_KEY=your_api_key
      - RADARR_API_KEY=your_api_key
      - RADARR_URL=http://your-radarr:7878
      - SONARR_API_KEY=your_api_key
      - SONARR_URL=http://your-sonarr:8989
      - DOWNLOADS_PATH=/downloads
      # - RELEASE_BUFFER_DAYS=7
      # - DELETION_DELAY_DAYS=2
      # - KEEP_REQUESTS_OLDER_THAN_DAYS=14
      # - DELETE_NOT_MANAGED_JELLYSEERR=true
      # - STUCK_DOWNLOAD_MINUTES=20.0
      # - MAX_DOWNLOAD_HOURS=6.0
      # - DAEMON_INTERVAL_SECONDS=60
      # - SEARCH_INTERVAL_MINUTES=15
      # - IMPORT_INTERVAL_MINUTES=30
      # - CLEAN_INTERVAL_MINUTES=240
      # - MOVIE_CYCLE_HOURS=12
      # - MOVIE_MAX_SEARCHES=2
      # - SEASON_CYCLE_HOURS=12
      # - SEASON_MAX_SEARCHES=2
      # - EPISODE_CYCLE_HOURS=12
      # - EPISODE_MAX_SEARCHES=1
    volumes:
      - /path/to/your/downloads:/downloads
```
2. Start the container:
```bash
docker compose up -d
```

### Using Docker CLI

```bash
docker run -d \
  --name seerr-sentinel \
  --restart unless-stopped \
  -e JELLYSEER_API_KEY=your_api_key \
  -e JELLYSEER_URL=http://your-Seerr:5055 \
  -e TMDB_API_KEY=your_api_key \
  -e RADARR_API_KEY=your_api_key \
  -e RADARR_URL=http://your-radarr:7878 \
  -e SONARR_API_KEY=your_api_key \
  -e SONARR_URL=http://your-sonarr:8989 \
  -e DOWNLOADS_PATH=/downloads \
  --user 1000:1000 \
  # Other optional variables
  -v /path/to/your/downloads:/downloads \
  ghcr.io/banawoke/seerrsentinel:latest
```

### Manual Installation (Python)

If you prefer to run the scripts manually:

1. **Configure the environment**
```bash
cp .env.example .env
# Edit .env with your API keys and URLs
```

2. **Python dependencies**
```bash
pip install -r requirements.txt
```

3. **Check your configuration**
```bash
python3 seerr_sentinel.py --health-check
```

## Usage

### Docker (Daemon Mode)

When using the Docker image, the script automatically runs in `daemon` mode. It stays alive in the background and handles its own schedule.
These intervals are customizable via environment variables (defaults shown below):
- **Search**: every 15 minutes (`SEARCH_INTERVAL_MINUTES`)
- **Import**: every 30 minutes (`IMPORT_INTERVAL_MINUTES`)
- **Clean**: every 4 hours (`CLEAN_INTERVAL_MINUTES`)

The daemon checks the timers every 60 seconds (`DAEMON_INTERVAL_SECONDS`).

You can check everything it does in real-time by reading the logs:
```bash
docker logs -f seerr-sentinel
```

Alternatively, you can manually trigger operations inside the container:
```bash
docker exec -it seerr-sentinel python3 seerr_sentinel.py clean --dry-run
docker exec -it seerr-sentinel python3 seerr_sentinel.py search
```

### Manual Usage (Python)

If you are running the scripts manually:

#### Simple

```bash
# Check your environment setup before anything else
python3 seerr_sentinel.py --health-check

# Run everything in one go (dry-run to test safely first)
python3 seerr_sentinel.py all --dry-run

# Run everything in one go
python3 seerr_sentinel.py all
```

### Step by step

```bash
# Trigger Radarr/Sonarr searches for missing media
python3 seerr_sentinel.py search

# Cleanup (dry-run to test safely first)
python3 seerr_sentinel.py clean --dry-run
python3 seerr_sentinel.py clean

# Video file injection
python3 seerr_sentinel.py import
python3 seerr_sentinel.py import --sonarr --force-id 42
```

## Configuration (`.env`)

| Variable | Required | Description |
|---|---|---|
| `JELLYSEER_API_KEY` | yes | Seerr API key |
| `JELLYSEER_URL` | yes | Seerr URL (`http://your-Seerr:5055`) |
| `TMDB_API_KEY` | yes | TMDB API key (register here [TMDB](https://www.themoviedb.org/settings/api))|
| `RADARR_API_KEY` | yes | Radarr API key |
| `RADARR_URL` | yes | Radarr URL (`http://your-radarr:7878`) |
| `SONARR_API_KEY` | yes | Sonarr API key |
| `SONARR_URL` | yes | Sonarr URL (`http://your-sonarr:8989`) |
| `DOWNLOADS_PATH` | yes | Path to the downloads folder |
| `RELEASE_BUFFER_DAYS` | optional | Days after release before cleanup (default: `7`) |
| `DELETION_DELAY_DAYS` | optional | Grace period before deletion (default: `2`) |
| `KEEP_REQUESTS_OLDER_THAN_DAYS` | optional | Keep Seerr requests older than N days (default: `14`) |
| `DELETE_NOT_MANAGED_JELLYSEERR` | optional | Allow cleaner to delete media not tracked by Jellyseerr (default: `true`) |
| `STUCK_DOWNLOAD_MINUTES` | optional | Minutes to wait before removing a download with <= 5% progress (default: `20.0`) |
| `MAX_DOWNLOAD_HOURS` | optional | Maximum hours before a download is removed regardless of progress (default: `6.0`) |
| `DAEMON_INTERVAL_SECONDS` | optional | How often the background daemon checks the timers (default: `60`) |
| `SEARCH_INTERVAL_MINUTES` | optional | How frequently the search module runs (default: `15`) |
| `IMPORT_INTERVAL_MINUTES` | optional | How frequently the import module runs (default: `30`) |
| `CLEAN_INTERVAL_MINUTES` | optional | How frequently the clean module runs (default: `240`) |
| `MOVIE_CYCLE_HOURS` | optional | Duration of search cycle for movies (default: `12`) |
| `MOVIE_MAX_SEARCHES` | optional | Max searches per cycle for a movie (default: `2`) |
| `SEASON_CYCLE_HOURS` | optional | Duration of search cycle for seasons (default: `12`) |
| `SEASON_MAX_SEARCHES` | optional | Max searches per cycle for a season (default: `2`) |
| `EPISODE_CYCLE_HOURS` | optional | Duration of search cycle for individual episodes (default: `12`) |
| `EPISODE_MAX_SEARCHES` | optional | Max searches per cycle for an episode (default: `1`) |

## Architecture

### `seerr_sentinel.py all` and `daemon` logic

When running the `all` command (or the `daemon` mode), the script manages its own sub-intervals via a lightweight local JSON cache. By default these are the timers (which can be overriden via `.env` variables):
- **Search**: Executes only every 15 min.
- **Import**: Executes only if 30 minutes have passed since the last run.
- **Clean**: Executes only if 4 hours have passed since the last run.

### `sentinel_search` logic

1. Checks for active commands (global lock)
2. Looks for missing Radarr candidates → triggers `MoviesSearch`
3. Looks at Sonarr missing episodes → determines Pack Search vs. Individual Search
4. **Standard Search Triggers**: Triggers standard `SeasonSearch` or `EpisodeSearch` commands on Sonarr
5. Per-cycle quota (12h) to avoid flooding indexers

### `sentinel_cleaner` logic

1. Fetches all missing media from Radarr/Sonarr
2. Ignores recent releases (`RELEASE_BUFFER_DAYS`)
3. After `DELETION_DELAY_DAYS` days → deletes from Radarr/Sonarr
4. **Declines** the Jellyseerr request and sends a notification to the requester (via configured notification agents)
5. The declined request stays visible in Jellyseerr — the user can re-request with one click
6. Keeps Seerr requests older than `KEEP_REQUESTS_OLDER_THAN_DAYS` days (no decline sent for those)
7. **Smart Queue Cleanup**: For downloads lingering in the queue whose media is **already imported in the library** (`hasFile=True`), clears the stale queue entry without blocklisting (`blocklist=false`) to keep Sonarr/Radarr in sync
8. **Automated Queue Blocklisting**: For truly stuck downloads (<= 5% progress after `STUCK_DOWNLOAD_MINUTES` or > `MAX_DOWNLOAD_HOURS`) or unimportable/corrupt releases with missing files, automatically removes & blocklists them (`removeFromClient=true`, `blocklist=true`) so indexers can try other releases

### `sentinel_import` logic

1. Scans the `DOWNLOADS_PATH` folder
2. Matches files against missing media using title tokens + TMDB aliases
3. **Smart Hardlink Detection**: Differentiates between hardlinks within the `downloads` directory (legitimate multi-source torrents) and external library hardlinks, eliminating false-positive skips
4. Skips import if content is actively downloading
5. Creates hard-links in Radarr/Sonarr media folders
6. Triggers `RescanMovie` / `RescanSeries`, waits for confirmation, and re-fetches library state for fresh verification
