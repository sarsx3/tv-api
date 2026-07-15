#!/usr/bin/env python3
"""
FM FTP Movie Crawler — Incremental Smart Version
==================================================
কাজের ধরন:
  1. movies_fmftp.json লোড করো (আগের সব মুভি)
  2. fmftp.net crawl করো — শুধু নতুন মুভি খোঁজো
  3. নতুন মুভি পেলে TMDB থেকে poster আনো
  4. নতুন মুভি লিস্টের শুরুতে যোগ করো
  5. Save করো

পুরনো মুভিতে কোনো হাত দেওয়া হয় না।
"""

import argparse
import json
import os
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote, quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ══════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════

BASE_URL        = "https://fmftp.net/data/disk-1/movies/"
OUTPUT_FILE     = "movies_fmftp.json"
REQUEST_DELAY   = 0.3
REQUEST_TIMEOUT = 20
MAX_RETRIES     = 3

TMDB_API_TOKEN = os.environ.get("TMDB_API_TOKEN",
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI2OTQ0YzRmOGM2OTAzMjUzZGQ3ODk2M2YyN2UyODkwZSIsIm5iZiI6MTc4NDExNTA0OS42NTQsInN1YiI6IjZhNTc2ZjY5YzdhNmJlMzQxMmE3YmZiYSIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.IWpeUu3ZcJg0-gewjhtRMBq_CoJE2myi6NLzYXtjnlI"
)

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.m4v',
              '.webm', '.ts', '.flv', '.wmv', '.mpg', '.mpeg'}
SKIP_NAMES = {'..', '.', '__pycache__', '.git',
              'thumbnail', 'thumbs', 'cover', 'artwork', 'subs', 'subtitles'}

CATEGORY_MAP = {
    'bollywood':       'Bollywood',
    'hollywood':       'Hollywood',
    'hindidub':        'Hindi Dubbed',
    'hindi dub':       'Hindi Dubbed',
    'hindi dubbed':    'Hindi Dubbed',
    'indianbangla':    'Indian Bangla',
    'indian bangla':   'Indian Bangla',
    'bangla':          'Bangla',
    'korean':          'Korean',
    'turkish':         'Turkish',
    'thai':            'Thai',
    'animation':       'Animation',
    'horror':          'Horror',
    'pakisthani':      'Pakistani',
    'pakistani':       'Pakistani',
    'foreigner movie': 'Foreign',
    'foreign':         'Foreign',
    'fm':              'FM Originals',
    'south':           'South Indian',
    'tamil':           'Tamil',
    'telugu':          'Telugu',
    'malayalam':       'Malayalam',
    'action':          'Action',
    'comedy':          'Comedy',
    'drama':           'Drama',
    'thriller':        'Thriller',
    'sci-fi':          'Sci-Fi',
    'romance':         'Romance',
    'documentary':     'Documentary',
    'kids':            'Kids',
    'english':         'English',
    'chinese':         'Chinese',
    'japanese':        'Japanese',
    'nepali':          'Nepali',
    'urdu':            'Urdu',
}


# ══════════════════════════════════════════════════════════════════
# Existing Data Load
# ══════════════════════════════════════════════════════════════════

def load_existing(path: str) -> tuple[list, set]:
    """
    আগের movies_fmftp.json লোড করো।
    Return: (movie_list, set_of_known_streamUrls)
    """
    if not os.path.exists(path):
        print("📂 No existing JSON — full crawl mode")
        return [], set()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        known = {m['streamUrl'] for m in data if 'streamUrl' in m}
        print(f"📂 Existing movies loaded: {len(data)} | Known URLs: {len(known)}")
        return data, known
    except Exception as e:
        print(f"⚠ Could not load existing JSON: {e}")
        return [], set()


# ══════════════════════════════════════════════════════════════════
# HTML Parser
# ══════════════════════════════════════════════════════════════════

class IndexPageParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links    = []
        self._href    = None
        self._text    = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href and not href.startswith('?') and href not in ('/', '../', '..'):
                full = urljoin(self.base_url, href)
                if 'fmftp.net' in full:
                    self._href = full
                    self._text = ''

    def handle_endtag(self, tag):
        if tag == 'a' and self._href:
            text = self._text.strip().rstrip('/')
            if text and text not in ('Parent Directory', '..', '../', '.'):
                self.links.append({
                    'href':   self._href,
                    'text':   text,
                    'is_dir': self._href.endswith('/'),
                })
            self._href = None

    def handle_data(self, data):
        if self._href:
            self._text += data


