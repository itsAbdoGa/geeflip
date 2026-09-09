"""eBay search fetches with requests — used on production.

Mirrors the working HTTP scraper: keep-alive session, browser headers,
homepage warmup, then the search URL with a same-origin Referer. Proxy is
tried first when configured, then direct.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests
from requests import Response
from requests.exceptions import RequestException, Timeout
from selectolax.parser import HTMLParser

from lib.paths import INPUT_DIR, ensure_data_dirs

REQUEST_TIMEOUT = (20, 120)
EBAY_HOME_URL = "https://www.ebay.com/"
REQUESTS_COOKIES_FILE = INPUT_DIR / "requests_cookies.json"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class HttpFetchResult:
    url: str
    final_url: str
    status_code: int
    html: str


class EbayHttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        status_code: int = 0,
        final_url: str = "",
        html: str = "",
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.final_url = final_url or url
        self.html = html


def ebay_proxy_mode() -> str:
    return os.getenv("EBAY_PROXY_MODE", "auto").strip().lower() or "auto"


def ebay_fetch_attempts() -> int:
    try:
        return max(1, int(os.getenv("EBAY_FETCH_ATTEMPTS", "1")))
    except ValueError:
        return 1


def _proxy_url() -> str | None:
    explicit = (
        os.getenv("EBAY_PROXY_URL")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or ""
    ).strip()
    if explicit:
        return explicit

    host = (os.getenv("PROXY_HOST") or os.getenv("EBAY_PROXY_HOST") or "").strip()
    port = (os.getenv("PROXY_PORT") or os.getenv("EBAY_PROXY_PORT") or "").strip()
    if not host or not port:
        return None

    user = (os.getenv("PROXY_USER") or os.getenv("EBAY_PROXY_USER") or "").strip()
    password = (
        os.getenv("PROXY_PASSWORD") or os.getenv("EBAY_PROXY_PASSWORD") or ""
    ).strip()
    auth = f"{quote(user)}:{quote(password)}@" if user else ""
    scheme = (os.getenv("PROXY_SCHEME") or "http").strip() or "http"
    return f"{scheme}://{auth}{host}:{port}"


def _proxy_label(proxies: dict[str, str] | None) -> str:
    if not proxies:
        return "direct"
    raw = proxies.get("https") or proxies.get("http") or ""
    parsed = urlparse(raw)
    host = parsed.hostname or "proxy"
    port = f":{parsed.port}" if parsed.port else ""
    region = (os.getenv("EBAY_PROXY_REGION") or "").strip()
    extra = f" region={region}" if region else ""
    return f"proxy={parsed.scheme}://{host}{port}{extra}"


def requests_proxies() -> dict[str, str] | None:
    url = _proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def request_routes() -> list[tuple[str, dict[str, str] | None]]:
    mode = ebay_proxy_mode()
    proxies = requests_proxies()
    if mode in {"direct", "none", "off", "0"}:
        return [("direct", None)]
    if mode in {"proxy", "proxied", "on", "1"}:
        if proxies is None:
            print(
                "[ebay scraper] EBAY_PROXY_MODE=proxy but no proxy is configured; "
                "set EBAY_PROXY_URL or PROXY_HOST/PROXY_PORT",
                flush=True,
            )
            return [("direct", None)]
        return [("proxy", proxies)]
    if proxies is None:
        print(
            "[ebay scraper] no proxy configured; using direct requests",
            flush=True,
        )
        return [("direct", None)]
    return [("proxy", proxies), ("direct", None)]


def print_dom_body(html: str | None) -> None:
    print("[ebay scraper] DOM body start", flush=True)
    if not html:
        print("", flush=True)
    else:
        parser = HTMLParser(html)
        body = parser.css_first("body")
        print(body.html if body else html, flush=True)
    print("[ebay scraper] DOM body end", flush=True)


def is_challenge_response(response: Response, html: str) -> bool:
    url = (response.url or "").casefold()
    if "splashui/challenge" in url or "/splashui/" in url:
        return True
    lowered = html.lower()
    return "splashui/challenge" in lowered or "appname=orch" in lowered


def is_recoverable_http_error(error: BaseException) -> bool:
    text = str(error).casefold()
    return any(
        needle in text
        for needle in (
            "anti-bot",
            "challenge",
            "splashui",
            "timed out",
            "did not return search results",
            "bot-detection",
            "captcha",
            "blocked the request",
        )
    )


def _load_saved_cookies() -> list[dict]:
    path = REQUESTS_COOKIES_FILE
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        print(f"[ebay scraper] could not read saved cookies: {error}", flush=True)
        return []
    if isinstance(payload, dict):
        payload = payload.get("cookies") or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and item.get("name")]


def _save_cookies(session: requests.Session) -> None:
    ensure_data_dirs()
    cookies = []
    for cookie in session.cookies:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or ".ebay.com",
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
            }
        )
    REQUESTS_COOKIES_FILE.write_text(
        json.dumps({"cookies": cookies}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    names = sorted({item["name"] for item in cookies})
    preview = ", ".join(names[:8])
    extra = f" +{len(names) - 8} more" if len(names) > 8 else ""
    print(
        f"[ebay scraper] session cookies saved: {len(cookies)}"
        + (f" ({preview}{extra})" if names else ""),
        flush=True,
    )


def _apply_cookies(session: requests.Session, cookies: list[dict]) -> None:
    for cookie in cookies:
        try:
            session.cookies.set(
                str(cookie.get("name") or ""),
                str(cookie.get("value") or ""),
                domain=str(cookie.get("domain") or ".ebay.com"),
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure", True)),
            )
        except Exception:
            continue


class EbayHttpSession:
    """Keep-alive requests session that warms ebay.com before each search."""

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        return session

    def __init__(self, cookies: list[dict] | None = None) -> None:
        self._injected_cookies = cookies or []
        self.http = self._new_session()
        self.last_url = ""
        self.last_html = ""
        self._apply_saved_cookies()
        self._apply_injected_cookies()
        routes = request_routes()
        print(
            f"[ebay scraper] HTTP session ready proxy_mode={ebay_proxy_mode()} "
            f"routes={','.join(label for label, _ in routes)}",
            flush=True,
        )

    def _apply_saved_cookies(self) -> None:
        cookies = _load_saved_cookies()
        if not cookies:
            print(
                "[ebay scraper] starting a fresh requests session "
                "(eBay will set cookies)",
                flush=True,
            )
            return
        _apply_cookies(self.http, cookies)
        print(
            f"[ebay scraper] restored {len(cookies)} session cookies into requests",
            flush=True,
        )

    def _apply_injected_cookies(self) -> None:
        if not self._injected_cookies:
            return
        applied = 0
        for cookie in self._injected_cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if not name:
                continue
            try:
                self.http.cookies.set(
                    name,
                    value,
                    domain=str(cookie.get("domain") or ".ebay.com"),
                    path=str(cookie.get("path") or "/"),
                )
                applied += 1
            except Exception:
                continue
        print(
            f"[ebay scraper] applied {applied} admin cookies to requests session",
            flush=True,
        )

    def persist(self) -> None:
        try:
            _save_cookies(self.http)
        except Exception as error:
            print(f"[ebay scraper] could not persist cookies: {error}", flush=True)

    def reset(self) -> None:
        print("[ebay scraper] resetting requests session", flush=True)
        try:
            self.http.close()
        except Exception:
            pass
        self.http = self._new_session()
        self.last_url = ""
        self.last_html = ""
        self._apply_saved_cookies()
        self._apply_injected_cookies()

    def close(self) -> None:
        self.persist()
        try:
            self.http.close()
        except Exception:
            pass

    def __enter__(self) -> "EbayHttpSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        proxies: dict[str, str] | None,
        route_label: str,
        label: str,
    ) -> Response:
        print(
            f"[ebay scraper] {label} timeout={REQUEST_TIMEOUT} "
            f"route={route_label} {_proxy_label(proxies)}",
            flush=True,
        )
        try:
            response = self.http.get(
                url,
                headers=headers,
                proxies=proxies or {},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except Timeout as exc:
            print(f"[ebay scraper] request timed out: {exc}", flush=True)
            raise EbayHttpError(
                f"eBay request timed out after connect/read timeout {REQUEST_TIMEOUT}",
                url=url,
            ) from exc
        except RequestException as exc:
            print(f"[ebay scraper] request failed: {exc}", flush=True)
            raise EbayHttpError(f"eBay request failed: {exc}", url=url) from exc

        html = response.text
        self.last_url = response.url
        self.last_html = html
        print(
            f"[ebay scraper] {label} response status={response.status_code} "
            f"final_url={response.url} bytes={len(response.content or b'')}",
            flush=True,
        )
        for index, redirect in enumerate(response.history, start=1):
            print(
                f"[ebay scraper] redirect[{index}] status={redirect.status_code} "
                f"url={redirect.url}",
                flush=True,
            )
        if response.status_code >= 400:
            print_dom_body(html)
            raise EbayHttpError(
                f"eBay {label} returned HTTP {response.status_code} for {response.url}",
                url=url,
                status_code=response.status_code,
                final_url=response.url,
                html=html,
            )
        if is_challenge_response(response, html):
            print_dom_body(html)
            raise EbayHttpError(
                f"eBay returned an anti-bot challenge page during {label}: {response.url}",
                url=url,
                status_code=response.status_code,
                final_url=response.url,
                html=html,
            )
        return response

    def fetch_search(self, url: str) -> HttpFetchResult:
        last_error: Exception | None = None
        attempts = ebay_fetch_attempts()
        for attempt in range(1, attempts + 1):
            print(
                f"[ebay scraper] fetch attempt {attempt}/{attempts} url={url}",
                flush=True,
            )
            for route_label, proxies in request_routes():
                try:
                    print(
                        f"[ebay scraper] warming eBay home session url={EBAY_HOME_URL}",
                        flush=True,
                    )
                    self._get(
                        EBAY_HOME_URL,
                        headers=REQUEST_HEADERS,
                        proxies=proxies,
                        route_label=route_label,
                        label="home",
                    )
                    search_headers = {
                        **REQUEST_HEADERS,
                        "Referer": EBAY_HOME_URL,
                        "Sec-Fetch-Site": "same-origin",
                    }
                    print(
                        f"[ebay scraper] requesting search url={url}",
                        flush=True,
                    )
                    response = self._get(
                        url,
                        headers=search_headers,
                        proxies=proxies,
                        route_label=route_label,
                        label="search",
                    )
                    html = response.text
                    if "srp-results" not in html and "s-card__title" not in html:
                        print_dom_body(html)
                        raise EbayHttpError(
                            "eBay did not return search results. "
                            "The page may be blocked or require a browser session.",
                            url=url,
                            status_code=response.status_code,
                            final_url=response.url,
                            html=html,
                        )
                    self.persist()
                    return HttpFetchResult(
                        url=url,
                        final_url=response.url,
                        status_code=response.status_code,
                        html=html,
                    )
                except Exception as exc:
                    last_error = exc
                    print(
                        f"[ebay scraper] fetch attempt {attempt} route={route_label} "
                        f"failed: {exc}",
                        flush=True,
                    )
                    continue
        if last_error is not None:
            raise last_error
        raise EbayHttpError("eBay search failed", url=url)

    def fetch_html(self, url: str, *, referer: str = EBAY_HOME_URL) -> HttpFetchResult:
        headers = {
            **REQUEST_HEADERS,
            "Referer": referer,
            "Sec-Fetch-Site": "same-origin" if "ebay.com" in url else "none",
        }
        last_error: Exception | None = None
        for route_label, proxies in request_routes():
            try:
                response = self._get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    route_label=route_label,
                    label="document",
                )
                self.persist()
                return HttpFetchResult(
                    url=url,
                    final_url=response.url,
                    status_code=response.status_code,
                    html=response.text,
                )
            except Exception as exc:
                last_error = exc
                print(
                    f"[ebay scraper] document route={route_label} failed: {exc}",
                    flush=True,
                )
        if last_error is not None:
            raise last_error
        raise EbayHttpError("document fetch failed", url=url)
