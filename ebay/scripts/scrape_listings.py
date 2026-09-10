import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.ebay_scraper import (
    BrowserOptions,
    EbayShipToNotUsError,
    browser_session,
    describe_ebay_cookie_session,
    evaluate_winning_listing,
    fetch_amazon_sales_rank,
    fill_missing_seller_reviews,
    filter_us_listings,
    first_listing_image_url,
    format_below_buybox_listing_logs,
    format_block_log,
    format_cheapest_listing_log,
    is_playwright_page,
    load_ebay_cookies,
    log_page_debug,
    parse_price,
    refresh_and_verify_ship_to_us,
    scrape_image_search_page,
    scrape_search_page,
)
from lib.paths import EBAY_COOKIES_FILE


@dataclass
class ScrapeSettings:
    """One scrape's worth of options, built by the website from its filter panel."""

    start_row: int | None = None
    end_row: int | None = None
    limit: int | None = None

    include_brands: tuple[str, ...] = ()
    exclude_brands: tuple[str, ...] = ()

    min_roi_percent: float = 80.0
    max_roi_percent: float | None = 300.0
    max_listing_age_days: int = 2
    min_seller_reviews: int = 50
    min_sales_rank: int | None = None
    max_sales_rank: int | None = 500_000
    min_drop_count: int | None = None
    min_buybox_price: float | None = 40.0

    upc_as_well: bool = True
    cleaned_title_as_well: bool = False
    titles_only: bool = False
    image_search: bool = True
    skip_previously_won: bool = True
    winner_history_retention_days: int = 3
    identifier_no_match_retention_days: int = 3

    cookies_file: Path | None = EBAY_COOKIES_FILE
    cookie_header: str | None = None

    browser: BrowserOptions = field(default_factory=BrowserOptions)
    should_stop: Callable[[], bool] | None = None
    store: object | None = None

    def validate(self) -> None:
        if self.start_row is not None and self.start_row < 2:
            raise ValueError("start_row must be at least 2 (Excel row 1 is the header)")
        if self.end_row is not None and self.end_row < 2:
            raise ValueError("end_row must be at least 2 (Excel row 1 is the header)")
        if (
            self.start_row is not None
            and self.end_row is not None
            and self.start_row > self.end_row
        ):
            raise ValueError("start_row cannot be greater than end_row")
        if self.min_sales_rank is not None and self.min_sales_rank < 0:
            raise ValueError("min_sales_rank cannot be negative")
        if self.max_sales_rank is not None and self.max_sales_rank < 0:
            raise ValueError("max_sales_rank cannot be negative")
        if (
            self.min_sales_rank is not None
            and self.max_sales_rank is not None
            and self.min_sales_rank > self.max_sales_rank
        ):
            raise ValueError("min_sales_rank cannot exceed max_sales_rank")
        if self.min_drop_count is not None and self.min_drop_count < 0:
            raise ValueError("min_drop_count cannot be negative")
        if self.min_buybox_price is not None and self.min_buybox_price < 0:
            raise ValueError("min_buybox_price cannot be negative")
        if (
            self.max_roi_percent is not None
            and self.max_roi_percent < self.min_roi_percent
        ):
            raise ValueError("max_roi_percent cannot be less than min_roi_percent")
        if self.min_seller_reviews < 0:
            raise ValueError("min_seller_reviews cannot be negative")
        if (
            self.winner_history_retention_days is not None
            and self.winner_history_retention_days < 1
        ):
            raise ValueError("winner_history_retention_days must be at least 1")
        if self.identifier_no_match_retention_days < 1:
            raise ValueError("identifier_no_match_retention_days must be at least 1")

    def winner_kwargs(self) -> dict:
        return {
            "min_roi_percent": self.min_roi_percent,
            "max_roi_percent": self.max_roi_percent,
            "max_listing_age_days": self.max_listing_age_days,
            "min_seller_reviews": self.min_seller_reviews,
        }


def parse_sales_rank(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)

    match = re.search(r"\d[\d,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def sales_rank_qualified(
    value: object,
    *,
    min_sales_rank: int | None,
    max_sales_rank: int | None,
) -> bool:
    sales_rank = parse_sales_rank(value)
    return (
        sales_rank is not None
        and (min_sales_rank is None or sales_rank >= min_sales_rank)
        and (max_sales_rank is None or sales_rank <= max_sales_rank)
    )


def ebay_search_url_for_identifier(search_url: str, identifier: str) -> str:
    parts = urlsplit(search_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    replaced = False
    updated_query: list[tuple[str, str]] = []
    for key, value in query:
        if key.casefold() in {"_nkw", "nkw"}:
            updated_query.append((key, identifier))
            replaced = True
        else:
            updated_query.append((key, value))
    if not replaced:
        updated_query.append(("_nkw", identifier))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(updated_query),
            parts.fragment,
        )
    )


def identifier_query_key(identifier_type: str, identifier: object) -> str:
    value = str(identifier or "").strip().casefold()
    return f"{identifier_type.upper()}:{value}" if value else ""


