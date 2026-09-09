import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from selectolax.parser import HTMLParser

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import image_search

PAGE_TIMEOUT_MS = 25_000
RESULTS_SELECTOR_TIMEOUT_MS = 20_000
VISUAL_SEARCH_RESULTS_TIMEOUT_MS = 8_000
CHALLENGE_WAIT_TIMEOUT_MS = 35_000

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-position=0,0",
    "--ignore-certificate-errors",
]
LINUX_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--mute-audio",
]
BROWSER_RESTART_EVERY = 1000
BROWSER_RESTART_PAUSE_SECONDS = 1.5
BROWSER_WARMUP_ATTEMPTS = 3

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""

PLACEHOLDER_ITEM_ID = "itm/123456"
BRAND_NEW_CONDITION = "brand new"

BLOCK_MARKERS = (
    "verify yourself",
    "robot check",
    "are you a robot",
    "pardon our interruption",
    "checking your browser before accessing",
    "checking your browser before you access",
    "please verify yourself to continue",
)

CAPTCHA_WARNING_MARKERS = (
    "captcha",
    "hcaptcha",
    "recaptcha",
    "verify yourself",
    "robot check",
    "are you a robot",
    "checking your browser",
    "pardon our interruption",
    "challenge-platform",
    "unusual activity",
    "access denied",
)

RESULT_MARKERS = (
    "srp-river-results",
    "li.s-card",
    "s-item-card",
    "srp-save-null-search",
)

RESULTS_WAIT_SELECTOR = (
    ".srp-river-results li.s-card[data-listingid], "
    ".srp-river-results li.s-card, "
    ".srp-river-results .s-item-card"
)
NO_EXACT_MATCH_SELECTOR = ".srp-save-null-search__heading"
SEARCH_READY_SELECTOR = f"{RESULTS_WAIT_SELECTOR}, {NO_EXACT_MATCH_SELECTOR}"
VISUAL_SEARCH_READY_SELECTOR = (
    f"{SEARCH_READY_SELECTOR}, li.s-card[data-listingid]"
)
SELLER_CARD_SELECTOR = ".x-sellercard-atf__avatar-info"
RESULTS_LIST_SELECTORS = ("ul.srp-results", ".srp-river-results")
INTERNATIONAL_DIVIDER_CLASS = "srp-river-answer--REWRITE_START"
LISTING_CARD_CLASSES = frozenset({"s-card", "s-item-card"})
SHIP_TO_CONTAINER_SELECTOR = ".gh-ship-to"
SHIP_TO_US_ICON_SELECTOR = ".gh-ship-to .fl-us, .gh-ship-to__menu-icon.fl-us"
SHIP_TO_WAIT_TIMEOUT_MS = 8_000
SHIP_TO_RETRY_WAIT_SECONDS = 3.0
DEFAULT_SHIP_ZIP = "73072"
DEFAULT_SHIP_COUNTRY = "USA"
DEFAULT_SHIP_LATITUDE = 35.2226
DEFAULT_SHIP_LONGITUDE = -97.4395
MIN_RESULTS_PAGE_BYTES = 10_000
BODY_PREVIEW_CHARS = 500
HTML_DEBUG_CHARS = 12_000
BODY_TEXT_DEBUG_CHARS = 2_500
NEW_LISTING_SELECTOR = "span.s-card__new-listing"
EBAYIMG_URL_RE = re.compile(
    r"https://i\.ebayimg\.com/images/g/[^/\s,]+/s-l\d+\.(?:webp|jpg)",
    re.IGNORECASE,
)

PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
PLAIN_PRICE_RE = re.compile(r"^\s*([\d,]+(?:\.\d{1,2})?)\s*$")
SELLER_REVIEW_COUNT_RE = re.compile(
    r"\(\s*(?P<count>[\d,.]+)\s*(?P<suffix>[KMB]?)\s*\)",
    re.IGNORECASE,
)
SHIPPING_PAID_RE = re.compile(
    r"\+?\$\s*([\d,]+(?:\.\d{2})?)\s*delivery",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"^(?:Listed\s+)?(?:"
    r"(?:\d+\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days)\s+ago)"
    r"|today|yesterday"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"(?:-|\s+)\d{1,2}(?:\s+\d{1,2}:\d{2})?"
    r")$",
    re.IGNORECASE,
)
RELATIVE_LISTING_DATE_RE = re.compile(
    r"^(?P<amount>\d+)\s*(?P<unit>"
    r"s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days"
    r")\s+ago$",
    re.IGNORECASE,
)
DATE_PARSE_FORMATS = ("%b-%d %H:%M", "%b-%d", "%b %d %H:%M", "%b %d")
SHIPPING_FREE_MARKERS = ("free delivery", "free shipping")
US_LOCATION_MARKER = "united states"


@dataclass
class PageFetchResult:
    url: str
    final_url: str
    status_code: int
    html: str

    @property
    def content_length(self) -> int:
        return len(self.html.encode("utf-8"))


class EbayBlockedError(Exception):
    def __init__(
        self,
        url: str,
        status_code: int,
        final_url: str,
        content_length: int,
        reason: str,
        body_preview: str,
    ) -> None:
        self.status_code = status_code
        self.final_url = final_url
        self.content_length = content_length
        self.reason = reason
        self.body_preview = body_preview
        message = (
            f"eBay blocked the request ({reason}): "
            f"status={status_code}, final_url={final_url}, "
            f"bytes={content_length}"
        )
        super().__init__(message)


class EbayShipToNotUsError(Exception):
    """Raised when the header Ship to control is not set to the United States."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = f"eBay Ship to is not US ({reason})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def parse_price(value: object) -> float | None:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = str(value)
    match = PRICE_RE.search(text) or PLAIN_PRICE_RE.fullmatch(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_seller_review_count(value: str) -> int | None:
    """Parse eBay's parenthesized feedback count, including K/M/B suffixes."""
    if not value:
        return None
    match = SELLER_REVIEW_COUNT_RE.search(value)
    if match is None:
        return None

    try:
        count = float(match.group("count").replace(",", ""))
    except ValueError:
        return None

    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return round(count * multiplier[match.group("suffix").upper()])


def extract_seller(card) -> tuple[str, int | None]:
    seller_row = card.css_first(
        ".su-card-container__attributes__secondary .s-card__attribute-row"
    )
    if seller_row is None:
        return "", None

    spans = seller_row.css(".su-styled-text")
    seller_name = spans[0].text(strip=True) if spans else ""
    reviews_text = (
        spans[1].text(separator=" ", strip=True)
        if len(spans) > 1
        else seller_row.text(separator=" ", strip=True)
    )
    return seller_name, parse_seller_review_count(reviews_text)


