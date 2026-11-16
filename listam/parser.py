"""HTML parsing for list.am listing pages and item pages."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import List, Optional

from bs4 import BeautifulSoup

ITEM_RE = re.compile(r"/item/(\d+)")
BASE = "https://www.list.am"
YEREVAN = timezone(timedelta(hours=4))  # no DST in Armenia


@dataclass
class Card:
    id: object  # int for list.am, uuid str for banali
    url: str
    title: str
    price_text: str  # e.g. "$27,000" or "" when no price
    price: Optional[int]  # numeric part, None if absent
    currency: str  # "$", "֏", "€" ... or ""
    location: str
    top: bool = False
    source: str = "listam"
    auto_bump: bool = False           # update was a scheduled/automatic raise, not an owner action
    amd: Optional[int] = None         # price converted to AMD, when the source needs it
    posted_ts: Optional[int] = None   # unix seconds, when the source gives it in the listing
    updated_ts: Optional[int] = None  # last update/raise time, when the source gives it in the listing


def _digits(s: str) -> Optional[int]:
    d = re.sub(r"[^\d]", "", s)
    return int(d) if d else None


def parse_listing(html: str) -> List[Card]:
    soup = BeautifulSoup(html, "lxml")
    cards: List[Card] = []
    seen = set()
    for a in soup.select('a[href*="/item/"]'):
        m = ITEM_RE.search(a.get("href", ""))
        if not m:
            continue
        item_id = int(m.group(1))
        # Only real cards (they carry the testid), skip menu/promo links.
        if not (a.get("data-testid") or "").startswith("favorite-ad-card-"):
            continue
        if item_id in seen:
            continue
        seen.add(item_id)

        amount = a.select_one(".category-data-list-card__amount")
        currency = ""
        price_text = ""
        price = None
        if amount:
            cur = amount.select_one(".category-data-list-card__currency")
            currency = cur.get_text(strip=True) if cur else ""
            price_text = amount.get_text("", strip=True)
            price = _digits(price_text)

        title_el = a.select_one("div.l")
        loc_el = a.select_one(".category-data-list-card__location")
        top = a.find_parent(class_="toppost") is not None or a.find_parent(id="tp") is not None
        cards.append(
            Card(
                id=item_id,
                url="%s/en/item/%d" % (BASE, item_id),
                title=title_el.get_text(" ", strip=True) if title_el else "",
                price_text=price_text,
                price=price,
                currency=currency,
                location=loc_el.get_text(" ", strip=True) if loc_el else "",
                top=top,
            )
        )
    return cards


def parse_item_description(html: str) -> Optional[str]:
    """Return normalized description text, or None if the page has none."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one('[data-testid="post-view-description"]')
    if not el:
        return None
    for t in el.select(".trans"):  # "Translated from Armenian" note
        t.decompose()
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True))


def desc_hash(text: Optional[str]) -> str:
    if not text:
        return ""  # "no description known" must never compare as a change
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def parse_item_dates(html: str):
    """Return (posted, renewed) as aware datetimes; either may be None."""
    posted = renewed = None
    m = re.search(r'itemprop="datePosted"\s+content="([^"]+)"', html)
    if m:
        try:
            posted = datetime.fromisoformat(m.group(1))
        except ValueError:
            pass
    m = re.search(r"Renewed\s+(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}):(\d{2})", html)
    if m:
        d, mo, y, hh, mm = (int(x) for x in m.groups())
        renewed = datetime(y, mo, d, hh, mm, tzinfo=YEREVAN)
    return posted, renewed
