"""GEEFLIP V2 — Flask front end for the eBay arbitrage scraper.

    python geeflip/app.py
    http://127.0.0.1:8787
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
EBAY_ROOT = PROJECT_ROOT / "ebay"
for entry in (str(PACKAGE_ROOT), str(EBAY_ROOT), str(PROJECT_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from flask import Flask, jsonify, render_template, request

from browser import DEFAULT_BROWSER, PRESETS, describe, load_browser, save_browser
from cookies import cookie_status, read_cookie, write_cookie
from filters import DEFAULT_FILTERS, build_settings, coerce_filters
from importer import ensure_ready, import_amazon_images, import_catalog
from runner import ScrapeRunner
from store import Store

app = Flask(__name__)
store = Store()
runner = ScrapeRunner(store)


def _int_arg(name: str, default: int | None = None) -> int | None:
    raw = request.args.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float_arg(name: str, default: float | None = None) -> float | None:
    raw = request.args.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ----------------------------------------------------------------------
# pages
# ----------------------------------------------------------------------


@app.get("/")
def page_feed():
    return render_template(
        "index.html",
        filters=store.load_filters(DEFAULT_FILTERS),
        stats=store.stats(),
    )


@app.get("/asins")
def page_asins():
    return render_template("asins.html", stats=store.stats(), brands=store.brands())


@app.get("/admin")
def page_admin():
    browser = load_browser(store)
    return render_template(
        "admin.html",
        cookie=read_cookie(),
        cookie_status=cookie_status(),
        browser=browser,
        browser_summary=describe(browser),
        presets=PRESETS,
    )


# ----------------------------------------------------------------------
# scrape control
# ----------------------------------------------------------------------


@app.get("/api/status")
def api_status():
    return jsonify(runner.snapshot())


@app.get("/api/logs")
def api_logs():
    after = _int_arg("after", 0) or 0
    return jsonify({"logs": runner.logs_after(after)})


@app.post("/api/logs/clear")
def api_clear_logs():
    runner.clear_logs()
    return jsonify({"ok": True})


@app.get("/api/filters")
def api_get_filters():
    return jsonify(store.load_filters(DEFAULT_FILTERS))


@app.put("/api/filters")
def api_put_filters():
    try:
        filters = coerce_filters(request.get_json(silent=True) or {})
        build_settings(filters, store).validate()
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(store.save_filters(filters))


@app.post("/api/preview")
def api_preview():
    try:
        return jsonify(runner.preview(request.get_json(silent=True) or {}))
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/scrape/start")
def api_start():
    payload = request.get_json(silent=True)
    filters = coerce_filters(payload if payload else store.load_filters(DEFAULT_FILTERS))
    try:
        build_settings(filters, store).validate()
        store.save_filters(filters)
        return jsonify(runner.start(filters))
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 409
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/scrape/stop")
def api_stop():
    return jsonify(runner.stop())


# ----------------------------------------------------------------------
# winning listings feed
# ----------------------------------------------------------------------


@app.get("/api/winners")
def api_winners():
    limit = max(1, min(_int_arg("limit", 40) or 40, 200))
    return jsonify(
        store.list_winners(
            run_id=_int_arg("run_id"),
            review=request.args.get("review", "all"),
            search=request.args.get("q", ""),
            limit=limit,
            offset=max(0, _int_arg("offset", 0) or 0),
        )
    )


@app.post("/api/winners/<int:winner_id>/seen")
def api_winner_seen(winner_id: int):
    payload = request.get_json(silent=True) or {}
    winner = store.set_winner_seen(winner_id, bool(payload.get("seen", True)))
    if winner is None:
        return jsonify({"error": "Listing not found"}), 404
    return jsonify(winner)


@app.post("/api/winners/<int:winner_id>/verdict")
def api_winner_verdict(winner_id: int):
    payload = request.get_json(silent=True) or {}
    verdict = payload.get("verdict")
    if verdict in ("", "none"):
        verdict = None
    try:
        winner = store.set_winner_verdict(winner_id, verdict)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if winner is None:
        return jsonify({"error": "Listing not found"}), 404
    return jsonify(winner)


# ----------------------------------------------------------------------
# ASIN database
# ----------------------------------------------------------------------


@app.get("/api/asins")
def api_asins():
    limit = max(1, min(_int_arg("limit", 50) or 50, 200))
    return jsonify(
        store.list_asins(
            search=request.args.get("q", ""),
            brand=request.args.get("brand", ""),
            activity=request.args.get("activity", "all"),
            sort=request.args.get("sort", "row"),
            min_rank=_int_arg("min_rank"),
            max_rank=_int_arg("max_rank"),
            min_buybox=_float_arg("min_buybox"),
            max_buybox=_float_arg("max_buybox"),
            limit=limit,
            offset=max(0, _int_arg("offset", 0) or 0),
        )
    )


@app.get("/api/stats")
def api_stats():
    return jsonify(store.stats())


# ----------------------------------------------------------------------
# maintenance
# ----------------------------------------------------------------------


@app.get("/api/cookie")
def api_get_cookie():
    return jsonify({**cookie_status(), "cookie": read_cookie()})


@app.post("/api/cookie")
def api_set_cookie():
    cookie = str((request.get_json(silent=True) or {}).get("cookie") or "").strip()
    if cookie and "=" not in cookie:
        return jsonify({"error": "That does not look like a cookie header"}), 400
    write_cookie(cookie)
    return jsonify({**cookie_status(), "cookie": read_cookie()})


@app.get("/api/browser")
def api_get_browser():
    browser = load_browser(store)
    return jsonify({"browser": browser, "summary": describe(browser)})


@app.put("/api/browser")
def api_put_browser():
    try:
        browser = save_browser(store, request.get_json(silent=True) or {})
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(
        {
            "browser": browser,
            "summary": describe(browser),
            "restart_needed": runner.running,
        }
    )


@app.post("/api/browser/reset")
def api_reset_browser():
    browser = save_browser(store, DEFAULT_BROWSER)
    return jsonify({"browser": browser, "summary": describe(browser)})


@app.post("/api/catalog/reimport")
def api_reimport():
    if runner.running:
        return jsonify({"error": "Cannot reimport while a scrape is running"}), 409
    try:
        products = import_catalog(store)
        images = import_amazon_images(store)
    except (FileNotFoundError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"products": products, "images": images, **store.stats()})


def main() -> None:
    summary = ensure_ready(store)
    if summary.get("imported"):
        print(f"GEEFLIP: imported {summary['products']:,} products from Excel")
    print(
        f"GEEFLIP V2: {summary['products']:,} products, "
        f"{summary['winners']:,} winning listings"
    )
    print("GEEFLIP V2: http://127.0.0.1:8787")
    app.run(host="127.0.0.1", port=8787, threaded=True, debug=False)


if __name__ == "__main__":
    main()
