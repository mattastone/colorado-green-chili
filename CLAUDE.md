# Green Chile Reviews — transcript fetcher + analyzer

Fetches transcripts for every Short on a YouTube channel (default: `@GreenChileReviews`),
extracts a structured review dataset (episode, restaurant, city, score, tags),
geocodes each spot, and renders an interactive map.

## Layout

```
transcripts.py        Fetch loop: yt-dlp lists Shorts, youtube-transcript-api pulls
                      captions, yt-dlp falls back when the API gets IP-blocked.
analyze.py            Parses cached transcripts -> reviews.csv, geocodes via
                      Nominatim (with Photon fallback), renders index.html.
thumbnail_addresses.py  OCR script: reads address text from YouTube thumbnail images
                      and writes to manual_locations.csv (newest entry first). Works best for Ep 80+.
Makefile              Entry point. Run `make` for a help summary.
requirements.txt      Python deps. yt-dlp and tesseract are brew-installed, not pip.
manual_locations.csv  Hand-placed lat/lon for spots OSM lacks. Source of truth.

transcripts/          Cached per-video .txt files (NEVER hand-edit).
  _index.json         Per-video status: ok | no_captions | blocked | error.
  _channel_listing.json  Cached yt-dlp enumeration (1h TTL).
  _ALL_TRANSCRIPTS.txt   Rebuilt every run from all cached .txt files.

index.html            Standalone Leaflet map (repo root). Open over http (not file://).

out/                  Generated artifacts. Safe to delete (`make clean`).
  reviews.csv         12-column dataset, one row per Short.
  geo_cache.json      Per-restaurant lat/lng cache. Keys: "name|city" for
                      restaurants, "__city__|City" for centroids.
```

## Setup (once)

```
brew install yt-dlp python@3.12 tesseract
make install
```

Python 3.10+ required (`str | None` syntax). The venv is `.venv/` in the repo root.

## Common commands

```
make all          # full pipeline: fetch → analyze → addresses (OCR) → analyze
make retry        # re-attempt IP-blocked videos (use after rate-limit clears)
make analyze      # rebuild map only (e.g. after editing manual_locations.csv)
make addresses    # OCR thumbnails → write precise addresses to manual_locations.csv
make serve        # http://localhost:8765 in a browser
```

Override the channel: `make fetch URL="https://www.youtube.com/@OtherChannel/shorts"`.
Force a fresh channel enumeration (skip the 1h listing cache):
`.venv/bin/python transcripts.py "$URL" --refresh-listing`.

## Geocoding waterfall

For each restaurant, analyze.py tries in order:
1. **Nominatim, bbox-restricted** to a ~25km box around the declared city's
   centroid — solves the chain-restaurant problem (Rio Grande, Don Juan).
2. **Nominatim, unbounded** as a fallback for cities without a centroid.
3. **Distance check** — if the precise hit is >`MAX_KM_FROM_CITY` (30km) from
   the city centroid, downgrade to the centroid (chain mismatch).
4. **Photon** (komoot's OSM-based geocoder, free) for fuzzy name matches
   Nominatim missed. Filtered to amenity types: restaurant/cafe/bar/etc.

Rows whose `video_id` appears in `manual_locations.csv` **skip steps 1–4 entirely**
and use the stored lat/lon directly.

## Manual location overrides

`manual_locations.csv` (repo root): `video_id,lat,lon,address,# restaurant`
(5 columns; the last is a comment for human readability). Entries are kept in
reverse-chronological order (newest at top after header). Re-run `make analyze`
to apply. For city-precision rows, try `make addresses` first (OCR from thumbnails),
then enter any remaining addresses manually.

## Known quirks

- **YouTube rate-limits aggressively.** Both `youtube-transcript-api` and `yt-dlp`
  get HTTP 429 after ~30 sequential transcript fetches. The fetcher bails after
  5 consecutive blocks (`MAX_CONSECUTIVE_BLOCKS` in transcripts.py). Wait ~1h
  and run `make retry`.

- **No street addresses in transcripts.** Host shows them as on-screen overlays.
  We rely on "I am at X in Y" phrasing for restaurant + city, then geocode.
  When that regex misses, see `CITY_OVERRIDES` in analyze.py for the city,
  or `manual_locations.csv` for full lat/lon.

- **Title parsing** depends on the host's "Ep. N - Restaurant" format. Episodes
  1–5 use "Episode N -" instead; both forms are handled.

- **Non-review Shorts** (channel intros, announcements) are filtered via
  `EXCLUDE_VIDS` in analyze.py — a set of `video_id` strings. Add to it when a
  new non-review Short appears in the listing.

- **Two-tier markers on the map.** Solid+opaque = precise restaurant address.
  Dashed+translucent = city centroid (geocoder couldn't find the spot).

## Extending

- **Different channel:** the scripts are channel-agnostic; just pass `URL=...`.
  But the analyzer's location regex and `CITY_OVERRIDES` are tuned to this host's
  speech patterns — re-tuning needed for other channels.

- **More tags:** edit `extract_tags()` in analyze.py. Tags surface in popups
  and reviews.csv.

- **Different map style:** edit `MAP_TEMPLATE` in analyze.py. It's a single
  HTML string with embedded Leaflet JS.

## Don't

- Hand-edit files in `transcripts/` (the per-video .txt caches). For location
  corrections, use `manual_locations.csv`; for city-disambiguation, use
  `CITY_OVERRIDES` in analyze.py. Either path survives a fresh fetch.
- Skip the `make install` step on a fresh checkout — system Python is 3.9 and
  won't run the scripts.
- Re-run `make fetch` in a tight loop after a 429. You'll just deepen the block.
