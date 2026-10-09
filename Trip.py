import random
import re
import sys
import os
import time
from urllib.parse import unquote, urljoin, urlparse
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ----------------------------- CONFIGURATION -----------------------------
OUTPUT_CSV = "tripadvisor_output.csv"

# CAPACITY SETTINGS: Crawls 60 pages to hunt down valid business emails
MAX_LISTING_PAGES = 60          
MAX_RESTAURANTS = 450           

NAV_TIMEOUT_MS = 45_000

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
IGNORED_EMAIL_DOMAINS = ("tripadvisor.", "sentry.", "example.", "wixpress.", "google.", "facebook.", "instagram.")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

BASE_URL = "https://tripadvisor.com"

def smooth_scroll(page, step: int = 350, max_steps: int = 40) -> None:
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
    page.wait_for_timeout(random.randint(500, 1200))

def dismiss_popups(page) -> None:
    candidates = ["#onetrust-accept-btn-handler", "button:has-text('Accept all')", "button:has-text('I Accept')", "button[aria-label='Close']"]
    for selector in candidates:
        try:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible():
                button.click(timeout=2_000)
                page.wait_for_timeout(random.randint(600, 1200))
        except Exception:
            continue

def wait_if_challenged(page) -> None:
    try:
        html = page.content().lower()
    except Exception:
        return
    markers = ("captcha", "datadome", "verify you are human", "access denied")
    if any(m in html for m in markers) and "restaurant" not in page.title().lower():
        print("\n[!] A verification challenge appears to be showing.")
        print("    Solve it manually in the browser window, then press Enter here.")
        input("    Press Enter to continue... ")
        page.wait_for_timeout(2000)

def clean_mailto(href: str) -> str:
    if not href or not isinstance(href, str) or "mailto:" not in href:
        return ""
    try:
        parts = href.split("mailto:", 1)
        if len(parts) > 1:
            email_part = parts[1].split("?", 1)
            return unquote(email_part[0]).strip()
    except Exception:
        pass
    return ""

def is_valid_email(address: str) -> bool:
    if not address or "@" not in address:
        return False
    lowered = address.lower()
    if lowered.endswith(IGNORED_EMAIL_SUFFIXES):
        return False
    domain = lowered.split("@")[-1]
    return not any(domain.startswith(bad) or bad in domain for bad in IGNORED_EMAIL_DOMAINS)

def collect_listing_links(page) -> list[str]:
    anchors = page.query_selector_all("a[href*='/Restaurant_Review-']")
    links = []
    seen = set()
    for anchor in anchors:
        href = anchor.get_attribute("href")
        if not href:
            continue
        absolute = urljoin(BASE_URL, href)
        parsed = urlparse(absolute)
        canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if canonical in seen:
            continue
        seen.add(canonical)
        links.append(canonical)
    return links

def go_to_next_listing_page(page) -> bool:
    next_selectors = ["a[aria-label='Next page']", "a.nav.next", "a[data-smoothing='true']:has-text('Next')", "a:has-text('Next')"]
    for selector in next_selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            disabled = locator.get_attribute("aria-disabled")
            if disabled == "true":
                return False
            locator.scroll_into_view_if_needed(timeout=3_000)
            page.wait_for_timeout(random.randint(1500, 3000))
            locator.click(timeout=5_000)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(random.randint(3500, 7000))
            return True
        except Exception:
            continue
    return False

def extract_restaurant(detail_page, url: str) -> dict:
    result = {"Restaurant Name": "", "Email": "", "URL": url}
    try:
        detail_page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout:
        print(f"    timeout loading {url}")
        return result

    detail_page.wait_for_timeout(random.randint(3500, 7000))
    wait_if_challenged(detail_page)
    dismiss_popups(detail_page)
    smooth_scroll(detail_page)

    for selector in ("h1[data-test-target='top-info-header']", "h1"):
        heading = detail_page.locator(selector).first
        try:
            if heading.count() > 0:
                text = heading.inner_text(timeout=3_000).strip()
                if text:
                    result["Restaurant Name"] = text
                    break
        except Exception:
            continue

    if not result["Restaurant Name"]:
        try:
            title = detail_page.title()
            result["Restaurant Name"] = title.split(" - ")[0].split(",")[0].strip()
        except Exception:
            result["Restaurant Name"] = "Unknown Restaurant"

    try:
        body_text = detail_page.locator("body").inner_text()
        found_emails = EMAIL_RE.findall(body_text)
        for email_addr in found_emails:
            if is_valid_email(email_addr):
                result["Email"] = email_addr
                return result
    except Exception:
        pass

    try:
        mailto_anchors = detail_page.query_selector_all("a[href^='mailto:']")
        for anchor in mailto_anchors:
            href = anchor.get_attribute("href")
            if href:
                address = clean_mailto(href)
                if address and is_valid_email(address):
                    result["Email"] = address
                    return result
    except Exception:
        pass

    return result

def run(target_url: str) -> pd.DataFrame:
    visited = set()
    
    # ANTI-DUPLICATION ENGINE: Automatically reads past logs to protect links
    if os.path.exists(OUTPUT_CSV):
        try:
            existing_df = pd.read_csv(OUTPUT_CSV)
            if "URL" in existing_df.columns:
                for old_url in existing_df["URL"].dropna():
                    visited.add(str(old_url).strip())
            print(f"[*] Loaded existing database. Found {len(visited)} historical links to protect against duplicates.")
            df = existing_df
        except Exception as e:
            print(f"[!] Warning reading historical CSV file, starting fresh: {e}")
            df = pd.DataFrame(columns=["Restaurant Name", "Email", "URL"])
    else:
        df = pd.DataFrame(columns=["Restaurant Name", "Email", "URL"])

    current_session_scraped = 0

    with sync_playwright() as p:
        print("[*] Starting automation engine...")
        
        # HEADLESS=TRUE: Configured for silent GitHub Action runner background layers
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        context = browser.new_context(
            viewport={"width": 1366, "height": 850},
            locale="en-US"
        )
            
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        listing_page = context.new_page()

        print(f"[*] Navigating to listing target URL: {target_url}")
        listing_page.goto(target_url, wait_until="domcontentloaded")
        listing_page.wait_for_timeout(5000)
        dismiss_popups(listing_page)

        for page_number in range(1, MAX_LISTING_PAGES + 1):
            print(f"\n--- Listing page {page_number} ---")
            smooth_scroll(listing_page)

            links = collect_listing_links(listing_page)
            new_links = [u for u in links if u not in visited]
            print(f"Found {len(links)} links on page, {len(new_links)} are brand new entries.")

            for url in new_links:
                if current_session_scraped >= MAX_RESTAURANTS:
                    break
                
                visited.add(url)
                current_session_scraped += 1

                detail_page = context.new_page()
                try:
                    row = extract_restaurant(detail_page, url)
                except Exception as exc:
                    print(f"    error on {url}: {exc}")
                    row = {"Restaurant Name": "", "Email": "", "URL": url}
                finally:
                    detail_page.close()

                # Appends new logs cleanly to the end of your growing CSV database
                df.loc[len(df)] = [row["Restaurant Name"], row["Email"], row["URL"]]
                df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
                print(f"  [Session: {current_session_scraped} | Total: {len(df)}] {row['Restaurant Name'] or '(no name)'} -> {row['Email'] or 'no email found'}")
                
                listing_page.wait_for_timeout(random.randint(3500, 7000))

            if current_session_scraped >= MAX_RESTAURANTS:
                print(f"Reached current daily execution cap of {MAX_RESTAURANTS} new entries.")
                break

            if page_number < MAX_LISTING_PAGES:
