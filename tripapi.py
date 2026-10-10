"""TripAdvisor restaurant scraper built on the Oxylabs Web API.

No browser, no proxy: Oxylabs fetches each page and we parse the HTML.
Key comes from the OXYLABS_WEB_API_KEY environment variable (a GitHub secret in CI).
Resumes automatically from tripadvisor_output.csv (skips restaurants already saved).
"""
import html as htmllib
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

import pandas as pd
import requests

# ----------------------------- CONFIGURATION -----------------------------
API_URL = "https://webapi.oxylabs.io/v1/scrape"
OUTPUT_CSV = "tripadvisor_output.csv"
BASE_URL = "https://www.tripadvisor.com"
DEFAULT_TARGET_URL = "https://www.tripadvisor.com/Restaurants-g295424-Dubai_Emirate_of_Dubai.html"
COLUMNS = ["Restaurant Name", "Email", "URL"]
PAGE_SIZE = 30                      # TripAdvisor shows 30 restaurants per listing page


def env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def env_flag(name, default="0"):
    return (os.environ.get(name) or default).strip().lower() in ("1", "true", "yes", "y")


START_PAGE = max(1, env_int("START_PAGE", 1))             # skip ahead (e.g. 16 if pages 1-15 are already done)
MAX_LISTING_PAGES = env_int("MAX_LISTING_PAGES", 25)      # last listing page number to visit
MAX_RESTAURANTS = env_int("MAX_RESTAURANTS", 400)        # per run
MAX_RUNTIME_MIN = env_int("MAX_RUNTIME_MIN", 330)        # stop cleanly before GitHub's 6h job limit
CONCURRENCY = env_int("CONCURRENCY", 5)                  # parallel API requests
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 180)        # seconds; Oxylabs docs advise ~150+
RUN_JS_LISTING = env_flag("RUN_JS_LISTING", "1")         # listing pages worked with JS rendering
RUN_JS_DETAIL = env_flag("RUN_JS_DETAIL", "0")           # restaurant pages: try without JS first (cheaper)
MAX_CONSECUTIVE_FAILURES = env_int("MAX_CONSECUTIVE_FAILURES", 10)

START_TIME = time.time()
_counter_lock = threading.Lock()
API_REQUESTS = 0

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'<>\s?]+)", re.I)
JSON_EMAIL_RE = re.compile(r'"email"\s*:\s*"([^"]+)"', re.I)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
LINK_RE = re.compile(r"/Restaurant_Review-[^\"'#?<>\s\\]+\.html")

IGNORED_EMAIL_DOMAINS = ("tripadvisor.", "sentry.", "example.", "wixpress.", "google.", "facebook.", "instagram.")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


class AuthError(Exception):
    pass


def out_of_time():
    return (time.time() - START_TIME) > MAX_RUNTIME_MIN * 60


def restaurant_id(url):
    m = re.search(r"-d(\d+)-", str(url))
    return m.group(1) if m else ""


def url_key(url):
    """Stable key for a restaurant: its TripAdvisor ID (ignores www / http / path differences)."""
    return restaurant_id(url) or str(url).strip().lower()


def footprint(text):
    return re.sub(r"[^a-zA-Z0-9]", "", str(text)).lower().strip()


# ------------------------------- API ---------------------------------
def fetch(url, run_js, tries=3):
    """Return page HTML via the Oxylabs Web API, or '' on failure. Raises AuthError on 401."""
    global API_REQUESTS
    payload = {"url": url, "output": ["html"], "location": "US"}
    if run_js:
        payload["run_js"] = True
    headers = {"Authorization": f"Bearer {API_KEY}"}
    for attempt in range(1, tries + 1):
        try:
            with _counter_lock:
                API_REQUESTS += 1
            r = requests.post(API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"  [!] Request error ({type(e).__name__}); retry {attempt}/{tries}")
            time.sleep(3 * attempt)
            continue
        if r.status_code == 200:
            try:
                return (r.json().get("results") or [{}])[0].get("html") or ""
            except Exception:
                return ""
        if r.status_code == 401:
            raise AuthError("401 Unauthorized - check OXYLABS_WEB_API_KEY")
        if r.status_code == 429:
            time.sleep(5 * attempt)       # too many parallel requests: back off
            continue
        if r.status_code >= 500:
            time.sleep(3 * attempt)
            continue
        print(f"  [!] API returned HTTP {r.status_code}: {r.text[:200]}")
        return ""
    return ""


# ------------------------------ parsing ------------------------------
def is_valid_email(address):
    if not address or "@" not in address:
        return False
    lowered = address.lower()
    if lowered.endswith(IGNORED_EMAIL_SUFFIXES):
        return False
    domain = lowered.split("@")[-1]
    return not any(domain.startswith(bad) or bad in domain for bad in IGNORED_EMAIL_DOMAINS)


def collect_links(html):
    seen = {}
    for path in LINK_RE.findall(html or ""):
        seen.setdefault(BASE_URL + path, None)
    return list(seen)


def extract_name_from_url(url):
    match = re.search(r"-Reviews-([^-]+)", urlparse(url).path)
    return match.group(1).replace("_", " ").strip().lower() if match else ""


def extract_name(html, url=""):
    match = H1_RE.search(html or "")
    if match:
        text = re.sub(r"\s+", " ", htmllib.unescape(TAG_RE.sub(" ", match.group(1)))).strip()
        if text:
            return text
    match = TITLE_RE.search(html or "")
    if match:
        text = htmllib.unescape(match.group(1)).split(" - ")[0].strip()
        if text:
            return text
    return extract_name_from_url(url)


