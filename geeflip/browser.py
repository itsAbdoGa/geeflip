"""Chromium tuning knobs, stored per install and surfaced on the admin page.

The scraper drives a real browser, which is the heaviest thing this project
does. On a weak machine the difference between the defaults and a stripped
down window is the difference between a scrape finishing and the PC crawling,
so every one of these is editable from the website instead of being a constant
buried in the scraper.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
EBAY_ROOT = PACKAGE_ROOT.parent / "ebay"
for entry in (str(PACKAGE_ROOT), str(EBAY_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from lib.ebay_scraper import BrowserOptions

SETTING_KEY = "browser"

DEFAULT_BROWSER: dict = {
    "headless": True,
    "use_installed_chrome": True,
    "window_width": 1440,
    "window_height": 900,
    "disable_gpu": False,
    "low_memory": False,
    "block_images": False,
    "block_fonts": False,
    "block_styles": False,
    "restart_every": 1000,
    "restart_pause_seconds": 1.5,
    "page_timeout_ms": 25_000,
    "results_timeout_ms": 4_000,
    "extra_args": [],
}

# Starting points the admin page offers as one-click buttons. Each one is a
# full set of values so picking a preset never leaves a stale field behind.
PRESETS: dict[str, dict] = {
    "balanced": {
        "label": "Balanced",
        "hint": "Default. Hidden window, normal rendering, restarts rarely.",
        "values": dict(DEFAULT_BROWSER),
    },
    "low_end": {
        "label": "Low-end PC",
        "hint": (
            "Small hidden window, no GPU, images and fonts never download, "
            "and the browser restarts often so memory cannot pile up."
        ),
        "values": {
            **DEFAULT_BROWSER,
            "headless": True,
            "window_width": 1024,
            "window_height": 720,
            "disable_gpu": True,
            "low_memory": True,
            "block_images": True,
            "block_fonts": True,
            "restart_every": 150,
            "restart_pause_seconds": 2.5,
            "page_timeout_ms": 40_000,
            "results_timeout_ms": 8_000,
        },
    },
    "watch": {
        "label": "Watch the browser",
        "hint": (
            "Shows a real Chrome window so you can see what the scraper sees. "
            "Heaviest option — use it to debug, not for long runs."
        ),
        "values": {
            **DEFAULT_BROWSER,
            "headless": False,
            "block_images": False,
            "block_fonts": False,
            "block_styles": False,
        },
    },
}

_FLAGS = (
    "headless",
    "use_installed_chrome",
    "disable_gpu",
    "low_memory",
    "block_images",
    "block_fonts",
    "block_styles",
)
# key -> (minimum, maximum)
_INT_BOUNDS = {
    "window_width": (320, 3840),
    "window_height": (240, 2160),
    "restart_every": (0, 100_000),
    "page_timeout_ms": (5_000, 180_000),
    "results_timeout_ms": (500, 60_000),
}
_FLOAT_BOUNDS = {"restart_pause_seconds": (0.0, 60.0)}


def _clamp(value, low, high, fallback):
    try:
        number = type(low)(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _extra_args(value: object) -> list[str]:
    """Accept a textarea or a list, keep only things that look like flags."""
    if isinstance(value, str):
        parts = value.replace(",", "\n").split()
    elif value:
        parts = [str(part) for part in value]
    else:
        parts = []
    cleaned = [part.strip() for part in parts if part.strip().startswith("--")]
    return list(dict.fromkeys(cleaned))


def coerce_browser(payload: dict | None) -> dict:
    """Normalise whatever the admin page posted into a safe settings dict."""
    data = dict(DEFAULT_BROWSER)
    data.update(payload or {})

    for key in _FLAGS:
        data[key] = bool(data.get(key))
    for key, (low, high) in _INT_BOUNDS.items():
        data[key] = _clamp(data.get(key), low, high, DEFAULT_BROWSER[key])
    for key, (low, high) in _FLOAT_BOUNDS.items():
        data[key] = round(_clamp(data.get(key), low, high, DEFAULT_BROWSER[key]), 2)
    data["extra_args"] = _extra_args(data.get("extra_args"))

    # Blocking stylesheets can hide the result cards entirely; only allow it
    # when images are already blocked, which is the low-end case it belongs to.
    if data["block_styles"] and not data["block_images"]:
        data["block_styles"] = False

    return {key: data[key] for key in DEFAULT_BROWSER}


def load_browser(store) -> dict:
    return coerce_browser(store.load_json(SETTING_KEY, DEFAULT_BROWSER))


def save_browser(store, payload: dict | None) -> dict:
    settings = coerce_browser(payload)
    store.save_json(SETTING_KEY, settings)
    return settings


def build_options(settings: dict | None) -> BrowserOptions:
    data = coerce_browser(settings)
    return BrowserOptions(
        headless=data["headless"],
        use_installed_chrome=data["use_installed_chrome"],
        window_width=data["window_width"],
        window_height=data["window_height"],
        disable_gpu=data["disable_gpu"],
        low_memory=data["low_memory"],
        block_images=data["block_images"],
        block_fonts=data["block_fonts"],
        block_styles=data["block_styles"],
        restart_every=data["restart_every"],
        restart_pause_seconds=data["restart_pause_seconds"],
        page_timeout_ms=data["page_timeout_ms"],
        results_timeout_ms=data["results_timeout_ms"],
        extra_args=tuple(data["extra_args"]),
    )


def describe(settings: dict | None) -> str:
    return build_options(settings).describe()