def extract_seller_from_listing_html(html: str) -> tuple[str, int | None]:
    tree = HTMLParser(html)
    card = tree.css_first(SELLER_CARD_SELECTOR) or tree.css_first(
        ".x-sellercard-atf__info"
    )
    if card is None:
        return "", None

    name_node = card.css_first(
        ".x-sellercard-atf__about-seller-item--seller-name"
    )
    seller_name = name_node.text(strip=True) if name_node else ""
    review_node = card.css_first(
        ".x-sellercard-atf__about-seller-item span.ux-textspans--SECONDARY"
    )
    reviews_text = review_node.text(strip=True) if review_node else ""
    return seller_name, parse_seller_review_count(reviews_text)


def fetch_listing_seller_details(page: Page, url: str) -> tuple[str, int | None]:
    goto_ebay(page, url)
    try:
        page.wait_for_selector(
            SELLER_CARD_SELECTOR,
            timeout=RESULTS_SELECTOR_TIMEOUT_MS,
            state="attached",
        )
    except PlaywrightTimeoutError:
        pass
    return extract_seller_from_listing_html(page.content())


def parse_listing_month_day(value: str) -> tuple[int, int] | None:
    if not value:
        return None

    text = re.sub(r"^Listed\s+", "", value.strip(), flags=re.IGNORECASE)
    if not DATE_RE.match(text):
        return None

    for fmt in DATE_PARSE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.month, parsed.day
    return None


def parse_relative_listing_date(
    value: str,
    *,
    now: datetime | None = None,
) -> date | None:
    if not value:
        return None

    text = re.sub(r"^Listed\s+", "", value.strip(), flags=re.IGNORECASE)
    current = now or datetime.now()
    lower = text.casefold()
    if lower == "today":
        return current.date()
    if lower == "yesterday":
        return current.date() - timedelta(days=1)

    match = RELATIVE_LISTING_DATE_RE.match(text)
    if match is None:
        return None

    amount = int(match.group("amount"))
    unit = match.group("unit").casefold()
    if unit.startswith("d"):
        return current.date() - timedelta(days=amount)
    if unit.startswith("h"):
        return (current - timedelta(hours=amount)).date()
    if unit.startswith("m"):
        return (current - timedelta(minutes=amount)).date()
    return (current - timedelta(seconds=amount)).date()


