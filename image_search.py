"""Simulate eBay visual search from the first listing image (headed Playwright)."""

from urllib.parse import urlparse, urlunparse
import re

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

HOME_URL = "https://www.ebay.com/"
SEARCH_URL = (
    "https://www.ebay.com/sch/i.html?"
    "_nkw=0673419359658&_sacat=0&_from=R40&LH_BIN=1"
    "&_sop=10&LH_PrefLoc=1&rt=nc&LH_ItemCondition=3"
)
INSPECT_SECONDS = 2
NAV_TIMEOUT_MS = 30000
OVERLAY_TIMEOUT_MS = 500
EXPECTED_IMAGE = "https://i.ebayimg.com/images/g/bCsAAeSwHUpqmJbW/s-l500.webp"


def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)


def dismiss_overlays(page):
    selectors = [
        "button.tourtip__close",
        'button[aria-label="Close dialogue"]',
        'button[aria-label="Close dialog"]',
        "button.lightbox-dialog__close",
        "#gdpr-banner-accept",
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                loc.click()
        except PlaywrightTimeoutError:
            continue


IMAGE_SEARCH_FILTERS = ("LH_BIN=1", "LH_ItemCondition=3", "LH_PrefLoc=1")


def ensure_filter_suffix(url):
    parsed = urlparse(url)
    kept = []
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0]
        if key in ("LH_BIN", "LH_ItemCondition", "LH_PrefLoc"):
            continue
        kept.append(pair)
    kept.extend(IMAGE_SEARCH_FILTERS)
    return urlunparse(parsed._replace(query="&".join(kept)))


