"""Polling loop: fetch each saved search from its source, diff against the DB, emit alerts."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List

from .config import Config
from .db import DB
from .fetcher import Blocked, Waiting
from .parser import YEREVAN, Card, desc_hash

log = logging.getLogger("listam.monitor")

Send = Callable[[int, str], Awaitable[None]]  # (chat_id, html_text)
LABELS = {"listam": "list.am", "banali": "banali.am"}


def fmt_card(kind: str, c: Card, extra: str = "") -> str:
    loc = " · %s" % html.escape(c.location) if c.location else ""
    return "%s · %s\n<b>%s</b>\n%s%s\n%s%s" % (
        kind,
        LABELS.get(c.source, c.source),
        html.escape(c.title),
        html.escape(c.price_text or "price not set"),
        loc,
        extra,
        c.url,
    )


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, YEREVAN).strftime("%d.%m %H:%M")


class Monitor:
    def __init__(self, cfg: Config, db: DB, sources: Dict[str, object], send: Send):
        self.cfg, self.db, self.sources, self.send = cfg, db, sources, send
        self.blocked_notified: Dict[str, bool] = {}
        self.last_cycle_ok = 0.0
        self._recent_lock = asyncio.Lock()

    def max_price(self) -> int:
        saved = self.db.get_setting("max_price")
        return int(saved) if saved else self.cfg.max_price

    async def run(self) -> None:
        while True:
            try:
                await self.cycle()
            except Exception:
                log.exception("cycle failed")
            await asyncio.sleep(self.cfg.poll_interval * random.uniform(0.85, 1.25))

    async def cycle(self) -> None:
        for s in self.db.list_searches(active_only=True):
            name = s["source"]
            try:
                await self.check_search(s)
                if self.blocked_notified.pop(name, False):
                    for chat in self.cfg.allowed_chats:
                        await self.send(chat, "✅ %s is reachable again" % LABELS.get(name, name))
            except Waiting as e:
                log.info("%s: waiting for data (%s)", name, e)
            except Blocked as e:
                log.warning("blocked: %s", e)
                if not self.blocked_notified.get(name):
                    self.blocked_notified[name] = True
                    for chat in self.cfg.allowed_chats:
                        await self.send(chat, "⚠️ %s" % html.escape(str(e)))
            except Exception:
                log.exception("search %s failed", s["id"])
            await asyncio.sleep(random.uniform(1, 4))
        self.last_cycle_ok = time.time()

    async def check_search(self, s) -> None:
        src = self.sources.get(s["source"])
        if src is None:
            return
        sid, chat = s["id"], s["chat_id"]
        cards = await src.fetch_cards(s["url"], self.max_price())
        seeded = bool(s["seeded"])

        new: List[Card] = []
        bumped: List = []
        for c in cards:
            row = self.db.get_item(sid, c.id)
            if row is None:
                self.db.insert_item(sid, c)
                new.append(c)
                continue
            if seeded and c.price is not None and row["price"] is not None and (
                c.price != row["price"] or c.currency != row["currency"]
            ):
                old = "%s%s" % (row["currency"] or "", "{:,}".format(row["price"]))
                arrow = "📉" if c.price < row["price"] else "📈"
                await self.send(chat, fmt_card("%s Price changed" % arrow, c, "was %s\n" % old))
            if (seeded and src.has_timestamps and not c.auto_bump and c.updated_ts and row["renewed_at"]
                    and c.updated_ts > row["renewed_at"]):
                bumped.append(c.id)  # the listing itself tells us it was updated
            self.db.update_item_card(sid, c)

        order = [c.id for c in cards if not c.top]  # promoted ads sit out of date order
        prev_rank = self.db.get_ranks(sid)

        if not seeded:
            for c in new:  # baseline: remember descriptions/dates without alerting
                await self._inspect(s, c, alert=False)
            self.db.set_ranks(sid, order)
            self.db.mark_seeded(sid)
            await self.send(chat, "📌 «%s»: baseline saved, %d ads. Alerts start now."
                            % (html.escape(s["name"]), len(cards)))
            return

        if not src.has_timestamps:
            bumped = self._bumped(order, prev_rank)  # jumped up the update-sorted list

        inspected = set()
        by_id = {c.id: c for c in cards}
        for iid in bumped[: self.cfg.max_bump_checks]:
            await self._inspect(s, by_id[iid], alert=True, moved=not src.has_timestamps)
            inspected.add(iid)

        for c in new[: self.cfg.max_new_alerts]:
            await self._inspect(s, c, alert=False)
            await self.send(chat, fmt_card("🆕 New", c))
        if len(new) > self.cfg.max_new_alerts:
            await self.send(chat, "…and %d more new ads in «%s»" % (len(new) - self.cfg.max_new_alerts, html.escape(s["name"])))

        await self._check_descriptions(s, cards, skip=inspected)
        self.db.set_ranks(sid, order)

    @staticmethod
    def _bumped(order: list, prev_rank: dict) -> list:
        """Ads now sitting above an ad that used to be above them = updated since last pass."""
        out = []
        for i, x in enumerate(order):
            px = prev_rank.get(x)
            if px is None:
                continue
            if any(prev_rank.get(y, -1) != -1 and prev_rank[y] < px for y in order[i + 1:]):
                out.append(x)
        return out

    async def _inspect(self, s, c: Card, alert: bool, moved: bool = False) -> None:
        """Fetch ad details, store description hash + update time, optionally alert on changes."""
        sid, chat = s["id"], s["chat_id"]
        src = self.sources[s["source"]]
        try:
            text, rts = await src.details(c)
        except Blocked:
            raise
        except Exception:
            log.exception("ad details failed for %s", c.id)
            return
        h = desc_hash(text)
        row = self.db.get_item(sid, c.id)
        if alert and row is not None:
            kinds, extra = [], ""
            prev = row["renewed_at"]
            if rts is not None and (rts > prev if prev else rts >= time.time() - 3600):
                kinds.append("♻️ Renewed %s" % _hm(rts))
            elif moved and rts is None:
                kinds.append("♻️ Renewed")  # it jumped up the update-sorted list; the source has no exact time
            if row["desc_hash"] and text is not None and h != row["desc_hash"]:
                kinds.append("✏️ Description updated")
                extra = "%s\n" % html.escape((text[:300] + "…") if len(text) > 300 else text)
            if kinds:
                await self.send(chat, fmt_card(" · ".join(kinds), c, extra))
        if text is None and row is not None and row["desc_hash"]:
            text, h = row["desc_text"], row["desc_hash"]  # source gave no text this time: keep what we knew
        self.db.set_page_info(sid, c.id, text, h, rts)
        await asyncio.sleep(random.uniform(1.5, 4) if not src.has_timestamps else random.uniform(0.2, 0.6))

    async def _check_descriptions(self, s, cards: List[Card], skip=()) -> None:
        by_id = {c.id: c for c in cards}
        ids = [i for i in by_id if i not in skip]
        for row in self.db.stale_description_items(s["id"], ids, self.cfg.desc_checks_per_cycle):
            await self._inspect(s, by_id[row["item_id"]], alert=True)

    async def recent(self, chat_id: int, hours: int = 24) -> None:
        """Send ads posted or renewed within the last `hours` (one-off, on demand)."""
        if self._recent_lock.locked():
            await self.send(chat_id, "⏳ A /recent run is already in progress.")
            return
        async with self._recent_lock:
            cutoff = int(time.time() - hours * 3600)
            total = 0
            for s in self.db.list_searches(chat_id):
                src = self.sources.get(s["source"])
                if src is None:
                    continue
                found = 0
                try:
                    async for c, posted, last in src.recent(s["url"], self.max_price(), cutoff):
                        found += 1
                        kind = "🆕 Posted" if posted and posted >= cutoff else "♻️ Renewed"
                        await self.send(chat_id, fmt_card("%s %s" % (kind, _hm(last)), c))
                except Blocked as e:
                    await self.send(chat_id, "⚠️ %s" % html.escape(str(e)))
                total += found
                await self.send(chat_id, "✅ %s: %d ads from the last %dh" % (LABELS.get(s["source"], s["source"]), found, hours))