def infer_ordered_listing_date(
    value: str,
    *,
    latest_allowed: date,
) -> date | None:
    relative_date = parse_relative_listing_date(value)
    if relative_date is not None:
        return min(relative_date, latest_allowed)

    month_day = parse_listing_month_day(value)
    if month_day is None:
        return None

    month, day = month_day
    for year in range(latest_allowed.year, latest_allowed.year - 3, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= latest_allowed:
            return candidate
    return None


def parse_listing_date(value: str, *, reference: date | None = None) -> date | None:
    if not value:
        return None

    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    return infer_ordered_listing_date(
        text,
        latest_allowed=reference or date.today(),
    )


def extract_date_listed(attr_rows: list[str]) -> str:
    for row in reversed(attr_rows):
        text = row.strip()
        if DATE_RE.match(text):
            return text
    return ""


def extract_location(attr_rows: list[str]) -> str:
    for row in attr_rows:
        text = row.strip()
        if text.casefold().startswith("located in "):
            return text
    return ""


def extract_shipping(attr_rows: list[str]) -> tuple[str, float | None]:
    for row in attr_rows:
        text = row.strip()
        lower = text.casefold()
        if "delivery" not in lower and "shipping" not in lower:
            continue
        if any(marker in lower for marker in SHIPPING_FREE_MARKERS):
            return text, 0.0
        match = SHIPPING_PAID_RE.search(text.replace(",", ""))
        if match:
            return text, float(match.group(1))
    # Cards often omit shipping (Best Offer / sponsored). Treat as $0 so
    # item price alone can still qualify; paid shipping is still scraped when shown.
    return "Not listed", 0.0


def is_united_states_listing(location: str) -> bool:
    return US_LOCATION_MARKER in location.casefold()


def _class_tokens(node) -> set[str]:
    raw = node.attributes.get("class") or ""
    return {part.casefold() for part in raw.split() if part}


def is_ship_to_us_html(html: str) -> bool | None:
    """Return True if Ship to is US, False if present but not US, None if missing."""
    tree = HTMLParser(html)
    container = tree.css_first(SHIP_TO_CONTAINER_SELECTOR)
    if container is None:
        return None

    for icon in container.css(".gh-ship-to__menu-icon, .fl-pic, i"):
        if "fl-us" in _class_tokens(icon):
            return True

    if container.css_first(SHIP_TO_US_ICON_SELECTOR) is not None:
        return True

    button = container.css_first("button.gh-ship-to__menu")
    if button is not None:
        aria = (button.attributes.get("aria-label") or "").casefold()
        if "united states" in aria:
            return True

    return False


def assert_ship_to_us_html(html: str) -> None:
    status = is_ship_to_us_html(html)
    if status is True:
        return
    if status is None:
        raise EbayShipToNotUsError(
            "missing_ship_to_control",
            detail=f"selector={SHIP_TO_CONTAINER_SELECTOR}",
        )
    raise EbayShipToNotUsError(
        "not_us",
        detail="expected .gh-ship-to .fl-us (United States)",
    )


def _ship_to_button(page: Page):
    return page.query_selector(f"{SHIP_TO_CONTAINER_SELECTOR} button.gh-ship-to__menu")


def _ship_to_aria_label(page: Page) -> str:
    button = _ship_to_button(page)
    if button is None:
        return ""
    return (button.get_attribute("aria-label") or "").strip()


def verify_ship_to_us(page: Page) -> None:
    """Ensure the header Ship to control shows the US flag (fl-us)."""
    try:
        page.wait_for_selector(
            SHIP_TO_CONTAINER_SELECTOR,
            timeout=SHIP_TO_WAIT_TIMEOUT_MS,
            state="attached",
        )
    except PlaywrightTimeoutError as exc:
        raise EbayShipToNotUsError(
            "missing_ship_to_control",
            detail=f"selector={SHIP_TO_CONTAINER_SELECTOR}",
        ) from exc

    # Country flag is hydrated async after the container mounts.
    try:
        page.wait_for_function(
            """() => {
                const icon = document.querySelector('.gh-ship-to .fl-us, .gh-ship-to__menu-icon.fl-us');
                if (icon) return true;
                const button = document.querySelector('.gh-ship-to button.gh-ship-to__menu');
                const aria = (button && button.getAttribute('aria-label') || '').toLowerCase();
                return aria.includes('united states');
            }""",
            timeout=SHIP_TO_WAIT_TIMEOUT_MS,
        )
        return
    except PlaywrightTimeoutError:
        pass

    detail = _ship_to_aria_label(page) or "no fl-us class on .gh-ship-to__menu-icon"
    raise EbayShipToNotUsError("not_us", detail=detail)


def refresh_and_verify_ship_to_us(page: Page) -> None:
    try:
        set_ship_to_united_states(page)
    except Exception as error:
        print(f"Ship-to dialog failed ({error}); reloading homepage", flush=True)
        goto_ebay(page, "https://www.ebay.com/")
        try:
            set_ship_to_united_states(page)
        except Exception as retry_error:
            print(f"Ship-to dialog retry failed: {retry_error}", flush=True)
            page.reload(
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )
            wait_out_ebay_challenge(page)
    verify_ship_to_us(page)


def set_ship_to_united_states(
    page: Page,
    zip_code: str = DEFAULT_SHIP_ZIP,
) -> None:
    print(f"Setting Ship to United States ({zip_code})", flush=True)
    menu = page.locator(".gh-ship-to button.gh-ship-to__menu").first
    menu.wait_for(state="visible", timeout=SHIP_TO_WAIT_TIMEOUT_MS)
    menu.click()
    dialog = page.locator(".gh-ship-to__lightbox").first
    dialog.wait_for(state="visible", timeout=SHIP_TO_WAIT_TIMEOUT_MS)

    country = dialog.locator(
        "button.listbox-button__control, "
        "input[role='combobox'], "
        "select, "
        "input[aria-label*='Country' i], "
        "input[placeholder*='Country' i]"
    ).first
    country.click(timeout=SHIP_TO_WAIT_TIMEOUT_MS)
    option = page.locator("[role='option']").filter(
        has_text=re.compile(r"United States", re.I)
    ).first
    try:
        option.wait_for(state="visible", timeout=2_000)
    except PlaywrightTimeoutError:
        page.keyboard.type("United States", delay=40)
        option.wait_for(state="visible", timeout=SHIP_TO_WAIT_TIMEOUT_MS)
    option.click()

    zip_box = dialog.locator(
        "input[aria-label*='ZIP' i], "
        "input[aria-label*='zip' i], "
        "input[placeholder*='ZIP' i], "
        "input[name*='zip' i], "
        "input[type='text'], "
        "input[type='tel']"
    ).first
    zip_box.fill(zip_code)

    done = dialog.get_by_role("button", name=re.compile(r"^(Done|Apply|Save)$", re.I))
    done.first.click()
    try:
        dialog.wait_for(state="hidden", timeout=SHIP_TO_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        page.keyboard.press("Escape")
    time.sleep(1.0)


def filter_us_listings(listings: list[dict]) -> list[dict]:
    return [
        listing
        for listing in listings
        if is_united_states_listing(listing.get("location", ""))
    ]


def calculate_total_price(
    listing_price: float | None,
    shipping_value: float | None,
) -> float | None:
    # Search cards often omit shipping (esp. Best Offer / sponsored). Treat
    # missing shipping as $0 so item price alone can still qualify as a winner.
    if listing_price is None:
        return None
    shipping = 0.0 if shipping_value is None else shipping_value
    return round(listing_price + shipping, 2)


def normalize_listing_url(url: str) -> str:
    if not url:
        return ""
    return url.split("?")[0]


def is_placeholder_listing(url: str, title: str) -> bool:
    if PLACEHOLDER_ITEM_ID in url:
        return True
    return title.strip().casefold() == "shop on ebay"


def has_search_results(html: str) -> bool:
    return any(marker in html for marker in RESULT_MARKERS)


def detect_captcha_signals(html: str) -> list[str]:
    lowered = html.lower()
    return [marker for marker in CAPTCHA_WARNING_MARKERS if marker in lowered]


def format_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:.2f}"


def find_cheapest_listing(listings: list[dict], *, us_only: bool = True) -> dict | None:
    if us_only:
        listings = filter_us_listings(listings)

    cheapest: dict | None = None
    cheapest_total: float | None = None

    for listing in listings:
        total = listing.get("total_price_value")
        if total is None:
            total = listing.get("listing_price_value")
        if total is None:
            continue
        if cheapest_total is None or total < cheapest_total:
            cheapest_total = total
            cheapest = listing

    return cheapest


def listing_rejection_reasons(
    listing: dict,
    *,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
) -> list[str]:
    reasons: list[str] = []

    if not listing.get("is_brand_new"):
        reasons.append("not brand new")

    location = listing.get("location", "")
    if not location:
        reasons.append("location unknown")
    elif not listing.get("is_united_states"):
        reasons.append(f"not US ({location.removeprefix('Located in ').strip()})")

    if min_seller_reviews > 0:
        seller_reviews = listing.get("seller_reviews_count")
        if seller_reviews is None:
            reasons.append("seller reviews missing")
        elif not listing.get("has_minimum_seller_reviews"):
            reasons.append(
                f"seller reviews {seller_reviews} below {min_seller_reviews}"
            )

    date_listed = listing.get("listing_date_iso") or listing.get("date_listed", "")
    if not date_listed:
        reasons.append("listing date missing")
    elif not listing.get("is_recently_listed"):
        reasons.append(f"older than {max_listing_age_days} days ({date_listed})")

    roi_percent = listing.get("roi_percent")
    if roi_percent is None:
        reasons.append("price/ROI could not be calculated")
    elif roi_percent < min_roi_percent:
        reasons.append(f"ROI {roi_percent:.1f}% below {min_roi_percent:.1f}%")
    elif max_roi_percent is not None and roi_percent > max_roi_percent:
        reasons.append(f"ROI {roi_percent:.1f}% above {max_roi_percent:.1f}%")

    return reasons


def format_listing_price_summary(listing: dict) -> str:
    total = listing.get("total_price_value")
    if total is None:
        total = listing.get("listing_price_value")

    item = listing.get("listing_price_value")
    shipping = listing.get("shipping_value")

    if shipping is not None and item is not None:
        return (
            f"{format_money(total)} "
            f"(item {format_money(item)} + ship {format_money(shipping)})"
        )
    return format_money(total)


def format_did_not_win_reasons(
    listing: dict,
    *,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
) -> str:
    reasons = listing_rejection_reasons(
        listing,
        min_roi_percent=min_roi_percent,
        max_listing_age_days=max_listing_age_days,
        min_seller_reviews=min_seller_reviews,
        max_roi_percent=max_roi_percent,
    )
    return ", ".join(reasons) if reasons else "unknown"


def format_cheapest_listing_log(
    result: dict,
    *,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
) -> str:
    buybox = result.get("buybox_price_value")
    if buybox is None:
        buybox = parse_price(result.get("buybox_price", ""))

    cheapest = find_cheapest_listing(result.get("listings", []), us_only=True)
    buybox_text = format_money(buybox)

    if cheapest is None:
        return f"cheapest US eBay: n/a | buy box: {buybox_text}"

    total = cheapest.get("total_price_value")
    if total is None:
        total = cheapest.get("listing_price_value")

    evaluated = evaluate_winning_listing(
        cheapest,
        buybox_price=buybox,
        min_roi_percent=min_roi_percent,
        max_listing_age_days=max_listing_age_days,
        min_seller_reviews=min_seller_reviews,
        max_roi_percent=max_roi_percent,
    )
    ebay_text = format_listing_price_summary(cheapest)
    line = f"cheapest US eBay: {ebay_text} | buy box: {buybox_text}"

    if evaluated.get("is_winner"):
        roi_percent = evaluated.get("roi_percent")
        if roi_percent is not None:
            return f"{line} | winner (ROI {roi_percent:.1f}%)"
        return f"{line} | winner"

    if buybox is not None and total is not None and total >= buybox:
        return f"{line} | did not win: above buy box"

    return (
        f"{line} | did not win: "
        f"{format_did_not_win_reasons(evaluated, min_roi_percent=min_roi_percent, max_listing_age_days=max_listing_age_days, min_seller_reviews=min_seller_reviews, max_roi_percent=max_roi_percent)}"
    )


def format_below_buybox_listing_logs(
    result: dict,
    *,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
) -> list[str]:
    buybox = result.get("buybox_price_value")
    if buybox is None:
        buybox = parse_price(result.get("buybox_price", ""))
    if buybox is None:
        return []

    us_listings = filter_us_listings(result.get("listings", []))
    cheapest = find_cheapest_listing(us_listings, us_only=False)
    cheapest_url = (
        normalize_listing_url(cheapest.get("listing_url", "")) if cheapest else ""
    )

    logs: list[str] = []
    for listing in us_listings:
        listing_url = normalize_listing_url(listing.get("listing_url", ""))
        if cheapest_url and listing_url == cheapest_url:
            continue

        evaluated = evaluate_winning_listing(
            listing,
            buybox_price=buybox,
            min_roi_percent=min_roi_percent,
            max_listing_age_days=max_listing_age_days,
            min_seller_reviews=min_seller_reviews,
            max_roi_percent=max_roi_percent,
        )
        if evaluated.get("is_winner"):
            continue

        spent = evaluated.get("spent")
        if spent is None or spent >= buybox:
            continue

        price_text = format_listing_price_summary(listing)
        roi_percent = evaluated.get("roi_percent")
        roi_part = f", ROI {roi_percent:.1f}%" if roi_percent is not None else ""
        reason_text = format_did_not_win_reasons(
            evaluated,
            min_roi_percent=min_roi_percent,
            max_listing_age_days=max_listing_age_days,
            min_seller_reviews=min_seller_reviews,
            max_roi_percent=max_roi_percent,
        )
        logs.append(f"  below buy box: {price_text}{roi_part} - did not win: {reason_text}")

    return logs


def format_block_log(result: dict) -> str | None:
    if result.get("block_reason"):
        return f"blocked: {result['block_reason']}"
    if result.get("error"):
        return f"blocked: {result['error']}"
    warnings = result.get("captcha_warnings") or []
    if warnings:
        joined = ", ".join(warnings)
        if result.get("listings"):
            return f"captcha detected ({joined}) - results still parsed"
        return f"captcha detected ({joined})"
    return None


def analyze_page(url: str, result: PageFetchResult) -> None:
    html = result.html
    lowered = html.lower()
    content_length = result.content_length

    if has_search_results(html):
        return

    if result.status_code and result.status_code not in (200, 0):
        raise EbayBlockedError(
            url=url,
            status_code=result.status_code,
            final_url=result.final_url,
            content_length=content_length,
            reason=f"unexpected HTTP status {result.status_code}",
            body_preview=html[:BODY_PREVIEW_CHARS],
        )

    if any(marker in lowered for marker in BLOCK_MARKERS):
        raise EbayBlockedError(
            url=url,
            status_code=result.status_code,
            final_url=result.final_url,
            content_length=content_length,
            reason="captcha or bot-detection page",
            body_preview=html[:BODY_PREVIEW_CHARS],
        )

    if content_length < MIN_RESULTS_PAGE_BYTES:
        raise EbayBlockedError(
            url=url,
            status_code=result.status_code,
            final_url=result.final_url,
            content_length=content_length,
            reason="response too small and missing search-result markers",
            body_preview=html[:BODY_PREVIEW_CHARS],
        )


def node_has_class(node, class_name: str) -> bool:
    class_attr = node.attributes.get("class", "")
    return class_name in class_attr.split()


def is_listing_card_node(node) -> bool:
    class_attr = node.attributes.get("class", "")
    return any(card_class in class_attr.split() for card_class in LISTING_CARD_CLASSES)


def find_results_list(tree: HTMLParser):
    for selector in RESULTS_LIST_SELECTORS:
        results_list = tree.css_first(selector)
        if results_list is not None:
            return results_list
    return None


def has_zero_search_results(tree: HTMLParser) -> bool:
    count_heading = tree.css_first(".srp-controls__count-heading")
    if count_heading is None:
        return False
    heading_text = count_heading.text(separator=" ", strip=True)
    return re.search(r"\b0\s+results?\s+for\b", heading_text, re.IGNORECASE) is not None


def has_no_exact_search_results(tree: HTMLParser) -> bool:
    heading = tree.css_first(NO_EXACT_MATCH_SELECTOR)
    if heading is None:
        return False
    return "no exact matches found" in heading.text(separator=" ", strip=True).casefold()


def html_has_listing_cards(tree: HTMLParser) -> bool:
    return bool(
        tree.css("li.s-card[data-listingid]")
        or tree.css(".srp-river-results li.s-card")
        or tree.css(".s-item-card")
    )


def log_page_debug(
    *,
    reason: str,
    error: BaseException | None = None,
    html: str = "",
    url: str = "",
    page: Page | None = None,
) -> None:
    if page is not None:
        try:
            url = page.url or url
        except Exception:
            pass
        if not html:
            try:
                html = page.content()
            except Exception as read_error:
                print(
                    f"  DEBUG could not read page HTML ({type(read_error).__name__}: {read_error})",
                    flush=True,
                )
                html = ""
    tree = HTMLParser(html or "")
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node is not None else ""
    body = tree.css_first("body")
    body_html = body.html if body is not None else (html or "")
    body_text = " ".join(
        (body.text(separator=" ", strip=True) if body is not None else "")[:4000].split()
    )
    ship = tree.css_first(SHIP_TO_CONTAINER_SELECTOR)
    ship_html = (ship.html or "")[:800] if ship is not None else "(missing .gh-ship-to)"
    print(f"  DEBUG {reason}", flush=True)
    if error is not None:
        print(f"  DEBUG exception: {type(error).__name__}: {error}", flush=True)
    print(f"  DEBUG url: {url}", flush=True)
    print(f"  DEBUG title: {title}", flush=True)
    print(
        f"  DEBUG html_bytes={len(html or '')} "
        f"s-card={len(tree.css('li.s-card'))} "
        f"listingid={len(tree.css('[data-listingid]'))} "
        f"null-search={len(tree.css(NO_EXACT_MATCH_SELECTOR))}",
        flush=True,
    )
    print(f"  DEBUG ship-to html: {ship_html}", flush=True)
    print(f"  DEBUG body text: {body_text[:BODY_TEXT_DEBUG_CHARS]}", flush=True)
    print(f"  DEBUG body html:\n{body_html[:HTML_DEBUG_CHARS]}", flush=True)


def iter_direct_element_children(node):
    child = node.child
    while child:
        if child.tag:
            yield child
        child = child.next


def extract_domestic_listing_cards(tree: HTMLParser) -> list:
    results_list = find_results_list(tree)
    if results_list is None:
        cards = tree.css("li.s-card")
        return cards if cards else tree.css(".s-item-card")

    cards = []
    reached_non_exact_results = False
    for item in iter_direct_element_children(results_list):
        if node_has_class(item, INTERNATIONAL_DIVIDER_CLASS):
            reached_non_exact_results = True
            break
        if is_listing_card_node(item):
            cards.append(item)

    if cards or reached_non_exact_results:
        return cards

    nested_cards = results_list.css("li.s-card")
    return nested_cards if nested_cards else results_list.css(".s-item-card")


def to_s_l500(url: str) -> str:
    return image_search.to_s_l500(url)


def extract_card_image_url(card) -> str:
    blobs: list[str] = []
    for img in card.css("img.s-card__image"):
        blobs.append(
            " ".join(
                value
                for value in (
                    img.attributes.get("data-defer-load"),
                    img.attributes.get("src"),
                    img.attributes.get("data-src"),
                    img.attributes.get("srcset"),
                )
                if value
            )
        )
    for blob in blobs:
        match = EBAYIMG_URL_RE.search(blob)
        if match:
            return to_s_l500(match.group(0))
    return ""


def extract_search_listings(html: str) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    if has_zero_search_results(tree) or has_no_exact_search_results(tree):
        return []
    cards = extract_domestic_listing_cards(tree)

    listings: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    latest_listing_date = date.today()

    for card in cards:
        link = card.css_first("a.s-card__link") or card.css_first("a[href*='/itm/']")
        if link is None:
            continue

        listing_url = link.attributes.get("href", "").strip()
        if not listing_url:
            continue

        title_node = card.css_first(".s-card__title")
        title = title_node.text(strip=True) if title_node else link.text(strip=True)
        if is_placeholder_listing(listing_url, title):
            continue

        normalized_url = normalize_listing_url(listing_url)
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)

        price_node = card.css_first(".s-card__price")
        price_text = price_node.text(strip=True) if price_node else ""
        condition_node = card.css_first(".s-card__subtitle")
        condition = condition_node.text(strip=True) if condition_node else ""
        attr_rows = [node.text(strip=True) for node in card.css(".s-card__attribute-row")]
        date_listed = extract_date_listed(attr_rows)
        inferred_listing_date = infer_ordered_listing_date(
            date_listed,
            latest_allowed=latest_listing_date,
        )
        if inferred_listing_date is not None:
            latest_listing_date = inferred_listing_date
        location = extract_location(attr_rows)
        shipping_text, shipping_value = extract_shipping(attr_rows)
        listing_price_value = parse_price(price_text)
        seller_name, seller_reviews_count = extract_seller(card)
        image_url = extract_card_image_url(card)
        is_new_listing = card.css_first(NEW_LISTING_SELECTOR) is not None

        listings.append(
            {
                "listing_title": title,
                "listing_url": listing_url,
                "listing_price": price_text,
                "listing_price_value": listing_price_value,
                "shipping": shipping_text,
                "shipping_value": shipping_value,
                "total_price_value": calculate_total_price(
                    listing_price_value,
                    shipping_value,
                ),
                "location": location,
                "condition": condition,
                "seller_name": seller_name,
                "seller_reviews_count": seller_reviews_count,
                "date_listed": date_listed,
                "listing_date_iso": (
                    inferred_listing_date.isoformat()
                    if inferred_listing_date is not None
                    else ""
                ),
                "image_url": image_url,
                "is_new_listing": is_new_listing,
            }
        )

    return listings


