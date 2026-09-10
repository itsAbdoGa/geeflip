"""Scrape filter defaults and the translation into the scraper's settings."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
EBAY_ROOT = PACKAGE_ROOT.parent / "ebay"
for entry in (str(PACKAGE_ROOT), str(EBAY_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.scrape_listings import ScrapeSettings

from paths import COOKIE_FILE

DEFAULT_FILTERS: dict = {
    "start_row": None,
    "end_row": None,
    "limit": None,
    "include_brands": [],
    "exclude_brands": [],
    "min_roi_percent": 80.0,
    "max_roi_percent": 300.0,
    "max_listing_age_days": 2,
    "min_seller_reviews": 40,
    "min_sales_rank": None,
    "max_sales_rank": 500_000,
    "min_drop_count": None,
    "min_buybox_price": 40.0,
    "upc_as_well": True,
    "cleaned_title_as_well": False,
    "titles_only": False,
    "image_search": False,
    "skip_previously_won": True,
    "winner_history_retention_days": 3,
    "identifier_no_match_retention_days": 3,
}

_OPTIONAL_INTS = (
    "start_row",
    "end_row",
    "limit",
    "max_listing_age_days",
    "min_seller_reviews",
    "min_sales_rank",
    "max_sales_rank",
    "min_drop_count",
    "winner_history_retention_days",
    "identifier_no_match_retention_days",
)
_OPTIONAL_FLOATS = ("min_roi_percent", "max_roi_percent", "min_buybox_price")
_FLAGS = (
    "upc_as_well",
    "cleaned_title_as_well",
    "titles_only",
    "image_search",
    "skip_previously_won",
)
_REQUIRED = (
    "min_roi_percent",
    "max_listing_age_days",
    "min_seller_reviews",
    "winner_history_retention_days",
    "identifier_no_match_retention_days",
)


def _brands(value: object) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    elif value:
        parts = [str(part) for part in value]
    else:
        parts = []
    return [part.strip() for part in parts if part.strip()]


def coerce_filters(payload: dict | None) -> dict:
    """Normalise whatever the browser posted into a clean filter dict."""
    data = dict(DEFAULT_FILTERS)
    data.update(payload or {})

    for key in _OPTIONAL_INTS:
        value = data.get(key)
        data[key] = None if value in (None, "") else int(value)
    for key in _OPTIONAL_FLOATS:
        value = data.get(key)
        data[key] = None if value in (None, "") else float(value)
    for key in _FLAGS:
        data[key] = bool(data.get(key))
    for key in ("include_brands", "exclude_brands"):
        data[key] = _brands(data.get(key))

    # These four have no sensible "off" state, so an empty box falls back.
    for key in _REQUIRED:
        if data[key] is None:
            data[key] = DEFAULT_FILTERS[key]

    return data


def build_settings(filters: dict, store, *, should_stop=None) -> ScrapeSettings:
    data = coerce_filters(filters)
    return ScrapeSettings(
        start_row=data["start_row"],
        end_row=data["end_row"],
        limit=data["limit"],
        include_brands=tuple(data["include_brands"]),
        exclude_brands=tuple(data["exclude_brands"]),
        min_roi_percent=data["min_roi_percent"],
        max_roi_percent=data["max_roi_percent"],
        max_listing_age_days=data["max_listing_age_days"],
        min_seller_reviews=data["min_seller_reviews"],
        min_sales_rank=data["min_sales_rank"],
        max_sales_rank=data["max_sales_rank"],
        min_drop_count=data["min_drop_count"],
        min_buybox_price=data["min_buybox_price"],
        upc_as_well=data["upc_as_well"],
        cleaned_title_as_well=data["cleaned_title_as_well"],
        titles_only=data["titles_only"],
        image_search=data["image_search"],
        skip_previously_won=data["skip_previously_won"],
        winner_history_retention_days=data["winner_history_retention_days"],
        identifier_no_match_retention_days=data["identifier_no_match_retention_days"],
        cookies_file=COOKIE_FILE,
        headless=True,
        should_stop=should_stop,
        store=store,
    )
