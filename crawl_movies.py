#!/usr/bin/env python3
"""
FM FTP Movie Crawler — Improved Version
========================================
- যত গভীরেই folder থাকুক, শেষ পর্যন্ত crawl করে
- Parent folder name = Category (যেমন hindidub → Hindi Dubbed)
- Movie folder name = Movie name
- TMDB থেকে poster/logo আনে
- movies_fmftp.json এ save করে

Usage:
    python crawl_movies.py
    python crawl_movies.py --output custom.json
    python crawl_movies.py --no-poster   # poster ছাড়া (দ্রুত)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote, quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ══════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════

BASE_URL        = "https://fmftp.net/data/disk-1/movies/"
OUTPUT_FILE     = "movies_fmftp.json"
REQUEST_DELAY   = 0.4          # প্রতি request এর পরে wait (seconds)
REQUEST_TIMEOUT = 20           # connection timeout
MAX_RETRIES     = 3            # fail হলে retry count

# TMDB API credentials
# GitHub Actions এ TMDB_API_TOKEN secret set করলে সেটা automatically use হবে
TMDB_API_KEY   = os.environ.get("TMDB_API_KEY",   "6944c4f8c6903253dd78963f27e2890e")
TMDB_API_TOKEN = os.environ.get("TMDB_API_TOKEN",
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI2OTQ0YzRmOGM2OTAzMjUzZGQ3ODk2M2YyN2UyODkwZSIsIm5iZiI6MTc4NDExNTA0OS42NTQsInN1YiI6IjZhNTc2ZjY5YzdhNmJlMzQxMmE3YmZiYSIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.IWpeUu3ZcJg0-gewjhtRMBq_CoJE2myi6NLzYXtjnlI"
)

# Video file extensions
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.m4v',
              '.webm', '.ts', '.flv', '.wmv', '.mpg', '.mpeg'}

# Skip এই folder গুলো
SKIP_NAMES = {'..', '.', '__pycache__', '.git', 'thumbnail', 'thumbs', 'cover', 'artwork'}

# Folder name → সুন্দর Category name
CATEGORY_MAP = {
    'bollywood':        'Bollywood',
    'hollywood':        'Hollywood',
    'hindidub':         'Hindi Dubbed',
    'hindi dub':        'Hindi Dubbed',
    'hindi dubbed':     'Hindi Dubbed',
    'indianbangla':     'Indian Bangla',
    'indian bangla':    'Indian Bangla',
    'bangla':           'Bangla',
    'korean':           'Korean',
    'turkish':          'Turkish',
    'thai':             'Thai',
    'animation':        'Animation',
    'horror':           'Horror',
    'pakisthani':       'Pakistani',
    'pakistani':        'Pakistani',
    'foreigner movie':  'Foreign',
    'foreign':          'Foreign',
    'fm':               'FM Originals',
    'south':            'South Indian',
    'tamil':            'Tamil',
    'telugu':           'Telugu',
    'malayalam':        'Malayalam',
    'action':           'Action',
    'comedy':           'Comedy',
    'drama':            'Drama',
    'thriller':         'Thriller',
    'sci-fi':           'Sci-Fi',
    'romance':          'Romance',
    'documentary':      'Documentary',
    'kids':             'Kids',
    'english':          'English',
    'chinese':          'Chinese',
    'japanese':         'Japanese',
    'nepali':           'Nepali',
    'urdu':             'Urdu',
}


# ══════════════════════════════════════════════════════════════════
# HTML Parser — Apache/Nginx "Index of" page
# ══════════════════════════════════════════════════════════════════

class IndexPageParser(HTMLParser):
    """
    Apache/Nginx style "Index of /path/" page থেকে
    সব links (files ও folders) বের করে।
    """

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[dict] = []
        self._current_href = None
        self._current_text = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            d = dict(attrs)
            href = d.get('href', '')
            # শুধু relative links নাও, query strings ও parent links বাদ দাও
            if href and not href.startswith('?') and href not in ('/', '../', '..'):
                full_url = urljoin(self.base_url, href)
                # শুধু same domain এর links নাও
                if full_url.startswith('https://fmftp.net') or full_url.startswith('http://fmftp.net'):
                    self._current_href = full_url
                    self._current_text = ''

    def handle_endtag(self, tag):
        if tag == 'a' and self._current_href:
            text = self._current_text.strip().rstrip('/')
            # Parent directory ও empty links বাদ দাও
            if text and text not in ('Parent Directory', '..', '../', '.'):
                self.links.append({
                    'href': self._current_href,
                    'text': text,
                    'is_dir': self._current_href.endswith('/'),
                })
            self._current_href = None
            self._current_text = ''

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text += data


# ══════════════════════════════════════════════════════════════════
# HTTP Helper
# ══════════════════════════════════════════════════════════════════

def fetch_html(url: str) -> str | None:
    """URL fetch করে HTML return করে। Fail হলে None।"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except HTTPError as e:
            if e.code == 403:
                print(f"  ✗ 403 Forbidden: {url}")
                return None
            if attempt < MAX_RETRIES - 1:
                print(f"  ⚠ Retry {attempt+1}: {url} ({e})")
                time.sleep(2)
            else:
                print(f"  ✗ Failed ({e.code}): {url}")
                return None
        except (URLError, Exception) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                print(f"  ✗ Error: {url} — {e}")
                return None
    return None