def expand_identifier_searches(
    products: list[dict[str, str]],
    *,
    upc_as_well: bool,
    cleaned_title_as_well: bool,
    titles_only: bool = False,
    skipped_identifier_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    expanded: list[dict[str, str]] = []
    emitted_title_searches: set[tuple[str, str]] = set()
    skipped_identifier_keys = skipped_identifier_keys or set()
    for product in products:
        ean = product.get("ean", "").strip()
        upc = product.get("upc", "").strip()
        asin = product.get("asin", "").strip().casefold()
        cleaned_title = product.get("cleaned_title", "").strip()
        identifiers: list[tuple[str, str]] = []
        title_key = (asin, cleaned_title.casefold())
        include_title = (
            (titles_only or cleaned_title_as_well)
            and asin
            and cleaned_title
            and title_key not in emitted_title_searches
        )
        if titles_only:
            if include_title:
                identifiers.append(("TITLE", cleaned_title))
                emitted_title_searches.add(title_key)
            if not identifiers:
                continue
        else:
            has_identifier_candidate = bool(ean or (upc_as_well and upc and upc != ean))
            if ean and identifier_query_key("EAN", ean) not in skipped_identifier_keys:
                identifiers.append(("EAN", ean))
            if (
                upc_as_well
                and upc
                and upc != ean
                and identifier_query_key("UPC", upc) not in skipped_identifier_keys
            ):
                identifiers.append(("UPC", upc))
            if include_title:
                identifiers.append(("TITLE", cleaned_title))
                emitted_title_searches.add(title_key)
            if not identifiers:
                if cleaned_title_as_well and title_key in emitted_title_searches:
                    continue
                if has_identifier_candidate:
                    continue
                identifiers.append(("QUERY", ""))

        for identifier_type, identifier in identifiers:
            variant = product.copy()
            variant["search_identifier_type"] = identifier_type
            variant["search_identifier"] = identifier
            if identifier:
                variant["search_url"] = ebay_search_url_for_identifier(
                    product["search_url"],
                    identifier,
                )
            expanded.append(variant)
    return expanded


def select_products(
    products: list[dict[str, str]],
    settings: ScrapeSettings,
    *,
    skipped_identifier_keys: set[str] | None = None,
) -> tuple[list[dict[str, str]], str]:
    start = settings.start_row
    end = settings.end_row
    limit = settings.limit
    min_sales_rank = settings.min_sales_rank
    max_sales_rank = settings.max_sales_rank
    min_drop_count = settings.min_drop_count
    min_buybox_price = settings.min_buybox_price
    upc_as_well = settings.upc_as_well
    cleaned_title_as_well = settings.cleaned_title_as_well
    titles_only = settings.titles_only

    total_rows = len(products)
    selected = products
    if start is not None:
        selected = [
            product for product in selected if int(product["csv_row"]) >= start
        ]
    if end is not None:
        selected = [
            product for product in selected if int(product["csv_row"]) <= end
        ]
    rows_in_range = len(selected)

    include_brand_keys = {
        brand.strip().casefold()
        for brand in settings.include_brands
        if brand.strip()
    }
    exclude_brand_keys = {
        brand.strip().casefold()
        for brand in settings.exclude_brands
        if brand.strip()
    }
    if include_brand_keys:
        selected = [
            product
            for product in selected
            if product["brand"].strip().casefold() in include_brand_keys
        ]
    if exclude_brand_keys:
        selected = [
            product
            for product in selected
            if product["brand"].strip().casefold() not in exclude_brand_keys
        ]

    if min_drop_count is not None:
        selected = [
            product
            for product in selected
            if (
                (
                    (drops := parse_sales_rank(product.get("drops_count")))
                    is not None
                    and drops >= min_drop_count
                )
                or (
                    drops is None
                    and sales_rank_qualified(
                        product.get("sales_rank"),
                        min_sales_rank=min_sales_rank,
                        max_sales_rank=max_sales_rank,
                    )
                )
            )
        ]
    elif min_sales_rank is not None or max_sales_rank is not None:
        selected = [
            product
            for product in selected
            if sales_rank_qualified(
                product.get("sales_rank"),
                min_sales_rank=min_sales_rank,
                max_sales_rank=max_sales_rank,
            )
        ]

    before_buybox_filter = len(selected)
    selected = [
        product
        for product in selected
        if (
            (buybox := parse_price(product.get("buybox_price"))) is not None
            and (
                min_buybox_price is None
                or buybox >= min_buybox_price
            )
        )
    ]
    skipped_without_buybox = before_buybox_filter - len(selected)

    scrapeable = [product for product in selected if product["search_url"]]
    skipped_without_url = len(selected) - len(scrapeable)
    selected = scrapeable
    skipped_without_title = 0
    if titles_only:
        with_title = [
            product
            for product in selected
            if product.get("cleaned_title", "").strip()
        ]
        skipped_without_title = len(selected) - len(with_title)
        selected = with_title
    filtered_total = len(selected)
    if limit is not None:
        selected = selected[:limit]

    selected = expand_identifier_searches(
        selected,
        upc_as_well=upc_as_well,
        cleaned_title_as_well=cleaned_title_as_well,
        titles_only=titles_only,
        skipped_identifier_keys=skipped_identifier_keys,
    )
    query_total = len(selected)
    for query_position, product in enumerate(selected, start=1):
        product["selection_position"] = query_position
        product["selection_total"] = query_total

    if start is not None or end is not None:
        last_excel_row = max((int(product["csv_row"]) for product in products), default=1)
        range_label = f"Excel rows {start or 2}-{end or last_excel_row}"
    elif limit is not None:
        range_label = f"first {limit} filtered products"
    else:
        range_label = "all filtered products"

    brand_filter_parts = []
    if include_brand_keys:
        brand_filter_parts.append(f"brands: {', '.join(sorted(include_brand_keys))}")
    if exclude_brand_keys:
        brand_filter_parts.append(
            f"excluding brands: {', '.join(sorted(exclude_brand_keys))}"
        )
    if min_drop_count is not None:
        brand_filter_parts.append(f"minimum drops: {min_drop_count:,}")
        if min_sales_rank is not None:
            brand_filter_parts.append(
                f"fallback minimum sales rank: {min_sales_rank:,}"
            )
        if max_sales_rank is not None:
            brand_filter_parts.append(
                f"fallback maximum sales rank: {max_sales_rank:,}"
            )
    else:
        if min_sales_rank is not None:
            brand_filter_parts.append(f"minimum sales rank: {min_sales_rank:,}")
        if max_sales_rank is not None:
            brand_filter_parts.append(f"maximum sales rank: {max_sales_rank:,}")
    if min_buybox_price is not None:
        brand_filter_parts.append(f"minimum buybox: ${min_buybox_price:,.2f}")
    if titles_only:
        brand_filter_parts.append("cleaned-title searches only")
    else:
        if upc_as_well:
            brand_filter_parts.append("EAN + UPC searches")
        if cleaned_title_as_well:
            brand_filter_parts.append("one cleaned-title search per ASIN")
    brand_filter_label = (
        f"; {'; '.join(brand_filter_parts)}" if brand_filter_parts else ""
    )
    details = (
        f"Selected {range_label}{brand_filter_label}: {len(selected)} searches to scrape "
        f"(of {filtered_total} matching products from {rows_in_range} rows in range, "
        f"{total_rows} source rows)"
    )
    if skipped_without_url:
        details += f", skipped {skipped_without_url} without eBay URL"
    if skipped_without_buybox:
        details += (
            f", skipped {skipped_without_buybox} without a qualifying buybox"
        )
    if skipped_without_title:
        details += f", skipped {skipped_without_title} without a cleaned title"

    return selected, details


TITLE_MATCH_IGNORED_TOKENS = {
    "brand",
    "new",
    "sealed",
    "official",
    "authentic",
    "free",
    "shipping",
}


def title_match_tokens(value: object) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(value).casefold())
        if token not in TITLE_MATCH_IGNORED_TOKENS
    ]


