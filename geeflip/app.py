"""GEEFLIP website — scrape control room.

Run from anywhere:
    python geeflip/app.py
Then open http://127.0.0.1:8787
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

GEEFLIP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = GEEFLIP_ROOT.parent
EBAY_ROOT = PROJECT_ROOT / "ebay"
for path in (str(GEEFLIP_ROOT), str(EBAY_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from cookies import read_cookie, write_cookie, cookie_status
from db import GeeflipStore
from lib.ebay_scraper import ensure_playwright_chromium_installed
from scrape_runner import ScrapeRunner, coerce_filters

store = GeeflipStore()
runner = ScrapeRunner(store)


class _QuietAccessLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        noisy = (
            "GET /api/status" in message
            or "GET /api/winners" in message
            or "GET /api/scrape/logs" in message
            or '"HEAD / ' in message
        )
        return not noisy


logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("GEEFLIP: loading catalog into SQLite if needed", flush=True)
    result = store.ensure_imported()
    if result.get("imported"):
        print(
            f"GEEFLIP: imported {result['products']:,} products, "
            f"{result.get('winners', 0):,} winners",
            flush=True,
        )
    elif result.get("skipped"):
        print("GEEFLIP: catalog Excel not found; starting with empty SQLite", flush=True)
    else:
        print(
            f"GEEFLIP: catalog already loaded ({result['products']:,} products)",
            flush=True,
        )
    store.load_products()
    print("GEEFLIP: loading Amazon images from Keepa if needed", flush=True)
    images = store.ensure_keepa_images()
    if images.get("imported"):
        print(
            f"GEEFLIP: attached Keepa photos to {images.get('updated', 0):,} ASINs "
            f"({images.get('with_image', 0):,} with images)",
            flush=True,
        )
    elif images.get("skipped"):
        print("GEEFLIP: Keepa workbook not found; Amazon photos skipped", flush=True)
    else:
        print(
            f"GEEFLIP: Amazon photos already loaded "
            f"({images.get('with_image', 0):,} products)",
            flush=True,
        )
    print("GEEFLIP: http://0.0.0.0:8787", flush=True)
    threading.Thread(
        target=ensure_playwright_chromium_installed,
        name="playwright-install",
        daemon=True,
    ).start()
    yield


app = FastAPI(title="GEEFLIP", lifespan=lifespan)


def html_page(name: str) -> FileResponse:
    return FileResponse(GEEFLIP_ROOT / "static" / name)


@app.get("/")
def index() -> FileResponse:
    return html_page("index.html")


@app.head("/")
def index_head() -> Response:
    return Response(status_code=200)


@app.api_route("/asins", methods=["GET", "HEAD"])
def asins_page() -> FileResponse:
    return html_page("asins.html")


@app.api_route("/history", methods=["GET", "HEAD"])
def history_page() -> FileResponse:
    return html_page("history.html")


@app.api_route("/admin", methods=["GET", "HEAD"])
def admin_page() -> FileResponse:
    return html_page("admin.html")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/status")
def status() -> dict:
    return runner.snapshot()


@app.get("/api/filters")
def get_filters() -> dict:
    return store.load_filters()


@app.put("/api/filters")
def put_filters(payload: dict = Body(...)) -> dict:
    try:
        filters = coerce_filters(payload)
        from scrape_runner import settings_from_filters

        settings_from_filters(filters, store).validate()
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return store.save_filters(filters)


@app.post("/api/preview")
def preview(payload: dict = Body(...)) -> dict:
    try:
        filters = coerce_filters(payload)
        return runner.preview(filters)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/scrape/start")
def start_scrape(payload: dict | None = Body(None)) -> dict:
    filters = coerce_filters(payload or store.load_filters())
    try:
        from scrape_runner import settings_from_filters

        settings_from_filters(filters, store).validate()
        store.save_filters(filters)
        return runner.start(filters)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/scrape/stop")
def stop_scrape() -> dict:
    return runner.stop()


def winners_payload(run_id: int | None = None, limit: int = 80, offset: int = 0) -> dict:
    rows = store.list_winners(
        run_id=run_id,
        limit=max(1, min(limit, 300)),
        offset=max(0, offset),
    )
    return {
        "run_id": run_id,
        "offset": offset,
        "count": store.winner_count(run_id=run_id),
        "winners": rows,
    }


@app.websocket("/ws/live")
async def live_updates(websocket: WebSocket, after: int = 0) -> None:
    await websocket.accept()
    last_log_id = max(0, after)
    last_status_key = None
    last_winners_key = None
    try:
        while True:
            snapshot = runner.snapshot()
            logs = runner.logs_after(last_log_id)
            if logs:
                last_log_id = logs[-1]["id"]
                await websocket.send_json({"type": "logs", "logs": logs})

            status_key = (
                snapshot["status"],
                snapshot["run_id"],
                snapshot["winners_run"],
                snapshot["winners_total"],
                snapshot["exit_code"],
                snapshot.get("cookies"),
            )
            if status_key != last_status_key:
                await websocket.send_json({"type": "status", "status": snapshot})
                last_status_key = status_key

            winners_key = (
                snapshot["status"],
                snapshot["run_id"],
                snapshot["winners_run"],
                snapshot["winners_total"],
            )
            if winners_key != last_winners_key:
                run_id = (
                    snapshot["run_id"]
                    if snapshot["status"] in {"running", "stopping"}
                    else None
                )
                await websocket.send_json(
                    {"type": "winners", **winners_payload(run_id=run_id)}
                )
                last_winners_key = winners_key

            await asyncio.to_thread(runner.wait_for_update, 1.0)
    except WebSocketDisconnect:
        return


@app.get("/api/winners")
def winners(
    run_id: int | None = None,
    limit: int = 80,
    offset: int = 0,
) -> dict:
    return winners_payload(run_id=run_id, limit=limit, offset=offset)


@app.post("/api/winners/{winner_id}/seen")
def mark_winner_seen(winner_id: int, payload: dict = Body(...)) -> dict:
    seen = bool(payload.get("seen", True))
    row = store.set_winner_seen(winner_id, seen)
    if row is None:
        raise HTTPException(status_code=404, detail="Winner not found")
    return row


@app.post("/api/winners/{winner_id}/mismatch")
def mark_winner_mismatched(winner_id: int, payload: dict = Body(...)) -> dict:
    mismatched = bool(payload.get("mismatched", True))
    row = store.set_winner_mismatched(winner_id, mismatched)
    if row is None:
        raise HTTPException(status_code=404, detail="Winner not found")
    return row


@app.get("/api/asins")
def asins(q: str = "", offset: int = 0, limit: int = 50) -> dict:
    return store.list_asins(
        query=q,
        offset=max(0, offset),
        limit=max(1, min(limit, 200)),
    )


@app.get("/api/admin/cookie")
def get_cookie() -> dict:
    status = cookie_status()
    status["cookie"] = read_cookie()
    return status


@app.post("/api/admin/cookie")
def update_cookie(payload: dict = Body(...)) -> dict:
    cookie = str(payload.get("cookie") or "").strip()
    if cookie and "=" not in cookie:
        raise HTTPException(
            status_code=400,
            detail="That does not look like a cookie header (expected name=value pairs).",
        )
    write_cookie(cookie)
    status = cookie_status()
    status["cookie"] = read_cookie()
    if cookie and not status["present"]:
        raise HTTPException(
            status_code=400,
            detail="No location/zip cookies found. Need dp1, nonsession, ns1, ebay, or zip.",
        )
    return status


app.mount("/static", StaticFiles(directory=GEEFLIP_ROOT / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
