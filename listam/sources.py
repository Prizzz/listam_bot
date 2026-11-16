"""Ad sources. Each one turns "the search" into a list of Cards and can open one ad for details.

Interface used by the monitor:
    name, label, has_timestamps
    async fetch_cards(url, max_price) -> List[Card]      (raises Blocked when the site refuses us)
    async details(card)               -> (description | None, updated_ts | None)
    async recent(url, max_price, cutoff_ts) -> async iterator of (card, posted_ts, last_ts)
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional, Tuple

import aiohttp

from .fetcher import Blocked, Fetcher, Waiting
from .parser import Card, parse_item_dates, parse_item_description, parse_listing

log = logging.getLogger("listam.sources")


def norm(s: str) -> str:
    """'Nor-Nork' / 'Nor Nork' / 'nor nork' -> 'nornork' (for district comparisons)."""
    return re.sub(r"[^a-z]", "", (s or "").lower())


class ListAmSource:
    name = "listam"
    label = "list.am"
    has_timestamps = False  # the listing has no dates: order inversions + ad pages are used instead

    def __init__(self, fetcher: Fetcher, exclude: List[str]):
        self.fetcher = fetcher
        self.exclude = {norm(x) for x in exclude}

    def _keep(self, cards: List[Card]) -> List[Card]:
        return [c for c in cards if norm(c.location) not in self.exclude]

    async def fetch_cards(self, url: str, max_price: int) -> List[Card]:
        cards = parse_listing(await self.fetcher.get(url))
        if not cards:
            raise Blocked("list.am: no ads parsed from the page (layout changed or soft block)")
        return self._keep(cards)  # max price is already part of the URL (price2=)

    async def details(self, card: Card) -> Tuple[Optional[str], Optional[int]]:
        page = await self.fetcher.get(card.url)
        _, renewed = parse_item_dates(page)
        return parse_item_description(page), int(renewed.timestamp()) if renewed else None

    async def recent(self, url: str, max_price: int, cutoff_ts: int, max_checked: int = 120):
        cards = await self.fetch_cards(url, max_price)
        checked = old_streak = 0
        for c in cards:  # sorted by last update; promoted "top" ads are out of order
            if checked >= max_checked:
                break
            page = await self.fetcher.get(c.url)
            checked += 1
            posted, renewed = parse_item_dates(page)
            last = max([d for d in (posted, renewed) if d], default=None)
            if last is not None and int(last.timestamp()) >= cutoff_ts:
                old_streak = 0
                yield c, int(posted.timestamp()) if posted else None, int(last.timestamp())
            elif not c.top:
                old_streak += 1
                if old_streak >= 3:  # everything after is older
                    break
            await asyncio.sleep(random.uniform(1.0, 2.5))


class SnapshotListAmSource:
    """list.am data pushed by a GitHub Actions runner (a real browser from an IP Cloudflare accepts).

    The runner POSTs {fetched_at, cards, details} to /ingest; this source serves that snapshot. Details
    (description, posted/renewed time) exist only for the newest ads, which is where renewals show up.
    """

    name = "listam"
    label = "list.am"
    has_timestamps = False  # order inversions detect renewals, details confirm them

    def __init__(self, holder: dict, exclude: List[str], max_age: int = 90 * 60, grace: int = 45 * 60):
        self.holder = holder
        self.exclude = {norm(x) for x in exclude}
        self.max_age, self.grace = max_age, grace
        self.started = time.time()

    def _snap(self) -> dict:
        s = self.holder.get("snapshot")
        if s is None:
            if time.time() - self.started < self.grace:
                raise Waiting("no snapshot received from the GitHub runner yet")
            raise Blocked("list.am: no snapshot has arrived from the GitHub runner for %d min" % (self.grace // 60))
        age = time.time() - s["received_at"]
        if age > self.max_age:
            raise Blocked("list.am: the latest snapshot is %d min old (is the GitHub workflow running?)" % (age // 60))
        return s

    async def fetch_cards(self, url: str, max_price: int) -> List[Card]:
        out = []
        for c in self._snap()["cards"]:
            card = Card(id=int(c["id"]), url=c["url"], title=c["title"], price_text=c["price_text"], price=c["price"],
                        currency=c["currency"], location=c["location"], top=bool(c.get("top")))
            if card.price is not None and card.currency in ("", "\u058f") and card.price > max_price:
                continue
            if norm(card.location) in self.exclude:
                continue
            out.append(card)
        return out

    async def details(self, card: Card) -> Tuple[Optional[str], Optional[int]]:
        d = self._snap().get("details", {}).get(str(card.id))
        return (d.get("desc"), d.get("renewed")) if d else (None, None)

    async def recent(self, url: str, max_price: int, cutoff_ts: int):
        details = self._snap().get("details", {})
        old_streak = 0
        for c in await self.fetch_cards(url, max_price):
            d = details.get(str(c.id))
            if d is None:
                if c.top:
                    continue
                break  # no details beyond the newest ads: cannot judge further
            last = max([t for t in (d.get("posted"), d.get("renewed")) if t], default=0)
            if last >= cutoff_ts:
                old_streak = 0
                yield c, d.get("posted"), last
            elif not c.top:
                old_streak += 1
                if old_streak >= 3:
                    break


CURRENCY = {"051": "֏", "840": "$", "978": "€", "643": "₽"}
API = "https://banali.am"
# Yerevan districts we want, by govId (Ajapnyak, Arabkir, Erebuni, Kentron, Davtashen,
# Malatia-Sebastia, Shengavit, Kanaker-Zeytun). Left out: Avan, Nor Nork, Nork-Marash, Nubarashen.
BANALI_DISTRICTS = [10010130, 10010338, 10010536, 10010635, 10010437, 10010734, 10011138, 10011237]


def _ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(datetime.strptime(s[:19] + "+0000", "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except ValueError:
        return None


class BanaliSource:
    """banali.am: a plain JSON API, no browser or Cloudflare needed."""

    name = "banali"
    label = "banali.am"
    has_timestamps = True  # every listed ad carries publishedAt / updatedToTop

    def __init__(self, exclude: List[str], pages: int = 3):
        self.exclude = {norm(x) for x in exclude}
        self.pages = pages
        self._rates: Optional[dict] = None
        self._rates_at = 0.0
        self._district_en: Optional[dict] = None

    # --- http -----------------------------------------------------------
    async def _json(self, method: str, path: str, body: Optional[dict] = None):
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=headers) as s:
                async with s.request(method, API + path, json=body) as r:
                    if r.status != 200:
                        raise Blocked("banali.am: HTTP %d for %s" % (r.status, path.split("?")[0]))
                    return json.loads(await r.text())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise Blocked("banali.am: %s" % e)

    async def _usd_rate(self) -> float:
        if self._rates is None or time.time() - self._rates_at > 6 * 3600:
            try:
                data = await self._json("GET", "/api/exchange-rates")
                self._rates = {k: float(v["Rate"]) / float(v.get("Amount", 1) or 1) for k, v in data["rates"].items()}
                self._rates_at = time.time()
            except Exception:
                log.warning("exchange rates unavailable, using fallback")
                self._rates = self._rates or {"USD": 390.0, "EUR": 420.0}
        return self._rates.get("USD", 390.0)

    async def _districts(self) -> dict:
        if self._district_en is None:
            try:
                data = await self._json("GET", "/api/states-districts/?states_place_ids[]=1")
                self._district_en = {d["text"]: d["translations"].get("en", d["text"]) for d in data["data"]}
            except Exception:
                return {}
        return self._district_en

    # --- listing --------------------------------------------------------
    @staticmethod
    def _body() -> dict:
        return {"params": {"sortBy": "date_desc", "filters": {
            "public_code": "", "renovation": [], "structure_type": "", "developer_name": "", "rooms": [],
            "rental_price_type": ["monthly_price"], "property_type": ["apartment"], "is_developer": [],
            "sqm_price": [], "price": [], "monthly_mortgage": [], "area": [], "heating": [], "furniture": [],
            "amenities": [], "bedrooms": [], "floor": [], "has_3D_tour": [], "building_name": "",
            "animals": [], "with_installment": [], "is_favorite": [], "deal": "rent",
            "state_id": ["1"], "district_id": BANALI_DISTRICTS,
        }}}

    async def _page(self, n: int) -> List[dict]:
        data = await self._json("POST", "/api/search/filter-posts?page=%d" % n, self._body())
        return data["data"]["posts"]["data"]

    @staticmethod
    def _url(p: dict) -> str:
        slug = p.get("slug") or {}
        for lang in ("en", "ru", "hy"):  # some ads only have a slug in one language
            if slug.get(lang):
                return "https://banali.am/%s/%s" % (lang, slug[lang])
        return "https://banali.am/en/real-estate-yerevan?public_code=%s" % p.get("publicCode", "")

    async def _card(self, p: dict, usd: float, en: dict) -> Optional[Card]:
        pf = (p.get("priceFull") or [{}])[0]
        try:
            amount = int(float(pf.get("price")))
        except (TypeError, ValueError):
            return None
        cur = CURRENCY.get(pf.get("currency"), "")
        rate = {"$": usd, "֏": 1.0}.get(cur) or (self._rates or {}).get({"€": "EUR", "₽": "RUB"}.get(cur, ""), 0)
        amd = int(amount * rate) if rate else amount
        rooms = (p.get("rooms") or [""])[0]
        area = (p.get("area") or [""])[0]
        floor = (p.get("floor") or [""])[0]
        parts = ["%s-room apartment" % rooms if rooms else "Apartment"]
        if area:
            parts.append("%s m²" % area)
        if floor:
            parts.append("floor %s%s" % (floor, "/%s" % p["buildingFloors"] if p.get("buildingFloors") else ""))
        street = (p.get("address") or "").split(",")[-1].strip()
        if street:
            parts.append(street)
        posted, updated = _ts(p.get("publishedAt")), _ts(p.get("updatedToTop"))
        # banali raises every ad automatically each ~3 days in a nightly batch at 00:00-00:05 UTC.
        # Those are not owner actions, so they must not trigger "renewed" alerts.
        auto = bool(updated and posted and updated != posted and updated % 86400 < 300)
        card = Card(
            id=p["id"],
            url=self._url(p),
            title=", ".join(parts),
            price_text="%s%s" % ("{:,}".format(amount), cur),
            price=amount,
            currency=cur,
            location=en.get(p.get("district"), p.get("district") or ""),
            top=bool(p.get("isPinToTop")),
            source=self.name,
            posted_ts=posted,
            updated_ts=updated or posted,
            auto_bump=auto,
            amd=amd,
        )
        return card

    async def _cards(self, pages: int, max_price: int, stop_before_ts: Optional[int] = None) -> AsyncIterator[Card]:
        usd, en = await self._usd_rate(), await self._districts()
        for n in range(1, pages + 1):
            posts = await self._page(n)
            if not posts:
                if n == 1:
                    raise Blocked("banali.am: empty listing (API changed or blocked)")
                return
            for p in posts:
                if not p.get("active", True):
                    continue
                c = await self._card(p, usd, en)
                if c is None or c.amd > max_price or norm(c.location) in self.exclude:
                    continue
                yield c
            last = _ts(posts[-1].get("updatedToTop")) or 0
            if stop_before_ts is not None and last < stop_before_ts:
                return  # sorted by update time: nothing newer on later pages
            await asyncio.sleep(random.uniform(0.3, 0.9))

    async def fetch_cards(self, url: str, max_price: int) -> List[Card]:
        return [c async for c in self._cards(self.pages, max_price)]

    async def details(self, card: Card) -> Tuple[Optional[str], Optional[int]]:
        data = await self._json("GET", "/api/post/%s" % card.id)
        desc = (data.get("post") or {}).get("description")
        text = re.sub(r"\s+", " ", desc).strip() if isinstance(desc, str) and desc.strip() else None
        await asyncio.sleep(random.uniform(0.2, 0.6))
        return text, None if card.auto_bump else card.updated_ts  # automatic raises are not 'renewals'

    async def recent(self, url: str, max_price: int, cutoff_ts: int):
        async for c in self._cards(15, max_price, stop_before_ts=cutoff_ts):
            stamps = [c.posted_ts] if c.auto_bump else [c.posted_ts, c.updated_ts]
            last = max([t for t in stamps if t], default=0)
            if last >= cutoff_ts:
                yield c, c.posted_ts, last
