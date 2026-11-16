"""SQLite storage: saved searches and last-seen state of each ad."""
from __future__ import annotations

import sqlite3
import time
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    seeded INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    search_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    title TEXT,
    price INTEGER,
    currency TEXT,
    price_text TEXT,
    location TEXT,
    desc_hash TEXT,
    desc_text TEXT,
    desc_checked_at INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (search_id, item_id)
);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, col in (("items", "renewed_at INTEGER"), ("items", "rank INTEGER"),
                           ("searches", "source TEXT NOT NULL DEFAULT 'listam'")):  # migrations for older DBs
            try:
                self.conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, col))
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    # --- searches -------------------------------------------------------
    def ensure_search(self, chat_id: int, name: str, url: str, source: str = "listam") -> int:
        """One search per (chat, source); reset its baseline if the URL changed."""
        row = self.conn.execute(
            "SELECT * FROM searches WHERE chat_id=? AND source=? ORDER BY id LIMIT 1", (chat_id, source)
        ).fetchone()
        if row is None:
            return self.add_search(chat_id, name, url, source)
        if row["url"] != url or row["name"] != name:
            changed = row["url"] != url
            self.conn.execute(
                "UPDATE searches SET url=?, name=?, seeded=CASE WHEN ? THEN 0 ELSE seeded END WHERE id=?",
                (url, name, int(changed), row["id"]),
            )
            if changed:
                self.conn.execute("DELETE FROM items WHERE search_id=?", (row["id"],))
            self.conn.commit()
        return row["id"]

    def get_setting(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
        self.conn.commit()

    def set_url_keep_items(self, chat_id: int, url: str, source: str = "listam") -> None:
        """Change a search URL but keep known ads; the next pass absorbs new ones silently."""
        self.conn.execute("UPDATE searches SET url=?, seeded=0 WHERE chat_id=? AND source=?", (url, chat_id, source))
        self.conn.commit()

    def reseed(self, chat_id: int) -> None:
        """Make the next pass of every search of this chat a silent baseline (keeps known ads)."""
        self.conn.execute("UPDATE searches SET seeded=0 WHERE chat_id=?", (chat_id,))
        self.conn.commit()

    def add_search(self, chat_id: int, name: str, url: str, source: str = "listam") -> int:
        cur = self.conn.execute(
            "INSERT INTO searches (chat_id, name, url, source, created_at) VALUES (?,?,?,?,?)",
            (chat_id, name, url, source, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_searches(self, chat_id: Optional[int] = None, active_only: bool = False) -> List[sqlite3.Row]:
        q = "SELECT * FROM searches WHERE 1=1"
        args: list = []
        if chat_id is not None:
            q += " AND chat_id=?"
            args.append(chat_id)
        if active_only:
            q += " AND active=1"
        return self.conn.execute(q + " ORDER BY id", args).fetchall()

    def remove_search(self, chat_id: int, search_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM searches WHERE id=? AND chat_id=?", (search_id, chat_id))
        self.conn.execute("DELETE FROM items WHERE search_id=?", (search_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_active(self, chat_id: int, search_id: int, active: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE searches SET active=? WHERE id=? AND chat_id=?", (int(active), search_id, chat_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_seeded(self, search_id: int) -> None:
        self.conn.execute("UPDATE searches SET seeded=1 WHERE id=?", (search_id,))
        self.conn.commit()

    # --- items ----------------------------------------------------------
    def get_item(self, search_id: int, item_id) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE search_id=? AND item_id=?", (search_id, item_id)
        ).fetchone()

    def insert_item(self, search_id: int, c) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT OR REPLACE INTO items (search_id,item_id,title,price,currency,price_text,location,"
            "first_seen,last_seen,renewed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (search_id, c.id, c.title, c.price, c.currency, c.price_text, c.location, now, now,
             getattr(c, "updated_ts", None)),
        )
        self.conn.commit()

    def update_item_card(self, search_id: int, c) -> None:
        self.conn.execute(
            "UPDATE items SET title=?, price=?, currency=?, price_text=?, location=?, last_seen=? "
            "WHERE search_id=? AND item_id=?",
            (c.title, c.price, c.currency, c.price_text, c.location, int(time.time()), search_id, c.id),
        )
        self.conn.commit()

    def get_ranks(self, search_id: int) -> dict:
        rows = self.conn.execute(
            "SELECT item_id, rank FROM items WHERE search_id=? AND rank IS NOT NULL", (search_id,)
        ).fetchall()
        return {r["item_id"]: r["rank"] for r in rows}

    def set_ranks(self, search_id: int, ordered_ids: list) -> None:
        """Remember the current on-page order (promoted ads excluded)."""
        self.conn.execute("UPDATE items SET rank=NULL WHERE search_id=?", (search_id,))
        self.conn.executemany(
            "UPDATE items SET rank=? WHERE search_id=? AND item_id=?",
            [(i, search_id, iid) for i, iid in enumerate(ordered_ids)],
        )
        self.conn.commit()

    def set_page_info(self, search_id: int, item_id: int, text: Optional[str], h: str, renewed_ts: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE items SET desc_text=?, desc_hash=?, desc_checked_at=?, renewed_at=COALESCE(?, renewed_at) "
            "WHERE search_id=? AND item_id=?",
            (text, h, int(time.time()), renewed_ts, search_id, item_id),
        )
        self.conn.commit()

    def set_description(self, search_id: int, item_id: int, text: Optional[str], h: str) -> None:
        self.conn.execute(
            "UPDATE items SET desc_text=?, desc_hash=?, desc_checked_at=? WHERE search_id=? AND item_id=?",
            (text, h, int(time.time()), search_id, item_id),
        )
        self.conn.commit()

    def stale_description_items(self, search_id: int, item_ids: list, limit: int) -> List[sqlite3.Row]:
        """Items from the current page whose description was checked longest ago."""
        if not item_ids:
            return []
        marks = ",".join("?" * len(item_ids))
        return self.conn.execute(
            "SELECT * FROM items WHERE search_id=? AND item_id IN (%s) "
            "ORDER BY desc_checked_at ASC LIMIT ?" % marks,
            [search_id, *item_ids, limit],
        ).fetchall()
