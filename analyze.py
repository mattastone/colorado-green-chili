#!/usr/bin/env python3
"""
Analyze fetched transcripts.

Steps:
  1. Parse each cached transcript: pull episode #, restaurant, city, score, tags.
  2. Geocode restaurant+city via Nominatim (OpenStreetMap, free, no API key).
  3. Write out/reviews.csv and index.html (interactive Leaflet map).

Setup:
  pip install -r requirements.txt

Usage (prefer the Makefile targets):
  python analyze.py
  python analyze.py --no-geocode    # skip step 2 (still writes CSV)
  python analyze.py --no-map        # skip step 3
"""

import argparse
import csv
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

# --- Paths -------------------------------------------------------------------
OUTDIR = Path("transcripts")
INDEX_FILE = OUTDIR / "_index.json"
ARTIFACTS = Path("out")
GEO_CACHE = ARTIFACTS / "geo_cache.json"
REVIEWS_CSV = ARTIFACTS / "reviews.csv"
MAP_HTML = Path("index.html")
MANUAL_LOCATIONS = Path("manual_locations.csv")

# --- Geocoding tunables ------------------------------------------------------
DEFAULT_REGION = "Colorado, USA"
MAX_KM_FROM_CITY = 30.0  # downgrade precise hit to city centroid past this
BBOX_RADIUS_KM = 25.0    # restrict precise queries to this radius around city

# --- Parsing regexes ---------------------------------------------------------
VID_RE = re.compile(r"_([A-Za-z0-9_-]{11})$")
EP_RE = re.compile(r"Ep(?:isode|\.)?\s*(\d+)", re.IGNORECASE)

# Score: "X out of 5 / five on the chili/chile scale"
SCORE_RES = [
    # "X [two] out of 5 on [the] [green] chili/chile scale" or "for the chili"
    re.compile(
        r"(\d(?:\.\s*\d)?)\s*(?:two\s+)?out\s*of\s*(?:5|five)"
        r"\s*(?:on\s*(?:the\s*)?(?:green\s*)?chil[ei]|for\s+the\s+chil[ei])",
        re.IGNORECASE,
    ),
    # "five/5 out of five on [green] chili scale"
    re.compile(
        r"\b(five|5(?:\.0)?)\s*out\s*of\s*(?:5|five)"
        r"\s*(?:on\s*(?:the\s*)?(?:green\s*)?chil[ei]|for\s+the\s+chil[ei])",
        re.IGNORECASE,
    ),
    # "a [perfect] five out of five"
    re.compile(r"a\s+(?:perfect\s+)?(five|5(?:\.0)?)\s*out\s*of\s*(?:5|five)", re.IGNORECASE),
    # "in a X out of 5" (phrasing without "on the chili scale")
    re.compile(r"in\s+a\s+(\d(?:\.\s*\d)?)\s*out\s*of\s*(?:5|five)\b", re.IGNORECASE),
    # Bare fallback: any "X [two] out of 5/five" not caught above
    re.compile(r"(\d(?:\.\s*\d)?)\s*(?:two\s+)?out\s*of\s*(?:5|five)\b", re.IGNORECASE),
]

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Within one sentence: "(Today,) I am at X in Y" / "we are at X in Y"
# Case-sensitive on purpose so City stays Title-Case and we don't gobble
# trailing lowercase verbs.
LOCATION_IN_SENTENCE = re.compile(
    r"(?:I'm|I am|we are|I was invited to check out|invited to|I have a special guest [^.]*?and we are)"
    r"\s+(?:at\s+)?([^.]+?)\s+in\s+(?:downtown\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})\b"
)

KNOWN_DENVER_LANDMARKS = ["South Broadway", "Santa Fe", "Federal", "Colfax"]
CITY_OVERRIDES = {
    # Restaurants the regex can't pin a city to — host mentions only the area
    # or uses non-standard phrasing.
    "Ricco's Burritos": "Denver",
    "Don Juan": "LaSalle",
    "Gomez Burritos": "Denver",
    "Niwot Market": "Niwot",
    "Monaghan's": "Denver",
    "Salsas": "Denver",
    "La Fiesta": "Denver",
    "Consuelo's Express": "Fort Collins",
    "Santiagos Mexican Restaurant": "Brighton",
    "Las Delicias": "Glendale",
    "Corona's Mexican Grill": "Broomfield",
    "Los Arcos Express": "Federal Heights",
    # Early episodes — city transcribed wrong or not said on camera
    "Los Gallitos": "Denver",
    "Chili Shack.": "Denver",
    "The Original Chubby's Denver": "Denver",
    "Mudrocks Tap & Tavern": "Louisville",
    "Old Santa Fe": "Louisville",
}