def parse_index_page(url: str) -> list[dict]:
    """Index page parse করে links return করে।"""
    html = fetch_html(url)
    if not html:
        return []
    parser = IndexPageParser(url)
    parser.feed(html)
    return parser.links


# ══════════════════════════════════════════════════════════════════
# TMDB Poster
# ══════════════════════════════════════════════════════════════════

_poster_cache: dict[str, str] = {}

def get_poster_url(movie_name: str, year: str = '') -> str:
    """
    TMDB থেকে movie poster URL আনে।
    Bearer token দিয়ে authenticate করে।
    """
    if not TMDB_API_TOKEN:
        return ''

    # Movie name clean করো — year ও extra info বাদ দাও
    clean_name = re.sub(r'\s*\(\d{4}\)\s*', '', movie_name).strip()
    clean_name = re.sub(r'\s*\[\d{4}\]\s*', '', clean_name).strip()
    clean_name = re.sub(r'\s*\[\w+\]\s*', '', clean_name).strip()

    cache_key = f"{clean_name}_{year}"
    if cache_key in _poster_cache:
        return _poster_cache[cache_key]

    try:
        query = quote(clean_name)
        year_param = f"&primary_release_year={year}" if year else ''
        api_url = (f"https://api.themoviedb.org/3/search/movie"
                   f"?query={query}{year_param}&include_adult=false&language=en-US&page=1")

        req = Request(api_url, headers={
            'User-Agent':    'FMFTPCrawler/1.0',
            'Authorization': f'Bearer {TMDB_API_TOKEN}',
            'Accept':        'application/json',
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        results = data.get('results', [])
        if results:
            # প্রথম result এর poster নাও
            poster_path = results[0].get('poster_path', '')
            if poster_path:
                poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
                _poster_cache[cache_key] = poster_url
                return poster_url

    except Exception as e:
        pass  # poster না পেলে খালি রাখো, crawl চলতে থাকবে

    _poster_cache[cache_key] = ''
    return ''


# ══════════════════════════════════════════════════════════════════
# Utility Functions
# ══════════════════════════════════════════════════════════════════

def is_video_file(href: str) -> bool:
    """URL টা video file কিনা check করে।"""
    clean = href.split('?')[0].lower()
    return any(clean.endswith(ext) for ext in VIDEO_EXTS)


def get_year(name: str) -> str:
    """'Movie Name (2025)' থেকে '2025' বের করে।"""
    m = re.search(r'\((\d{4})\)', name)
    if m:
        return m.group(1)
    m = re.search(r'\[(\d{4})\]', name)
    if m:
        return m.group(1)
    return ''


def get_category_name(folder_name: str) -> str:
    """Folder name থেকে সুন্দর category name বানায়।"""
    lower = folder_name.lower().strip()
    if lower in CATEGORY_MAP:
        return CATEGORY_MAP[lower]
    # Title case করে return করো
    return folder_name.replace('-', ' ').replace('_', ' ').title()


def clean_movie_name(name: str) -> str:
    """Movie name clean করো।"""
    # Extension সরাও
    for ext in VIDEO_EXTS:
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    # Underscore ও dot কে space বানাও (যদি অনেক বেশি থাকে)
    if '_' in name and ' ' not in name:
        name = name.replace('_', ' ')
    if '.' in name and ' ' not in name and name.count('.') > 2:
        name = name.replace('.', ' ')
    return name.strip()


def url_to_name(url: str) -> str:
    """URL এর শেষ segment থেকে name বের করে।"""
    path = url.rstrip('/').split('/')[-1]
    return clean_movie_name(unquote(path))


# ══════════════════════════════════════════════════════════════════
# Core Recursive Crawler
# ══════════════════════════════════════════════════════════════════

def find_video_in_folder(folder_url: str, depth: int = 0) -> str | None:
    """
    একটা folder এ ঢুকে প্রথম video file খোঁজে।
    Nested folder হলে recursively খোঁজে।
    """
    if depth > 5:  # অনেক গভীরে গেলে stop
        return None

    links = parse_index_page(folder_url)
    time.sleep(REQUEST_DELAY)

    # আগে direct video files খোঁজো
    for link in links:
        if is_video_file(link['href']):
            return link['href']

    # Video না পেলে sub-folders এ খোঁজো
    for link in links:
        if link['is_dir'] and link['text'].lower() not in SKIP_NAMES:
            video_url = find_video_in_folder(link['href'], depth + 1)
            if video_url:
                return video_url

    return None


def crawl_folder(
    url: str,
    category: str,
    depth: int = 0,
    stats: dict = None,
) -> list[dict]:
    """
    ══ মূল recursive function ══

    একটা folder crawl করে সব movies বের করে।
    Logic:
      - যদি folder এ directly video file থাকে → সেটাই movie, folder name = movie name
      - যদি folder এ sub-folders থাকে:
          * sub-folder এর নাম দিয়ে year check করো
          * যদি year আছে বা pattern movie-like → movie folder
          * নইলে → category/sub-category folder, recursively crawl করো
    """
    if stats is None:
        stats = {'total': 0, 'errors': 0}

    links = parse_index_page(url)
    time.sleep(REQUEST_DELAY)

    if not links:
        return []

    movies = []

    # এই folder এ কী আছে check করো
    video_files   = [l for l in links if is_video_file(l['href'])]
    sub_folders   = [l for l in links if l['is_dir'] and
                     l['text'].lower() not in SKIP_NAMES]

    # ── Case 1: এই folder এ সরাসরি video files আছে ──
    if video_files:
        # Folder name = movie name, parent folder = category
        folder_name = url_to_name(url)
        # সবচেয়ে বড় file টা নাও (best quality)
        best_video = video_files[0]['href']  # প্রথমটাই নাও

        # .mp4 prefer করো
        mp4_files = [v for v in video_files if v['href'].lower().endswith('.mp4')]
        if mp4_files:
            best_video = mp4_files[0]['href']

        movie_name = folder_name if folder_name else url_to_name(best_video)
        year = get_year(movie_name) or get_year(url_to_name(best_video))

        poster = ''
        if TMDB_API_KEY:
            poster = get_poster_url(movie_name, year)

        movies.append({
            'id':          movie_name,
            'name':        movie_name,
            'streamUrl':   best_video,
            'logo':        poster,
            'description': category,
        })
        stats['total'] += 1
        print(f"    🎬 {movie_name[:55]:<55} [{category}]")
        return movies

    # ── Case 2: Sub-folders আছে — movie folders নাকি category folders? ──
    for folder in sub_folders:
        folder_name = folder['text']
        folder_url  = folder['href']
        lower_name  = folder_name.lower()

        # Year pattern check — '2024', '2025' এরকম শুধু year নামের folder
        is_year_folder = bool(re.match(r'^\d{4}$', folder_name.strip()))

        # Movie-like folder check:
        # "Movie Name (2025)" বা "Movie.Name.2025" pattern
        has_year_in_name = bool(re.search(r'\(?20\d{2}\)?|\(?19\d{2}\)?', folder_name))
        looks_like_movie = has_year_in_name

        if is_year_folder:
            # Year folder → year কে category suffix হিসেবে রাখো, ভেতরে যাও
            sub_cat = category  # year folder এ category একই রাখো
            sub_movies = crawl_folder(folder_url, sub_cat, depth + 1, stats)
            movies.extend(sub_movies)

        elif looks_like_movie:
            # Movie folder — ভেতরে ঢুকে video খোঁজো
            print(f"    🎬 {folder_name[:55]:<55} [{category}]")
            video_url = find_video_in_folder(folder_url)
            if video_url:
                year = get_year(folder_name)
                poster = ''
                if TMDB_API_KEY:
                    poster = get_poster_url(folder_name, year)

                movies.append({
                    'id':          folder_name,
                    'name':        folder_name,
                    'streamUrl':   video_url,
                    'logo':        poster,
                    'description': category,
                })
                stats['total'] += 1
            else:
                print(f"    ⚠ No video found: {folder_name[:50]}")
                stats['errors'] += 1

        else:
            # এটা একটা sub-category folder
            # Parent folder নাম থেকে নতুন category নাম বানাও
            sub_cat_name = get_category_name(folder_name)

            # Category chain: "Hindi Dubbed" না বানিয়ে শুধু leaf category রাখো
            # (তোমার app এ description = category হিসেবে দেখায়)
            print(f"  📁 Sub-category: {sub_cat_name}")
            sub_movies = crawl_folder(folder_url, sub_cat_name, depth + 1, stats)
            movies.extend(sub_movies)

    return movies


# ══════════════════════════════════════════════════════════════════
# Main Crawl
# ══════════════════════════════════════════════════════════════════

def crawl_all(base_url: str) -> list[dict]:
    """Root থেকে সব categories crawl করে পুরো movie list বানায়।"""

    print(f"\n🚀 Crawling: {base_url}")
    print("=" * 65)

    all_movies = []
    stats = {'total': 0, 'errors': 0}

    root_links = parse_index_page(base_url)
    time.sleep(REQUEST_DELAY)

    # Root level folders = top-level categories
    top_cats = [l for l in root_links
                if l['is_dir'] and l['text'].lower() not in SKIP_NAMES]

    print(f"📂 Found {len(top_cats)} top categories:\n")
    for cat in top_cats:
        print(f"   • {cat['text']}")
    print()

    for cat in top_cats:
        cat_name = get_category_name(cat['text'])
        print(f"\n🎯 ══ {cat_name} ══")
        movies = crawl_folder(cat['href'], cat_name, depth=0, stats=stats)
        print(f"   ✅ {len(movies)} movies found")
        all_movies.extend(movies)

    # Deduplicate by streamUrl
    seen = set()
    unique = []
    for m in all_movies:
        key = m['streamUrl']
        if key not in seen:
            seen.add(key)
            unique.append(m)

    print(f"\n{'=' * 65}")
    print(f"✅ Total unique movies : {len(unique)}")
    print(f"⚠  Errors / skipped   : {stats['errors']}")

    return unique


# ══════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='FM FTP Movie Crawler')
    parser.add_argument('--base-url',  default=BASE_URL,   help='Root URL to crawl')
    parser.add_argument('--output',    default=OUTPUT_FILE, help='Output JSON file')
    parser.add_argument('--no-poster', action='store_true', help='Skip TMDB poster fetch')
    args = parser.parse_args()

    if args.no_poster:
        global TMDB_API_KEY
        TMDB_API_KEY = ''

    start = time.time()
    movies = crawl_all(args.base_url)

    # Save
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(movies, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    print(f"\n💾 Saved {len(movies)} movies → {args.output}")
    print(f"⏱  Time: {mins}m {secs}s")
    print(f"\n✅ Done! GitHub Actions এ push করো।")


if __name__ == '__main__':
    main()