def title_listing_matches(cleaned_title: str, listing_title: str) -> bool:
    query_tokens = list(dict.fromkeys(title_match_tokens(cleaned_title)))
    listing_tokens = set(title_match_tokens(listing_title))
    if len(query_tokens) < 2 or not listing_tokens:
        return False

    model_tokens = {
        token
        for token in query_tokens
        if (
            (any(character.isalpha() for character in token)
             and any(character.isdigit() for character in token))
            or (token.isdigit() and len(token) >= 4)
        )
    }
    if model_tokens and model_tokens.isdisjoint(listing_tokens):
        return False

    matched_weight = 0.0
    total_weight = 0.0
    for token in query_tokens:
        weight = 2.0 if any(character.isdigit() for character in token) else 1.0
        total_weight += weight
        if token in listing_tokens:
            matched_weight += weight
    return matched_weight / total_weight >= 0.55


def extract_winners_from_result(
    result: dict,
    *,
    min_roi_percent: float,
    max_listing_age_days: int,
    min_seller_reviews: int = 0,
    max_roi_percent: float | None = None,
    search_source: str = "upc_ean",
    require_new_listing: bool = False,
) -> list[dict]:
    winners: list[dict] = []
    buybox_value = result.get("buybox_price_value")
    if buybox_value is None:
        buybox_value = parse_price(result.get("buybox_price", ""))

    for listing in filter_us_listings(result.get("listings", [])):
        if require_new_listing and not listing.get("is_new_listing"):
            continue
        if (
            result.get("search_identifier_type") == "TITLE"
            and not title_listing_matches(
                str(result.get("cleaned_title") or ""),
                str(listing.get("listing_title") or ""),
            )
        ):
            continue

        evaluated = evaluate_winning_listing(
            listing,
            buybox_price=buybox_value,
            min_roi_percent=min_roi_percent,
            max_listing_age_days=max_listing_age_days,
            min_seller_reviews=min_seller_reviews,
            max_roi_percent=max_roi_percent,
        )
        if not evaluated.get("is_winner"):
            continue

        roi_percent = evaluated.get("roi_percent")
        winners.append(
            {
                "title": result.get("title", ""),
                "ASIN": result.get("asin", ""),
                "EAN": result.get("ean", ""),
                "Brand": result.get("brand", ""),
                "BUYBOX": result.get("buybox_price", ""),
                "AMAZON URL": result.get("amazon_url", ""),
                "EBAY full cost": evaluated.get("spent"),
                "SELLER": evaluated.get("seller_name", ""),
                "SELLER REVIEWS": evaluated.get("seller_reviews_count"),
                "LISTING DATE": (
                    evaluated.get("listing_date_iso")
                    or evaluated.get("date_listed", "")
                ),
                "ROI": None if roi_percent is None else round(roi_percent, 2),
                "EBAY listing URL": evaluated.get("listing_url", ""),
                "EBAY listing title": listing.get("listing_title") or "",
                "EBAY image URL": listing.get("image_url") or "",
                "ebay listing query": result.get("search_url", ""),
                "search_source": search_source,
            }
        )

    return winners


