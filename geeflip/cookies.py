from __future__ import annotations

from pathlib import Path

GEEFLIP_ROOT = Path(__file__).resolve().parent
COOKIE_FILE = GEEFLIP_ROOT / "data" / "ebay_cookies.txt"
EBAY_COOKIE_FILE = GEEFLIP_ROOT.parent / "ebay" / "data" / "input" / "ebay_cookies.txt"

# Guest location / ship-to only. Session, bot, and tracking cookies from a
# personal PC confuse eBay when the scraper runs on another machine.
LOCATION_COOKIE_NAMES = ("dp1", "nonsession", "ns1", "ebay", "zip")
LOCATION_COOKIE_NAME_SET = {name.casefold() for name in LOCATION_COOKIE_NAMES}


def keep_location_cookie_header(text: str) -> str:
    by_name: dict[str, str] = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name.casefold() in LOCATION_COOKIE_NAME_SET:
            by_name[name.casefold()] = f"{name}={value}"
    return "; ".join(
        by_name[name] for name in LOCATION_COOKIE_NAMES if name in by_name
    )


def cookie_paths() -> list[Path]:
    return [COOKIE_FILE, EBAY_COOKIE_FILE]


def read_cookie() -> str:
    for path in cookie_paths():
        if path.exists():
            text = keep_location_cookie_header(path.read_text(encoding="utf-8"))
            if text:
                return text
    return ""


def write_cookie(text: str) -> Path:
    cleaned = keep_location_cookie_header(text)
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{cleaned}\n" if cleaned else ""
    COOKIE_FILE.write_text(payload, encoding="utf-8")
    if EBAY_COOKIE_FILE.parent.exists():
        EBAY_COOKIE_FILE.write_text(payload, encoding="utf-8")
    return COOKIE_FILE


def cookie_status() -> dict:
    text = read_cookie()
    names = []
    for part in text.split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return {
        "present": bool(text),
        "characters": len(text),
        "cookie_names": names,
        "kept": list(LOCATION_COOKIE_NAMES),
        "path": str(COOKIE_FILE),
    }
