"""Immich Cleaner — AI photo quality classifier + descriptor.

Classifies photos via Ollama vision model (TRASH/REVIEW/KEEP),
sorts them into albums, and writes short descriptions to Immich
metadata for search.
"""

import base64
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IMMICH_API_URL = os.environ.get("IMMICH_API_URL", "http://host.docker.internal:2283").rstrip("/")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-vl:4b")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "1"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "500"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))
MODE = os.environ.get("MODE", "continuous")  # once | continuous
DB_PATH = os.environ.get("DB_PATH", "/data/cleaner.db")
MAX_ASSETS = int(os.environ.get("MAX_ASSETS", "0"))  # 0 = unlimited, for testing

ALBUM_TRASH = os.environ.get("ALBUM_TRASH", "To Delete")
ALBUM_REVIEW = os.environ.get("ALBUM_REVIEW", "To Review")
WRITE_DESCRIPTIONS = os.environ.get("WRITE_DESCRIPTIONS", "false").lower() in ("true", "1", "yes")

SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", """\
You are a photo analyzer for a personal photo library.
For each photo, provide:
1. Quality category (TRASH, REVIEW, or KEEP)
2. Brief description for search

Reply in EXACTLY this format (two lines, nothing else):
CATEGORY: reason
DESCRIPTION: what is in the photo""")

USER_PROMPT = os.environ.get("USER_PROMPT", """\
Analyze this photo.

Category rules:
TRASH: screenshot, screen recording, accidental (pocket/floor/ceiling), utility (receipt/QR/meter/ticket/document), technical failure (completely blurry/black/white)
REVIEW: slightly blurry but recognizable, too dark/bright but visible, low quality but possibly meaningful
KEEP: people, pets, places, events, food, nature, selfies, any intentional photo. When unsure → KEEP

