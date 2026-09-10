"""The eBay cookie header used by the scraper, editable from the website."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from paths import COOKIE_FILE, ensure_data_dir


def read_cookie() -> str:
    if not COOKIE_FILE.exists():
        return ""
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def write_cookie(text: str) -> None:
    ensure_data_dir()
    cleaned = text.strip()
    COOKIE_FILE.write_text(f"{cleaned}\n" if cleaned else "", encoding="utf-8")


def cookie_status() -> dict:
    text = read_cookie()
    names = [
        part.split("=", 1)[0].strip()
        for part in text.split(";")
        if part.split("=", 1)[0].strip()
    ]
    return {
        "present": bool(text),
        "characters": len(text),
        "names": names[:12],
        "path": str(COOKIE_FILE),
    }
