"""Simulate eBay visual search from the first listing image (headed Playwright)."""

from urllib.parse import unquote, urlparse, urlunparse
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
COOKIE_HEADER = (
    "__uzma=7a5339c5-fc0e-4250-ad42-3372fd37a630; __uzmb=1788345913; __uzme=8423; "
    "utag_main__sn=1; __ssds=2; __ssuzjsr2=a9be0cd8e; "
    "__uzmaj2=195173b8-f678-4023-9f85-c0a6b1aac47c; __uzmbj2=1788345918; "
    "s=CgAD4ACBqmpE7NjFiOGY3NWQxYTAwYWFiMjg4MTcxYmYwZmRiNjk2MDLEdoFz; "
    "ak_bmsc=13D18CD0223C37B81A37A85A44858214~000000000000000000000000000000~YAAQqEAQAiAIxDWgAQAAIib6ZwFb+n9BftxIXW0BWje+UZ/aR35024CG+hdmfQi6tvMigXD5OwAB/nRhzYLHtZc25Pqv0fTc2LaZqUUhXgx2ESelSQk8pjZShOjBMPSi6K4pVDyt6KkXx1aqpCIRLwaQaVjJJGNCbhCe5uOZ65E3YZ5B91QvhIUlqKGgbvSJSpNqfXUUXQSJ69ryPxWT8AndOWWBG0AWfXpqYJovhZku6zEIRGmCL5vnLJC8ztOOqDvzOCfG/7anT+TlpKW87mVNkGKiKcAlxL5bcHOBkHfVHy6cp51NIZfOq7K8ZyB7GTKlhZDUULciLVolnEOKYnT7CczfhMcwQJjfg+30DfX3B/XbSaJdzcX0qoZ9eAvqPq2SKZhJIM/s5t0=; "
    "__uzmlj2=c1e8Ro7/TVFIIj9e+NLeM6aJeX12qQz+6gQLlVfnsL4=; "
    "bm_sv=50EC4C1756511C277CD8D12D94A4B783~YAAQGlUQYNUAZ0WgAQAAIvspaAF4nkWMJYqkYloK0S3MG7NYBUNIxMWqlF5haQ6rPNzpMhmWPSPFFwP2BENRuvvl7IEr+dY0QZrn4ESMUsGosuXzvk+FZxuUUP9DJ30HpqwSNDktADfr0S/qNtiVGTeRcFgXz2x/ycIqjFH1SMpOsu7rM4BG2x4hTLxERrBv5DwHiXmv+NIqCE73APRft4unxGlyVT9yjGeigbmnYZ1Rt5s0JixbTWj7TkLOPQ==~1; "
    "ds2=asotr/b8_5azzzzzzz^msg/61b8f75d1a00aab288171bf0fdb69602^sotr/b8_5az8wzmHq^; "
    "ebay=%5Ejs%3D1%5Esbf%3D%23000000%5E; "
    "__uzmcj2=267975825200; __uzmdj2=1788454487; "
    "__uzmfj2=7f6000170d2146-819d-4c95-bf68-110797462a6f1788345918579108568953-7288b7ffca9192c558; "
    "__uzmc=90432131548893; __uzmd=1788454487; "
    "__uzmf=7f6000170d2146-819d-4c95-bf68-110797462a6f1788345913170108574672-e52a320e7c5712cb1315; "
    "ds2=asotr/b8_5az8wzmHq^msg/61b8f75d1a00aab288171bf0fdb69602^; "
    "totp=1788454587832.Opa8huo48ND3YDlCb5HYmrFFcnmQE5FF1iYHjJ8ifynQM4pgv1gYdR8dRBDcZPD7gHedGrY60pJVX3E9mtkd1Q==.X00NDtcOWAHSyuu2WTAVcypWvm5iiB2ZV5Amy458_dk; "
    "bm_so=1D0B13B957C881F1890866B77C99B86353DC9A54C11E57385E7729840B2AFD74~YAAQnEAQAm27/z+gAQAAiVIzaAgd2QP5/6sOzDOhhezX5hJu3n2r27ITTjy+0jlYOnJBXTjiqSVDmsOt13hv045oS1Kp2FTBHuCBVJcv6vSewMANd8ZiiPCUv4fQ7j+RoztSwyKInnMFjx87f8id5Yuy95giZmfpNUavplbb8sRVBKVLCKGL1+1kECPjYmhvB4K+pr2zbTzDh41eLzYqPK/JRd2M1gEwGOd5OGAZGrkpA/1uVnaUSeL2M7FMRrwcD4x9yc6uMp0y8VZF0RkJqP1KQluwjv9HX4xlvHYHFPjB9YmiM5b7YbH04jSAS/w+FVRwJo8DIBVMsAPhyh/B57PVZNMqdjQoQprm0LewNhS9QYJlhAC2wNlREtK72Gcmj0Wd7f83/UCijVkpSD7ZiifvHbqXHHqq2WnfAB+0LoGo6MmvPsrkcb7EdCWZCCQ2TXLmatVTIKfY27PIaveyvTc=; "
    "bm_lso=1D0B13B957C881F1890866B77C99B86353DC9A54C11E57385E7729840B2AFD74~YAAQnEAQAm27/z+gAQAAiVIzaAgd2QP5/6sOzDOhhezX5hJu3n2r27ITTjy+0jlYOnJBXTjiqSVDmsOt13hv045oS1Kp2FTBHuCBVJcv6vSewMANd8ZiiPCUv4fQ7j+RoztSwyKInnMFjx87f8id5Yuy95giZmfpNUavplbb8sRVBKVLCKGL1+1kECPjYmhvB4K+pr2zbTzDh41eLzYqPK/JRd2M1gEwGOd5OGAZGrkpA/1uVnaUSeL2M7FMRrwcD4x9yc6uMp0y8VZF0RkJqP1KQluwjv9HX4xlvHYHFPjB9YmiM5b7YbH04jSAS/w+FVRwJo8DIBVMsAPhyh/B57PVZNMqdjQoQprm0LewNhS9QYJlhAC2wNlREtK72Gcmj0Wd7f83/UCijVkpSD7ZiifvHbqXHHqq2WnfAB+0LoGo6MmvPsrkcb7EdCWZCCQ2TXLmatVTIKfY27PIaveyvTc=~1788454588341; "
    "bm_s=YAAQnEAQAqK7/z+gAQAA9VozaAaMfISnzOrntkzOkanHD+T95c0rfouUxUnmjElS9/IvsqITj2UihpJ7mYy7f/irJqN7xB9QYW8RUbARlKVW16x6cfnvKH3K0z8WgKenJ7Ias4ieSrD7VB0x20Nxe4a8X5SS+2+DooGHsa9lRP2dsp8FWgY4rQTRJ5HNSlI92OF1KUlrcx3Tbo3Ezn8lSqS06gJbRySOqRKuGze6UQJVVfA0mdeHKQ53v3F0YFSdWZAyqyEzhnglnbb/WVMZi0Fm7CTytFJGDZUqEWTdtZTooomnetrBJfiUxlxab7AozMUxIlP44kGp2raPoSCEJay6hxaDII+5GbWVttMTltFj/yOkSB8JtnOEQG1B7d7lW8//CCCpxzrfD6ZoJDFIGeKrx5O069iaoWoPVnS3hr2zW/m/ROYmfxgugYF2r5CHe46XwumvWGOjGJyUbZU4v+AbA4Q7VzQpB8Qvp5RgAHFmF0SEgrZ9E29Daa7rkHZDu3w92JRAP6uC58FDwRv5nAghW2kaKgbhuX1pjFAc7wgJ9IBEu+ZyzgSU43AP26bx+T0b5CfCLADpZPuJKGsCfiq73Bh8mVZSU6KapZXmkqZsWdh5IU4/n6SLRRdrk/p3RP2xCRIJsTbGTukiYvgL0/Oebj+Bs5yHpqFCXfT7QinWrvlvkC17t6H8NPgFECtQd+fZunm5MDD70GIgBqWwIGV3XLw21aMXLx/kFLbusxu0LZom2flGIrFkL/bhx53cENy2F/IgV05iEw9KL5TQo52Nm02J1mfZDmgE5AM77PY8onSvdu+fRbEhH/NQU7efbN62h/uuLF+53LKyiipA0JeJrYgeZfrsqmcqhqvtMfo6OSMXer+dmg9e8OjjDfDRRvpIQQLt1jAVUi+P7vzkHpGvcmjECwDO13xkP6SSir61Y7O8IA+5FWtOpXwkPk+V472831jpJ9WOLjY9Dur4/jp7MU1wWsW2jC/nOVazYH5ecvXIBtbSx7v+RPdj63c8E0/DhacJAgRLUeWHoKn3n05vQiknhL3nUQbohYwSSSnnQuMMFP1EphsxbkNHf3bjJQ==; "
    "ns1=BAQAAAZ5muxf0AAaAANgAU2x62j9jNjl8NjAxXjE3ODgzNDc2Njk3MDZeXjFeM3wyfDV8NHw3fDEwfDQyfDQzfDExXl5eNF4zXjEyXjEyXjJeMV4xXjBeMV4wXjFeNjQ0MjQ1OTA3Nc5p9WCXPmK2QaUX8Fwj5byDIJUj; "
    "nonsession=BAQAAAZ5muxf0AAaAADMACWx62j83MzA3MixVU0EAygAgblwNvzYxYjhmNzVkMWEwMGFhYjI4ODE3MWJmMGZkYjY5NjAyAMsAAmqZrccyNmGpoKNmq4kxH97t8uyzd4vX304p; "
    "dp1=bpbf/%23e000000000000000006c7ada3f^bl/DZen-US6e5c0dbf^"
)


def cookies_from_header(header, domain=".ebay.com"):
    cookies = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = {
            "name": name.strip(),
            "value": unquote(value),
            "domain": domain,
            "path": "/",
        }
    return list(cookies.values())


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
        cookies = cookies_from_header(COOKIE_HEADER)
        context.add_cookies(cookies)
        print(f"Loaded {len(cookies)} cookies")
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
