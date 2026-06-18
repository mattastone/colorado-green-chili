# Green Chile Reviews Map

Fetches transcripts for every Short on the [@GreenChileReviews](https://www.youtube.com/@GreenChileReviews/shorts) YouTube channel, extracts a structured restaurant dataset, geocodes each spot, and renders an interactive Leaflet map at `index.html`.

## Setup

Requires macOS with Homebrew:

```
brew install yt-dlp python@3.12
make install
```

## Common commands

| Command | Description |
|---------|-------------|
| `make all` | Fetch new transcripts + rebuild map |
| `make fetch` | Fetch transcripts only |
| `make analyze` | Rebuild map from cached transcripts (no network fetch) |
| `make retry` | Re-attempt IP-blocked videos after rate-limit clears |
| `make serve` | Serve map at http://localhost:8765 |
| `make clean` | Remove generated artifacts (`out/`, `index.html`) |

Run `make` with no arguments for the full target list.

## Viewing the map

`index.html` must be served over HTTP — it won't work opened directly as a file. Use `make serve`, then open http://localhost:8765.

Markers are two-tiered:
- **Solid/opaque** — precise restaurant address found by geocoder
- **Dashed/translucent** — fell back to city centroid (geocoder couldn't find the spot)

## Fixing a wrong or missing location

1. Find the `video_id` from the transcript filename in `transcripts/` or from the YouTube URL.
2. Add a row to `manual_locations.csv`:
   ```
   video_id,lat,lon,address
   ABC123xyz,39.754,-104.990,"2148 Larimer St, Denver, CO 80205"
   ```
3. Run `make analyze` — manual entries always win over geocoder output.

For wrong city extraction (geocoder finds the right name in the wrong city), add to `CITY_OVERRIDES` in `analyze.py` instead.

## Rate limits

YouTube blocks both `youtube-transcript-api` and `yt-dlp` after ~30 sequential transcript fetches (HTTP 429). When this happens, wait ~1 hour, then run `make retry`. Don't loop `make fetch` — it deepens the block.

To fetch a different channel:

```
make fetch URL="https://www.youtube.com/@OtherChannel/shorts"
```

## Project layout

```
transcripts.py        Fetch loop: yt-dlp lists Shorts, youtube-transcript-api pulls
                      captions, yt-dlp falls back when the API gets IP-blocked.
analyze.py            Parses cached transcripts -> reviews.csv, geocodes, renders map.
manual_locations.csv  Hand-placed lat/lon overrides keyed by video_id.
index.html            Generated Leaflet map (repo root). Rebuilt by make analyze.

transcripts/          Cached per-video .txt files. Do not hand-edit.
out/reviews.csv       12-column dataset, one row per Short.
out/geo_cache.json    Geocoder cache (persists across runs).
```