def wait_for_visual_search_page(page):
    try:
        page.wait_for_url(re.compile(r"visualSearchGuid="), timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        page.wait_for_url("**/sch/**", timeout=NAV_TIMEOUT_MS)
    page.wait_for_load_state("domcontentloaded")

    filtered = ensure_filter_suffix(page.url)
    print(f"Image search URL:\n  {page.url}")
    if page.url != filtered:
        print(f"Reloading with required filters:\n  {filtered}")
        goto(page, filtered)
        print(f"Filtered image search URL:\n  {page.url}")
    else:
        print("Image search URL already has LH_BIN, LH_ItemCondition, LH_PrefLoc.")

    dismiss_overlays(page)
    page.wait_for_selector("li.s-card[data-listingid]")
    print("Visual search listings loaded.")


def scrape_new_listings(page):
    new_listing_card = "li.s-card[data-listingid]:has(span.s-card__new-listing)"
    page.wait_for_selector(new_listing_card)

    listings = page.evaluate(
        """() => {
            const results = document.querySelector('#srp-river-results, ul.srp-results') || document;
            return [...results.querySelectorAll('li.s-card[data-listingid]')].filter((el) => {
                return Boolean(el.querySelector('span.s-card__new-listing'));
            }).map((card) => {
                const link = card.querySelector('a.s-card__link');
                const img = [...card.querySelectorAll('img.s-card__image')].find((el) => {
                    const src = el.currentSrc || el.src || '';
                    return src.includes('i.ebayimg.com');
                });
                const titleEl = card.querySelector('.s-card__title .su-styled-text.primary');
                return {
                    listingId: card.getAttribute('data-listingid'),
                    title: titleEl?.innerText?.trim() || null,
                    price: card.querySelector('.s-card__price')?.innerText?.trim() || null,
                    condition: card.querySelector('.s-card__subtitle')?.innerText?.trim() || null,
                    url: link ? link.href : null,
                    image: img ? (img.currentSrc || img.src) : null,
                };
            });
        }"""
    )

    print(f"New Listing cards: {len(listings)}")
    for item in listings:
        print(f"  {item}")
    return listings


def to_s_l500(url):
    marker = "/images/g/"
    if "i.ebayimg.com" not in url or marker not in url:
        return url
    prefix, rest = url.split(marker, 1)
    image_id = rest.split("/", 1)[0]
    ext = "webp" if url.lower().endswith(".webp") else "jpg"
    return f"{prefix}{marker}{image_id}/s-l500.{ext}"


def first_listing_image(page):
    page.wait_for_selector("li.s-card[data-listingid]")
    page.wait_for_selector(
        'li.s-card[data-listingid] img.s-card__image[src*="i.ebayimg.com"]'
    )

    info = page.evaluate(
        """() => {
            const results = document.querySelector('#srp-river-results, ul.srp-results') || document;
            const cards = [...results.querySelectorAll('li.s-card[data-listingid]')].filter((el) => {
                const box = el.getBoundingClientRect();
                return box.height > 80 && box.width > 80;
            });
            const card = cards[0];
            if (!card) {
                return { src: null, title: null, listingId: null, candidates: [] };
            }
            const imgs = [...card.querySelectorAll('img.s-card__image')];
            const candidates = imgs.map((img) => ({
                src: img.getAttribute('src'),
                currentSrc: img.currentSrc,
                srcset: img.getAttribute('srcset'),
                loading: img.getAttribute('loading'),
            }));
            const urls = [];
            for (const img of imgs) {
                const blob = [img.currentSrc, img.src, img.getAttribute('src'), img.getAttribute('srcset')].join(' ');
                const found = blob.match(/https:\\/\\/i\\.ebayimg\\.com\\/images\\/g\\/[^/\\s,]+\\/s-l\\d+\\.(?:webp|jpg)/g) || [];
                urls.push(...found);
            }
            const preferred = urls.find((u) => u.includes('/s-l500.')) || urls[0] || null;
            return {
                src: preferred,
                title: card.querySelector('.s-card__title')?.innerText?.trim() || null,
                listingId: card.getAttribute('data-listingid'),
                candidates,
            };
        }"""
    )

    print("Listing scrape debug:")
    print(f"  listingId: {info.get('listingId')}")
    print(f"  title: {info.get('title')}")
    print(f"  candidates: {info.get('candidates')}")

    src = info.get("src")
    if not src:
        raise RuntimeError("Could not read an i.ebayimg.com URL from the first listing")
    return to_s_l500(src)


def fill_image_url(field, image_url, page):
    field.click()
    field.fill("")
    field.fill(image_url)
    value = field.input_value()
    if value.strip() != image_url:
        field.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(image_url, delay=15)
        value = field.input_value()
    if value.strip() != image_url:
        raise RuntimeError(f"URL was not written correctly. Got: {value!r}")
    print(f"Image URL written:\n  {value}")
    return value


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            slow_mo=250,
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        print("Opening eBay home...")
        goto(page, HOME_URL)
        dismiss_overlays(page)

        print("Opening search results...")
        goto(page, SEARCH_URL)
        dismiss_overlays(page)

        image_url = first_listing_image(page)
        print(f"Scraped image address:\n  {image_url}")
        if image_url != EXPECTED_IMAGE:
            print(f"WARNING: expected\n  {EXPECTED_IMAGE}")
        else:
            print("Scraped image matches the expected first-listing photo.")

        camera = page.locator(
            'button.gh-search-input__camera-btn, button[aria-label="Camera icon"]'
        ).first
        camera.wait_for(state="visible")
        camera.click()

        dialog = page.get_by_role("dialog", name="Can't find the words? Search with an image")
        dialog.wait_for(state="visible")

        url_input = dialog.locator(
            'input[aria-label="Image URL link input"], input[placeholder="Paste an image link"]'
        ).first
        url_input.wait_for(state="visible")
        url_input.click()
        fill_image_url(url_input, image_url, page)

        go_btn = dialog.locator("button.visual-search-modal__go-btn, button:has-text('Go')").first
        go_btn.click()

        wait_for_visual_search_page(page)
        scrape_new_listings(page)

        print(f"Holding browser open {INSPECT_SECONDS}s so you can inspect results...")
        page.wait_for_timeout(INSPECT_SECONDS * 1000)
        browser.close()


if __name__ == "__main__":
    main()