# Video IDs that are channel intro/meta videos, not actual reviews.
EXCLUDE_VIDS = {
    "Z9zW5pPLD3A",  # Channel intro — no restaurant visit
    "aADf-tg-xTs",  # New Website Launch announcement — no restaurant visit
}


# --- Geo helpers -------------------------------------------------------------
def haversine_km(a_lat, a_lon, b_lat, b_lon):
    R = 6371.0
    a_lat, a_lon, b_lat, b_lon = map(math.radians, [a_lat, a_lon, b_lat, b_lon])
    dlat, dlon = b_lat - a_lat, b_lon - a_lon
    h = math.sin(dlat / 2) ** 2 + math.cos(a_lat) * math.cos(b_lat) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def bbox_around(lat: float, lon: float, radius_km: float):
    """Return [(top_left_lat, top_left_lon), (bottom_right_lat, bottom_right_lon)]
    suitable for Nominatim's viewbox parameter."""
    # 1 deg lat ≈ 111 km; 1 deg lon ≈ 111 * cos(lat) km
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.1))
    return [(lat + dlat, lon - dlon), (lat - dlat, lon + dlon)]


def title_from_filename(path: Path):
    stem = path.stem
    m = VID_RE.search(stem)
    vid = m.group(1) if m else "?"
    title = re.sub(r"^\d+_", "", stem)
    title = re.sub(r"_" + re.escape(vid) + r"$", "", title)
    return title.replace("_", " "), vid


def load_index() -> dict:
    if INDEX_FILE.exists():
        return json.loads(INDEX_FILE.read_text())
    return {}


def extract_episode(title: str):
    m = EP_RE.search(title)
    return int(m.group(1)) if m else None


