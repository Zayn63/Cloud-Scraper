import random
import re
import sys
import os
from urllib.parse import unquote, urljoin, urlparse
import pandas as pd
from playwright.sync_api import sync_playwright

# ----------------------------- CONFIGURATION -----------------------------
OUTPUT_CSV = "tripadvisor_output.csv"
MAX_LISTING_PAGES = 25
MAX_RESTAURANTS = 400
NAV_TIMEOUT_MS = 45_000

# Used when no URL is passed as an argument or TARGET_URL env var. Change as needed.
DEFAULT_TARGET_URL = "https://www.tripadvisor.com/Restaurants-g295424-Dubai_Emirate_of_Dubai.html"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
IGNORED_EMAIL_DOMAINS = ("tripadvisor.", "sentry.", "example.", "wixpress.", "google.", "facebook.", "instagram.")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
BASE_URL = "https://tripadvisor.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
COLUMNS = ["Restaurant Name", "Email", "URL"]


def smooth_scroll(page, step=350, max_steps=40):
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
        last_height = 0
        stagnant_rounds = 0
        for _ in range(max_steps):
            page.mouse.wheel(0, step + random.randint(-80, 120))
            page.wait_for_timeout(random.randint(400, 900))
            height = page.evaluate("document.documentElement.scrollHeight")
            position = page.evaluate("window.scrollY + window.innerHeight")
            if position >= height - 5:
                stagnant_rounds = stagnant_rounds + 1 if height == last_height else 0
                if stagnant_rounds >= 2:
                    break
            last_height = height
        page.wait_for_timeout(random.randint(1500, 3000))
        page.mouse.wheel(0, -random.randint(300, 700))
    except Exception:
        pass


def dismiss_popups(page):
    for selector in ["#onetrust-accept-btn-handler", "button:has-text('Accept all')", "button[aria-label='Close']"]:
        try:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible():
                button.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            continue


def wait_if_challenged(page):
    try:
        html = page.content().lower()
        if any(m in html for m in ["captcha", "datadome", "verify you are human"]):
            print(f"[!] Security challenge visible on screen. Title: '{page.title()}'")
    except Exception:
        pass


def clean_mailto(href):
    if not href or "mailto:" not in href:
        return ""
    try:
        after = href.split("mailto:", 1)[1]          # FIX: was indexing a list incorrectly
        return unquote(after.split("?", 1)[0]).strip()
    except Exception:
        return ""


def is_valid_email(address):
    if not address or "@" not in address:
        return False
    lowered = address.lower()
    if lowered.endswith(IGNORED_EMAIL_SUFFIXES):
        return False
    domain = lowered.split("@")[-1]
    return not any(domain.startswith(bad) or bad in domain for bad in IGNORED_EMAIL_DOMAINS)


def collect_listing_links(page):
    anchors = page.query_selector_all("a[href*='/Restaurant_Review-']")
    links = []
    seen = set()
    for anchor in anchors:
        try:
            href = anchor.get_attribute("href")
            if not href:
                continue
            absolute = urljoin(BASE_URL, href)
            parsed = urlparse(absolute)
            canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if canonical not in seen:
                seen.add(canonical)
                links.append(canonical)
        except Exception:
            continue
    return links


def go_to_next_listing_page(page):
    for selector in ["a[aria-label='Next page']", "a.nav.next", "a:has-text('Next')"]:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            if locator.get_attribute("aria-disabled") == "true":
                return False
            locator.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(1500)
            locator.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded")
            return True
        except Exception:
            continue
    return False


def extract_restaurant_name_from_url(url):
    # URLs look like .../Restaurant_Review-g295424-d123456-Reviews-Some_Place-Dubai_Emirate_of_Dubai.html
    # FIX: the old code looked for parts starting with "Reviews_", which never matches.
    try:
        match = re.search(r"-Reviews-([^-]+)", urlparse(url).path)
        if match:
            return match.group(1).replace("_", " ").strip().lower()
    except Exception:
        pass
    return ""


def footprint(text):
    return re.sub(r"[^a-zA-Z0-9]", "", str(text)).lower().strip()