def verify_winners_amazon_sales_rank(
    page,
    winners: list[dict],
    *,
    amazon_url: str,
    min_sales_rank: int | None,
    max_sales_rank: int | None,
    cache: dict[str, int | None],
) -> list[dict]:
    if not winners or (min_sales_rank is None and max_sales_rank is None):
        return winners

    amazon_url = amazon_url.strip()
    if page is None or not amazon_url:
        print("  Skipping winners: Amazon sales rank could not be verified")
        return []

    if amazon_url not in cache:
        print("  Verifying Amazon sales rank once for this query")
        try:
            cache[amazon_url] = fetch_amazon_sales_rank(page, amazon_url)
        except Exception as error:
            cache[amazon_url] = None
            print(f"  Amazon lookup failed: {type(error).__name__}: {error}")
    else:
        print("  Using Amazon sales rank already verified for this product")

    sales_rank = cache[amazon_url]
    if sales_rank_qualified(
        sales_rank,
        min_sales_rank=min_sales_rank,
        max_sales_rank=max_sales_rank,
    ):
        print(f"  Amazon sales rank {sales_rank:,} meets criteria")
        return winners

    rank_label = "missing" if sales_rank is None else f"{sales_rank:,}"
    print(
        f"  Skipping {len(winners)} winners: Amazon sales rank {rank_label} "
        "does not meet criteria"
    )
    return []


def winner_listing_key(winner: dict) -> str:
    url = str(winner.get("EBAY listing URL") or "").strip()
    if not url:
        return ""

    parts = urlsplit(url)
    item_match = re.search(
        r"/itm/(?:[^/?#]+/)?(\d{9,})",
        parts.path,
        re.IGNORECASE,
    )
    if item_match:
        return f"ebay-item:{item_match.group(1)}"

    normalized_path = parts.path.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            normalized_path,
            "",
            "",
        )
    )


def persist_records(
    records: list[dict],
    persist: Callable[[list[dict]], None] | None,
) -> None:
    if persist is not None:
        persist(records)


def normalize_identifier_no_match_history(
    history: list[dict],
    retention_days: int,
) -> tuple[list[dict], set[str], set[str]]:
    retained: list[dict] = []
    active_keys: set[str] = set()
    pending_deletions: set[str] = set()
    seen_keys: set[str] = set()
    now = datetime.now()

    for record in history:
        key = identifier_query_key(
            str(record.get("query_type") or ""),
            record.get("identifier"),
        )
        timestamp = str(record.get("no_exact_match_at") or "")
        if not key or not timestamp:
            continue
        try:
            recorded_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if recorded_at.tzinfo is not None:
                recorded_at = recorded_at.replace(tzinfo=None)
        except ValueError:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)

        stored = record.copy()
        stored["consecutive_no_matches"] = max(
            1,
            int(record.get("consecutive_no_matches") or 1),
        )
        retained.append(stored)
        if stored["consecutive_no_matches"] >= 2:
            pending_deletions.add(key)
            active_keys.add(key)
        elif now < recorded_at + timedelta(days=retention_days):
            active_keys.add(key)

    return retained, active_keys, pending_deletions


def record_identifier_no_match(
    product: dict[str, str],
    *,
    history: list[dict],
    history_keys: set[str],
    retention_days: int,
    persist: Callable[[list[dict]], None] | None = None,
) -> bool:
    query_type = str(product.get("search_identifier_type") or "").upper()
    identifier = str(product.get("search_identifier") or "").strip()
    if query_type not in {"EAN", "UPC"}:
        return False
    key = identifier_query_key(query_type, identifier)
    if not key:
        return False

    history_keys.add(key)
    existing = next(
        (
            record
            for record in history
            if identifier_query_key(
                str(record.get("query_type") or ""),
                record.get("identifier"),
            )
            == key
        ),
        None,
    )
    if existing is None:
        existing = {
            "query_type": query_type,
            "identifier": identifier,
            "consecutive_no_matches": 0,
        }
        history.append(existing)

    consecutive = int(existing.get("consecutive_no_matches") or 0) + 1
    existing["consecutive_no_matches"] = consecutive
    existing["no_exact_match_at"] = datetime.now().isoformat(timespec="seconds")
    persist_records(history, persist)
    if consecutive >= 2:
        print(
            f"  {query_type} {identifier} returned no exact matches twice; "
            "scheduled for deletion"
        )
        return True

    print(
        f"  Cached {query_type} {identifier}: no exact matches for "
        f"{retention_days} days"
    )
    return False


