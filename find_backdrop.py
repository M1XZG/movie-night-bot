#!/usr/bin/env python3
"""
Find a landscape (16:9 backdrop) image for a movie from TheMovieDB and download
it to a temp dir (override with the MOVIE_BOT_TMP env var). Stdlib only.

Usage:
    python3 find_backdrop.py "Movie Title" [year]

Prints the local path of the downloaded image on success, or "NONE" on failure.
"""
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TMP = Path(os.environ.get(
    "MOVIE_BOT_TMP", Path(tempfile.gettempdir()) / "movie-night-bot"))


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def find_movie_id(title: str, year: str | None) -> str | None:
    q = urllib.parse.quote(title)
    html = get(f"https://www.themoviedb.org/search?query={q}")
    ids = re.findall(r"/movie/(\d+)", html)
    # de-dup preserving order
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    if not ordered:
        return None
    if not year:
        return ordered[0]
    # pick the candidate whose page release year matches
    for mid in ordered[:6]:
        try:
            page = get(f"https://www.themoviedb.org/movie/{mid}")
            m = re.search(r'release_date">\((\d{4})', page)
            if m and m.group(1) == str(year):
                return mid
        except Exception:
            continue
    return ordered[0]


def backdrop_hashes(mid: str) -> list[str]:
    html = get(f"https://www.themoviedb.org/movie/{mid}/images/backdrops")
    hashes = re.findall(r"image\.tmdb\.org/t/p/original/([A-Za-z0-9]+)\.jpg", html)
    if not hashes:  # fall back to og:image / main page
        html = get(f"https://www.themoviedb.org/movie/{mid}")
        hashes = re.findall(
            r"image\.tmdb\.org/t/p/[a-z0-9_]+/([A-Za-z0-9]+)\.jpg", html)
    out, seen = [], set()
    for h in hashes:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def download(h: str, dest: Path) -> bool:
    url = f"https://image.tmdb.org/t/p/w1280/{h}.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if len(data) < 5000:
            return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print("NONE")
        return 1
    title = sys.argv[1]
    year = sys.argv[2] if len(sys.argv) > 2 else None
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        mid = find_movie_id(title, year)
        if not mid:
            print("NONE")
            return 1
        hashes = backdrop_hashes(mid)
        if not hashes:
            print("NONE")
            return 1
        safe = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_").lower()
        dest = TMP / f"movienight_{safe}.jpg"
        for h in hashes[:6]:
            if download(h, dest):
                print(str(dest))
                return 0
        print("NONE")
        return 1
    except Exception:
        print("NONE")
        return 1


if __name__ == "__main__":
    sys.exit(main())