def is_brand_new(condition: str) -> bool:
    return BRAND_NEW_CONDITION in condition.casefold()


def is_recently_listed(
    date_listed: str,
    *,
    max_days: int,
    reference: date | None = None,
) -> bool:
    listing_date = parse_listing_date(date_listed, reference=reference)
    if listing_date is None:
        return False
    reference = reference or date.today()
    return reference - listing_date <= timedelta(days=max_days)


def calculate_roi_percent(
    spent: float | None,
    income: float | None,
) -> float | None:
    if spent is None or income is None or spent <= 0:
        return None
    return round(((income - spent) / spent) * 100, 2)


def evaluate_winning_listing(
    listing: dict,
    *,
    buybox_price: float | None,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
    reference: date | None = None,
) -> dict:
    listing_price = listing.get("listing_price_value")
    if isinstance(listing_price, str):
        listing_price = parse_price(listing_price)

    shipping_value = listing.get("shipping_value")
    if isinstance(shipping_value, str):
        shipping_value = parse_price(shipping_value)

    spent = listing.get("total_price_value")
    if spent is None:
        spent = calculate_total_price(listing_price, shipping_value)

    income = buybox_price
    condition = listing.get("condition", "")
    location = listing.get("location", "")
    date_listed = listing.get("listing_date_iso") or listing.get("date_listed", "")
    brand_new = is_brand_new(condition)
    united_states = is_united_states_listing(location)
    seller_reviews = listing.get("seller_reviews_count")
    seller_qualified = (
        True
        if min_seller_reviews <= 0
        else (
            isinstance(seller_reviews, (int, float))
            and seller_reviews >= min_seller_reviews
        )
    )
    recent = is_recently_listed(
        date_listed,
        max_days=max_listing_age_days,
        reference=reference,
    )
    roi_percent = calculate_roi_percent(spent, income)
    profitable = (
        roi_percent is not None
        and roi_percent >= min_roi_percent
        and (max_roi_percent is None or roi_percent <= max_roi_percent)
    )

    profit = None
    if spent is not None and income is not None:
        profit = round(income - spent, 2)

    return {
        **listing,
        "shipping_value": shipping_value,
        "total_price_value": spent,
        "spent": spent,
        "income": income,
        "profit": profit,
        "roi_percent": roi_percent,
        "buybox_price_value": buybox_price,
        "is_brand_new": brand_new,
        "is_united_states": united_states,
        "has_minimum_seller_reviews": seller_qualified,
        "is_recently_listed": recent,
        "is_profitable": profitable,
        "needs_listing_seller_lookup": (
            min_seller_reviews > 0
            and seller_reviews is None
            and brand_new
            and united_states
            and recent
            and profitable
        ),
        "is_winner": (
            brand_new
            and united_states
            and seller_qualified
            and recent
            and profitable
        ),
    }