def clear_identifier_no_match_streak(
    product: dict[str, str],
    *,
    history: list[dict],
    history_keys: set[str],
    persist: Callable[[list[dict]], None] | None = None,
) -> None:
    query_type = str(product.get("search_identifier_type") or "").upper()
    identifier = str(product.get("search_identifier") or "").strip()
    key = identifier_query_key(query_type, identifier)
    if query_type not in {"EAN", "UPC"} or not key:
        return

    retained = [
        record
        for record in history
        if identifier_query_key(
            str(record.get("query_type") or ""),
            record.get("identifier"),
        )
        != key
    ]
    if len(retained) == len(history):
        return
    history[:] = retained
    history_keys.discard(key)
    persist_records(history, persist)


def apply_identifier_deletions(
    deletion_keys: set[str],
    *,
    history: list[dict],
    history_keys: set[str],
    persist: Callable[[list[dict]], None] | None = None,
    identifier_deleter: Callable[[set[str]], int] | None = None,
) -> int:
    if not deletion_keys:
        return 0
    if identifier_deleter is None:
        raise ValueError("Cannot delete identifiers without a store")
    changed = identifier_deleter(deletion_keys)
    history[:] = [
        record
        for record in history
        if identifier_query_key(
            str(record.get("query_type") or ""),
            record.get("identifier"),
        )
        not in deletion_keys
    ]
    history_keys.difference_update(deletion_keys)
    persist_records(history, persist)
    deletion_keys.clear()
    return changed


def normalize_winner_history(
    history: list[dict],
    *,
    retention_days: int | None,
) -> tuple[list[dict], set[str], int]:
    history_keys: set[str] = set()
    deduplicated_history: list[dict] = []
    pruned = 0
    now = datetime.now()
    cutoff = (
        now - timedelta(days=retention_days)
        if retention_days is not None
        else None
    )

    for record in history:
        first_won_at = str(record.get("first_won_at") or "")
        if cutoff is not None and first_won_at:
            try:
                won_at = datetime.fromisoformat(first_won_at.replace("Z", "+00:00"))
                if won_at.tzinfo is not None:
                    won_at = won_at.replace(tzinfo=None)
                if won_at < cutoff:
                    pruned += 1
                    continue
            except ValueError:
                pass

        key = str(record.get("history_key") or winner_listing_key(record))
        if not key or key in history_keys:
            continue
        history_keys.add(key)
        stored = record.copy()
        stored["history_key"] = key
        if not first_won_at:
            stored["first_won_at"] = now.isoformat(timespec="seconds")
        deduplicated_history.append(stored)

    return deduplicated_history, history_keys, pruned


def persist_new_winners(
    candidates: list[dict],
    *,
    winners: list[dict],
    session_keys: set[str],
    history: list[dict],
    history_keys: set[str],
    skip_previously_won: bool,
    store: object | None = None,
) -> tuple[int, int]:
    accepted: list[dict] = []
    skipped = 0

    for winner in candidates:
        key = winner_listing_key(winner)
        if key and key in session_keys:
            skipped += 1
            continue
        if key and skip_previously_won and key in history_keys:
            skipped += 1
            continue

        accepted.append(winner)
        if key:
            session_keys.add(key)
        if key and key not in history_keys:
            history_keys.add(key)
            stored = winner.copy()
            stored["history_key"] = key
            stored["first_won_at"] = datetime.now().isoformat(timespec="seconds")
            history.append(stored)

    if accepted:
        winners.extend(accepted)
        if store is not None:
            append_winners = getattr(store, "append_winners", None)
            if append_winners is not None:
                append_winners(accepted)
            save_live = getattr(store, "save_live_winners", None)
            if save_live is not None:
                save_live(winners)
    return len(accepted), skipped


def query_label(product: dict[str, str]) -> tuple[str, str]:
    query_type = str(product.get("search_identifier_type") or "EAN")
    identifier = (
        product.get("search_identifier")
        or product.get("ean")
        or product.get("asin")
        or "(unknown)"
    )
    return query_type, str(identifier)


def scrape_product_result(page, product: dict[str, str]) -> dict:
    result = scrape_search_page(
        page,
        product["search_url"],
        title=product["title"],
        asin=product["asin"],
        ean=product["ean"],
        buybox_price=product["buybox_price"],
    )

    result["amazon_url"] = product["amazon_url"]
    result["brand"] = product["brand"]
    result["search_identifier_type"] = product.get("search_identifier_type", "")
    result["cleaned_title"] = product.get("cleaned_title", "")
    return result


def identifier_search_source(query_type: object) -> str:
    normalized = str(query_type or "").strip().upper()
    if normalized in {"EAN", "UPC"}:
        return "upc_ean"
    if normalized:
        return normalized.lower()
    return "upc_ean"


