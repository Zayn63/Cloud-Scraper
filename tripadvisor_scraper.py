import asyncio
import random
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

# ----------------------------- CONFIGURATION -----------------------------
# Put pyt_name_email.csv in the same repository as this script.
MASTER_CSV = Path("pyt_name_email.csv")
OUTPUT_CSV = MASTER_CSV

MAX_LISTING_PAGES = 60
MAX_NEW_RESTAURANTS = 450  # Counts NEW restaurants only, not skipped ones.

DELAY_SHORT = (1.5, 3.0)
DELAY_LONG = (3.5, 7.0)
NAV_TIMEOUT_MS = 45_000

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IGNORED_EMAIL_DOMAINS = (
    "tripadvisor.", "sentry.", "example.", "wixpress.",
    "google.", "facebook.", "instagram."
)
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
BASE_URL = "https://www.tripadvisor.com"

COLUMNS = ["Restaurant Name", "Email", "URL"]


def normalize_name(name: str) -> str:
    """Case-insensitive comparison with repeated/outer whitespace removed."""
    return " ".join(str(name or "").casefold().split())


def normalize_url(url: str) -> str:
    """Ignore query strings and fragments when comparing listing URLs."""
    if not url:
        return ""
    parsed = urlparse(urljoin(BASE_URL, str(url).strip()))
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


async def human_delay(low: float, high: float) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def dismiss_popups(page) -> None:
    # Only dismiss ordinary consent/close dialogs; do not attempt to bypass challenges.
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('I Accept')",
        "button[aria-label='Close']",
    ):
        try:
            button = page.locator(selector).first
            if await button.count() and await button.is_visible():
                await button.click(timeout=2_000)
                await human_delay(0.4, 0.8)
        except Exception:
            pass


async def page_has_access_challenge(page) -> bool:
    try:
        html = (await page.content()).lower()
        title = (await page.title()).lower()
    except Exception:
        return False
    markers = ("captcha", "datadome", "verify you are human", "access denied")
    return any(marker in html for marker in markers) and "restaurant" not in title


def clean_mailto(href: str) -> str:
    try:
        if "mailto:" in href.lower():
            address = href.split("mailto:", 1)[1].split("?", 1)[0]
            return unquote(address).strip()
    except Exception:
        pass
    return ""


def is_valid_email(address: str) -> bool:
    lowered = address.lower().strip()
    if "@" not in lowered or lowered.endswith(IGNORED_EMAIL_SUFFIXES):
        return False
    domain = lowered.split("@")[-1]
    return not any(bad in domain for bad in IGNORED_EMAIL_DOMAINS)