Description rules:
- One sentence, 10-20 words
- What/who is in the photo, where, what's happening
- In English""")

CATEGORIES = ("TRASH", "REVIEW", "KEEP")  # order matters: first match wins in parse_response

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cleaner")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

shutdown_requested = False
db_lock = Lock()


def handle_signal(signum, _frame):
    global shutdown_requested
    log.info("Received signal %s, shutting down gracefully…", signum)
    shutdown_requested = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def init_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS processed (
            asset_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            reason TEXT,
            description TEXT,
            processed_at TEXT NOT NULL,
            model TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def load_processed_ids(conn: sqlite3.Connection) -> set:
    cur = conn.execute("SELECT asset_id FROM processed")
    return {row[0] for row in cur.fetchall()}


def save_result(conn: sqlite3.Connection, asset_id: str, category: str,
                reason: str, description: str):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        conn.execute(
            "INSERT OR REPLACE INTO processed "
            "(asset_id, category, reason, description, processed_at, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (asset_id, category, reason, description, now, OLLAMA_MODEL),
        )
        conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM state WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str):
    with db_lock:
        conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Immich API
# ---------------------------------------------------------------------------


def immich_headers() -> dict:
    return {"x-api-key": IMMICH_API_KEY, "Accept": "application/json"}


def immich_get(path: str, **kwargs) -> requests.Response:
    return requests.get(
        f"{IMMICH_API_URL}/api{path}", headers=immich_headers(), timeout=30, **kwargs
    )


def immich_post(path: str, json_data=None, **kwargs) -> requests.Response:
    return requests.post(
        f"{IMMICH_API_URL}/api{path}",
        headers=immich_headers(),
        json=json_data,
        timeout=30,
        **kwargs,
    )


def immich_put(path: str, json_data=None, **kwargs) -> requests.Response:
    return requests.put(
        f"{IMMICH_API_URL}/api{path}",
        headers=immich_headers(),
        json=json_data,
        timeout=30,
        **kwargs,
    )


def check_immich() -> bool:
    try:
        r = immich_get("/server/ping")
        return r.status_code == 200
    except Exception:
        return False


def search_assets(page: int = 1, updated_after: str | None = None) -> dict:
    body = {
        "type": "IMAGE",
        "size": PAGE_SIZE,
        "page": page,
        "order": "asc",
    }
    if updated_after:
        # Filter on assets.updatedAt (advances on upload + edits).
        # Do NOT use `createdAfter` — that filter operates on `fileCreatedAt`
        # (EXIF date taken), so backdated uploads of older photos would be
        # silently skipped.
        body["updatedAfter"] = updated_after
    r = immich_post("/search/metadata", json_data=body)
    r.raise_for_status()
    return r.json()


def get_thumbnail(asset_id: str) -> bytes | None:
    try:
        r = immich_get(f"/assets/{asset_id}/thumbnail")
        if r.status_code == 200:
            return r.content
        log.debug("Thumbnail %s returned %d", asset_id, r.status_code)
        return None
    except Exception as e:
        log.debug("Thumbnail %s error: %s", asset_id, e)
        return None


def get_all_albums() -> list:
    r = immich_get("/albums")
    r.raise_for_status()
    return r.json()


def create_album(name: str) -> str:
    r = immich_post("/albums", json_data={"albumName": name})
    r.raise_for_status()
    return r.json()["id"]


def find_or_create_album(name: str) -> str:
    for album in get_all_albums():
        if album.get("albumName") == name:
            return album["id"]
    return create_album(name)


def add_assets_to_album(album_id: str, asset_ids: list[str]):
    if not asset_ids:
        return
    r = immich_put(f"/albums/{album_id}/assets", json_data={"ids": asset_ids})
    r.raise_for_status()


def update_description(asset_id: str, description: str):
    """Write description to Immich asset metadata."""
    try:
        immich_put(f"/assets/{asset_id}", json_data={"description": description})
    except Exception as e:
        log.debug("Failed to update description for %s: %s", asset_id, e)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def check_ollama() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def analyze_image(thumbnail_bytes: bytes) -> tuple[str, str, str]:
    """Send thumbnail to Ollama, return (category, reason, description)."""
    b64 = base64.b64encode(thumbnail_bytes).decode("ascii")
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT, "images": [b64]},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
    r.raise_for_status()
    data = r.json()
    msg = data.get("message", {})
    content = msg.get("content", "").strip()
    thinking = msg.get("thinking", "")

    # Primary: parse content field; fallback: parse thinking
    text = content if content else thinking
    if not text:
        return "KEEP", "empty response", ""

    return parse_response(text)


def parse_response(text: str) -> tuple[str, str, str]:
    """Parse 'CATEGORY: reason' and 'DESCRIPTION: ...' from model output."""
    # Strip <think>...</think> blocks
    clean = text
    while "<think>" in clean:
        start = clean.index("<think>")
        end = clean.find("</think>")
        if end == -1:
            clean = clean[:start]
        else:
            clean = clean[:start] + clean[end + len("</think>"):]
    clean = clean.strip()

    # Extract category
    category = "KEEP"
    reason = ""
    for cat in CATEGORIES:
        idx = clean.upper().find(cat)
        if idx != -1:
            category = cat
            after = clean[idx + len(cat):]
            # Take text after "CATEGORY:" until newline
            reason = after.lstrip(":").strip().split("\n")[0].strip()
            if not reason:
                reason = cat.lower()
            break

    # Extract description
    description = ""
    m = re.search(r"DESCRIPTION:\s*(.+)", clean, re.IGNORECASE)
    if m:
        description = m.group(1).strip()

    return category, reason, description


# ---------------------------------------------------------------------------
# Health checks with retry
# ---------------------------------------------------------------------------


def wait_for_services():
    for name, check_fn in [("Immich", check_immich), ("Ollama", check_ollama)]:
        for attempt in range(1, 11):
            if check_fn():
                log.info("%s is available", name)
                break
            log.warning("%s unavailable, retry %d/10 in 30s…", name, attempt)
            time.sleep(30)
        else:
            log.error("%s not reachable after 10 retries, exiting", name)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def process_asset(asset_id: str) -> tuple[str, str, str, str] | None:
    """Download thumbnail, analyze, update description.
    Returns (asset_id, category, reason, description) or None."""
    thumb = get_thumbnail(asset_id)
    if thumb is None:
        return None
    category, reason, description = analyze_image(thumb)
    if WRITE_DESCRIPTIONS and description:
        update_description(asset_id, description)
    return asset_id, category, reason, description


def flush_batches(
    conn: sqlite3.Connection,
    album_ids: dict[str, str],
    batches: dict[str, list[str]],
):
    """Add accumulated assets to albums and clear batches."""
    for cat, ids in batches.items():
        if not ids:
            continue
        album_id = album_ids.get(cat)
        if not album_id:
            continue
        try:
            add_assets_to_album(album_id, ids)
            log.info("Added %d assets to %s", len(ids), cat)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                name = ALBUM_TRASH if cat == "TRASH" else ALBUM_REVIEW
                album_ids[cat] = find_or_create_album(name)
                add_assets_to_album(album_ids[cat], ids)
                log.info("Recreated album %s, added %d assets", name, len(ids))
            else:
                raise
    batches["TRASH"].clear()
    batches["REVIEW"].clear()


def collect_asset_ids(
    processed_ids: set,
    updated_after: str | None = None,
) -> list[str]:
    """Collect unprocessed asset IDs from Immich."""
    page = 1
    all_ids = []

    while not shutdown_requested:
        try:
            data = search_assets(page=page, updated_after=updated_after)
        except requests.RequestException as e:
            log.error("Search failed on page %d: %s", page, e)
            break

        items = data.get("assets", {}).get("items", [])
        if not items:
            break

        for item in items:
            aid = item["id"]
            if aid not in processed_ids:
                all_ids.append(aid)
                if MAX_ASSETS and len(all_ids) >= MAX_ASSETS:
                    break

        if MAX_ASSETS and len(all_ids) >= MAX_ASSETS:
            break

        next_page = data.get("assets", {}).get("nextPage")
        if next_page is None:
            break
        page = int(next_page) if next_page else page + 1

    return all_ids


def run_scan(
    conn: sqlite3.Connection,
    processed_ids: set,
    album_ids: dict[str, str],
    updated_after: str | None = None,
):
    """Run a scan (bulk or incremental). Returns number of processed photos."""
    counts = {"TRASH": 0, "REVIEW": 0, "KEEP": 0}
    batches: dict[str, list[str]] = {"TRASH": [], "REVIEW": []}
    total_processed = 0
    scan_start = time.time()

    all_asset_ids = collect_asset_ids(processed_ids, updated_after)
    total_assets = len(all_asset_ids)

    if total_assets == 0:
        log.info("No new assets to process")
        return 0

    log.info("Found %d unprocessed assets", total_assets)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {}
        idx = 0

        for _ in range(min(CONCURRENCY * 2, total_assets)):
            if idx < total_assets:
                f = executor.submit(process_asset, all_asset_ids[idx])
                futures[f] = all_asset_ids[idx]
                idx += 1

        while futures:
            if shutdown_requested:
                log.info("Shutdown requested, flushing remaining batches…")
                flush_batches(conn, album_ids, batches)
                return total_processed

            for future in as_completed(futures):
                aid = futures.pop(future)
                try:
                    result = future.result()
                except Exception as e:
                    log.warning("Error %s: %s, skipping", aid[:8], e)
                    total_processed += 1
                    # Submit next to keep the pipeline full
                    if idx < total_assets:
                        f = executor.submit(process_asset, all_asset_ids[idx])
                        futures[f] = all_asset_ids[idx]
                        idx += 1
                    break

                total_processed += 1
                if result is not None:
                    _, category, reason, description = result
                    save_result(conn, aid, category, reason, description)
                    processed_ids.add(aid)
                    counts[category] += 1
                    if category in batches:
                        batches[category].append(aid)
                    log.info(
                        "[%d/%d] %s: %s | %s",
                        total_processed, total_assets, category,
                        reason[:50], (description or "")[:50],
                    )

                if total_processed % BATCH_SIZE == 0:
                    flush_batches(conn, album_ids, batches)

                # Submit next to keep the pipeline full
                if idx < total_assets:
                    f = executor.submit(process_asset, all_asset_ids[idx])
                    futures[f] = all_asset_ids[idx]
                    idx += 1

                break  # one future at a time for ordering

    flush_batches(conn, album_ids, batches)

    elapsed = time.time() - scan_start
    log.info(
        "Scan complete: %d assets in %.0fs — TRASH: %d, REVIEW: %d, KEEP: %d",
        total_processed,
        elapsed,
        counts["TRASH"],
        counts["REVIEW"],
        counts["KEEP"],
    )
    return total_processed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not IMMICH_API_KEY:
        log.error("IMMICH_API_KEY is required")
        sys.exit(1)

    log.info(
        "Immich Cleaner starting — mode=%s, model=%s, concurrency=%d, descriptions=%s",
        MODE, OLLAMA_MODEL, CONCURRENCY, WRITE_DESCRIPTIONS,
    )
    log.info("Immich: %s | Ollama: %s", IMMICH_API_URL, OLLAMA_URL)

    wait_for_services()

    conn = init_db()
    processed_ids = load_processed_ids(conn)
    log.info("Loaded %d previously processed assets", len(processed_ids))

    album_ids = {
        "TRASH": find_or_create_album(ALBUM_TRASH),
        "REVIEW": find_or_create_album(ALBUM_REVIEW),
    }
    set_state(conn, "album_to_delete", album_ids["TRASH"])
    set_state(conn, "album_to_review", album_ids["REVIEW"])
    log.info("Albums ready — %s: %s, %s: %s", ALBUM_TRASH, album_ids["TRASH"], ALBUM_REVIEW, album_ids["REVIEW"])

    run_scan(conn, processed_ids, album_ids)
    set_state(conn, "last_scan_at", datetime.now(timezone.utc).isoformat())

    if MODE == "once":
        log.info("Mode=once, exiting")
        conn.close()
        return

    log.info("Entering continuous mode, checking every %ds", CHECK_INTERVAL)
    while not shutdown_requested:
        for _ in range(CHECK_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

        if shutdown_requested:
            break

        log.info("Starting incremental scan…")
        last_scan = get_state(conn, "last_scan_at")

        if not check_immich() or not check_ollama():
            log.warning("Service unavailable, retrying in 60s…")
            retries = 0
            while retries < 5 and not shutdown_requested:
                time.sleep(60)
                if check_immich() and check_ollama():
                    break
                retries += 1
            else:
                if not shutdown_requested:
                    log.warning("Services still unavailable after retries, sleeping until next interval")
                    continue

        run_scan(conn, processed_ids, album_ids, updated_after=last_scan)
        set_state(conn, "last_scan_at", datetime.now(timezone.utc).isoformat())

    log.info("Shutdown complete")
    conn.close()


if __name__ == "__main__":
    main()