def fill_missing_seller_reviews(
    page: Page | None,
    listings: list[dict],
    *,
    buybox_price: float | None,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
    cache: dict[str, tuple[str, int | None]] | None = None,
) -> dict[str, tuple[str, int | None]]:
    cache = cache if cache is not None else {}
    if page is None or min_seller_reviews <= 0:
        return cache

    for listing in listings:
        if listing.get("seller_reviews_count") is not None:
            continue
        evaluated = evaluate_winning_listing(
            listing,
            buybox_price=buybox_price,
            min_roi_percent=min_roi_percent,
            max_listing_age_days=max_listing_age_days,
            min_seller_reviews=min_seller_reviews,
            max_roi_percent=max_roi_percent,
        )
        if not evaluated.get("needs_listing_seller_lookup"):
            continue

        url = str(listing.get("listing_url") or "").strip()
        if not url:
            continue
        if url not in cache:
            print(f"  Looking up seller reviews on listing page")
            cache[url] = fetch_listing_seller_details(page, url)
        seller_name, seller_reviews = cache[url]
        if seller_name:
            listing["seller_name"] = seller_name
        listing["seller_reviews_count"] = seller_reviews
        if seller_reviews is None:
            print("  Seller reviews still missing after listing lookup")
        else:
            print(f"  Seller reviews from listing: {seller_reviews}")
    return cache


