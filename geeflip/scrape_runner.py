from __future__ import annotations

import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

GEEFLIP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = GEEFLIP_ROOT.parent
EBAY_ROOT = PROJECT_ROOT / "ebay"
if str(EBAY_ROOT) not in sys.path:
    sys.path.insert(0, str(EBAY_ROOT))

from lib.paths import COMBINED_XLSX
from scripts.scrape_listings import ScrapeSettings, main as scrape_main, select_products

from cookies import COOKIE_FILE, cookie_status
from db import DEFAULT_FILTERS, GeeflipStore


class _LogTee:
    def __init__(self, original, emit, *, thread_id: int) -> None:
        self._original = original
        self._emit = emit
        self._thread_id = thread_id
        self._buffer = ""
        self._encoding = getattr(original, "encoding", None) or "utf-8"

    @property
    def encoding(self) -> str:
        return self._encoding

    def write(self, data: str) -> int:
        if not data:
            return 0
        if not isinstance(data, str):
            data = data.decode("utf-8", errors="replace")
        try:
            self._original.write(data)
        except Exception:
            pass
        if threading.get_ident() != self._thread_id:
            return len(data)
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(data)

    def flush(self) -> None:
        try:
            self._original.flush()
        except Exception:
            pass
        if threading.get_ident() != self._thread_id:
            return
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        return False


def coerce_filters(payload: dict | None) -> dict:
    data = dict(DEFAULT_FILTERS)
    if payload:
        data.update(payload)

    def opt_number(key: str, caster):
        value = data.get(key)
        if value is None or value == "":
            data[key] = None
            return
        data[key] = caster(value)

    def brand_list(key: str) -> None:
        value = data.get(key) or []
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        else:
            value = [str(part).strip() for part in value if str(part).strip()]
        data[key] = value

    opt_number("start_row", int)
    opt_number("end_row", int)
    opt_number("limit", int)
    opt_number("min_roi_percent", float)
    opt_number("max_roi_percent", float)
    opt_number("max_listing_age_days", int)
    opt_number("min_seller_reviews", int)
    opt_number("min_sales_rank", int)
    opt_number("max_sales_rank", int)
    opt_number("min_drop_count", int)
    opt_number("min_buybox_price", float)
    opt_number("winner_history_retention_days", int)
    opt_number("identifier_no_match_retention_days", int)
    brand_list("include_brands")
    brand_list("exclude_brands")
    for flag in (
        "upc_as_well",
        "cleaned_title_as_well",
        "titles_only",
        "image_search",
        "skip_previously_won",
    ):
        data[flag] = bool(data.get(flag))
    if data["min_roi_percent"] is None:
        data["min_roi_percent"] = 80.0
    if data["max_listing_age_days"] is None:
        data["max_listing_age_days"] = 2
    if data["min_seller_reviews"] is None:
        data["min_seller_reviews"] = 40
    if data["winner_history_retention_days"] is None:
        data["winner_history_retention_days"] = 3
    if data["identifier_no_match_retention_days"] is None:
        data["identifier_no_match_retention_days"] = 3
    return data


def settings_from_filters(
    filters: dict,
    store: GeeflipStore,
    *,
    should_stop=None,
) -> ScrapeSettings:
    data = coerce_filters(filters)
    return ScrapeSettings(
        input_csv=COMBINED_XLSX,
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
        write_xlsx=False,
        write_json=False,
        should_stop=should_stop,
        store=store,
    )


class ScrapeRunner:
    def __init__(self, store: GeeflipStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "idle"
        self.run_id: int | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.logs: deque[dict[str, Any]] = deque(maxlen=5000)
        self.log_id = 0
        self._wake = threading.Event()

    @property
    def running(self) -> bool:
        return self.status in {"running", "stopping"}

    def _notify(self) -> None:
        self._wake.set()

    def wait_for_update(self, timeout: float = 1.0) -> bool:
        fired = self._wake.wait(timeout)
        if fired:
            self._wake.clear()
        return fired

    def emit(self, line: str) -> None:
        with self._lock:
            self.log_id += 1
            item = {
                "id": self.log_id,
                "text": line,
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
            self.logs.append(item)
        self._notify()

    def logs_after(self, after_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [item for item in self.logs if item["id"] > after_id]

    def snapshot(self) -> dict[str, Any]:
        catalog = self.store.catalog_stats()
        with self._lock:
            status = self.status
            run_id = self.run_id
            last_log = self.logs[-1]["text"] if self.logs else ""
        return {
            "status": status,
            "run_id": run_id,
            "exit_code": self.exit_code,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "playwright": "headless",
            "cookies": cookie_status()["present"],
            "catalog": catalog,
            "winners_total": self.store.winner_count(),
            "winners_run": self.store.winner_count(run_id=run_id) if run_id else 0,
            "last_log": last_log,
            "latest_run": self.store.latest_run(),
        }

    def preview(self, filters: dict) -> dict[str, Any]:
        settings = settings_from_filters(filters, self.store)
        settings.validate()
        skipped = {
            identifier_query_key_safe(record)
            for record in self.store.load_identifier_no_match()
            if identifier_query_key_safe(record)
        }
        selected, details = select_products(
            self.store.load_products(),
            settings,
            skipped_identifier_keys=skipped,
        )
        return {
            "searches": len(selected),
            "details": details,
        }

    def start(self, filters: dict) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("A scrape is already running")
        settings = settings_from_filters(filters, self.store, should_stop=self._stop.is_set)
        settings.validate()
        self._stop.clear()
        self.exit_code = None
        self.error = None
        self.finished_at = None
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.run_id = self.store.begin_run(coerce_filters(filters))
        self.status = "running"
        self.emit(f"GEEFLIP scrape #{self.run_id} starting (Playwright headless)")
        self._thread = threading.Thread(
            target=self._run,
            args=(settings,),
            name="geeflip-scrape",
            daemon=True,
        )
        self._thread.start()
        self._notify()
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        if not self.running:
            return self.snapshot()
        self.status = "stopping"
        self._stop.set()
        self.emit("Stop requested — waiting for the current search to finish")
        return self.snapshot()

    def _run(self, settings: ScrapeSettings) -> None:
        stdout = sys.stdout
        exit_code = 1
        error = None
        try:
            sys.stdout = _LogTee(stdout, self.emit, thread_id=threading.get_ident())
            from lib.ebay_scraper import ensure_playwright_chromium_installed

            ensure_playwright_chromium_installed()
            exit_code = scrape_main(settings)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                print(error)
            except Exception:
                self.emit(error)
            exit_code = 1
        finally:
            sys.stdout = stdout
            if self._stop.is_set() and exit_code == 130:
                finish_status = "stopped"
            elif exit_code == 0:
                finish_status = "completed"
            else:
                finish_status = "error"
            self.store.finish_run(finish_status, error=error)
            self.exit_code = exit_code
            self.error = error
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            self.status = "idle"
            self._stop.clear()
            self.emit(f"Scrape finished ({finish_status}, exit {exit_code})")


def identifier_query_key_safe(record: dict) -> str:
    query_type = str(record.get("query_type") or "").strip()
    identifier = str(record.get("identifier") or "").strip()
    if not query_type or not identifier:
        return ""
    return f"{query_type.upper()}:{identifier.casefold()}"