# ══════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════

def fetch_html(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,*/*',
    }
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return r.read().decode('utf-8', errors='replace')
        except HTTPError as e:
            if e.code == 403:
                return None
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                return None
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                return None
    return None


def parse_page(url):
    html = fetch_html(url)
    if not html:
        return []
    p = IndexPageParser(url)
    p.feed(html)
    return p.links


# ══════════════════════════════════════════════════════════════════
# TMDB Poster
# ══════════════════════════════════════════════════════════════════

_tmdb_cache = {}   # session cache — একই নামে দুইবার call না হয়

def get_poster(movie_name, year=''):
    if not TMDB_API_TOKEN:
        return ''

    clean = re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*', '', movie_name).strip()
    clean = re.sub(r'\s*\[\w+\]\s*', '', clean).strip()
    key   = f"{clean.lower()}_{year}"

    if key in _tmdb_cache:
        return _tmdb_cache[key]

    try:
        q      = quote(clean)
        yp     = f"&primary_release_year={year}" if year else ''
        url    = (f"https://api.themoviedb.org/3/search/movie"
                  f"?query={q}{yp}&include_adult=false&language=en-US&page=1")
        req    = Request(url, headers={
            'Authorization': f'Bearer {TMDB_API_TOKEN}',
            'Accept':        'application/json',
        })
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        poster = ''
        res    = data.get('results', [])
        if res:
            path = res[0].get('poster_path', '')
            if path:
                poster = f"https://image.tmdb.org/t/p/w500{path}"

        _tmdb_cache[key] = poster
        return poster
    except Exception:
        _tmdb_cache[key] = ''
        return ''


# ══════════════════════════════════════════════════════════════════
# Utility
# ══════════════════════════════════════════════════════════════════

def is_video(href):
    return any(href.split('?')[0].lower().endswith(e) for e in VIDEO_EXTS)

def get_year(name):
    m = re.search(r'[\(\[](\d{4})[\)\]]', name)
    return m.group(1) if m else ''

def cat_name(folder):
    low = folder.lower().strip()
    return CATEGORY_MAP.get(low, folder.replace('-',' ').replace('_',' ').title())

def clean_name(name):
    for e in VIDEO_EXTS:
        if name.lower().endswith(e):
            name = name[:-len(e)]
            break
    if '_' in name and ' ' not in name:
        name = name.replace('_', ' ')
    if '.' in name and ' ' not in name and name.count('.') > 2:
        name = name.replace('.', ' ')
    return name.strip()

def url_name(url):
    return clean_name(unquote(url.rstrip('/').split('/')[-1]))


# ══════════════════════════════════════════════════════════════════
# Recursive video finder
# ══════════════════════════════════════════════════════════════════

def find_video(folder_url, depth=0):
    if depth > 5:
        return None
    links = parse_page(folder_url)
    time.sleep(REQUEST_DELAY)
    for l in links:
        if is_video(l['href']):
            return l['href']
    for l in links:
        if l['is_dir'] and l['text'].lower() not in SKIP_NAMES:
            v = find_video(l['href'], depth+1)
            if v:
                return v
    return None


# ══════════════════════════════════════════════════════════════════
# Incremental Crawl — মূল function
# ══════════════════════════════════════════════════════════════════

def crawl_folder(url, category, known_urls, depth=0, stats=None):
    """
    known_urls = আগে থেকে থাকা সব streamUrl এর set।
    কোনো url এই set এ থাকলে সেটা skip করো।
    """
    if stats is None:
        stats = {'new': 0, 'skipped': 0, 'errors': 0}

    links = parse_page(url)
    time.sleep(REQUEST_DELAY)
    if not links:
        return []

    new_movies  = []
    video_files = [l for l in links if is_video(l['href'])]
    sub_folders = [l for l in links if l['is_dir']
                   and l['text'].lower() not in SKIP_NAMES]

    # ── Case 1: সরাসরি video file ──
    if video_files:
        # MP4 prefer
        best = next((v['href'] for v in video_files
                     if v['href'].lower().endswith('.mp4')),
                    video_files[0]['href'])

        # ★ ইতিমধ্যে আছে? → skip
        if best in known_urls:
            stats['skipped'] += 1
            return []

        # নতুন মুভি!
        name   = url_name(url) or url_name(best)
        year   = get_year(name)
        poster = get_poster(name, year)

        entry = {
            'id':          name,
            'name':        name,
            'streamUrl':   best,
            'logo':        poster,
            'description': category,
        }
        new_movies.append(entry)
        known_urls.add(best)   # এখন known এ যোগ করো
        stats['new'] += 1
        print(f"  ✨ NEW: {name[:55]:<55} [{category}]"
              + (" 🖼" if poster else ""))
        return new_movies

    # ── Case 2: Sub-folders ──
    for folder in sub_folders:
        fname   = folder['text']
        furl    = folder['href']
        is_year = bool(re.match(r'^\d{4}$', fname.strip()))
        has_yr  = bool(re.search(r'\(?(?:19|20)\d{2}\)?', fname))

        if is_year:
            # Year folder → ভেতরে যাও, category একই রাখো
            result = crawl_folder(furl, category, known_urls, depth+1, stats)
            new_movies.extend(result)

        elif has_yr:
            # Movie folder
            video_url = find_video(furl)
            if not video_url:
                stats['errors'] += 1
                continue

            # ★ ইতিমধ্যে আছে? → skip
            if video_url in known_urls:
                stats['skipped'] += 1
                continue

            # নতুন মুভি!
            year   = get_year(fname)
            poster = get_poster(fname, year)
            entry  = {
                'id':          fname,
                'name':        fname,
                'streamUrl':   video_url,
                'logo':        poster,
                'description': category,
            }
            new_movies.append(entry)
            known_urls.add(video_url)
            stats['new'] += 1
            print(f"  ✨ NEW: {fname[:55]:<55} [{category}]"
                  + (" 🖼" if poster else ""))

        else:
            # Sub-category folder
            sub_cat = cat_name(fname)
            result  = crawl_folder(furl, sub_cat, known_urls, depth+1, stats)
            new_movies.extend(result)

    return new_movies


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url',  default=BASE_URL)
    parser.add_argument('--output',    default=OUTPUT_FILE)
    parser.add_argument('--no-poster', action='store_true')
    parser.add_argument('--full',      action='store_true',
                        help='Full crawl — আগেরগুলো ignore করে সব নতুন করে আনো')
    args = parser.parse_args()

    if args.no_poster:
        global TMDB_API_TOKEN
        TMDB_API_TOKEN = ''

    # ── আগের data load করো ──
    if args.full:
        existing, known_urls = [], set()
        print("🔄 Full crawl mode — ignoring existing data")
    else:
        existing, known_urls = load_existing(args.output)

    start = time.time()

    print(f"\n🚀 Incremental crawl: {args.base_url}")
    print("=" * 65)

    stats = {'new': 0, 'skipped': 0, 'errors': 0}

    # Root categories
    root_links = parse_page(args.base_url)
    time.sleep(REQUEST_DELAY)
    top_cats   = [l for l in root_links
                  if l['is_dir'] and l['text'].lower() not in SKIP_NAMES]

    print(f"📂 {len(top_cats)} categories found\n")

    all_new = []
    for c in top_cats:
        cname = cat_name(c['text'])
        print(f"\n🎯 ══ {cname} ══")
        new = crawl_folder(c['href'], cname, known_urls, depth=0, stats=stats)
        all_new.extend(new)
        print(f"   ➕ {len(new)} new | ⏭ {stats['skipped']} skipped so far")

    print(f"\n{'=' * 65}")
    print(f"✨ New movies found  : {stats['new']}")
    print(f"⏭  Skipped (existing): {stats['skipped']}")
    print(f"⚠  Errors           : {stats['errors']}")

    if not all_new:
        print("\nℹ️ কোনো নতুন মুভি নেই — JSON আপডেট করার দরকার নেই।")
        # কিছু না পেলেও existing ঠিকঠাক থাকবে
        if not existing:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump([], f)
        return

    # ★ নতুন মুভি লিস্টের শুরুতে যোগ করো
    final = all_new + existing

    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    elapsed    = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    print(f"\n💾 Saved {len(final)} total movies → {args.output}")
    print(f"   ({stats['new']} new + {len(existing)} existing)")
    print(f"⏱  Time: {mins}m {secs}s")
    print(f"\n✅ Done!")


if __name__ == '__main__':
    main()
