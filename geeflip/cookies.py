from __future__ import annotations

import base64
import re
from pathlib import Path

GEEFLIP_ROOT = Path(__file__).resolve().parent
COOKIE_FILE = GEEFLIP_ROOT / "data" / "ebay_cookies.txt"
EBAY_COOKIE_FILE = GEEFLIP_ROOT.parent / "ebay" / "data" / "input" / "ebay_cookies.txt"

# Guest location / ship-to only. Session, bot, and tracking cookies from a
# personal PC confuse eBay when the scraper runs on another machine.
LOCATION_COOKIE_NAMES = ("dp1", "nonsession", "ns1", "ebay", "zip")
LOCATION_COOKIE_NAME_SET = {name.casefold() for name in LOCATION_COOKIE_NAMES}
DEFAULT_SHIP_ZIP = "73072"
DEFAULT_SHIP_COUNTRY = "USA"
_ZIP_COUNTRY_RE = re.compile(r"^\d{3,10},[A-Z]{2,3}$")


def _zip_country_blob(zip_code: str, country: str) -> str:
    return (
        base64.b64encode(f"{zip_code},{country}".encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )


def replace_nonsession_zip(
    nonsession: str,
    zip_code: str = DEFAULT_SHIP_ZIP,
    country: str = DEFAULT_SHIP_COUNTRY,
) -> str:
    new_blob = _zip_country_blob(zip_code, country)
    if new_blob in nonsession:
        return nonsession
    target = f"{zip_code},{country}"
    for start in range(0, len(nonsession) - 12 + 1):
        blob = nonsession[start : start + 12]
        try:
            decoded = base64.b64decode(blob).decode("ascii")
        except Exception:
            continue
        if not _ZIP_COUNTRY_RE.fullmatch(decoded):
            continue
        if decoded == target:
            return nonsession
        return nonsession[:start] + new_blob + nonsession[start + 12 :]
    return nonsession


def strip_dp1_identity(value: str) -> str:
    """Drop personal identity fields so a datacenter IP is not tied to a login."""
    parts = []
    for part in (value or "").split("^"):
        if not part:
            continue
        lowered = part.casefold()
        if lowered.startswith("bu1p/") or lowered.startswith("u1f/"):
            continue
        parts.append(part)
    result = "^".join(parts)
    if value.endswith("^") and result:
        result += "^"
    return result


def rewrite_dp1_for_us(value: str) -> str:
    value = strip_dp1_identity(value)
    return re.sub(r"(^|\^)bl/[A-Za-z]{2}", r"\1bl/US", value)


def keep_location_cookie_header(text: str) -> str:
    by_name: dict[str, str] = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        lowered = name.casefold()
        if lowered not in LOCATION_COOKIE_NAME_SET:
            continue
        if lowered == "dp1":
            value = rewrite_dp1_for_us(value)
        elif lowered == "nonsession":
            value = replace_nonsession_zip(value)
        elif lowered == "zip":
            value = DEFAULT_SHIP_ZIP
        by_name[lowered] = f"{name}={value}"
    if "zip" not in by_name and by_name:
        by_name["zip"] = f"zip={DEFAULT_SHIP_ZIP}"
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
        "zip": DEFAULT_SHIP_ZIP,
        "country": DEFAULT_SHIP_COUNTRY,
        "path": str(COOKIE_FILE),
    }
