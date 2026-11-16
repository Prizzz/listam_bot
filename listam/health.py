"""Tiny HTTP server for Render's free web service: a health route for the external pinger.

Render requires a free *web* service to listen on $PORT and puts it to sleep after 15 minutes
without inbound traffic, so an external pinger (e.g. UptimeRobot) should hit /health every ~5 min.
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

log = logging.getLogger("listam.health")


async def start_health(port: int, monitor) -> web.AppRunner:
    started = time.time()

    async def health(request: web.Request) -> web.Response:
        ok = monitor.last_cycle_ok
        return web.json_response({
            "status": "ok",
            "uptime_s": int(time.time() - started),
            "last_cycle_s_ago": int(time.time() - ok) if ok else None,
        })

    app = web.Application()
    app.router.add_get("/health", health)  # HEAD is allowed too, which some pingers use
    app.router.add_get("/", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health endpoint on :%d/health", port)
    return runner
