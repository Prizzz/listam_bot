from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional


def _ints(s: str) -> List[int]:
    return [int(x) for x in s.replace(" ", "").split(",") if x]


# Long-term apartment rentals, Yerevan, up to 220,000 AMD.
# srt=0 ("By default") orders by the LAST UPDATE (max of posted/renewed), verified on live data;
# srt=3 ("By date") orders by posting date only and would hide renewals.
# n= lists the allowed districts: Ajapnyak 2, Arabkir 3, Davtashen 5, Erebuni 6,
# Kanaker-Zeytun 7, Kentron 8, Malatia-Sebastia 9, Shengavit 13.
# Left out on purpose: Avan 4, Nor Nork 10, Nork-Marash 11, Nubarashen 12.
DEFAULT_SEARCH_URL = (
    "https://www.list.am/en/category/56?n=2,3,5,6,7,8,9,13&price2=220000&crc=0&srt=0"
)
DEFAULT_MAX_PRICE = 220000


def with_max_price(url: str, price: int) -> str:
    """Set/replace price2= in a list.am URL."""
    if re.search(r"([?&])price2=\d*", url):
        return re.sub(r"([?&])price2=\d*", r"\g<1>price2=%d" % price, url)
    return url + ("&" if "?" in url else "?") + "price2=%d" % price


DEFAULT_EXCLUDE = "Nor Nork,Nork-Marash,Norq-Marash,Nubarashen,Avan"


@dataclass
class Config:
    bot_token: str
    allowed_chats: List[int]
    db_path: str = "data/listam.db"
    profile_dir: str = "data/browser-profile"
    proxy: Optional[str] = None
    search_url: str = DEFAULT_SEARCH_URL
    max_price: int = DEFAULT_MAX_PRICE
    search_name: str = "Yerevan rent"
    exclude_locations: List[str] = field(default_factory=list)  # safety net on card.location
    poll_interval: int = 180  # seconds between full cycles
    desc_checks_per_cycle: int = 5  # item pages re-opened per search per cycle
    port: Optional[int] = None  # set by Render ($PORT): start the /health server
    ephemeral: bool = False  # no persistent disk: DB is lost on restart/deploy
    fetcher: str = "http"  # "http" (light, ~40 MB) or "browser" (Chromium under Xvfb, ~1 GB)
    banali: bool = True
    banali_pages: int = 3  # 10 ads per page, newest-updated first
    max_bump_checks: int = 10  # ad pages opened per cycle for ads that jumped up the list
    max_new_alerts: int = 15  # cap per search per cycle, the rest is summarized

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "")
        if not token:
            raise SystemExit("BOT_TOKEN is not set (see .env.example)")
        chats = _ints(os.environ.get("ALLOWED_CHAT_IDS", ""))
        if not chats:
            raise SystemExit("ALLOWED_CHAT_IDS is not set (your Telegram user id)")
        return cls(
            bot_token=token,
            allowed_chats=chats,
            db_path=os.environ.get("DB_PATH", "data/listam.db"),
            profile_dir=os.environ.get("PROFILE_DIR", "data/browser-profile"),
            proxy=os.environ.get("PROXY_URL") or None,
            search_url=os.environ.get("SEARCH_URL") or DEFAULT_SEARCH_URL,
            exclude_locations=[x.strip().lower() for x in os.environ.get("EXCLUDE_LOCATIONS", DEFAULT_EXCLUDE).split(",") if x.strip()],
            poll_interval=int(os.environ.get("POLL_INTERVAL", "180")),
            desc_checks_per_cycle=int(os.environ.get("DESC_CHECKS_PER_CYCLE", "5")),
            max_price=int(os.environ.get("MAX_PRICE", DEFAULT_MAX_PRICE)),
            port=int(os.environ["PORT"]) if os.environ.get("PORT", "").isdigit() else None,
            ephemeral=os.environ.get("EPHEMERAL_STATE", "0") == "1",
            fetcher=os.environ.get("LISTAM_FETCHER", "http").strip().lower(),
            banali=os.environ.get("BANALI", "1") != "0",
            banali_pages=int(os.environ.get("BANALI_PAGES", "3")),
            max_bump_checks=int(os.environ.get("MAX_BUMP_CHECKS", "10")),
            max_new_alerts=int(os.environ.get("MAX_NEW_ALERTS", "15")),
        )
