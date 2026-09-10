"""Filesystem locations shared by the GEEFLIP V2 website."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

DATA_DIR = PACKAGE_ROOT / "data"
DB_PATH = DATA_DIR / "geeflip.db"
COOKIE_FILE = DATA_DIR / "ebay_cookies.txt"

CATALOG_XLSX = PROJECT_ROOT / "clean" / "10krows with List 1 6-25.xlsx"
KEEPA_XLSX = PROJECT_ROOT / "clean" / "combined_keepa.xlsx"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
