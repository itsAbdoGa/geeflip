"""SQLite storage for GEEFLIP V2.

The catalog, the winning listings, the scrape runs and the review state all live
in one SQLite file. The scraper in ``ebay/scripts/scrape_listings.py`` talks to
this class through the duck-typed methods grouped under "scraper contract"
below; everything else is used by the Flask routes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from paths import DB_PATH, ensure_data_dir

VERDICTS = ("matched", "mismatched")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    source_row INTEGER NOT NULL UNIQUE,
    asin TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    cleaned_title TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    upc TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    buybox_price REAL,
    sales_rank INTEGER,
    drops_count INTEGER,
    restriction TEXT NOT NULL DEFAULT '',
    amazon_url TEXT NOT NULL DEFAULT '',
    ebay_search_url TEXT NOT NULL DEFAULT '',
    last_enrichment_date TEXT NOT NULL DEFAULT '',
    amazon_image_url TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_products_asin ON products(asin);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    filters_json TEXT,
    winners_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS winners (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    history_key TEXT,
    found_at TEXT NOT NULL,
    first_won_at TEXT NOT NULL,
    asin TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    buybox TEXT NOT NULL DEFAULT '',
    amazon_url TEXT NOT NULL DEFAULT '',
    ebay_url TEXT NOT NULL DEFAULT '',
    ebay_title TEXT NOT NULL DEFAULT '',
    ebay_query TEXT NOT NULL DEFAULT '',
    ebay_image_url TEXT NOT NULL DEFAULT '',
    ebay_full_cost REAL,
    roi REAL,
    seller TEXT NOT NULL DEFAULT '',
    seller_reviews INTEGER,
    listing_date TEXT NOT NULL DEFAULT '',
    search_source TEXT NOT NULL DEFAULT '',
    seen INTEGER NOT NULL DEFAULT 0,
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('matched', 'mismatched')),
    reviewed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_winners_history_key
    ON winners(history_key) WHERE history_key IS NOT NULL AND history_key != '';
CREATE INDEX IF NOT EXISTS idx_winners_run ON winners(run_id);
CREATE INDEX IF NOT EXISTS idx_winners_found ON winners(found_at);
CREATE INDEX IF NOT EXISTS idx_winners_asin ON winners(asin);

CREATE TABLE IF NOT EXISTS identifier_no_match (
    query_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    consecutive_no_matches INTEGER NOT NULL DEFAULT 1,
    no_exact_match_at TEXT NOT NULL,
    PRIMARY KEY (query_type, identifier)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def identifier_query_key(query_type: str, identifier: object) -> str:
    """Build the ``EAN:1234`` style key the scraper uses to skip identifiers."""
    value = str(identifier or "").strip().casefold()
    return f"{query_type.upper()}:{value}" if value else ""


def listing_key(winner: dict) -> str:
    """Stable identity for an eBay listing, preferring its numeric item id."""
    url = str(winner.get("EBAY listing URL") or winner.get("ebay_url") or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    item = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", parts.path, re.IGNORECASE)
    if item:
        return f"ebay-item:{item.group(1)}"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), "", "")
    )


def clean_text(value: object) -> str:
    """Render a spreadsheet cell as text without Excel's float artefacts."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value).rstrip("0").rstrip(".")
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value).strip()


def clean_listing_title(value: object) -> str:
    """eBay glues its own UI text onto card titles; strip it before storing."""
    text = clean_text(value)
    text = re.sub(r"\s*Opens in a new window or tab\s*$", "", text)
    text = re.sub(r"^New Listing\s*", "", text)
    return text.strip()


