"""Runs the eBay scraper in a background thread and mirrors its output to the UI.

The scraper reports progress by printing, so while a run is active this module
tees ``sys.stdout`` into a ring buffer that the browser polls.
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent
EBAY_ROOT = PACKAGE_ROOT.parent / "ebay"
for entry in (str(PACKAGE_ROOT), str(EBAY_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.scrape_listings import ScrapeSettings, main as run_scrape, select_products

from cookies import cookie_status
from filters import build_settings, coerce_filters
from store import Store, identifier_query_key

LOG_LIMIT = 4000


class _LogTee:
    """Forwards writes to the real stdout and, for the scrape thread, to the UI."""

    def __init__(self, original, emit, *, thread_id: int) -> None:
        self._original = original
        self._emit = emit
        self._thread_id = thread_id
        self._buffer = ""
        self.encoding = getattr(original, "encoding", None) or "utf-8"

    def write(self, data) -> int:
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
        if threading.get_ident() == self._thread_id and self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        return False


class ScrapeRunner:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "idle"
        self.run_id: int | None = None
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.logs: deque[dict[str, Any]] = deque(maxlen=LOG_LIMIT)
        self._log_id = 0

    @property
    def running(self) -> bool:
        return self.status in {"running", "stopping"}

    def emit(self, text: str) -> None:
        with self._lock:
            self._log_id += 1
            self.logs.append(
                {
                    "id": self._log_id,
                    "text": text,
                    "at": datetime.now().strftime("%H:%M:%S"),
                }
            )

    def logs_after(self, after_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [item for item in self.logs if item["id"] > after_id]

    def clear_logs(self) -> None:
        with self._lock:
            self.logs.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status = self.status
            run_id = self.run_id
            last_log_id = self.logs[-1]["id"] if self.logs else 0
        return {
            "status": status,
            "running": status in {"running", "stopping"},
            "run_id": run_id,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_log_id": last_log_id,
            "cookies": cookie_status()["present"],
            "run_winners": self.store.winner_count(run_id=run_id) if run_id else 0,
            "latest_run": self.store.latest_run(),
            "stats": self.store.stats(),
        }

    def preview(self, filters: dict) -> dict[str, Any]:
        """How many searches the current filters would produce, without scraping."""
        settings = build_settings(filters, self.store)
        settings.validate()
        skipped = {
            identifier_query_key(record["query_type"], record["identifier"])
            for record in self.store.load_identifier_no_match()
        }
        selected, details = select_products(
            self.store.load_products(), settings, skipped_identifier_keys=skipped - {""}
        )
        return {"searches": len(selected), "details": details}

    def start(self, filters: dict) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("A scrape is already running")
        settings = build_settings(filters, self.store, should_stop=self._stop.is_set)
        settings.validate()

        self._stop.clear()
        self.error = None
        self.finished_at = None
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.run_id = self.store.begin_run(coerce_filters(filters))
        self.status = "running"
        self.emit(f"GEEFLIP scrape #{self.run_id} starting")

        self._thread = threading.Thread(
            target=self._run, args=(settings,), name="geeflip-scrape", daemon=True
        )
        self._thread.start()
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        if not self.running:
            return self.snapshot()
        self.status = "stopping"
        self._stop.set()
        self.emit("Stop requested — finishing the current search first")
        return self.snapshot()

    def _run(self, settings: ScrapeSettings) -> None:
        original_stdout = sys.stdout
        exit_code = 1
        error = None
        try:
            sys.stdout = _LogTee(original_stdout, self.emit, thread_id=threading.get_ident())
            exit_code = run_scrape(settings)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.emit(error)
            exit_code = 1
        finally:
            sys.stdout = original_stdout
            if self._stop.is_set() and exit_code == 130:
                outcome = "stopped"
            elif exit_code == 0:
                outcome = "completed"
            else:
                outcome = "error"
            self.store.finish_run(outcome, error=error)
            self.error = error
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            self.status = "idle"
            self._stop.clear()
            self.emit(f"Scrape finished — {outcome}")