def _sales_rank_from_text(text: str) -> int | None:
    match = re.search(r"#\s*([\d,]+)", text)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def extract_amazon_sales_rank(html: str) -> int | None:
    tree = HTMLParser(html)
    for row in tree.css("table.prodDetTable tr"):
        heading = row.css_first("th")
        value = row.css_first("td")
        if heading is None or value is None:
            continue
        label = " ".join(heading.text(separator=" ", strip=True).split()).casefold()
        if label != "best sellers rank":
            continue
        value_text = " ".join(value.text(separator=" ", strip=True).split())
        return _sales_rank_from_text(value_text)

    for item in tree.css("#detailBulletsWrapper_feature_div li"):
        text = " ".join(item.text(separator=" ", strip=True).split())
        if "best sellers rank" not in text.casefold():
            continue
        return _sales_rank_from_text(text)
    return None


def fetch_amazon_sales_rank(page: Page, url: str) -> int | None:
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    try:
        page.wait_for_selector(
            "table.prodDetTable",
            state="attached",
            timeout=RESULTS_SELECTOR_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        pass
    return extract_amazon_sales_rank(page.content())


def create_browser_context(
    browser: Browser,
    *,
    headless: bool = False,
):
    viewport = (
        {"width": 1024, "height": 720}
        if headless
        else {"width": 1440, "height": 900}
    )
    context = browser.new_context(
        locale="en-US",
        timezone_id="America/Chicago",
        geolocation={
            "latitude": DEFAULT_SHIP_LATITUDE,
            "longitude": DEFAULT_SHIP_LONGITUDE,
        },
        permissions=["geolocation"],
        viewport=viewport,
    )
    context.add_init_script(STEALTH_INIT_SCRIPT)
    return context


def is_ebay_challenge_url(url: str) -> bool:
    return "splashui/challenge" in (url or "").casefold()


def page_is_ebay_challenge(page: Page) -> bool:
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if is_ebay_challenge_url(url):
        return True
    try:
        title = (page.title() or "").casefold()
    except Exception:
        title = ""
    return "pardon our interruption" in title


def wait_out_ebay_challenge(
    page: Page,
    *,
    timeout_ms: int = CHALLENGE_WAIT_TIMEOUT_MS,
) -> bool:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    if not page_is_ebay_challenge(page):
        return False
    print("eBay bot-check splash detected; waiting for redirect...", flush=True)
    try:
        page.wait_for_url(
            lambda landed: not is_ebay_challenge_url(landed),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        try:
            page.wait_for_function(
                """() => {
                    const title = (document.title || '').toLowerCase();
                    if (title.includes('pardon our interruption')) return false;
                    const heading = document.querySelector('.pgHeading, h1');
                    const text = ((heading && heading.innerText) || '').toLowerCase();
                    return !text.includes('checking your browser');
                }""",
                timeout=8_000,
            )
        except PlaywrightTimeoutError:
            pass
        print(f"eBay bot-check finished; now at {page.url}", flush=True)
        return True
    except PlaywrightTimeoutError:
        current = ""
        try:
            current = page.url or ""
        except Exception:
            pass
        print(
            f"eBay bot-check still showing after {timeout_ms}ms: {current}",
            flush=True,
        )
        raise EbayBlockedError(
            url=current,
            status_code=0,
            final_url=current,
            content_length=0,
            reason="bot-check splash did not redirect",
            body_preview="Pardon Our Interruption / Checking your browser",
        )


def goto_ebay(page: Page, url: str, *, wait_until: str = "domcontentloaded"):
    last_error = None
    for attempt in range(1, 4):
        try:
            response = page.goto(
                url,
                wait_until=wait_until,
                timeout=PAGE_TIMEOUT_MS,
            )
            wait_out_ebay_challenge(page)
            return response
        except Exception as error:
            text = str(error)
            recoverable = (
                "ERR_ABORTED" in text
                or "interrupted" in text.casefold()
                or "navigating and changing the content" in text.casefold()
            )
            if not recoverable:
                raise
            print(
                f"Navigation interrupted (attempt {attempt}/3): {error}",
                flush=True,
            )
            wait_out_ebay_challenge(page)
            last_error = error
    raise last_error


def warm_up_session(page: Page, *, headless: bool = False) -> None:
    print("Opening eBay homepage to pass bot-check and set ship-to", flush=True)
    goto_ebay(page, "https://www.ebay.com/")
    try:
        verify_ship_to_us(page)
        print("Ship to is already United States", flush=True)
        return
    except EbayShipToNotUsError as error:
        print(f"Ship to is not US on browser open: {error}", flush=True)
    try:
        set_ship_to_united_states(page)
        verify_ship_to_us(page)
        return
    except Exception as error:
        print(f"Could not set Ship to via dialog: {error}", flush=True)
        print("Refreshing homepage and waiting before checking again", flush=True)
        page.reload(
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )
        wait_out_ebay_challenge(page)
        time.sleep(SHIP_TO_RETRY_WAIT_SECONDS)
        set_ship_to_united_states(page)
        verify_ship_to_us(page)


_PLAYWRIGHT_CHROMIUM_LOCK = threading.Lock()
_PLAYWRIGHT_CHROMIUM_READY = False


def ensure_playwright_chromium_installed() -> None:
    """Ensure Playwright Chromium exists in environments without shell access.

    Set SKIP_PLAYWRIGHT_INSTALL=1 to disable this bootstrap step.
    """
    global _PLAYWRIGHT_CHROMIUM_READY
    if os.getenv("SKIP_PLAYWRIGHT_INSTALL", "").strip().lower() in {"1", "true", "yes"}:
        print(
            "GEEFLIP: Skipping Playwright Chromium bootstrap (SKIP_PLAYWRIGHT_INSTALL=1).",
            flush=True,
        )
        return

    with _PLAYWRIGHT_CHROMIUM_LOCK:
        if _PLAYWRIGHT_CHROMIUM_READY:
            return

        try:
            from playwright.sync_api import sync_playwright as _sync_playwright
        except Exception as exc:
            print(f"GEEFLIP: Playwright package not available yet: {exc}", flush=True)
            print("GEEFLIP: Install dependencies first (requirements.txt includes playwright).", flush=True)
            return

        def probe() -> None:
            with _sync_playwright() as playwright:
                browser = _launch_chromium(playwright, headless=True)
                browser.close()

        try:
            probe()
            print("GEEFLIP: Playwright Chromium already installed.", flush=True)
            _PLAYWRIGHT_CHROMIUM_READY = True
            return
        except Exception as exc:
            print(
                f"GEEFLIP: Playwright Chromium missing/unusable ({exc}); installing...",
                flush=True,
            )

        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.stdout:
                print(proc.stdout, flush=True)
            if proc.returncode != 0:
                if proc.stderr:
                    print(proc.stderr, flush=True)
                print(
                    f"GEEFLIP: Playwright install failed with exit code {proc.returncode}.",
                    flush=True,
                )
                return
            print("GEEFLIP: Playwright Chromium installation complete.", flush=True)
        except Exception as exc:
            print(f"GEEFLIP: Failed to run Playwright install command: {exc}", flush=True)
            return

        try:
            probe()
            print("GEEFLIP: Playwright Chromium is ready.", flush=True)
            _PLAYWRIGHT_CHROMIUM_READY = True
        except Exception as exc:
            print(f"GEEFLIP: Chromium still unusable after install ({exc})", flush=True)


def is_browser_crash(error: BaseException) -> bool:
    text = str(error).casefold()
    return any(
        needle in text
        for needle in (
            "target crashed",
            "target closed",
            "browser has been closed",
            "browser closed",
            "page crashed",
        )
    )


def _chromium_launch_args(*, headless: bool) -> list[str]:
    args = list(BROWSER_ARGS)
    if headless or os.name != "nt":
        args.extend(LINUX_BROWSER_ARGS)
    return args


def _launch_chromium(playwright, *, headless: bool):
    kwargs = {
        "args": _chromium_launch_args(headless=headless),
        "chromium_sandbox": False,
        "handle_sigint": False,
        "handle_sigterm": False,
        "handle_sighup": False,
    }
    if headless:
        return playwright.chromium.launch(
            headless=True,
            channel="chromium",
            **kwargs,
        )
    return playwright.chromium.launch(headless=False, **kwargs)


def launch_ebay_browser(playwright, *, headless: bool = False):
    last_error = None
    for attempt in range(1, BROWSER_WARMUP_ATTEMPTS + 1):
        browser = None
        context = None
        try:
            browser = _launch_chromium(playwright, headless=headless)
            context = create_browser_context(
                browser,
                headless=headless,
            )
            page = context.new_page()
            warm_up_session(page, headless=headless)
            return browser, context, page
        except Exception as error:
            last_error = error
            close_ebay_browser(browser, context)
            if not is_browser_crash(error) or attempt >= BROWSER_WARMUP_ATTEMPTS:
                raise
            print(
                f"Browser crashed during warmup (attempt {attempt}/"
                f"{BROWSER_WARMUP_ATTEMPTS}): {error}",
                flush=True,
            )
            time.sleep(BROWSER_RESTART_PAUSE_SECONDS)
    raise last_error


def close_ebay_browser(browser, context) -> None:
    try:
        if context is not None:
            context.close()
    except Exception as error:
        print(f"Browser close failed: {type(error).__name__}: {error}")
    try:
        if browser is not None:
            browser.close()
    except Exception as error:
        print(f"Browser close failed: {type(error).__name__}: {error}")


class EbayBrowserSession:
    def __init__(self, playwright, *, headless: bool = False):
        self._playwright = playwright
        self._headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self.start()

    def start(self) -> None:
        self.browser, self.context, self.page = launch_ebay_browser(
            self._playwright,
            headless=self._headless,
        )

    def close(self) -> None:
        close_ebay_browser(self.browser, self.context)
        self.browser = None
        self.context = None
        self.page = None

    def restart(self) -> None:
        self.close()
        time.sleep(BROWSER_RESTART_PAUSE_SECONDS)
        self.start()


@contextmanager
def browser_session(
    *,
    headless: bool = False,
) -> Iterator[EbayBrowserSession]:
    ensure_playwright_chromium_installed()
    with sync_playwright() as playwright:
        session = EbayBrowserSession(playwright, headless=headless)
        try:
            yield session
        finally:
            session.close()


def fetch_search_page(page: Page, url: str) -> PageFetchResult:
    response = goto_ebay(page, url)
    try:
        page.wait_for_selector(
            SEARCH_READY_SELECTOR,
            timeout=RESULTS_SELECTOR_TIMEOUT_MS,
            state="attached",
        )
        html = page.content()
    except PlaywrightTimeoutError as error:
        try:
            html = page.content()
        except Exception:
            html = ""
        tree = HTMLParser(html)
        if not (
            has_zero_search_results(tree)
            or has_no_exact_search_results(tree)
            or html_has_listing_cards(tree)
        ):
            log_page_debug(
                reason="search selector timeout",
                error=error,
                html=html,
                url=page.url,
                page=page,
            )
            raise

    try:
        assert_ship_to_us_html(html)
    except EbayShipToNotUsError as error:
        log_page_debug(
            reason="ship-to check failed after search load",
            error=error,
            html=html,
            url=page.url,
            page=page,
        )
        raise
    status_code = response.status if response is not None else 0
    result = PageFetchResult(
        url=url,
        final_url=page.url,
        status_code=status_code,
        html=html,
    )
    analyze_page(url, result)
    return result


def first_listing_image_url(page: Page, listings: list[dict] | None = None) -> str:
    for listing in listings or []:
        url = str(listing.get("image_url") or "").strip()
        if url and "i.ebayimg.com" in url and not url.startswith("data:"):
            return to_s_l500(url)
    return image_search.first_listing_image(page)


def _wait_for_search_cards(page: Page, timeout_ms: int) -> None:
    try:
        page.wait_for_selector(
            VISUAL_SEARCH_READY_SELECTOR,
            timeout=timeout_ms,
            state="attached",
        )
    except PlaywrightTimeoutError:
        html = page.content()
        tree = HTMLParser(html)
        if (
            has_zero_search_results(tree)
            or has_no_exact_search_results(tree)
            or html_has_listing_cards(tree)
        ):
            return
        raise


def wait_for_visual_search_results(page: Page) -> str:
    try:
        page.wait_for_url(
            re.compile(r"visualSearchGuid="),
            wait_until="commit",
            timeout=image_search.NAV_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        page.wait_for_url(
            "**/sch/**",
            wait_until="commit",
            timeout=image_search.NAV_TIMEOUT_MS,
        )

    filtered = image_search.ensure_filter_suffix(page.url)
    if page.url != filtered:
        print(f"Image search URL:\n  {page.url}")
        print(f"Reloading with required filters:\n  {filtered}")
        goto_ebay(page, filtered)
        print(f"Filtered image search URL:\n  {page.url}")
    else:
        print("Image search URL already has LH_BIN, LH_ItemCondition, LH_PrefLoc.")

    image_search.dismiss_overlays(page)
    _wait_for_search_cards(page, VISUAL_SEARCH_RESULTS_TIMEOUT_MS)
    print("Visual search listings loaded.")
    return page.url


def run_visual_search(page: Page, image_url: str) -> str:
    image_search.dismiss_overlays(page)

    camera = page.locator(
        'button.gh-search-input__camera-btn, button[aria-label="Camera icon"]'
    ).first
    camera.wait_for(state="visible")
    camera.click()

    dialog = page.get_by_role(
        "dialog",
        name="Can't find the words? Search with an image",
    )
    dialog.wait_for(state="visible")

    url_input = dialog.locator(
        'input[aria-label="Image URL link input"], input[placeholder="Paste an image link"]'
    ).first
    url_input.wait_for(state="visible")
    url_input.click()
    image_search.fill_image_url(url_input, image_url, page)

    go_btn = dialog.locator(
        "button.visual-search-modal__go-btn, button:has-text('Go')"
    ).first
    go_btn.click()

    return wait_for_visual_search_results(page)


def scrape_image_search_page(
    page: Page,
    image_url: str,
    *,
    title: str = "",
    asin: str = "",
    ean: str = "",
    buybox_price: str = "",
) -> dict:
    result = _empty_search_result(
        "",
        title=title,
        asin=asin,
        ean=ean,
        buybox_price=buybox_price,
    )
    result["search_source"] = "image_search"
    try:
        search_url = run_visual_search(page, image_url)
        result["search_url"] = search_url
        html = page.content()
        assert_ship_to_us_html(html)
        analyze_page(
            search_url,
            PageFetchResult(
                url=search_url,
                final_url=page.url,
                status_code=200,
                html=html,
            ),
        )
        return _fill_search_result(
            result,
            html,
            final_url=page.url,
            status_code=200,
        )
    except EbayBlockedError as error:
        return _blocked_search_result(result, error)
    except PlaywrightTimeoutError as error:
        result["error"] = str(error)
        return result


def scrape_search_page(
    page: Page,
    url: str,
    *,
    title: str = "",
    asin: str = "",
    ean: str = "",
    buybox_price: str = "",
) -> dict:
    result = _empty_search_result(
        url, title=title, asin=asin, ean=ean, buybox_price=buybox_price
    )
    try:
        fetch_result = fetch_search_page(page, url)
        return _fill_search_result(
            result,
            fetch_result.html,
            final_url=fetch_result.final_url,
            status_code=fetch_result.status_code,
        )
    except EbayBlockedError as error:
        log_page_debug(
            reason="blocked search page",
            error=error,
            url=url,
            page=page,
        )
        return _blocked_search_result(result, error)
    except PlaywrightTimeoutError as error:
        log_page_debug(
            reason="search page timeout",
            error=error,
            url=url,
            page=page,
        )
        result["error"] = str(error)
        return result
    except EbayShipToNotUsError:
        raise
    except Exception as error:
        log_page_debug(
            reason="search page exception",
            error=error,
            url=url,
            page=page,
        )
        result["error"] = f"{type(error).__name__}: {error}"
        return result


def scrape_search_page_from_html(
    url: str,
    html: str,
    *,
    title: str = "",
    asin: str = "",
    ean: str = "",
    buybox_price: str = "",
) -> dict:
    result = _empty_search_result(
        url, title=title, asin=asin, ean=ean, buybox_price=buybox_price
    )
    try:
        assert_ship_to_us_html(html)
        analyze_page(
            url,
            PageFetchResult(url=url, final_url=url, status_code=200, html=html),
        )
        return _fill_search_result(result, html)
    except EbayBlockedError as error:
        return _blocked_search_result(result, error)


def _empty_search_result(
    url: str,
    *,
    title: str = "",
    asin: str = "",
    ean: str = "",
    buybox_price: str = "",
) -> dict:
    return {
        "title": title,
        "asin": asin,
        "ean": ean,
        "buybox_price": buybox_price,
        "buybox_price_value": parse_price(buybox_price),
        "search_url": url,
        "listings": [],
    }


def _fill_search_result(
    result: dict,
    html: str,
    *,
    final_url: str | None = None,
    status_code: int | None = None,
) -> dict:
    result["listings"] = extract_search_listings(html)
    if final_url is not None:
        result["final_url"] = final_url
    if status_code is not None:
        result["status_code"] = status_code
    warnings = detect_captcha_signals(html)
    if warnings:
        result["captcha_warnings"] = warnings
    return result


def _blocked_search_result(result: dict, error: EbayBlockedError) -> dict:
    result.update(
        {
            "error": str(error),
            "status_code": error.status_code,
            "final_url": error.final_url,
            "response_bytes": error.content_length,
            "block_reason": error.reason,
        }
    )
    return result