def extract_restaurant(detail_page, url):
    result = {"Restaurant Name": "", "Email": "", "URL": url}
    try:
        detail_page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        detail_page.wait_for_timeout(3000)
        wait_if_challenged(detail_page)
        dismiss_popups(detail_page)
        smooth_scroll(detail_page)
        for selector in ("h1[data-test-target='top-info-header']", "h1"):
            heading = detail_page.locator(selector).first
            if heading.count() > 0:
                text = heading.inner_text(timeout=3000).strip()
                if text:
                    result["Restaurant Name"] = text
                    break
    except Exception:
        return result

    if not result["Restaurant Name"]:
        try:
            result["Restaurant Name"] = detail_page.title().split(" - ")[0].strip()   # FIX: was .split(...).strip()
        except Exception:
            result["Restaurant Name"] = "Unknown Restaurant"

    try:
        body_text = detail_page.locator("body").inner_text()
        for email_addr in EMAIL_RE.findall(body_text):
            if is_valid_email(email_addr):
                result["Email"] = email_addr
                return result
    except Exception:
        pass

    try:
        for anchor in detail_page.query_selector_all("a[href^='mailto:']"):
            href = anchor.get_attribute("href")
            if href:
                address = clean_mailto(href)
                if address and is_valid_email(address):
                    result["Email"] = address
                    return result
    except Exception:
        pass
    return result


def load_history():
    visited_urls = set()
    visited_names = set()
    df = pd.DataFrame(columns=COLUMNS)
    if os.path.exists(OUTPUT_CSV) and os.path.getsize(OUTPUT_CSV) > 4:
        try:
            existing_df = pd.read_csv(OUTPUT_CSV)
            url_cols = [c for c in existing_df.columns if c.strip().upper() == "URL"]
            if url_cols:   # FIX: indexing with a list returned a DataFrame; use the column name
                for old_url in existing_df[url_cols[0]].dropna():
                    visited_urls.add(str(old_url).strip().lower())
            name_cols = [c for c in existing_df.columns if "NAME" in c.strip().upper() or "RESTAURANT" in c.strip().upper()]
            if name_cols:
                for old_name in existing_df[name_cols[0]].dropna():
                    fp = footprint(old_name)
                    if fp:
                        visited_names.add(fp)
            print(f"[*] Memory Database Active: Loaded {max(len(visited_urls), len(visited_names))} old rows.")
            df = existing_df
        except Exception as e:
            print(f"[!] History initialization error: {e}")
    return df, visited_urls, visited_names


def run(target_url):
    df, visited_urls, visited_names = load_history()
    current_session_scraped = 0

    with sync_playwright() as p:
        print("[*] Launching Chromium Cloud Core Engine...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 900}, locale="en-US")
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        listing_page = context.new_page()

        print(f"[*] Connecting to Target directory URL: {target_url}")
        listing_page.goto(target_url, wait_until="domcontentloaded")
        listing_page.wait_for_timeout(5000)
        dismiss_popups(listing_page)
        print(f"[*] Landed Page Title: '{listing_page.title()}'")
        if "access denied" in listing_page.title().lower():
            print("[!] Cloud IP blocked by firewall. Exiting safely.")
            browser.close()
            return df

        for page_number in range(1, MAX_LISTING_PAGES + 1):
            print(f"\n--- Processing Listing Page {page_number} ---")
            smooth_scroll(listing_page)
            links = collect_listing_links(listing_page)

            new_links = []
            for u in links:
                if u.lower().strip() in visited_urls:
                    continue
                url_fp = footprint(extract_restaurant_name_from_url(u))
                if url_fp and url_fp in visited_names:
                    continue
                new_links.append(u)

            print(f"Found {len(links)} links on page, {len(new_links)} are brand new entries.")

            for url in new_links:
                if current_session_scraped >= MAX_RESTAURANTS:
                    break
                detail_page = context.new_page()
                try:
                    row = extract_restaurant(detail_page, url)
                except Exception:
                    continue
                finally:
                    detail_page.close()

                scraped_fp = footprint(row["Restaurant Name"])
                if scraped_fp and scraped_fp in visited_names:
                    continue
                visited_urls.add(url.lower().strip())
                if scraped_fp:
                    visited_names.add(scraped_fp)
                current_session_scraped += 1

                new_row = {"Restaurant Name": row["Restaurant Name"], "Email": row["Email"], "URL": row["URL"]}
                for col in COLUMNS:
                    if col not in df.columns:
                        df[col] = None
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
                print(f"  [Session: {current_session_scraped} | Total: {len(df)}] {row['Restaurant Name']} -> {row['Email'] or 'no email found'}")
                listing_page.wait_for_timeout(random.randint(2000, 4000))

            if current_session_scraped >= MAX_RESTAURANTS:
                break

            # FIX: this block was missing its body (the cause of the IndentationError)
            if page_number < MAX_LISTING_PAGES:
                if not go_to_next_listing_page(listing_page):
                    print("[*] No further listing pages. Stopping.")
                    break
                listing_page.wait_for_timeout(random.randint(3000, 5000))
                dismiss_popups(listing_page)

        browser.close()

    print(f"\n[*] Done. Scraped {current_session_scraped} new restaurants this session. Total rows: {len(df)}")
    return df


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET_URL", DEFAULT_TARGET_URL)
    run(url)
