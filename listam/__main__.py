from __future__ import annotations

import asyncio
import logging
import os

from .bot import make_bot
from .config import Config
from .db import DB
from .fetcher import Fetcher, HttpFetcher
from .monitor import Monitor
from .sources import BanaliSource, ListAmSource


# Informational only: the banali search is built in code (sources.py), this is just its identity.
BANALI_URL = "https://banali.am/en/rent/real-estate-yerevan"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    holder: dict = {}
    bot, dp = make_bot(cfg, db, holder)
    fetcher = Fetcher(cfg.profile_dir, cfg.proxy) if cfg.fetcher == "browser" else HttpFetcher(cfg.proxy)
    logging.getLogger("listam").info("list.am fetcher: %s", cfg.fetcher)

    async def send(chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=False)
        except Exception:
            logging.getLogger("listam").exception("send failed")
        await asyncio.sleep(0.4)  # stay under Telegram flood limits

    sources = {"listam": ListAmSource(fetcher, cfg.exclude_locations)}
    if cfg.banali:
        sources["banali"] = BanaliSource(cfg.exclude_locations, cfg.banali_pages)
    monitor = Monitor(cfg, db, sources, send)
    holder["monitor"] = monitor
    try:
        await asyncio.gather(dp.start_polling(bot), monitor.run())
    finally:
        await fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
