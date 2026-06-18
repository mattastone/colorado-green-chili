#!/usr/bin/env python3
"""
Fetch transcripts for every Short on a YouTube channel.

Pipeline:
  1. yt-dlp enumerates Shorts (IDs + titles).
  2. For each video, try youtube-transcript-api; on IP-block, fall back to
     yt-dlp's subtitle download (different transport — often gets through).
  3. Persist a per-video status in _index.json so retries only hit recoverable
     failures (--retry-blocked / --retry-all-failed).
  4. Combined _ALL_TRANSCRIPTS.txt is rebuilt from every cached .txt on disk.

Setup:
  brew install yt-dlp           # also python@3.12 if you don't have it
  pip install -r requirements.txt

Usage (prefer the Makefile targets):
  python transcripts.py "https://www.youtube.com/@GreenChileReviews/shorts"
  python transcripts.py <url> --retry-blocked       # retry IP-blocked vids
  python transcripts.py <url> --retry-all-failed    # retry everything not ok
  python transcripts.py <url> --refresh-listing     # bypass channel-listing cache
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

try:
    from youtube_transcript_api._errors import RequestBlocked, IpBlocked
    BLOCKED_EXCS: tuple = (RequestBlocked, IpBlocked)
except ImportError:
    BLOCKED_EXCS = ()

OUTDIR = Path("transcripts")
INDEX_FILE = OUTDIR / "_index.json"
COMBINED_FILE = OUTDIR / "_ALL_TRANSCRIPTS.txt"
LISTING_CACHE = OUTDIR / "_channel_listing.json"
LISTING_TTL_SECONDS = 3600  # 1h — channel rarely changes between runs
LANGS = ["en", "en-US", "en-GB"]
DEFAULT_SLEEP = 3.0
MAX_CONSECUTIVE_BLOCKS = 5

VID_RE = re.compile(r"_([A-Za-z0-9_-]{11})$")


def list_shorts(channel_url: str, refresh: bool = False):
    """Cached enumeration of Shorts on a channel.

    yt-dlp --flat-playlist takes ~30s; cache to LISTING_CACHE keyed by URL.
    Refresh after LISTING_TTL_SECONDS or when --refresh-listing is passed.
    """
    OUTDIR.mkdir(exist_ok=True)
    cached = {}
    if LISTING_CACHE.exists():
        try:
            cached = json.loads(LISTING_CACHE.read_text())
        except json.JSONDecodeError:
            cached = {}
    entry = cached.get(channel_url)
    if entry and not refresh:
        age = time.time() - entry.get("fetched_at", 0)
        if age < LISTING_TTL_SECONDS:
            print(f"(using cached listing, age {age:.0f}s)")
            return entry["items"]

    cmd = ["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(title)s", channel_url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"yt-dlp failed:\n{result.stderr}")
    items = []
    for line in result.stdout.strip().splitlines():
        if "\t" in line:
            vid, title = line.split("\t", 1)
        else:
            vid, title = line, ""
        items.append({"id": vid.strip(), "title": title.strip()})

    cached[channel_url] = {"fetched_at": time.time(), "items": items}
    LISTING_CACHE.write_text(json.dumps(cached, indent=2))
    return items


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip()
    return re.sub(r"\s+", "_", s)[:80] or "untitled"


def is_block_error(exc: Exception) -> bool:
    if BLOCKED_EXCS and isinstance(exc, BLOCKED_EXCS):
        return True
    msg = str(exc).lower()
    return (
        ("ip" in msg and ("block" in msg or "429" in msg))
        or "too many requests" in msg
    )


def fetch_via_api(api: YouTubeTranscriptApi, vid: str):
    try:
        fetched = api.fetch(vid, languages=LANGS)
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None, "no_captions"
    except Exception as e:
        if is_block_error(e):
            return None, "blocked"
        return None, f"error: {type(e).__name__}"
    text = " ".join(s.text for s in fetched)
    return text, "ok"


def fetch_via_ytdlp(vid: str):
    """Fallback: yt-dlp uses a different transport so it sometimes gets through."""
    tmp = OUTDIR / "_ytdlp_tmp"
    tmp.mkdir(exist_ok=True)
    cmd = [
        "yt-dlp", "--skip-download",
        "--write-auto-subs", "--write-subs",
        "--sub-langs", "en.*", "--sub-format", "vtt",
        "-o", str(tmp / f"{vid}.%(ext)s"),
        f"https://www.youtube.com/watch?v={vid}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    vtts = list(tmp.glob(f"{vid}*.vtt"))
    if r.returncode != 0:
        for v in vtts:
            v.unlink(missing_ok=True)
        if "429" in r.stderr or "Too Many Requests" in r.stderr:
            return None, "blocked"
        if "no subtitles" in r.stderr.lower():
            return None, "no_captions"
        return None, f"error: yt-dlp rc={r.returncode}"
    if not vtts:
        return None, "no_captions"
    text = vtt_to_plain(vtts[0].read_text(encoding="utf-8"))
    for v in vtts:
        v.unlink(missing_ok=True)
    return text, "ok"


def vtt_to_plain(vtt: str) -> str:
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
    out: list[str] = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return " ".join(out)


def load_index() -> dict:
    if INDEX_FILE.exists():
        return json.loads(INDEX_FILE.read_text())
    return {}


def save_index(index: dict) -> None:
    OUTDIR.mkdir(exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2))


def find_existing_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for p in OUTDIR.glob("*.txt"):
        if p.name.startswith("_"):
            continue
        m = VID_RE.search(p.stem)
        if m:
            paths[m.group(1)] = p
    return paths


def rebuild_combined(index: dict) -> None:
    parts = []
    paths = find_existing_paths()
    title_by_vid = {vid: meta.get("title", "") for vid, meta in index.items()}
    for vid, p in sorted(paths.items(), key=lambda kv: kv[1].name):
        title = title_by_vid.get(vid) or p.stem
        text = p.read_text(encoding="utf-8")
        parts.append(f"### {title}\n# https://youtube.com/shorts/{vid}\n\n{text}\n")
    COMBINED_FILE.write_text("\n\n".join(parts), encoding="utf-8")


def should_skip(prev_status: str, args) -> bool:
    # Note: callers check the disk cache first; reaching here means the file is
    # NOT on disk. So a prev_status of "ok" means the cached file is gone — we
    # must re-fetch, not skip.
    if not prev_status or prev_status == "ok":
        return False
    if prev_status == "no_captions":
        return not args.retry_all_failed
    if prev_status == "blocked":
        return not (args.retry_blocked or args.retry_all_failed)
    if prev_status.startswith("error"):
        return not args.retry_all_failed
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--retry-blocked", action="store_true",
                    help="Re-attempt vids previously marked blocked")
    ap.add_argument("--retry-all-failed", action="store_true",
                    help="Re-attempt every vid that isn't ok")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help=f"Seconds between requests (default {DEFAULT_SLEEP})")
    ap.add_argument("--no-fallback", action="store_true",
                    help="Don't fall back to yt-dlp when the API is blocked")
    ap.add_argument("--refresh-listing", action="store_true",
                    help="Bypass the channel-listing cache and re-enumerate")
    args = ap.parse_args()

    OUTDIR.mkdir(exist_ok=True)
    index = load_index()
    api = YouTubeTranscriptApi()

    print(f"Listing Shorts on {args.url} ...")
    shorts = list_shorts(args.url, refresh=args.refresh_listing)
    print(f"Found {len(shorts)} Shorts.\n")

    existing = find_existing_paths()
    consec_blocks = 0
    counts = {"ok_new": 0, "ok_cached": 0, "no_captions": 0, "blocked": 0,
              "error": 0, "skipped": 0}

    for i, short in enumerate(shorts, 1):
        vid = short["id"]
        title = short["title"]
        print(f"[{i}/{len(shorts)}] {vid}  {title[:55]}")

        prev = index.get(vid, {})
        # Always record latest title
        prev["title"] = title

        # Disk cache wins over index status
        if vid in existing:
            print("    cached on disk")
            counts["ok_cached"] += 1
            index[vid] = {**prev, "status": "ok", "path": str(existing[vid])}
            continue

        if should_skip(prev.get("status", ""), args):
            print(f"    previously {prev.get('status')}, skipping (override with --retry-*)")
            counts["skipped"] += 1
            index[vid] = prev
            continue

        text, status = fetch_via_api(api, vid)
        used_fallback = False
        if status == "blocked" and not args.no_fallback:
            print("    api blocked, trying yt-dlp fallback")
            text, status = fetch_via_ytdlp(vid)
            used_fallback = (status == "ok")

        now = datetime.now(timezone.utc).isoformat()
        if status == "ok":
            fname = OUTDIR / f"{i:03d}_{safe_name(title)}_{vid}.txt"
            fname.write_text(text, encoding="utf-8")
            counts["ok_new"] += 1
            consec_blocks = 0
            index[vid] = {**prev, "status": "ok", "path": str(fname),
                          "fetched_at": now,
                          "source": "yt-dlp" if used_fallback else "api"}
            print(f"    saved ({'yt-dlp' if used_fallback else 'api'})")
        elif status == "blocked":
            print("    BLOCKED")
            counts["blocked"] += 1
            consec_blocks += 1
            index[vid] = {**prev, "status": "blocked", "last_attempt": now}
        elif status == "no_captions":
            print("    no captions available")
            counts["no_captions"] += 1
            consec_blocks = 0
            index[vid] = {**prev, "status": "no_captions", "last_attempt": now}
        else:
            print(f"    {status}")
            counts["error"] += 1
            consec_blocks = 0
            index[vid] = {**prev, "status": status, "last_attempt": now}

        save_index(index)
        time.sleep(args.sleep)

        if consec_blocks >= MAX_CONSECUTIVE_BLOCKS:
            print(f"\n!! {consec_blocks} consecutive blocks — bailing this run.")
            print("   Re-run later with --retry-blocked once the rate-limit clears.")
            break

    tmp = OUTDIR / "_ytdlp_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    rebuild_combined(index)
    save_index(index)

    print("\nDone.")
    for k, v in counts.items():
        print(f"  {k:>12}: {v}")
    print(f"Index:    {INDEX_FILE.resolve()}")
    print(f"Combined: {COMBINED_FILE.resolve()}")


if __name__ == "__main__":
    main()
