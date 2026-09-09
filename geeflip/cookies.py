from __future__ import annotations

from pathlib import Path

GEEFLIP_ROOT = Path(__file__).resolve().parent
COOKIE_FILE = GEEFLIP_ROOT / "data" / "ebay_cookies.txt"
EBAY_COOKIE_FILE = GEEFLIP_ROOT.parent / "ebay" / "data" / "input" / "ebay_cookies.txt"


def cookie_paths() -> list[Path]:
    return [COOKIE_FILE, EBAY_COOKIE_FILE]


def read_cookie() -> str:
    for path in cookie_paths():
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    return ""


def write_cookie(text: str) -> Path:
    cleaned = text.strip()
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EBAY_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{cleaned}\n" if cleaned else ""
    COOKIE_FILE.write_text(payload, encoding="utf-8")
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
        "cookie_names": names[:12],
        "path": str(COOKIE_FILE),
    }