def collect_image_search_winners(
    page,
    product: dict[str, str],
    result: dict,
    *,
    settings: ScrapeSettings,
    seller_review_cache: dict[str, tuple[str, int | None]] | None,
    amazon_rank_cache: dict[str, int | None],
    image_search_cache: dict[str, dict] | None = None,
) -> list[dict]:
    listings = result.get("listings") or []
    if not listings:
        print("  Skipping image search: no identifier listings to take a photo from")
        return []

    print("  Running image search from the first listing photo")
    image_url = first_listing_image_url(page, listings)
    print(f"  Image search photo: {image_url}")
    cache = image_search_cache if image_search_cache is not None else {}
    cached = cache.get(image_url)
    if cached is not None:
        print("  Reusing image search results for this photo")
        image_result = {
            "title": product["title"],
            "asin": product["asin"],
            "ean": product["ean"],
            "buybox_price": product["buybox_price"],
            "buybox_price_value": result.get("buybox_price_value"),
            "search_source": "image_search",
            "search_url": cached.get("search_url") or "",
            "final_url": cached.get("final_url") or "",
            "listings": [dict(item) for item in cached.get("listings") or []],
        }
    else:
        image_result = scrape_image_search_page(
            page,
            image_url,
            title=product["title"],
            asin=product["asin"],
            ean=product["ean"],
            buybox_price=product["buybox_price"],
        )
        if not image_result.get("error") and not image_result.get("block_reason"):
            cache[image_url] = {
                "search_url": image_result.get("search_url") or "",
                "final_url": image_result.get("final_url") or "",
                "listings": [dict(item) for item in image_result.get("listings") or []],
            }
    image_result["amazon_url"] = product["amazon_url"]
    image_result["brand"] = product["brand"]
    image_result["search_identifier_type"] = "IMAGE"
    print(f"  Image search URL: {image_result.get('final_url') or image_result.get('search_url') or ''}")

    image_listings = image_result.get("listings") or []
    new_listings = [
        listing for listing in image_listings if listing.get("is_new_listing")
    ]
    print(
        f"  Image search listings: {len(image_listings)} "
        f"({len(new_listings)} with New Listing tag)"
    )
    image_result_for_winners = dict(image_result)
    image_result_for_winners["listings"] = new_listings

    winner_kw = settings.winner_kwargs()
    us_image_listings = filter_us_listings(new_listings)
    fill_missing_seller_reviews(
        page,
        us_image_listings,
        buybox_price=image_result.get("buybox_price_value"),
        cache=seller_review_cache,
        **winner_kw,
    )
    image_winners = extract_winners_from_result(
        image_result_for_winners,
        search_source="image_search",
        require_new_listing=True,
        **winner_kw,
    )
    image_winners = verify_winners_amazon_sales_rank(
        page,
        image_winners,
        amazon_url=str(product.get("amazon_url") or result.get("amazon_url") or ""),
        min_sales_rank=settings.min_sales_rank,
        max_sales_rank=settings.max_sales_rank,
        cache=amazon_rank_cache,
    )
    winner_status = f"yes ({len(image_winners)})" if image_winners else "no"
    print(f"  Image search winners: {winner_status}")
    print(f"  {format_cheapest_listing_log(image_result_for_winners, **winner_kw)}")
    for line in format_below_buybox_listing_logs(image_result_for_winners, **winner_kw):
        print(line)
    block_log = format_block_log(image_result)
    if block_log:
        print(f"  Image search {block_log}")
    return image_winners


def process_product(
    page,
    product: dict[str, str],
    *,
    index: int,
    total: int,
    settings: ScrapeSettings,
    no_exact_match_callback: Callable[[dict[str, str]], None] | None = None,
    exact_match_callback: Callable[[dict[str, str]], None] | None = None,
    seller_review_cache: dict[str, tuple[str, int | None]] | None = None,
    amazon_rank_cache: dict[str, int | None] | None = None,
    image_search_cache: dict[str, dict] | None = None,
) -> list[dict]:
    identifier_type, identifier = query_label(product)
    result = scrape_product_result(page, product)
    query_type = str(product.get("search_identifier_type") or "").upper()
    if query_type in {"EAN", "UPC"}:
        if result.get("error") or result.get("block_reason"):
            raise RuntimeError(
                result.get("block_reason") or result.get("error") or "search error"
            )
        elif not result.get("listings"):
            if no_exact_match_callback is not None:
                no_exact_match_callback(product)
        elif exact_match_callback is not None:
            exact_match_callback(product)

    us_listings = filter_us_listings(result.get("listings", []))
    winner_kw = settings.winner_kwargs()
    rank_cache = amazon_rank_cache if amazon_rank_cache is not None else {}
    fill_missing_seller_reviews(
        page,
        us_listings,
        buybox_price=result.get("buybox_price_value"),
        cache=seller_review_cache,
        **winner_kw,
    )
    product_winners = extract_winners_from_result(
        result,
        search_source=identifier_search_source(query_type),
        **winner_kw,
    )
    product_winners = verify_winners_amazon_sales_rank(
        page,
        product_winners,
        amazon_url=str(product.get("amazon_url") or result.get("amazon_url") or ""),
        min_sales_rank=settings.min_sales_rank,
        max_sales_rank=settings.max_sales_rank,
        cache=rank_cache,
    )

    winner_status = f"yes ({len(product_winners)})" if product_winners else "no"
    print(f"[{index}/{total}] {identifier_type} {identifier} - winners: {winner_status}")
    print(f"  US listings found: {len(us_listings)}")
    print(f"  {format_cheapest_listing_log(result, **winner_kw)}")
    for line in format_below_buybox_listing_logs(result, **winner_kw):
        print(line)
    block_log = format_block_log(result)
    if block_log:
        print(f"  {block_log}")

    if (
        settings.image_search
        and is_playwright_page(page)
        and query_type in {"EAN", "UPC"}
        and result.get("listings")
        and not result.get("block_reason")
        and not result.get("error")
    ):
        try:
            product_winners.extend(
                collect_image_search_winners(
                    page,
                    product,
                    result,
                    settings=settings,
                    seller_review_cache=seller_review_cache,
                    amazon_rank_cache=rank_cache,
                    image_search_cache=image_search_cache,
                )
            )
        except Exception as error:
            log_page_debug(reason="image search failed", error=error, page=page)
            raise
    elif (
        settings.image_search
        and not is_playwright_page(page)
        and query_type in {"EAN", "UPC"}
        and result.get("listings")
    ):
        print("  Skipping image search: production HTTP scraper has no browser UI")

    return product_winners


