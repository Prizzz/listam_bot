from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .bot import make_bot
from .config import Config
from .db import DB
from .fetcher import Fetcher, HttpFetcher
from .health import start_health
from .monitor import Monitor
from .sources import BanaliSource, ListAmSource, SnapshotListAmSource


# Informational only: the banali search is built in code (sources.py), this is just its identity.
BANALI_URL = "https://banali.am/en/rent/real-estate-yerevan"


async def run(holder: Optional[dict] = None, handle_signals: bool = True) -> None:
    """Run the bot + monitor until cancelled. `holder` receives the Monitor (for an external /health)."""
    cfg = Config.from_env()
    os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
    db = DB(cfg.db_path)
    from .config import with_max_price
    saved = db.get_setting("max_price")
    max_price = int(saved) if saved else cfg.max_price
    for chat in cfg.allowed_chats:
        db.ensure_search(chat, "list.am · Yerevan rent", with_max_price(cfg.search_url, max_price), "listam")
        if cfg.banali:
            db.ensure_search(chat, "banali.am · Yerevan rent", BANALI_URL, "banali")
    holder = holder if holder is not None else {}
    bot, dp = make_bot(cfg, db, holder)
    fetcher = None
    if cfg.fetcher == "browser":
        fetcher = Fetcher(cfg.profile_dir, cfg.proxy, cfg.challenge_timeout)
    elif cfg.fetcher == "http":
        fetcher = HttpFetcher(cfg.proxy)
    logging.getLogger("listam").info("list.am fetcher: %s", cfg.fetcher)

    async def send(chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=False)
        except Exception:
            logging.getLogger("listam").exception("send failed")
        await asyncio.sleep(0.4)  # stay under Telegram flood limits

    if cfg.fetcher == "snapshot":
        sources = {"listam": SnapshotListAmSource(holder, cfg.exclude_locations)}
    else:
        sources = {"listam": ListAmSource(fetcher, cfg.exclude_locations)}
    if cfg.banali:
        sources["banali"] = BanaliSource(cfg.exclude_locations, cfg.banali_pages)
    monitor = Monitor(cfg, db, sources, send)
    holder["monitor"] = monitor
    health = await start_health(cfg.port, monitor) if cfg.port else None
    try:
        await asyncio.gather(dp.start_polling(bot, handle_signals=handle_signals), monitor.run())
    finally:
        if health:
            await health.cleanup()
        if fetcher is not None:
            await fetcher.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await run()


if __name__ == "__main__":
    asyncio.run(main())