def extract_restaurant_from_title(title: str):
    # "Green Chile Reviews Ep. 82 - Jus' Burritos" -> "Jus' Burritos"
    # "Episode 4 - Tamale Kitchen #food #..."     -> "Tamale Kitchen"
    m = re.search(r"-\s*([^#]+?)(?:\s*#|\s+with\s+@|$)", title, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def extract_score(text: str):
    for r in SCORE_RES:
        m = r.search(text)
        if m:
            raw = m.group(1).lower().replace(" ", "")
            if raw in ("five", "5", "5.0"):
                return 5.0
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def extract_restaurant_city(text: str, title: str):
    title_name = extract_restaurant_from_title(title)
    for sent in SENTENCE_SPLIT.split(text):
        m = LOCATION_IN_SENTENCE.search(sent)
        if m:
            city = m.group(2).strip()
            return title_name or clean_name(m.group(1)), city
    # No "in CITY" found — sometimes host names only a Denver landmark/street
    for landmark in KNOWN_DENVER_LANDMARKS:
        if landmark in text:
            return title_name, "Denver"
    # Manual override table for restaurants where regex never gets a city
    if title_name and title_name in CITY_OVERRIDES:
        return title_name, CITY_OVERRIDES[title_name]
    return title_name, None


def clean_name(raw: str) -> str:
    n = raw.strip().rstrip(",.")
    n = re.sub(r"^(?:the\s+(?!Armadillo))", "", n, flags=re.IGNORECASE)
    n = re.sub(r"\s+(?:Mexican\s+restaurant|Mexican\s+cuisine|restaurant|cafe|"
               r"bar\s+and\s+grill|sports\s+bar(?:\s+and\s+grill)?)$",
               "", n, flags=re.IGNORECASE)
    return n.strip()


def extract_tags(text: str) -> str:
    t = text.lower()
    tags = []
    if "vegetarian" in t:
        tags.append("vegetarian")
    if "gluten-free" in t or "gluten free" in t:
        tags.append("gluten-free")
    if "hatch" in t or "new mexico chil" in t:
        tags.append("hatch/NM chilies")
    if "habanero" in t or "ghost pepper" in t:
        tags.append("super-hot")
    if "carnitas" in t or "shredded pork" in t or "pork green chili" in t:
        tags.append("pork")
    if "smoked" in t and "pork" in t:
        tags.append("smoked-pork")
    if "gravy" in t:
        tags.append("gravy-style")
    if "smooth texture" in t or "smooth broth" in t:
        tags.append("smooth")
    return ",".join(tags)


def collect_rows() -> list[dict]:
    index = load_index()
    rows = []
    for p in sorted(OUTDIR.glob("*.txt")):
        if p.name.startswith("_"):
            continue
        title_filename, vid = title_from_filename(p)
        title = index.get(vid, {}).get("title", title_filename)
        text = p.read_text(encoding="utf-8")
        ep = extract_episode(title) or extract_episode(title_filename)
        name, city = extract_restaurant_city(text, title)
        score = extract_score(text)
        tags = extract_tags(text)
        rows.append({
            "episode": ep,
            "restaurant": name or "",
            "city": city or "",
            "address": "",
            "lat": "",
            "lon": "",
            "score": score if score is not None else "",
            "tags": tags,
            "video_id": vid,
            "url": f"https://youtube.com/shorts/{vid}",
            "transcript_path": str(p),
        })
    rows.sort(key=lambda r: (r["episode"] is None, -(r["episode"] or 0)))
    return rows


def _strip_possessive(name: str) -> str:
    return re.sub(r"'s\b", "", name)


def photon_search(name: str, city: str, viewbox):
    """Fallback geocoder — Photon is OSM-based but more tolerant on fuzzy names.

    Returns (lat, lon, address) on a confident restaurant/cafe match; None otherwise.
    """
    # viewbox is [(tl_lat, tl_lon), (br_lat, br_lon)] from bbox_around()
    if viewbox:
        (tl_lat, tl_lon), (br_lat, br_lon) = viewbox
        bbox = f"{tl_lon},{br_lat},{br_lon},{tl_lat}"
    else:
        bbox = ""
    q = f"{name} {city}".strip()
    url = (f"https://photon.komoot.io/api/?q={quote(q)}&limit=3"
           + (f"&bbox={bbox}" if bbox else ""))
    print(f"    photon: {q}")
    try:
        req = Request(url, headers={"User-Agent": "green-chili-shorts/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"      error: {e}")
        return None
    time.sleep(1.0)
    GOOD_TYPES = {"restaurant", "fast_food", "cafe", "bar", "pub", "food_court"}
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        if p.get("osm_value") in GOOD_TYPES:
            lon, lat = feat["geometry"]["coordinates"]
            parts = [p.get("name", ""), p.get("housenumber", ""), p.get("street", ""),
                     p.get("city", ""), p.get("state", "")]
            address = ", ".join(x for x in parts if x)
            return lat, lon, address
    return None


def geocode_rows(rows: list[dict], skip_vids: set | None = None) -> list[dict]:
    """Geocode each row's restaurant+city. Rows whose video_id is in skip_vids
    are left untouched — useful when a manual override will overwrite them
    anyway (avoids ~1.1s per row of wasted Nominatim calls on first run)."""
    skip_vids = skip_vids or set()
    try:
        from geopy.geocoders import Nominatim
    except ImportError:
        print("geopy not installed — `pip install geopy` to enable geocoding")
        return rows

    cache = json.loads(GEO_CACHE.read_text()) if GEO_CACHE.exists() else {}
    nom = Nominatim(user_agent="green-chili-shorts-analyzer/1.0")
    city_cache: dict[str, tuple[float, float] | None] = {}

    def lookup(query: str, viewbox=None, bounded=False):
        print(f"    try: {query}" + (" [bbox]" if bounded else ""))
        try:
            loc = nom.geocode(query, timeout=10, country_codes="us",
                              viewbox=viewbox, bounded=bounded)
        except Exception as e:
            print(f"      error: {e}")
            return None
        time.sleep(1.1)
        return loc

    def city_centroid(city: str):
        if city not in city_cache:
            key = f"__city__|{city}"
            if key in cache and cache[key].get("lat") is not None:
                city_cache[city] = (cache[key]["lat"], cache[key]["lon"])
            else:
                loc = lookup(f"{city}, Colorado")
                if loc:
                    cache[key] = {"lat": loc.latitude, "lon": loc.longitude,
                                  "address": f"{city}, Colorado (approx.)",
                                  "precision": "city"}
                    GEO_CACHE.write_text(json.dumps(cache, indent=2))
                    city_cache[city] = (loc.latitude, loc.longitude)
                else:
                    cache[key] = {"lat": None, "lon": None, "address": "", "precision": ""}
                    city_cache[city] = None
        return city_cache[city]

    for r in rows:
        if not r["restaurant"]:
            continue
        if r["video_id"] in skip_vids:
            continue
        city = r["city"] or ""
        key = f"{r['restaurant']}|{city}"
        if key in cache:
            entry = cache[key]
        else:
            print(f"  geocoding {r['restaurant']!r} in {city!r}")
            name = r["restaurant"]
            stripped = _strip_possessive(name)
            cc = city_centroid(city) if city else None
            viewbox = bbox_around(cc[0], cc[1], BBOX_RADIUS_KM) if cc else None

            # Try bbox-restricted queries first (returns the right branch of chains)
            loc = None
            if viewbox:
                for q in [f"{name}, {city}, Colorado",
                          *( [f"{stripped}, {city}, Colorado"] if stripped != name else [] ),
                          f"{name} {city} Colorado",
                          name]:  # name alone within bbox often works for small spots
                    loc = lookup(q, viewbox=viewbox, bounded=True)
                    if loc:
                        break
            # Fall back to unbounded queries
            if loc is None:
                for q in ([f"{name}, {city}, Colorado"] if city else []) + [f"{name}, Colorado"]:
                    loc = lookup(q)
                    if loc:
                        break

            entry = None
            if loc and cc:
                dist = haversine_km(loc.latitude, loc.longitude, cc[0], cc[1])
                if dist > MAX_KM_FROM_CITY:
                    print(f"      ! {dist:.0f}km from {city} centroid — chain "
                          f"mismatch, downgrading to city centroid")
                    entry = {"lat": cc[0], "lon": cc[1],
                             "address": f"{city}, Colorado (approx., "
                                        f"chain match was {dist:.0f}km away)",
                             "precision": "city"}
            if loc and entry is None:
                entry = {"lat": loc.latitude, "lon": loc.longitude,
                         "address": loc.address, "precision": "restaurant"}
            # Nominatim missed — try Photon (more tolerant fuzzy matching)
            if entry is None and city:
                hit = photon_search(name, city, viewbox)
                if hit:
                    plat, plon, paddr = hit
                    if cc and haversine_km(plat, plon, cc[0], cc[1]) <= MAX_KM_FROM_CITY:
                        entry = {"lat": plat, "lon": plon,
                                 "address": paddr, "precision": "restaurant"}
            if entry is None and cc:
                entry = {"lat": cc[0], "lon": cc[1],
                         "address": f"{city}, Colorado (approx.)",
                         "precision": "city"}
            if entry is None:
                entry = {"lat": None, "lon": None, "address": "", "precision": ""}
            cache[key] = entry
            GEO_CACHE.write_text(json.dumps(cache, indent=2))

        if entry.get("lat") is not None:
            r["lat"] = entry["lat"]
            r["lon"] = entry["lon"]
            r["address"] = entry.get("address", "")
            r["precision"] = entry.get("precision", "")
    return rows


def load_manual_locations() -> dict[str, dict]:
    """Load manual_locations.csv (video_id,lat,lon,address) for spots OSM lacks."""
    if not MANUAL_LOCATIONS.exists():
        return {}
    out = {}
    with MANUAL_LOCATIONS.open() as f:
        for r in csv.DictReader(f):
            vid = r.get("video_id", "").strip()
            try:
                lat = float(r["lat"])
                lon = float(r["lon"])
            except (KeyError, ValueError):
                continue
            if vid:
                out[vid] = {"lat": lat, "lon": lon,
                            "address": r.get("address", "").strip()}
    return out


def apply_manual_overrides(rows: list[dict], overrides: dict[str, dict]) -> list[dict]:
    applied = 0
    for r in rows:
        ov = overrides.get(r["video_id"])
        if ov:
            r["lat"] = ov["lat"]
            r["lon"] = ov["lon"]
            r["address"] = ov["address"] or r.get("address", "")
            r["precision"] = "restaurant"
            applied += 1
    print(f"Applied {applied} manual overrides")
    return rows


def write_csv(rows: list[dict]) -> None:
    fields = ["episode", "restaurant", "city", "address", "lat", "lon",
              "precision", "score", "tags", "video_id", "url", "transcript_path"]
    with REVIEWS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def write_map(rows: list[dict]) -> None:
    points = [
        {
            "episode": r["episode"],
            "restaurant": r["restaurant"],
            "city": r["city"],
            "address": r.get("address", ""),
            "lat": r["lat"],
            "lon": r["lon"],
            "score": r["score"],
            "tags": r["tags"],
            "url": r["url"],
            "precision": r.get("precision", ""),
        }
        for r in rows
        if r.get("lat") not in (None, "") and r["score"] != ""
    ]
    html = MAP_TEMPLATE.replace("__DATA__", json.dumps(points))
    MAP_HTML.write_text(html, encoding="utf-8")
    print(f"  map points: {len(points)}")


MAP_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <title>Green Chile Reviews — Map</title>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    html, body, #map { height: 100%; margin: 0; }
    .legend { background: white; padding: 10px 12px; border-radius: 6px;
              box-shadow: 0 1px 6px rgba(0,0,0,0.3); font: 13px/1.4 sans-serif; }
    .legend h3 { margin: 0 0 6px 0; font-size: 13px; }
    .legend i { display: inline-block; width: 14px; height: 14px; margin-right: 8px;
                border-radius: 50%; vertical-align: middle; border: 1px solid #222; }
    .popup { font: 13px/1.4 sans-serif; min-width: 200px; }
    .popup b { font-size: 14px; }
    .popup .score { font-size: 20px; font-weight: bold; display: block; margin: 4px 0; }
    .popup .addr { color: #666; font-size: 11px; }
    .popup .tags { color: #555; font-style: italic; margin-top: 4px; }
  </style>
</head>
<body>
<div id="map"></div>
<script>
const data = __DATA__;

const map = L.map('map').setView([39.8, -105.05], 9);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap', maxZoom: 19
}).addTo(map);

function color(score) {
  if (score >= 4.5) return '#2c7a2c';
  if (score >= 4.0) return '#7ab32a';
  if (score >= 3.5) return '#e0b020';
  if (score >= 3.0) return '#e07020';
  return '#c0282a';
}

const bounds = [];
data.forEach(d => {
  const approx = d.precision === 'city';
  const m = L.circleMarker([d.lat, d.lon], {
    radius: 7 + (d.score || 3) * 1.4,
    fillColor: color(d.score), color: '#222',
    weight: approx ? 1 : 1.5,
    dashArray: approx ? '3,3' : null,
    opacity: 1, fillOpacity: approx ? 0.55 : 0.85
  }).addTo(map);
  m.bindPopup(`
    <div class="popup">
      <b>${d.restaurant}</b><br/>
      ${d.city || ''}
      ${d.address ? '<br/><span class="addr">' + d.address + '</span>' : ''}
      <span class="score" style="color:${color(d.score)}">${d.score} / 5</span>
      ${d.tags ? '<div class="tags">' + d.tags + '</div>' : ''}
      <a href="${d.url}" target="_blank">Watch Ep. ${d.episode}</a>
    </div>
  `);
  bounds.push([d.lat, d.lon]);
});
if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });

const legend = L.control({ position: 'bottomright' });
legend.onAdd = function () {
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = '<h3>Chili scale</h3>' +
    [[4.5,'4.5+'],[4.0,'4.0–4.4'],[3.5,'3.5–3.9'],[3.0,'3.0–3.4'],[0,'<3.0']]
    .map(([s, l]) => '<i style="background:' + color(s) + '"></i> ' + l).join('<br/>');
  return div;
};
legend.addTo(map);
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-geocode", action="store_true")
    ap.add_argument("--no-map", action="store_true")
    args = ap.parse_args()

    ARTIFACTS.mkdir(exist_ok=True)
    rows = [r for r in collect_rows() if r["video_id"] not in EXCLUDE_VIDS]
    parsed = len(rows)
    scored = sum(1 for r in rows if r["score"] != "")
    with_city = sum(1 for r in rows if r["city"])
    print(f"Parsed {parsed} transcripts ({scored} with scores, {with_city} with cities)")

    missing = [r for r in rows if not r["city"] or not r["restaurant"]]
    if missing:
        print(f"\n{len(missing)} rows missing restaurant or city — edit reviews.csv after writing if you want them mapped:")
        for r in missing:
            print(f"  Ep {r['episode']}: restaurant={r['restaurant']!r} city={r['city']!r}")

    overrides = load_manual_locations()
    if overrides:
        print(f"\nLoaded {len(overrides)} manual location overrides")

    if not args.no_geocode:
        print("\nGeocoding (Nominatim, ~1 req/sec)...")
        rows = geocode_rows(rows, skip_vids=set(overrides))

    if overrides:
        rows = apply_manual_overrides(rows, overrides)

    write_csv(rows)
    print(f"\nWrote {REVIEWS_CSV}")

    if not args.no_map:
        write_map(rows)
        print(f"Wrote {MAP_HTML}")


if __name__ == "__main__":
    main()
