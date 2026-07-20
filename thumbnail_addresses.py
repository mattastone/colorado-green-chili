#!/usr/bin/env python3
"""
thumbnail_addresses.py

For each video that has only a city-precision (or no) location and is not
already in manual_locations.csv, fetch its YouTube thumbnail, use OCR to
read the address overlay on the green background card, geocode it, and
insert the result at the top of manual_locations.csv (newest-first convention).

No API key required — uses Tesseract OCR (brew install tesseract) and
Pillow for image processing.

Usage:
  .venv/bin/python thumbnail_addresses.py              # process all city-precision rows (newest first)
  .venv/bin/python thumbnail_addresses.py --dry-run    # preview without writing
  .venv/bin/python thumbnail_addresses.py --video ID   # process a single video ID
  .venv/bin/python thumbnail_addresses.py --verify [N] # test OCR vs known entries (default 10)
"""

import argparse
import csv
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from analyze import EXCLUDE_VIDS

REVIEWS_CSV    = Path("out/reviews.csv")
MANUAL_CSV     = Path("manual_locations.csv")
THUMB_URL      = "https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
THUMB_FALLBACK = "https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

_STREET_SUFFIXES = re.compile(
    r"\b(Street|Avenue|Boulevard|Drive|Road|Lane|Highway|Parkway|Terrace|Circle|"
    r"Court|Place|Trail|St|Ave|Blvd|Dr|Rd|Ln|Way|Ct|Pl|Cir|Hwy|Pkwy|Pike|Trl|Ter|Xing|Loop)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Thumbnail fetch
# ---------------------------------------------------------------------------

def fetch_thumbnail(vid: str) -> bytes | None:
    for url in [THUMB_URL.format(vid=vid), THUMB_FALLBACK.format(vid=vid)]:
        try:
            req = Request(url, headers={"User-Agent": "green-chili-thumb/1.0"})
            with urlopen(req, timeout=10) as r:
                return r.read()
        except HTTPError as e:
            if e.code == 404:
                continue
            print(f"  HTTP {e.code} fetching thumbnail for {vid}")
            return None
        except Exception as e:
            print(f"  Error fetching thumbnail: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# OCR — isolate green address box then read text
# ---------------------------------------------------------------------------

def _find_green_band(pixels, cw: int, ch: int) -> tuple[int, int] | None:
    """Return (y_top, y_bottom) of the green address card, or None.

    Requires ≥40% of a row to be dark-green AND ≥10 consecutive such rows,
    to filter out scattered green from foliage, logos, or clothing.
    """
    def row_green_fraction(y: int) -> float:
        hits = sum(
            1 for x in range(0, cw, 4)
            if (lambda r, g, b: g > 60 and g > r * 1.3
                and g > b * 1.2 and r < 120 and b < 120)(*pixels[x, y])
        )
        return hits / (cw // 4)

    green_ys = [y for y in range(ch) if row_green_fraction(y) >= 0.40]
    if not green_ys:
        return None

    best = run = 1
    for i in range(1, len(green_ys)):
        run = run + 1 if green_ys[i] == green_ys[i - 1] + 1 else 1
        best = max(best, run)
    if best < 10:
        return None

    return max(0, min(green_ys) - 4), min(ch, max(green_ys) + 4)


def _trim_street(s: str) -> str:
    """Cut after the last recognised street-type suffix (removes OCR noise).
    For streets without a standard suffix (e.g. Broadway), strips trailing
    1–2 char OCR noise tokens instead."""
    m = None
    for m in _STREET_SUFFIXES.finditer(s):
        pass
    if m:
        return s[:m.end()].strip()
    # No recognised suffix — strip trailing short OCR noise (e.g. "ae", "mm")
    return re.sub(r"(\s+[A-Za-z]{1,2})+\s*$", "", s).strip()


def _clean_address(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().strip(",")
    s = re.sub(r"\bCO[.\s]*1?(\d{5})\b", r"CO \1", s)
    s = re.sub(r"\bSte\b", "St", s)  # OCR noise: trailing 'e' on "St"
    return s


def _parse_address_lines(lines: list[str]) -> tuple[str | None, str | None]:
    """Find (street_line, city_line) from OCR output lines."""
    street_line = city_line = None
    for line in lines:
        if street_line is None:
            ms = re.search(r"\b(\d{3,5}\s+[A-Za-z][^\n]{4,40})", line)
            if ms:
                street_line = _trim_street(ms.group(1))
        if city_line is None:
            mc = re.search(
                r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s*CO[.\s]*1?(\d{5}))", line)
            if mc:
                city_line = re.sub(
                    r"CO[.\s]*1?(\d{5})", r"CO \1", mc.group(1)).strip()
    return street_line, city_line


def extract_address_from_image(image_bytes: bytes) -> str | None:
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        import pytesseract
    except ImportError:
        import sys
        sys.exit("Missing deps — run: make install && brew install tesseract")

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size

    # Crop to the centre column — left/right thirds are blurred repeats.
    cx0, cx1 = width // 4, width - width // 4
    center = img.crop((cx0, 0, cx1, height))
    cw, ch = center.size

    # Build candidate crops in priority order: green card first (cleanest
    # signal, inverted for white-on-black OCR), wide fallback second.
    crops: list[tuple] = []
    band = _find_green_band(center.load(), cw, ch)
    if band:
        y_top, y_bottom = band
        crops.append((center.crop((0, y_top, cw, y_bottom)), True))
    crops.append((center.crop((0, int(ch * 0.40), cw, int(ch * 0.82))), False))

    config = ("--psm 6 -c tessedit_char_whitelist='"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
              "0123456789 .,#-'")

    for crop, do_invert in crops:
        scale = 3
        c = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
        c = c.convert("L")
        c = ImageOps.invert(c) if do_invert else ImageEnhance.Contrast(c).enhance(2.0)
        c = c.filter(ImageFilter.SHARPEN)

        lines = [l.strip() for l in
                 pytesseract.image_to_string(c, config=config).splitlines() if l.strip()]
        street_line, city_line = _parse_address_lines(lines)

        # Only return when BOTH lines found — prevents garbage from a false-positive
        # green crop from blocking the fallback crop.
        if street_line and city_line:
            return _clean_address(f"{street_line}, {city_line}")

    return None


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def nominatim_geocode(address: str) -> tuple[float, float] | None:
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={quote(address)}&format=json&limit=1&countrycodes=us")
    try:
        req = Request(url, headers={"User-Agent": "green-chili-thumb/1.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        time.sleep(1.1)
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"  Geocoding error: {e}")
    return None


def geocode_with_retries(address: str) -> tuple[tuple[float, float] | None, str]:
    """Geocode, with fallback retries for common OCR noise patterns.
    Returns (coords_or_None, final_address_used).
    """
    coords = nominatim_geocode(address)
    if coords:
        return coords, address

    # Strip isolated single-char OCR noise (e.g. "2501 L Dallas St")
    cleaned = re.sub(r"(?<!\d)\s+[A-Za-z]\s+(?!\d)", " ", address)
    if cleaned != address:
        print(f"  Retrying (noise stripped): {cleaned}")
        coords = nominatim_geocode(cleaned)
        if coords:
            return coords, cleaned

    # If house number is 5 digits, leading digit may be OCR noise
    m = re.match(r"^\d{5}\b", address)
    if m:
        trimmed = address[1:]
        print(f"  Retrying (leading digit stripped): {trimmed}")
        coords = nominatim_geocode(trimmed)
        if coords:
            return coords, trimmed

    return None, address


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_manual_vids() -> set[str]:
    if not MANUAL_CSV.exists():
        return set()
    with MANUAL_CSV.open() as f:
        return {r["video_id"].strip() for r in csv.DictReader(f) if r.get("video_id")}


def load_manual_entries() -> dict[str, dict]:
    if not MANUAL_CSV.exists():
        return {}
    with MANUAL_CSV.open() as f:
        return {r["video_id"].strip(): r for r in csv.DictReader(f) if r.get("video_id")}


def load_reviews() -> dict[str, dict]:
    if not REVIEWS_CSV.exists():
        return {}
    with REVIEWS_CSV.open() as f:
        return {r["video_id"]: r for r in csv.DictReader(f)}


def load_target_rows() -> list[dict]:
    """Return city-precision (or no-location) rows, newest episode first."""
    if not REVIEWS_CSV.exists():
        import sys; sys.exit(f"{REVIEWS_CSV} not found — run make analyze first")
    rows = []
    with REVIEWS_CSV.open() as f:
        for r in csv.DictReader(f):
            if r["video_id"] in EXCLUDE_VIDS:
                continue
            if r.get("precision", "") in ("city", ""):
                rows.append(r)
    rows.sort(key=lambda r: -int(r["episode"]) if r.get("episode", "").isdigit() else 0)
    return rows


def prepend_to_manual_csv(vid: str, lat: float, lon: float,
                           address: str, restaurant: str) -> None:
    """Insert a new row directly after the header (newest-first convention)."""
    text = MANUAL_CSV.read_text()
    lines = text.splitlines(keepends=True)
    new_row = f'{vid},{lat:.6f},{lon:.6f},"{address}",# {restaurant}\n'
    lines.insert(1, new_row)
    MANUAL_CSV.write_text("".join(lines))


# ---------------------------------------------------------------------------
# Verify mode — test OCR accuracy against known manual entries
# ---------------------------------------------------------------------------

def run_verify(limit: int) -> None:
    entries = load_manual_entries()
    reviews = load_reviews()

    def ep_key(vid: str) -> int:
        ep = reviews.get(vid, {}).get("episode", "")
        return -int(ep) if ep.isdigit() else 0

    vids = sorted(entries, key=ep_key)[:limit]
    matched = failed_ocr = mismatched = 0

    for vid in vids:
        row = entries[vid]
        known_addr = row.get("address", "").strip().strip('"')
        r = reviews.get(vid, {})
        ep   = r.get("episode", "?")
        name = r.get("restaurant") or row.get("# restaurant", vid)

        print(f"Ep {ep} — {name} ({vid})")
        image = fetch_thumbnail(vid)
        if not image:
            print("  [no thumbnail]\n")
            failed_ocr += 1
            continue

        address = extract_address_from_image(image)
        if not address:
            print("  [no address in thumbnail]\n")
            failed_ocr += 1
            continue

        if address.lower() == known_addr.lower():
            print(f"  OK  {address}")
            matched += 1
        else:
            print(f"  OCR: {address}")
            print(f"  KNW: {known_addr}  <-- mismatch")
            mismatched += 1
        print()

    print(f"Verified {len(vids)}: {matched} matched, {mismatched} mismatched, {failed_ocr} no OCR result")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print results without writing to manual_locations.csv")
    ap.add_argument("--video", metavar="ID",
                    help="Process a single video ID")
    ap.add_argument("--verify", metavar="N", nargs="?", const=10, type=int,
                    help="Test OCR against N known manual entries, newest first (default 10)")
    args = ap.parse_args()

    if args.verify is not None:
        run_verify(args.verify)
        return

    already_done = load_manual_vids()

    if args.video:
        reviews = load_reviews()
        r = reviews.get(args.video, {})
        targets = [{
            "video_id": args.video,
            "restaurant": r.get("restaurant", args.video),
            "city": r.get("city", ""),
            "episode": r.get("episode", "?"),
            "precision": "city",
        }]
    else:
        targets = [r for r in load_target_rows()
                   if r["video_id"] not in already_done]

    if not targets:
        print("Nothing to process — all city-precision rows already have manual overrides.")
        return

    print(f"Processing {len(targets)} video(s)...\n")
    succeeded, failed = [], []

    for r in targets:
        vid  = r["video_id"]
        name = r.get("restaurant") or vid
        ep   = r.get("episode", "?")
        print(f"Ep {ep} — {name} ({vid})")

        image = fetch_thumbnail(vid)
        if not image:
            failed.append((ep, name, vid, "no thumbnail"))
            print()
            continue

        address = extract_address_from_image(image)
        if not address:
            print("  No address found in thumbnail")
            failed.append((ep, name, vid, "no address in thumbnail"))
            print()
            continue
        print(f"  Extracted: {address}")

        coords, address = geocode_with_retries(address)
        if not coords:
            print("  Geocoding failed — add to manual_locations.csv manually")
            failed.append((ep, name, vid, f"geocode failed for: {address!r}"))
            print()
            continue

        lat, lon = coords
        print(f"  Geocoded:  {lat:.6f}, {lon:.6f}")

        if args.dry_run:
            print(f"  [dry-run] would write to {MANUAL_CSV}")
        else:
            prepend_to_manual_csv(vid, lat, lon, address, name)
            print(f"  Written to {MANUAL_CSV}")

        succeeded.append((ep, name, address))
        print()

    print(f"Done — {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        print("\nFailed:")
        for ep, name, vid, reason in failed:
            print(f"  Ep {ep} {name} ({vid}): {reason}")


if __name__ == "__main__":
    main()
