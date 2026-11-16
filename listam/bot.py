"""Telegram bot: alerts + a few commands. Only ALLOWED_CHAT_IDS can use everything but /start."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .config import Config, with_max_price
from .db import DB

log = logging.getLogger("listam.bot")

HELP = (
    "/recent [hours] — ads posted or renewed in the last day (default 24h)\n"
    "/maxprice &lt;AMD&gt; — change the max price, e.g. <code>/maxprice 250000</code>\n"
    "/status — current settings"
)


def current_max_price(cfg: Config, db: DB) -> int:
    saved = db.get_setting("max_price")
    return int(saved) if saved else cfg.max_price


def make_bot(cfg: Config, db: DB, holder: dict):
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    allowed = F.chat.id.in_(cfg.allowed_chats)

    @dp.message(Command("start"))
    async def start(m: Message):
        # Open to everyone on purpose: it only reveals the chat id needed for ALLOWED_CHAT_IDS.
        if m.chat.id in cfg.allowed_chats:
            await m.answer("Watching list.am for you.\n" + HELP + "\n\nYour chat id: <code>%d</code>" % m.chat.id)
        else:
            await m.answer("Your chat id: <code>%d</code>\nNot in ALLOWED_CHAT_IDS." % m.chat.id)

    @dp.message(Command("status"), allowed)
    async def status(m: Message):
        lines = []
        for r in db.list_searches(m.chat.id):
            n = db.conn.execute("SELECT COUNT(*) FROM items WHERE search_id=?", (r["id"],)).fetchone()[0]
            lines.append("• %s — %d ads tracked%s" % (html.escape(r["name"]), n, "" if r["active"] else " (paused)"))
        await m.answer(
            "Max price: <b>%s ֏</b>\nPoll: every ~%d s\n%s"
            % ("{:,}".format(current_max_price(cfg, db)), cfg.poll_interval, "\n".join(lines) or "no searches")
        )

    @dp.message(Command("maxprice"), allowed)
    async def maxprice(m: Message, command: CommandObject):
        raw = re.sub(r"[\s,._֏]|amd", "", (command.args or "").lower())
        if not raw.isdigit() or not (10_000 <= int(raw) <= 100_000_000):
            return await m.answer("Usage: <code>/maxprice 250000</code> (AMD, 10,000–100,000,000)\nNow: %s ֏"
                                  % "{:,}".format(current_max_price(cfg, db)))
        price = int(raw)
        db.set_setting("max_price", str(price))
        for r in db.list_searches(m.chat.id):
            if r["source"] == "listam":  # list.am carries the price in its URL; banali is filtered in code
                db.set_url_keep_items(m.chat.id, with_max_price(r["url"], price), "listam")
        db.reseed(m.chat.id)
        await m.answer("✅ Max price is now <b>%s ֏</b>. Ads that newly fit are absorbed silently on the "
                       "next check; use /recent to see the fresh ones.%s"
                       % ("{:,}".format(price),
                          "\n\n⚠️ No persistent disk: this resets on restart. Set MAX_PRICE=%d in the service "
                          "environment to keep it." % price if cfg.ephemeral else ""))

    @dp.message(Command("recent"), allowed)
    async def recent(m: Message, command: CommandObject):
        arg = (command.args or "").strip()
        hours = int(arg) if arg.isdigit() and 1 <= int(arg) <= 168 else 24
        await m.answer("🔎 Looking at ads from the last %dh. This opens each ad page, so it takes a few minutes…" % hours)
        asyncio.create_task(holder["monitor"].recent(m.chat.id, hours))

    return bot, dp