def as_int(value: object) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"-?\d[\d,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def as_float(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        ensure_data_dir()
        self.path = path
        self.current_run_id: int | None = None
        self._lock = threading.RLock()
        self._products_cache: list[dict[str, str]] | None = None
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def load_json(self, key: str, defaults: dict) -> dict:
        """Read a saved settings blob, falling back to defaults key by key."""
        raw = self.get_setting(key)
        merged = dict(defaults)
        if raw:
            try:
                stored = json.loads(raw)
            except json.JSONDecodeError:
                return dict(defaults)
            if isinstance(stored, dict):
                merged.update(stored)
        return merged

    def save_json(self, key: str, value: dict) -> dict:
        self.set_setting(key, json.dumps(value))
        return value

    def load_filters(self, defaults: dict) -> dict:
        return self.load_json("filters", defaults)

    def save_filters(self, filters: dict) -> dict:
        return self.save_json("filters", filters)

    # ------------------------------------------------------------------
    # scraper contract — these names are looked up by scrape_listings.main
    # ------------------------------------------------------------------

    def load_products(self) -> list[dict[str, str]]:
        with self._lock:
            if self._products_cache is not None:
                return self._products_cache
            rows = self._conn.execute(
                """
                SELECT source_row, title, asin, ean, upc, cleaned_title, sales_rank,
                       drops_count, brand, buybox_price, amazon_url, ebay_search_url
                FROM products ORDER BY source_row
                """
            ).fetchall()
            self._products_cache = [
                {
                    "csv_row": int(row["source_row"]),
                    "title": row["title"],
                    "asin": row["asin"],
                    "ean": row["ean"],
                    "upc": row["upc"],
                    "cleaned_title": row["cleaned_title"],
                    "sales_rank": "" if row["sales_rank"] is None else str(row["sales_rank"]),
                    "drops_count": "" if row["drops_count"] is None else str(row["drops_count"]),
                    "brand": row["brand"],
                    "buybox_price": (
                        "" if row["buybox_price"] is None else str(row["buybox_price"])
                    ),
                    "amazon_url": row["amazon_url"],
                    "search_url": row["ebay_search_url"],
                }
                for row in rows
            ]
            return self._products_cache

    def delete_identifiers(self, deletion_keys: set[str]) -> int:
        """Strip EAN/UPC codes that repeatedly returned no exact match."""
        if not deletion_keys:
            return 0
        changed = 0
        with self._lock:
            rows = self._conn.execute("SELECT id, ean, upc FROM products").fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column, query_type in (("ean", "EAN"), ("upc", "UPC")):
                    codes = [code.strip() for code in str(row[column] or "").split(",") if code.strip()]
                    kept = [
                        code
                        for code in codes
                        if identifier_query_key(query_type, code) not in deletion_keys
                    ]
                    if kept != codes:
                        updates[column] = ", ".join(kept)
                if not updates:
                    continue
                assignments = ", ".join(f"{column} = ?" for column in updates)
                self._conn.execute(
                    f"UPDATE products SET {assignments} WHERE id = ?",
                    [*updates.values(), row["id"]],
                )
                changed += 1
            self._products_cache = None
            self._conn.commit()
        return changed

    def load_identifier_no_match(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT query_type, identifier, consecutive_no_matches, no_exact_match_at "
                "FROM identifier_no_match"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_identifier_no_match(self, records: list[dict]) -> None:
        payload = [
            (
                str(record.get("query_type") or ""),
                str(record.get("identifier") or ""),
                int(record.get("consecutive_no_matches") or 1),
                str(record.get("no_exact_match_at") or ""),
            )
            for record in records
            if record.get("query_type") and record.get("identifier")
        ]
        with self._lock:
            self._conn.execute("DELETE FROM identifier_no_match")
            self._conn.executemany(
                "INSERT INTO identifier_no_match("
                "query_type, identifier, consecutive_no_matches, no_exact_match_at"
                ") VALUES (?, ?, ?, ?)",
                payload,
            )
            self._conn.commit()

    def load_winner_history(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM winners ORDER BY first_won_at"
            ).fetchall()
        return [self._as_scraper_winner(row) for row in rows]

    def save_winner_history(self, records: list[dict]) -> None:
        """Persist the scraper's pruned dedup window without losing any listings.

        The retention setting exists so a listing won last week can be reported
        again, not so it disappears. The scraper keeps its pruned key set in
        memory for that; here the rows stay put, because the feed and the ASIN
        tallies are the permanent record of what I have reviewed.
        """
        with self._lock:
            for record in records:
                self._upsert_winner(record, run_id=record.get("run_id"))
            self._conn.commit()

    def save_live_winners(self, winners: list[dict]) -> None:
        if self.current_run_id is None:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE scrape_runs SET winners_count = ? WHERE id = ?",
                (len(winners), self.current_run_id),
            )
            self._conn.commit()

    def append_winners(self, winners: list[dict]) -> None:
        with self._lock:
            for winner in winners:
                self._upsert_winner(winner, run_id=self.current_run_id)
            if self.current_run_id is not None:
                count = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM winners WHERE run_id = ?",
                    (self.current_run_id,),
                ).fetchone()["n"]
                self._conn.execute(
                    "UPDATE scrape_runs SET winners_count = ? WHERE id = ?",
                    (int(count), self.current_run_id),
                )
            self._conn.commit()

    def _upsert_winner(self, winner: dict, *, run_id: object = None) -> None:
        """Insert or refresh one winner. Review state is never overwritten."""
        key = str(winner.get("history_key") or listing_key(winner) or "") or None
        found_at = str(winner.get("found_at") or winner.get("first_won_at") or now_iso())
        first_won_at = str(winner.get("first_won_at") or found_at)
        stored_run_id = winner.get("run_id") if winner.get("run_id") is not None else run_id
        buybox = winner.get("BUYBOX", winner.get("buybox"))
        columns = {
            "run_id": stored_run_id,
            "history_key": key,
            "found_at": found_at,
            "first_won_at": first_won_at,
            "asin": clean_text(winner.get("ASIN") or winner.get("asin")),
            "title": clean_text(winner.get("title")),
            "ean": clean_text(winner.get("EAN") or winner.get("ean")),
            "brand": clean_text(winner.get("Brand") or winner.get("brand")),
            "buybox": "" if buybox is None else clean_text(buybox),
            "amazon_url": clean_text(winner.get("AMAZON URL") or winner.get("amazon_url")),
            "ebay_url": clean_text(winner.get("EBAY listing URL") or winner.get("ebay_url")),
            "ebay_title": clean_listing_title(
                winner.get("EBAY listing title") or winner.get("ebay_title")
            ),
            "ebay_query": clean_text(winner.get("ebay listing query") or winner.get("ebay_query")),
            "ebay_image_url": clean_text(
                winner.get("EBAY image URL") or winner.get("ebay_image_url")
            ),
            "ebay_full_cost": as_float(
                winner.get("EBAY full cost", winner.get("ebay_full_cost"))
            ),
            "roi": as_float(winner.get("ROI", winner.get("roi"))),
            "seller": clean_text(winner.get("SELLER") or winner.get("seller")),
            "seller_reviews": as_int(
                winner.get("SELLER REVIEWS", winner.get("seller_reviews"))
            ),
            "listing_date": clean_text(winner.get("LISTING DATE") or winner.get("listing_date")),
            "search_source": clean_text(winner.get("search_source")),
        }

        existing = None
        if key:
            existing = self._conn.execute(
                "SELECT id FROM winners WHERE history_key = ?", (key,)
            ).fetchone()

        if existing:
            # A re-found listing keeps its original first_won_at and my review marks.
            updatable = {
                name: value
                for name, value in columns.items()
                if name not in {"history_key", "first_won_at"}
            }
            if updatable["run_id"] is None:
                updatable.pop("run_id")
            assignments = ", ".join(f"{name} = ?" for name in updatable)
            self._conn.execute(
                f"UPDATE winners SET {assignments} WHERE id = ?",
                [*updatable.values(), existing["id"]],
            )
            return

        names = ", ".join(columns)
        placeholders = ", ".join("?" * len(columns))
        self._conn.execute(
            f"INSERT INTO winners({names}) VALUES ({placeholders})",
            list(columns.values()),
        )

    @staticmethod
    def _as_scraper_winner(row: sqlite3.Row) -> dict:
        """Winner dict in the key style scrape_listings.py expects."""
        return {
            "history_key": row["history_key"],
            "first_won_at": row["first_won_at"],
            "found_at": row["found_at"],
            "run_id": row["run_id"],
            "title": row["title"],
            "ASIN": row["asin"],
            "EAN": row["ean"],
            "Brand": row["brand"],
            "BUYBOX": row["buybox"],
            "AMAZON URL": row["amazon_url"],
            "EBAY full cost": row["ebay_full_cost"],
            "SELLER": row["seller"],
            "SELLER REVIEWS": row["seller_reviews"],
            "LISTING DATE": row["listing_date"],
            "ROI": row["roi"],
            "EBAY listing URL": row["ebay_url"],
            "EBAY listing title": row["ebay_title"],
            "EBAY image URL": row["ebay_image_url"],
            "ebay listing query": row["ebay_query"],
            "search_source": row["search_source"],
        }

    # ------------------------------------------------------------------
    # scrape runs
    # ------------------------------------------------------------------

    def begin_run(self, filters: dict) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO scrape_runs(started_at, status, filters_json, winners_count) "
                "VALUES (?, 'running', ?, 0)",
                (now_iso(), json.dumps(filters)),
            )
            self._conn.commit()
            self.current_run_id = int(cursor.lastrowid)
        return self.current_run_id

    def finish_run(self, status: str, *, error: str | None = None) -> None:
        if self.current_run_id is None:
            return
        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM winners WHERE run_id = ?",
                (self.current_run_id,),
            ).fetchone()["n"]
            self._conn.execute(
                "UPDATE scrape_runs SET finished_at = ?, status = ?, winners_count = ?, "
                "error = ? WHERE id = ?",
                (now_iso(), status, int(count), error, self.current_run_id),
            )
            self._conn.commit()

    def latest_run(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 25) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # winners feed
    # ------------------------------------------------------------------

    _FEED_SELECT = """
        SELECT w.*,
               p.amazon_image_url AS amazon_image_url,
               p.sales_rank AS sales_rank,
               p.buybox_price AS catalog_buybox
        FROM winners w
        LEFT JOIN products p ON UPPER(p.asin) = UPPER(w.asin)
    """

    def list_winners(
        self,
        *,
        run_id: int | None = None,
        review: str = "all",
        search: str = "",
        limit: int = 60,
        offset: int = 0,
    ) -> dict:
        where, params = self._winner_filters(run_id=run_id, review=review, search=search)
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM winners w {where}", params
                ).fetchone()["n"]
            )
            rows = self._conn.execute(
                f"{self._FEED_SELECT} {where} ORDER BY w.found_at DESC, w.id DESC "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "winners": [self._as_feed_winner(row) for row in rows],
        }

    @staticmethod
    def _winner_filters(
        *, run_id: int | None, review: str, search: str
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if run_id is not None:
            clauses.append("w.run_id = ?")
            params.append(run_id)
        if review == "unseen":
            clauses.append("w.seen = 0")
        elif review == "seen":
            clauses.append("w.seen = 1")
        elif review == "matched":
            clauses.append("w.verdict = 'matched'")
        elif review == "mismatched":
            clauses.append("w.verdict = 'mismatched'")
        elif review == "unreviewed":
            clauses.append("w.verdict IS NULL")
        term = search.strip()
        if term:
            like = f"%{term}%"
            clauses.append(
                "(w.asin LIKE ? OR w.title LIKE ? OR w.brand LIKE ? "
                "OR w.ebay_title LIKE ? OR w.seller LIKE ?)"
            )
            params.extend([like] * 5)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def get_winner(self, winner_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"{self._FEED_SELECT} WHERE w.id = ?", (winner_id,)
            ).fetchone()
        return self._as_feed_winner(row) if row else None

    def set_winner_seen(self, winner_id: int, seen: bool) -> dict | None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE winners SET seen = ? WHERE id = ?",
                (1 if seen else 0, winner_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_winner(winner_id)

    def set_winner_verdict(self, winner_id: int, verdict: str | None) -> dict | None:
        """Matched and mismatched are one exclusive verdict; passing None clears it.

        Giving a verdict also implies I have looked at the listing, so it marks
        the card as seen.
        """
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS} or null")
        with self._lock:
            if verdict is None:
                cursor = self._conn.execute(
                    "UPDATE winners SET verdict = NULL, reviewed_at = NULL WHERE id = ?",
                    (winner_id,),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE winners SET verdict = ?, reviewed_at = ?, seen = 1 WHERE id = ?",
                    (verdict, now_iso(), winner_id),
                )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_winner(winner_id)

    @staticmethod
    def _as_feed_winner(row: sqlite3.Row) -> dict:
        keys = row.keys()
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "found_at": row["found_at"],
            "first_won_at": row["first_won_at"],
            "asin": row["asin"],
            "title": row["title"],
            "ean": row["ean"],
            "brand": row["brand"],
            "buybox": row["buybox"],
            "amazon_url": row["amazon_url"],
            "ebay_url": row["ebay_url"],
            "ebay_title": row["ebay_title"],
            "ebay_query": row["ebay_query"],
            "ebay_image_url": row["ebay_image_url"],
            "amazon_image_url": row["amazon_image_url"] if "amazon_image_url" in keys else "",
            "ebay_full_cost": row["ebay_full_cost"],
            "roi": row["roi"],
            "seller": row["seller"],
            "seller_reviews": row["seller_reviews"],
            "listing_date": row["listing_date"],
            "search_source": row["search_source"],
            "sales_rank": row["sales_rank"] if "sales_rank" in keys else None,
            "seen": bool(row["seen"]),
            "verdict": row["verdict"],
            "reviewed_at": row["reviewed_at"],
        }

    # ------------------------------------------------------------------
    # ASIN database
    # ------------------------------------------------------------------

    _ASIN_STATS = """
        LEFT JOIN (
            SELECT UPPER(asin) AS asin_key,
                   COUNT(*) AS winners_count,
                   SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) AS worked_count,
                   SUM(CASE WHEN verdict = 'matched' THEN 1 ELSE 0 END) AS matched_count,
                   SUM(CASE WHEN verdict = 'mismatched' THEN 1 ELSE 0 END) AS mismatched_count,
                   SUM(seen) AS seen_count,
                   MAX(found_at) AS last_won_at
            FROM winners
            WHERE asin != ''
            GROUP BY UPPER(asin)
        ) s ON s.asin_key = UPPER(p.asin)
    """

    # One catalog ASIN can sit on many spreadsheet rows (one repeats 41 times),
    # so the ASIN page collapses them into a single row per ASIN.
    _ASIN_GROUP = "COALESCE(NULLIF(UPPER(p.asin), ''), 'row:' || p.source_row)"

    _ASIN_SORTS = {
        "row": "source_row ASC",
        "winners": "winners_count DESC, source_row ASC",
        "worked": "worked_count DESC, source_row ASC",
        "matched": "matched_count DESC, source_row ASC",
        "mismatched": "mismatched_count DESC, source_row ASC",
        "recent": "last_won_at IS NULL, last_won_at DESC, source_row ASC",
        "rank": "sales_rank IS NULL, sales_rank ASC",
        "buybox": "buybox_price IS NULL, buybox_price DESC",
        "title": "title ASC",
    }

    def list_asins(
        self,
        *,
        search: str = "",
        brand: str = "",
        activity: str = "all",
        sort: str = "row",
        min_rank: int | None = None,
        max_rank: int | None = None,
        min_buybox: float | None = None,
        max_buybox: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        clauses: list[str] = []
        params: list = []

        term = search.strip()
        if term:
            like = f"%{term}%"
            clauses.append(
                "(p.asin LIKE ? OR p.title LIKE ? OR p.brand LIKE ? "
                "OR p.ean LIKE ? OR p.upc LIKE ?)"
            )
            params.extend([like] * 5)
        if brand.strip():
            clauses.append("p.brand = ?")
            params.append(brand.strip())
        if min_rank is not None:
            clauses.append("p.sales_rank >= ?")
            params.append(min_rank)
        if max_rank is not None:
            clauses.append("p.sales_rank <= ?")
            params.append(max_rank)
        if min_buybox is not None:
            clauses.append("p.buybox_price >= ?")
            params.append(min_buybox)
        if max_buybox is not None:
            clauses.append("p.buybox_price <= ?")
            params.append(max_buybox)

        activity_clause = {
            "winners": "COALESCE(s.winners_count, 0) > 0",
            "none": "COALESCE(s.winners_count, 0) = 0",
            "worked": "COALESCE(s.worked_count, 0) > 0",
            "unworked": "COALESCE(s.winners_count, 0) > 0 AND COALESCE(s.worked_count, 0) = 0",
            "matched": "COALESCE(s.matched_count, 0) > 0",
            "mismatched": "COALESCE(s.mismatched_count, 0) > 0",
        }.get(activity)
        if activity_clause:
            clauses.append(activity_clause)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = self._ASIN_SORTS.get(sort, self._ASIN_SORTS["row"])
        # Every displayed value is an explicit aggregate so the collapsed row is
        # deterministic: the earliest catalog row, the best rank, the best price.
        columns = """
            MIN(p.source_row) AS source_row,
            COUNT(*) AS catalog_rows,
            MAX(p.asin) AS asin,
            MAX(p.title) AS title,
            MAX(p.brand) AS brand,
            MAX(p.ean) AS ean,
            MAX(p.upc) AS upc,
            MAX(p.buybox_price) AS buybox_price,
            MIN(p.sales_rank) AS sales_rank,
            MAX(p.drops_count) AS drops_count,
            MAX(p.restriction) AS restriction,
            MAX(p.amazon_url) AS amazon_url,
            MAX(p.ebay_search_url) AS ebay_search_url,
            MAX(p.amazon_image_url) AS amazon_image_url,
            MAX(COALESCE(s.winners_count, 0)) AS winners_count,
            MAX(COALESCE(s.worked_count, 0)) AS worked_count,
            MAX(COALESCE(s.matched_count, 0)) AS matched_count,
            MAX(COALESCE(s.mismatched_count, 0)) AS mismatched_count,
            MAX(COALESCE(s.seen_count, 0)) AS seen_count,
            MAX(s.last_won_at) AS last_won_at
        """
        grouped = f"FROM products p {self._ASIN_STATS} {where} GROUP BY {self._ASIN_GROUP}"

        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM (SELECT 1 {grouped})", params
                ).fetchone()["n"]
            )
            rows = self._conn.execute(
                f"SELECT {columns} {grouped} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "asins": [dict(row) for row in rows],
        }

    def brands(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT brand FROM products WHERE brand != '' ORDER BY brand"
            ).fetchall()
        return [row["brand"] for row in rows]

    # ------------------------------------------------------------------
    # dashboard numbers
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            catalog = self._conn.execute(
                """
                SELECT COUNT(*) AS products,
                       COUNT(DISTINCT UPPER(asin)) AS asins,
                       COUNT(DISTINCT brand) AS brands,
                       COUNT(DISTINCT CASE WHEN amazon_image_url != ''
                                           THEN UPPER(asin) END) AS with_image,
                       SUM(CASE WHEN ebay_search_url != '' THEN 1 ELSE 0 END) AS searchable
                FROM products
                """
            ).fetchone()
            winners = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(seen) AS seen,
                       SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) AS worked,
                       SUM(CASE WHEN verdict = 'matched' THEN 1 ELSE 0 END) AS matched,
                       SUM(CASE WHEN verdict = 'mismatched' THEN 1 ELSE 0 END) AS mismatched,
                       COUNT(DISTINCT UPPER(asin)) AS asins
                FROM winners
                """
            ).fetchone()
        return {
            "products": int(catalog["products"] or 0),
            "asins": int(catalog["asins"] or 0),
            "brands": int(catalog["brands"] or 0),
            "with_image": int(catalog["with_image"] or 0),
            "searchable": int(catalog["searchable"] or 0),
            "winners": int(winners["total"] or 0),
            "seen": int(winners["seen"] or 0),
            "worked": int(winners["worked"] or 0),
            "matched": int(winners["matched"] or 0),
            "mismatched": int(winners["mismatched"] or 0),
            "winning_asins": int(winners["asins"] or 0),
            "imported_at": self.get_setting("catalog_imported_at"),
        }

    def product_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
            )

    def winner_count(self, *, run_id: int | None = None) -> int:
        with self._lock:
            if run_id is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM winners").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM winners WHERE run_id = ?", (run_id,)
                ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # bulk loading, used by the importer
    # ------------------------------------------------------------------

    def replace_products(self, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM products")
            self._conn.executemany(
                """
                INSERT INTO products(
                    source_row, asin, title, cleaned_title, ean, upc, brand,
                    buybox_price, sales_rank, drops_count, restriction,
                    amazon_url, ebay_search_url, last_enrichment_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._products_cache = None
            self._conn.commit()

    def apply_amazon_images(self, pairs: list[tuple[str, str]]) -> int:
        """pairs are (image_url, upper-cased ASIN)."""
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "UPDATE products SET amazon_image_url = ? WHERE UPPER(asin) = ?", pairs
            )
            self._conn.commit()
            self._products_cache = None
            return max(self._conn.total_changes - before, 0)

    def insert_winner_rows(self, rows: list[dict]) -> int:
        """Restore winners (including review state) during a rebuild."""
        inserted = 0
        with self._lock:
            for row in rows:
                columns = {key: value for key, value in row.items() if key != "id"}
                names = ", ".join(columns)
                placeholders = ", ".join("?" * len(columns))
                self._conn.execute(
                    f"INSERT OR IGNORE INTO winners({names}) VALUES ({placeholders})",
                    list(columns.values()),
                )
                inserted += 1
            self._conn.commit()
        return inserted

    def insert_runs(self, rows: list[dict]) -> None:
        with self._lock:
            for row in rows:
                names = ", ".join(row)
                placeholders = ", ".join("?" * len(row))
                self._conn.execute(
                    f"INSERT OR IGNORE INTO scrape_runs({names}) VALUES ({placeholders})",
                    list(row.values()),
                )
            self._conn.commit()