def load_master() -> pd.DataFrame:
    """Load existing data, preserving the user's name/email rows."""
    if MASTER_CSV.exists():
        try:
            df = pd.read_csv(MASTER_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(MASTER_CSV, dtype=str, keep_default_na=False)
    else:
        df = pd.DataFrame(columns=COLUMNS)

    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[COLUMNS].fillna("")
    # Remove duplicate existing records by normalized URL first, otherwise name.
    seen_urls, seen_names, rows = set(), set(), []
    for row in df.to_dict("records"):
        name_key = normalize_name(row["Restaurant Name"])
        url_key = normalize_url(row["URL"])
        if (url_key and url_key in seen_urls) or (not url_key and name_key and name_key in seen_names):
            continue
        rows.append(row)
        if url_key:
            seen_urls.add(url_key)
        if name_key:
            seen_names.add(name_key)
    return pd.DataFrame(rows, columns=COLUMNS)


def save_master(df: pd.DataFrame) -> None:
    df.to_csv(MASTER_CSV, index=False, encoding="utf-8-sig")


async def collect_listing_links(page) -> list[str]:
    anchors = await page.query_selector_all("a[href*='/Restaurant_Review-']")
    links, seen = [], set()
    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if not href:
            continue
        canonical = normalize_url(urljoin(BASE_URL, href))
        if canonical and canonical not in seen:
            seen.add(canonical)
            links.append(canonical)
    return links


async def go_to_next_listing_page(page) -> bool:
    next_selectors = [
        "a[aria-label='Next page']",
        "a.nav.next",
        "a[data-smoothing='true']:has-text('Next')",
        "a:has-text('Next')",
    ]
    for selector in next_selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            if await locator.get_attribute("aria-disabled") == "true":
                return False
            await locator.scroll_into_view_if_needed(timeout=3_000)
            await locator.click(timeout=5_000)
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await human_delay(*DELAY_LONG)
            return True
        except Exception:
            continue
    return False


async def extract_restaurant_name(page) -> str:
    """Read only the visible restaurant name before deciding whether to skip."""
    for selector in ("h1[data-test-target='top-info-header']", "h1"):
        try:
            heading = page.locator(selector).first
            if await heading.count() > 0:
                value = (await heading.inner_text(timeout=3_000)).strip()
                if value:
                    return value
        except Exception:
            continue

    try:
        title = await page.title()
        return title.split(" - ")[0].split(",")[0].strip()
    except Exception:
        return ""


async def extract_email_from_open_page(page) -> str:
    """Extract an email only after the restaurant has passed deduplication."""
    try:
        body_text = await page.locator("body").inner_text(timeout=5_000)
        for email_addr in EMAIL_RE.findall(body_text):
            if is_valid_email(email_addr):
                return email_addr.lower()
    except Exception:
        pass

    try:
        mailto_anchors = await page.query_selector_all("a[href^='mailto:']")
        for anchor in mailto_anchors:
            href = await anchor.get_attribute("href")
            address = clean_mailto(href or "")
            if address and is_valid_email(address):
                return address.lower()
    except Exception:
        pass
    return ""


async def run(target_url: str) -> pd.DataFrame:
    master = load_master()
    save_master(master)  # Normalizes/creates the file before scraping.

    seen_names = {
        normalize_name(name)
        for name in master["Restaurant Name"].tolist()
        if normalize_name(name)
    }
    seen_urls = {
        normalize_url(url)
        for url in master["URL"].tolist()
        if normalize_url(url)
    }

    new_count = 0
    visited_listing_urls = set()

    print(f"[*] Loaded {len(master)} existing rows from {MASTER_CSV}.")
    print("[*] Existing restaurant names/URLs will be skipped.")
    print(f"[*] Maximum NEW restaurants this run: {MAX_NEW_RESTAURANTS}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 850},
            locale="en-US",
        )
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        listing_page = await context.new_page()

        try:
            print(f"[*] Opening listing page: {target_url}")
            await listing_page.goto(target_url, wait_until="domcontentloaded")
            await human_delay(*DELAY_LONG)
            if await page_has_access_challenge(listing_page):
                print("[!] Access/verification challenge detected on the listing page. Stopping.")
                return master
            await dismiss_popups(listing_page)

            for page_number in range(1, MAX_LISTING_PAGES + 1):
                print(f"\\n--- Listing page {page_number} ---")
                links = await collect_listing_links(listing_page)
                new_links = [u for u in links if u not in visited_listing_urls]
                print(f"Found {len(new_links)} unseen listing URLs on this page.")

                for url in new_links:
                    if new_count >= MAX_NEW_RESTAURANTS:
                        print("[*] Reached the new-restaurant limit.")
                        break

                    visited_listing_urls.add(url)
                    canonical_url = normalize_url(url)

                    # URL match lets us skip known restaurants without opening their detail page.
                    if canonical_url in seen_urls:
                        print(f"[SKIP - URL ALREADY IN MASTER] {url}")
                        continue

                    detail_page = await context.new_page()
                    try:
                        await detail_page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                        await human_delay(*DELAY_LONG)

                        if await page_has_access_challenge(detail_page):
                            print("[!] Access/verification challenge detected. Ending this run safely.")
                            return master

                        # First read the name only. If it already exists, do not extract its email.
                        restaurant_name = await extract_restaurant_name(detail_page)
                        name_key = normalize_name(restaurant_name)

                        if name_key and name_key in seen_names:
                            print(f"[SKIP - NAME ALREADY IN MASTER] {restaurant_name}")
                            seen_urls.add(canonical_url)
                            continue

                        if not restaurant_name:
                            print(f"[!] Could not identify restaurant name; skipping record: {canonical_url}")
                            continue

                        # Only genuinely new names reach email extraction.
                        email = await extract_email_from_open_page(detail_page)
                        row = {
                            "Restaurant Name": restaurant_name,
                            "Email": email,
                            "URL": canonical_url,
                        }

                        master.loc[len(master)] = [
                            row["Restaurant Name"],
                            row["Email"],
                            canonical_url,
                        ]
                        save_master(master)  # Save after each new restaurant.
                        seen_names.add(name_key)
                        seen_urls.add(canonical_url)
                        new_count += 1

                        print(
                            f"[NEW {new_count}] {row['Restaurant Name']} -> "
                            f"{row['Email'] or 'no email found'}"
                        )
                    except PlaywrightTimeout:
                        print(f"[TIMEOUT] {canonical_url} — will be eligible for retry next run.")
                    except RuntimeError as exc:
                        print(f"[STOP] {exc}")
                        return master
                    except Exception as exc:
                        print(f"[ERROR] {canonical_url}: {exc}")
                    finally:
                        await detail_page.close()

                    await human_delay(*DELAY_LONG)

                if new_count >= MAX_NEW_RESTAURANTS:
                    break
                if page_number < MAX_LISTING_PAGES:
                    if not await go_to_next_listing_page(listing_page):
                        print("[*] No further listing page detected.")
                        break
        finally:
            await context.close()
            await browser.close()

    print(f"\\n[✓] Added {new_count} new restaurants.")
    print(f"[✓] Master CSV now contains {len(master)} rows: {MASTER_CSV}")
    return master


if __name__ == "__main__":
    TARGET_URL = "https://www.tripadvisor.com/Restaurants-g295424-Dubai_Emirate_of_Dubai.html"
    try:
        asyncio.run(run(TARGET_URL))
    except KeyboardInterrupt:
        print("\\n[!] Interrupted by user. Saved records remain in the CSV.")
    except Exception as exc:
        print(f"[!] Scraper stopped: {exc}")
        raise
