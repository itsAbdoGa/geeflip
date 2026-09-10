"""Scratch locations the Playwright scraper needs on disk."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parent

DATA_DIR = ROOT / "data"
INPUT_DIR = DATA_DIR / "input"

EBAY_COOKIES_FILE = INPUT_DIR / "ebay_cookies.txt"
PLAYWRIGHT_STORAGE_STATE = INPUT_DIR / "playwright_storage_state.json"
EBAY_SOURCE_HTML = INPUT_DIR / "ebay_source.html"


def ensure_data_dirs() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