def process_live_product_with_ship_to_retry(
    page,
    product: dict[str, str],
    **kwargs,
) -> list[dict]:
    try:
        return process_product(page, product, **kwargs)
    except EbayShipToNotUsError as error:
        identifier_type, identifier = query_label(product)
        print(f"  Ship to is not US for {identifier_type} {identifier}: {error}")
        log_page_debug(
            reason="ship-to not US before refresh",
            error=error,
            page=page,
        )
        print("  Refreshing and retrying this search")
        refresh_and_verify_ship_to_us(page)
        return process_product(page, product, **kwargs)


def _store_method(store: object | None, name: str):
    if store is None:
        return None
    method = getattr(store, name, None)
    return method if callable(method) else None


def main(settings: ScrapeSettings | None = None) -> int:
    settings = settings or ScrapeSettings()
    try:
        settings.validate()
    except ValueError as error:
        print(f"Error: {error}")
        return 1

    store = settings.store
    if store is None:
        print("Error: a store is required; run the scrape from the GEEFLIP website")
        return 1

    persist_no_match = _store_method(store, "save_identifier_no_match")
    identifier_deleter = _store_method(store, "delete_identifiers")
    products_loader = _store_method(store, "load_products")

    try:
        raw_no_match = _store_method(store, "load_identifier_no_match")() or []
        (
            identifier_no_match_history,
            identifier_no_match_keys,
            pending_identifier_deletions,
        ) = normalize_identifier_no_match_history(
            raw_no_match,
            settings.identifier_no_match_retention_days,
        )
        if identifier_no_match_history != raw_no_match:
            persist_records(identifier_no_match_history, persist_no_match)
    except Exception as error:
        print(f"Error loading identifier no-match history: {error}")
        return 1

    if pending_identifier_deletions:
        try:
            deleted_cells = apply_identifier_deletions(
                pending_identifier_deletions,
                history=identifier_no_match_history,
                history_keys=identifier_no_match_keys,
                persist=persist_no_match,
                identifier_deleter=identifier_deleter,
            )
            print(
                f"Deleted {deleted_cells} EAN/UPC codes after repeated "
                "no-match results"
            )
        except Exception as error:
            print(f"Error applying pending identifier deletions: {error}")
            return 1
    if identifier_no_match_keys:
        print(
            f"Skipping {len(identifier_no_match_keys)} EAN/UPC queries "
            "with recent no-match results"
        )

    try:
        products, selection_details = select_products(
            products_loader(),
            settings,
            skipped_identifier_keys=identifier_no_match_keys,
        )
    except ValueError as error:
        print(f"Error: {error}")
        return 1

    if not products:
        print("Error: no catalog rows matched these filters")
        return 1

    print(selection_details)

    winners: list[dict] = []
    session_winner_keys: set[str] = set()
    seller_review_cache: dict[str, tuple[str, int | None]] = {}
    amazon_rank_cache: dict[str, int | None] = {}
    image_search_cache: dict[str, dict] = {}
    total = len(products)
    exit_code = 0
    halted_by_error = False
    skipped_previous_winners = 0
    current_product: dict[str, str] | None = None
    winner_history: list[dict] = []
    winner_history_keys: set[str] = set()
    stop_reason: str | None = None

    def requested_stop() -> bool:
        return bool(settings.should_stop and settings.should_stop())

    def log_current_stop(reason: str) -> None:
        product = current_product or (products[0] if products else None)
        row = None if product is None else product.get("csv_row")
        print(f"Stopped at catalog row {row or 'unknown'} — {reason}")
        if row:
            print(f"Resume with start row {row}")

    def cache_identifier_no_match(product: dict[str, str]) -> None:
        should_delete = record_identifier_no_match(
            product,
            history=identifier_no_match_history,
            history_keys=identifier_no_match_keys,
            retention_days=settings.identifier_no_match_retention_days,
            persist=persist_no_match,
        )
        if should_delete:
            pending_identifier_deletions.add(
                identifier_query_key(
                    str(product.get("search_identifier_type") or ""),
                    product.get("search_identifier"),
                )
            )

    def clear_identifier_streak(product: dict[str, str]) -> None:
        clear_identifier_no_match_streak(
            product,
            history=identifier_no_match_history,
            history_keys=identifier_no_match_keys,
            persist=persist_no_match,
        )

    def keep_winners(product_winners: list[dict]) -> None:
        nonlocal skipped_previous_winners
        if not product_winners:
            return
        _accepted, skipped = persist_new_winners(
            product_winners,
            winners=winners,
            session_keys=session_winner_keys,
            history=winner_history,
            history_keys=winner_history_keys,
            skip_previously_won=settings.skip_previously_won,
            store=store,
        )
        skipped_previous_winners += skipped

    def process_kwargs(index: int, total_count: int) -> dict:
        return {
            "index": index,
            "total": total_count,
            "settings": settings,
            "seller_review_cache": seller_review_cache,
            "amazon_rank_cache": amazon_rank_cache,
            "image_search_cache": image_search_cache,
            "no_exact_match_callback": cache_identifier_no_match,
            "exact_match_callback": clear_identifier_streak,
        }

    try:
        stored_history = _store_method(store, "load_winner_history")() or []
        winner_history, winner_history_keys, pruned_history = normalize_winner_history(
            stored_history,
            retention_days=settings.winner_history_retention_days,
        )
        save_history = _store_method(store, "save_winner_history")
        if pruned_history and save_history is not None:
            save_history(winner_history)
    except Exception as error:
        print(f"Error loading winner history: {error}")
        return 1

    if pruned_history:
        print(
            f"{pruned_history} listings are older than "
            f"{settings.winner_history_retention_days} days and can win again"
        )
    print(
        f"Winner history: {len(winner_history_keys)} listings in the dedup window "
        f"({'skipping repeats' if settings.skip_previously_won else 'tracking only'})"
    )

    try:
        save_live = _store_method(store, "save_live_winners")
        if save_live is not None:
            save_live(winners)
        cookies = load_ebay_cookies(
            cookie_header=settings.cookie_header,
            cookies_file=settings.cookies_file,
            default_cookies_file=EBAY_COOKIES_FILE,
        )
        browser_options = settings.browser
        restart_every = max(0, int(browser_options.restart_every))
        print(f"Scraping {total} eBay search URLs with Playwright")
        print(f"Browser: {browser_options.describe()}")
        print(f"eBay session: {describe_ebay_cookie_session(cookies)}")
        with browser_session(cookies=cookies, options=browser_options) as session:
            skipped_previous_ean_for_ship_to = False
            searches_since_browser_start = 0
            for index, product in enumerate(products, start=1):
                if requested_stop():
                    stop_reason = "stopped from website"
                    break
                current_product = product
                if restart_every and searches_since_browser_start >= restart_every:
                    print(
                        f"Restarting browser after {index - 1} searches "
                        "to free memory"
                    )
                    session.restart()
                    searches_since_browser_start = 0
                searches_since_browser_start += 1
                display_index = int(product.get("selection_position", index))
                display_total = int(product.get("selection_total", total))
                query_type = str(product.get("search_identifier_type") or "").upper()
                query_key = identifier_query_key(
                    query_type,
                    product.get("search_identifier"),
                )
                if query_type in {"EAN", "UPC"} and query_key in identifier_no_match_keys:
                    print(
                        f"[{display_index}/{display_total}] Skipping "
                        f"{query_type} {product.get('search_identifier')}: "
                        "recently had no exact matches"
                    )
                    continue
                try:
                    product_winners = process_live_product_with_ship_to_retry(
                        session.page,
                        product,
                        **process_kwargs(display_index, display_total),
                    )
                except EbayShipToNotUsError as error:
                    ean = product.get("ean") or product.get("asin") or "(unknown)"
                    if skipped_previous_ean_for_ship_to:
                        raise EbayShipToNotUsError(
                            "consecutive_not_us",
                            detail=(
                                f"EAN {ean} was still not US after refresh; "
                                "the previous EAN was also skipped"
                            ),
                        ) from error
                    print(
                        f"  Skipping EAN {ean}: Ship to is still not US after refresh"
                    )
                    skipped_previous_ean_for_ship_to = True
                    continue

                skipped_previous_ean_for_ship_to = False
                keep_winners(product_winners)
        if stop_reason:
            print("Scrape stopped from the website; saving winners collected so far")
            log_current_stop(stop_reason)
            exit_code = 130
    except EbayShipToNotUsError as error:
        halted_by_error = True
        print(f"Stopping scrape: {error}")
        log_current_stop(str(error))
        exit_code = 1
    except KeyboardInterrupt:
        halted_by_error = True
        print("Scrape stopped by user; saving winners collected so far")
        log_current_stop("stopped by user")
        exit_code = 130
    except Exception as error:
        halted_by_error = True
        print(f"Scrape stopped by {type(error).__name__}: {error}")
        log_current_stop(f"{type(error).__name__}: {error}")
        exit_code = 1

    if pending_identifier_deletions and not halted_by_error:
        try:
            deleted_cells = apply_identifier_deletions(
                pending_identifier_deletions,
                history=identifier_no_match_history,
                history_keys=identifier_no_match_keys,
                persist=persist_no_match,
                identifier_deleter=identifier_deleter,
            )
            print(
                f"Deleted {deleted_cells} EAN/UPC codes after two "
                "consecutive no-match results"
            )
        except Exception as error:
            print(f"Error deleting repeated no-match identifiers: {error}")
            exit_code = 1
    elif pending_identifier_deletions:
        print("Not deleting EAN/UPC codes because the scrape stopped on an error")

    print(f"Found {len(winners)} winning listings")
    if skipped_previous_winners:
        print(
            f"Skipped {skipped_previous_winners} previously won "
            "or duplicate listings"
        )
    return exit_code