def extract_email(html):
    candidates = []
    for m in MAILTO_RE.findall(html or ""):
        candidates.append(unquote(m).strip())
    candidates += JSON_EMAIL_RE.findall(html or "")
    for c in candidates:
        c = c.replace("\\u0040", "@").strip()
        if is_valid_email(c):
            return c
    text = TAG_RE.sub(" ", SCRIPT_STYLE_RE.sub(" ", html or ""))
    for c in EMAIL_RE.findall(text):
        if is_valid_email(c):
            return c
    return ""


def listing_page_url(base, idx):
    """Page 1 is the base URL; later pages insert -oa<offset>- after the geo id."""
    if idx == 0:
        return base
    offset = idx * PAGE_SIZE
    if re.search(r"-oa\d+-", base):
        return re.sub(r"-oa\d+-", f"-oa{offset}-", base, count=1)
    return re.sub(r"(Restaurants-g\d+)-", rf"\1-oa{offset}-", base, count=1)


def scrape_restaurant(url):
    html = fetch(url, RUN_JS_DETAIL)
    if (not html or not H1_RE.search(html)) and not RUN_JS_DETAIL:
        html = fetch(url, True)           # fallback: render JavaScript (costs more)
    if not html or not H1_RE.search(html):
        return {"ok": False, "URL": url}
    return {"ok": True, "Restaurant Name": extract_name(html, url), "Email": extract_email(html), "URL": url}


# ------------------------------ history ------------------------------
def load_history():
    visited_urls, visited_names = set(), set()
    df = pd.DataFrame(columns=COLUMNS)
    if os.path.exists(OUTPUT_CSV) and os.path.getsize(OUTPUT_CSV) > 4:
        try:
            existing = pd.read_csv(OUTPUT_CSV)
            url_cols = [c for c in existing.columns if c.strip().upper() == "URL"]
            if url_cols:
                visited_urls = {url_key(u) for u in existing[url_cols[0]].dropna()}
            name_cols = [c for c in existing.columns if "NAME" in c.strip().upper() or "RESTAURANT" in c.strip().upper()]
            if name_cols:
                visited_names = {footprint(n) for n in existing[name_cols[0]].dropna() if footprint(n)}
            print(f"[*] Memory Database Active: Loaded {max(len(visited_urls), len(visited_names))} old rows.")
            df = existing
        except Exception as e:
            print(f"[!] History initialization error: {e}")
    return df, visited_urls, visited_names


# -------------------------------- main -------------------------------
def run(target_url):
    if not API_KEY:
        print("[!] OXYLABS_WEB_API_KEY is not set.")
        sys.exit(1)

    df, visited_urls, visited_names = load_history()
    scraped = 0
    consecutive_failures = 0
    stop = False
    seen_listing_links = set()
    repeat_pages = 0

    for page_idx in range(START_PAGE - 1, MAX_LISTING_PAGES):
        if stop or scraped >= MAX_RESTAURANTS or out_of_time():
            break
        page_url = listing_page_url(target_url, page_idx)
        print(f"\n--- Listing page {page_idx + 1}: {page_url}")
        try:
            html = fetch(page_url, RUN_JS_LISTING)
        except AuthError as e:
            print(f"[!] {e}")
            sys.exit(1)
        links = collect_links(html)
        if not links:
            print("[*] No restaurant links on this listing page (end of results, or blocked). Stopping.")
            break

        # If TripAdvisor stops paginating it may serve the same restaurants again: detect and stop.
        if not [u for u in links if u not in seen_listing_links]:
            repeat_pages += 1
            if repeat_pages >= 2:
                print("[*] Listing pages are repeating earlier results (end of pagination). Stopping.")
                break
        else:
            repeat_pages = 0
        seen_listing_links.update(links)

        new_links = []
        for u in links:
            if url_key(u) in visited_urls:
                continue
            fp = footprint(extract_name_from_url(u))
            if fp and fp in visited_names:
                continue
            new_links.append(u)
        print(f"Found {len(links)} links on page, {len(new_links)} are brand new entries.")

        batch = new_links[: max(0, MAX_RESTAURANTS - scraped)]
        if not batch:
            continue

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(scrape_restaurant, u): u for u in batch}
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    row = fut.result()
                except AuthError as e:
                    print(f"[!] {e}")
                    stop = True
                    for f in futures:
                        f.cancel()
                    break
                except Exception as e:
                    print(f"  [!] Error on {url}: {type(e).__name__}")
                    row = {"ok": False}

                if not row.get("ok"):
                    consecutive_failures += 1
                    print(f"  [!] Could not read page ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {url}")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print("[!] Too many failures in a row. Stopping; progress is saved.")
                        stop = True
                        for f in futures:
                            f.cancel()
                        break
                    continue
                consecutive_failures = 0

                fp = footprint(row["Restaurant Name"])
                visited_urls.add(url_key(url))
                if fp and fp in visited_names:
                    continue
                if fp:
                    visited_names.add(fp)
                scraped += 1

                new_row = {"Restaurant Name": row["Restaurant Name"], "Email": row["Email"], "URL": row["URL"]}
                for col in COLUMNS:
                    if col not in df.columns:
                        df[col] = None
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
                print(f"  [Session: {scraped} | Total: {len(df)}] {row['Restaurant Name']} -> "
                      f"{'email found' if row['Email'] else 'no email found'}")

                if out_of_time():
                    print(f"[*] Runtime budget of {MAX_RUNTIME_MIN} min reached. Stopping cleanly.")
                    stop = True
                    for f in futures:
                        f.cancel()
                    break

    print(f"\n[*] Done. Scraped {scraped} new restaurants. Total rows: {len(df)}. "
          f"API requests this run: {API_REQUESTS}.")
    return df


API_KEY = (os.environ.get("OXYLABS_WEB_API_KEY") or "").strip().strip('"').strip("'")

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET_URL") or DEFAULT_TARGET_URL
    run(url)
