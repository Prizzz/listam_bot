"""Hugging Face Space (Gradio SDK) entry point.

Runs the Telegram bot + monitor in the background of one process, serves GET/HEAD /health for an
external pinger (Spaces on the free tier sleep after 48 h without visitors) and a tiny status page.
list.am sits behind Cloudflare, which rejects this Space's IP even for a real browser. So a GitHub Actions
runner scrapes list.am (see .github/workflows/scrape.yml) and POSTs the snapshot to /ingest.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

# --- Space defaults: ephemeral disk, real browser. Secrets come from the Space settings. ---------
os.environ.setdefault("LISTAM_FETCHER", "snapshot")  # list.am data comes from a GitHub runner via /ingest
os.environ.setdefault("EPHEMERAL_STATE", "1")
os.environ.setdefault("DB_PATH", "/tmp/listam.db")
os.environ.setdefault("PROFILE_DIR", "/tmp/browser-profile")
os.environ.setdefault("GRADIO_SSR_MODE", "false")  # Gradio 6 SSR would start a Node server on our port
os.environ.pop("PORT", None)  # /health is served by the app below, not by listam.health

# ZeroGPU Spaces refuse to start without at least one @spaces.GPU function ("No @spaces.GPU function
# detected during startup"). The bot needs no GPU: this placeholder is never called and only satisfies
# that check. Where the `spaces` package does not exist, it is skipped.
try:
    import spaces  # noqa: E402

    @spaces.GPU
    def _zerogpu_startup_check() -> None:
        return None
except ImportError:
    pass

import gradio as gr  # noqa: E402  (provided by the Gradio SDK image)
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from listam.__main__ import run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("space")
holder: dict = {}
started = time.time()


def prepare_runtime() -> None:
    """Only for LISTAM_FETCHER=browser: virtual display + Chromium (system libs: packages.txt)."""
    if os.environ.get("LISTAM_FETCHER") != "browser":
        return
    if shutil.which("Xvfb"):
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1366x850x24", "-nolisten", "tcp"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ["DISPLAY"] = ":99"
        time.sleep(1)
    else:
        log.error("Xvfb not found: add 'xvfb' to packages.txt")
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], capture_output=True, text=True)
    log.info("playwright install chromium -> rc=%s %s", r.returncode, (r.stderr or r.stdout)[-300:].strip())


async def supervise() -> None:
    """Keep the bot alive: a crash or a config error must not take the web server down."""
    await asyncio.to_thread(prepare_runtime)
    while True:
        try:
            await run(holder, handle_signals=False)
        except asyncio.CancelledError:
            raise
        except SystemExit as e:  # e.g. BOT_TOKEN missing
            log.error("config error: %s", e)
            await asyncio.sleep(60)
        except Exception:
            log.exception("bot crashed, restarting in 30s")
            await asyncio.sleep(30)


async def ingest(request: Request):
    """The GitHub runner pushes its list.am snapshot here (X-Ingest-Key must match INGEST_KEY)."""
    key = os.environ.get("INGEST_KEY", "")
    if not key or not hmac.compare_digest(request.headers.get("x-ingest-key", ""), key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        data = await request.json()
        cards = data["cards"]
        assert isinstance(cards, list) and isinstance(data.get("details", {}), dict)
        for c in cards:
            for field in ("id", "url", "title", "price_text", "price", "currency", "location"):
                assert field in c
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)
    holder["snapshot"] = {"received_at": time.time(), "fetched_at": data.get("fetched_at"),
                          "cards": cards, "details": data.get("details", {})}
    log.info("snapshot received: %d cards, %d with details", len(cards), len(holder["snapshot"]["details"]))
    return {"ok": True, "cards": len(cards)}


def health() -> dict:
    monitor = holder.get("monitor")
    ok = getattr(monitor, "last_cycle_ok", 0.0)
    return {
        "status": "ok",
        "bot": "running" if monitor else "starting",
        "uptime_s": int(time.time() - started),
        "snapshot_age_s": int(time.time() - holder["snapshot"]["received_at"]) if holder.get("snapshot") else None,
        "last_cycle_s_ago": int(time.time() - ok) if ok else None,
    }


with gr.Blocks(title="listam") as demo:
    gr.Markdown("### listam\nTelegram monitor for list.am and banali.am. Nothing to do here: see the bot.")
    if "_zerogpu_startup_check" in globals():
        # ZeroGPU only counts @spaces.GPU functions that are wired to a Gradio event; never clicked.
        gr.Button("noop", visible=False).click(_zerogpu_startup_check)  # noqa: F821


if __name__ == "__main__":
    # The bot lives in its own thread/event loop; Gradio keeps the main thread and the port.
    threading.Thread(target=lambda: asyncio.run(supervise()), name="listam-bot", daemon=True).start()
    fastapi_app, _, _ = demo.launch(server_name="0.0.0.0", server_port=7860, ssr_mode=False,
                                    prevent_thread_lock=True)
    # /health (GET and HEAD) for the external pinger; first in line so Gradio's routes cannot shadow it.
    fastapi_app.router.routes.insert(0, APIRoute("/health", health, methods=["GET", "HEAD"]))
    fastapi_app.router.routes.insert(0, APIRoute("/ingest", ingest, methods=["POST"]))
    demo.block_thread()
