"""Tiny HTTP server for a free web service (Render): /health for the pinger and /ingest for snapshots.

* GET/HEAD /health, / : status JSON. A free Render web service sleeps after 15 minutes without inbound
  traffic, so something has to hit it at least that often (the GitHub runner's POSTs do, and an
  external pinger such as UptimeRobot is a good backup).
* POST /ingest : the GitHub runner pushes its list.am snapshot here (header X-Ingest-Key = INGEST_KEY).
"""
from __future__ import annotations

import hmac
import logging
import os
import time

from aiohttp import web

log = logging.getLogger("listam.health")

CARD_FIELDS = ("id", "url", "title", "price_text", "price", "currency", "location")


def parse_snapshot(data) -> dict:
    """Validate a runner payload and turn it into the snapshot dict the source reads."""
    cards, details = data["cards"], data.get("details", {})
    if not isinstance(cards, list) or not isinstance(details, dict):
        raise ValueError("cards must be a list and details an object")
    for c in cards:
        for field in CARD_FIELDS:
            if field not in c:
                raise ValueError("card without %s" % field)
    return {"received_at": time.time(), "fetched_at": data.get("fetched_at"), "cards": cards, "details": details}


async def start_health(port: int, monitor, holder: dict = None) -> web.AppRunner:
    started = time.time()
    holder = holder if holder is not None else {}

    async def health(request: web.Request) -> web.Response:
        ok = monitor.last_cycle_ok
        snap = holder.get("snapshot")
        return web.json_response({
            "status": "ok",
            "uptime_s": int(time.time() - started),
            "snapshot_age_s": int(time.time() - snap["received_at"]) if snap else None,
            "last_cycle_s_ago": int(time.time() - ok) if ok else None,
        })

    async def ingest(request: web.Request) -> web.Response:
        key = os.environ.get("INGEST_KEY", "")
        if not key or not hmac.compare_digest(request.headers.get("X-Ingest-Key", ""), key):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            snap = parse_snapshot(await request.json())
        except Exception:
            return web.json_response({"error": "bad payload"}, status=400)
        holder["snapshot"] = snap
        log.info("snapshot received: %d cards, %d with details", len(snap["cards"]), len(snap["details"]))
        return web.json_response({"ok": True, "cards": len(snap["cards"])})

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/health", health)  # HEAD is allowed too, which some pingers use
    app.router.add_get("/", health)
    app.router.add_post("/ingest", ingest)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("http server on :%d (/health, /ingest)", port)
    return runner
