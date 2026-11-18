import asyncio, os, sys, tempfile
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from listam.config import Config
from listam.db import DB
from listam.monitor import Monitor
from listam.parser import parse_item_description, parse_listing
from listam.sources import ListAmSource

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

def _load(name):
    try:  # real list.am pages are not committed (see .gitignore); tests that need them are skipped
        return open(os.path.join(FIX, name)).read()
    except FileNotFoundError:
        return None


LISTING, ITEM = _load("listing.html"), _load("item.html")
needs_fixtures = pytest.mark.skipif(LISTING is None, reason="tests/fixtures/*.html are not in the repo")


@needs_fixtures
def test_parse_listing():
    cards = parse_listing(LISTING)
    assert len(cards) == 105, len(cards)
    c = next(c for c in cards if c.id == 995129524)
    assert (c.price, c.currency, c.top) == (27000, "$", True), c
    assert c.location == "Nubarashen" and "Plot for residential" in c.title, c
    assert any(not c.top for c in cards)


@needs_fixtures
def test_parse_item():
    d = parse_item_description(ITEM)
    assert d.startswith("Land plot for sale") and "Translated" not in d, d


class FakeFetcher:
    def __init__(self): self.listing = LISTING; self.desc = ITEM
    async def get(self, url):
        return self.desc if "/item/" in url else self.listing


@needs_fixtures
def test_diff_flow():
    async def run():
        sent = []
        async def send(chat, text): sent.append(text)
        cfg = Config(bot_token="x", allowed_chats=[1], poll_interval=1, desc_checks_per_cycle=2)
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        f = FakeFetcher()
        m = Monitor(cfg, db, {"listam": ListAmSource(f, cfg.exclude_locations)}, send)
        import listam.monitor as mod
        async def nosleep(*a, **k): pass
        mod.asyncio.sleep = nosleep
        sid = db.add_search(1, "t", "https://www.list.am/en/category/62")
        await m.cycle()
        assert len(sent) == 1 and "baseline" in sent[0], sent       # silent seed
        sent.clear(); await m.cycle()
        assert sent == [], sent                                      # nothing changed
        f.listing = LISTING.replace("</span>27,000</span>", "</span>25,000</span>")
        await m.cycle()
        assert sent and all("Price changed" in s and "was $27,000" in s for s in sent), sent
        sent.clear()
        f.desc = ITEM.replace("Land plot for sale", "Land plot URGENT sale")
        f.get_orig = f.get
        await m.cycle()
        assert any("Description updated" in s for s in sent), sent   # description diff
        sent.clear()
        f.listing = LISTING.replace("/en/item/22177529", "/en/item/11111111").replace("favorite-ad-card-22177529", "favorite-ad-card-11111111")
        await m.cycle()
        assert any("New" in s and "11111111" in s for s in sent), sent
    asyncio.run(run())


@needs_fixtures
def test_exclude_and_single_search():
    async def run():
        sent = []
        async def send(chat, text): sent.append(text)
        cfg = Config(bot_token="x", allowed_chats=[1], exclude_locations=["nubarashen"])
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        a = db.ensure_search(1, "n", "u1"); assert db.ensure_search(1, "n", "u1") == a
        import listam.monitor as mod
        async def nosleep(*a, **k): pass
        mod.asyncio.sleep = nosleep
        m = Monitor(cfg, db, {"listam": ListAmSource(FakeFetcher(), cfg.exclude_locations)}, send)
        await m.cycle()
        assert db.get_item(a, 995129524) is None          # Nubarashen ad skipped
        assert db.get_item(a, 22177529) is not None
        assert db.ensure_search(1, "n", "u2") == a        # changed URL resets baseline
        assert db.list_searches(1)[0]["seeded"] == 0
    asyncio.run(run())


@needs_fixtures
def test_dates_and_price_url():
    from listam.parser import parse_item_dates
    from listam.config import with_max_price
    posted, renewed = parse_item_dates(ITEM)
    assert posted.year == 2026 and posted.month == 9 and renewed.month == 11 and renewed.hour == 11
    u = "https://www.list.am/en/category/56?n=2,3&price2=220000&crc=0&srt=3"
    assert with_max_price(u, 250000) == "https://www.list.am/en/category/56?n=2,3&price2=250000&crc=0&srt=3"
    assert "price2=90000" in with_max_price("https://www.list.am/en/category/56?n=2", 90000)


@needs_fixtures
def test_recent_flow():
    async def run():
        from datetime import datetime, timedelta, timezone
        import listam.monitor as mod
        sent = []
        async def send(chat, text): sent.append(text)
        async def nosleep(*a, **k): pass
        mod.asyncio.sleep = nosleep
        now = datetime.now(timezone.utc)
        fresh = ITEM.replace("Renewed 16.11.2026, 11:51", "Renewed " + (now.astimezone(mod.YEREVAN)).strftime("%d.%m.%Y, %H:%M"))
        old = ITEM.replace('Renewed 16.11.2026, 11:51', 'Renewed 01.10.2026, 11:51')
        class F2(FakeFetcher):
            n = 0
            async def get(self, url):
                if "/item/" not in url: return self.listing
                F2.n += 1
                return fresh if F2.n <= 4 else old
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.ensure_search(1, "n", "u")
        m = Monitor(Config(bot_token="x", allowed_chats=[1]), db, {"listam": ListAmSource(F2(), [])}, send)
        await m.recent(1, 24)
        assert sum("Renewed" in s or "Posted" in s for s in sent) == 4, sent
        assert "4 ads from the last" in sent[-1] and F2.n < 20, (sent[-1], F2.n)  # stops early: 3 old regular ads in a row
    asyncio.run(run())


def _listing(ids):
    cards = "".join(
        '<a href="/en/item/%d" data-testid="favorite-ad-card-%d"><div class="p"><span class="category-data-list-card__amount">'
        '<span class="category-data-list-card__currency">֏</span>100,000</span></div><div class="l">Ad %d</div>'
        '<div class="category-data-list-card__location">Kentron</div></a>' % (i, i, i) for i in ids)
    return "<html><body>%s</body></html>" % cards


def _item(renewed, text="same text"):
    return ('<html><div data-testid="post-view-description">%s</div><span itemprop="datePosted" '
            'content="2026-09-01T10:00:00+00:00">Posted</span><span>Renewed %s</span></html>' % (text, renewed))


def test_photo_only_update_detected_by_bump():
    async def run():
        import listam.monitor as mod
        sent = []
        async def send(chat, text): sent.append(text)
        async def nosleep(*a, **k): pass
        mod.asyncio.sleep = nosleep
        pages = {"list": _listing([1, 2, 3, 4]), 3: _item("01.11.2026, 10:00")}
        for i in (1, 2, 4): pages[i] = _item("01.11.2026, 10:00")
        class F3:
            async def get(self, url):
                return pages["list"] if "/item/" not in url else pages[int(url.rsplit("/", 1)[1])]
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db")); db.ensure_search(1, "n", "u")
        m = Monitor(Config(bot_token="x", allowed_chats=[1]), db, {"listam": ListAmSource(F3(), [])}, send)
        await m.cycle(); sent.clear()
        await m.cycle(); assert sent == [], sent                      # same order -> nothing
        pages["list"] = _listing([3, 1, 2, 4])                        # ad 3 jumped to the top...
        pages[3] = _item("16.11.2026, 23:30")                         # ...renewed, text unchanged
        await m.cycle()
        assert len(sent) == 1 and "Renewed 16.11 23:30" in sent[0] and "Ad 3" in sent[0] and "Description" not in sent[0], sent
        sent.clear(); await m.cycle(); assert sent == [], sent         # not repeated
        pages["list"] = _listing([1, 2, 4])                           # ad above others deleted -> no false bumps
        await m.cycle(); assert sent == [], sent
    asyncio.run(run())


# ---------------- banali ----------------
def _post(i, price, cur="051", pub="2026-11-10T10:00:00.000000Z", upd=None, district="Արաբկիր", slug="en"):
    return {"id": "uuid-%d" % i, "active": True, "priceFull": [{"price": str(price), "currency": cur}],
            "publishedAt": pub, "updatedToTop": upd or pub, "district": district, "isPinToTop": False,
            "rooms": ["2"], "area": ["50"], "floor": ["3"], "buildingFloors": "9", "address": "Երևան, Արաբկիր, Փողոց",
            "slug": {slug: "rent/apartment/2-rooms/Yerevan/Arabkir/-B%d" % i}, "publicCode": "B%d" % i}


class FakeBanali:
    """Replaces BanaliSource._json: serves posts in pages of 10 and detail docs."""
    def __init__(self, src): self.posts = []; self.desc = {}; src._json = self.call
    async def call(self, method, path, body=None):
        if "exchange-rates" in path: return {"rates": {"USD": {"Rate": "400", "Amount": "1"}}}
        if "states-districts" in path: return {"data": [{"text": "Արաբկիր", "translations": {"en": "Arabkir"}}]}
        if "filter-posts" in path:
            n = int(path.split("page=")[1]); return {"data": {"posts": {"data": self.posts[(n-1)*10:n*10]}}}
        if "/api/post/" in path: return {"post": {"description": self.desc.get(path.rsplit("/", 1)[1])}}
        raise AssertionError(path)


def test_banali_filters_and_conversion():
    async def run():
        from listam.sources import BanaliSource
        src = BanaliSource(["Norq-Marash"], pages=2); fk = FakeBanali(src)
        fk.posts = [_post(1, 200000), _post(2, 250000), _post(3, 500, "840"), _post(4, 600, "840"),
                    _post(5, 100000, district="Նորք-Մարաշ"), _post(6, 150000, slug="ru")]
        fk.posts[4]["district"] = "Norq-Marash"; src._district_en = {"Norq-Marash": "Norq-Marash", "Արաբկիր": "Arabkir"}
        cards = await src.fetch_cards("", 220000)
        ids = sorted(c.id for c in cards)
        assert ids == ["uuid-1", "uuid-3", "uuid-6"], ids          # 250k, $600 (=240k), excluded district dropped
        c3 = next(c for c in cards if c.id == "uuid-3"); assert c3.amd == 200000 and c3.price_text == "500$"
        assert next(c for c in cards if c.id == "uuid-6").url.startswith("https://banali.am/ru/")  # slug fallback
    asyncio.run(run())


def test_banali_renewal_vs_auto_bump_and_description():
    async def run():
        from listam.sources import BanaliSource
        import listam.monitor as mod
        async def nosleep(*a, **k): pass
        mod.asyncio.sleep = nosleep
        import listam.sources as ss; ss.asyncio.sleep = nosleep
        sent = []
        async def send(chat, text): sent.append(text)
        src = BanaliSource([], pages=1); fk = FakeBanali(src)
        fk.posts = [_post(1, 100000, upd="2026-11-12T08:00:00.000000Z"), _post(2, 100000, upd="2026-11-12T09:00:00.000000Z")]
        fk.desc = {"uuid-1": "first text", "uuid-2": "other"}
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db")); db.ensure_search(1, "b", "u", "banali")
        m = Monitor(Config(bot_token="x", allowed_chats=[1]), db, {"banali": src}, send)
        await m.cycle(); assert "baseline" in sent[0], sent; sent.clear()
        await m.cycle(); assert sent == [], sent
        # nightly automatic raise (00:00:20 UTC): must stay silent
        fk.posts[0] = _post(1, 100000, upd="2026-11-16T00:00:20.000000Z"); await m.cycle(); assert sent == [], sent
        # owner raise at 07:05 UTC with edited description: alert once
        fk.posts[1] = _post(2, 100000, upd="2026-11-16T07:05:00.000000Z"); fk.desc["uuid-2"] = "other, now cheaper deal"
        await m.cycle()
        assert len(sent) == 1 and "Renewed" in sent[0] and "Description updated" in sent[0] and "banali.am" in sent[0], sent
        sent.clear(); await m.cycle(); assert sent == [], sent
        # price change
        fk.posts[0] = _post(1, 90000, upd="2026-11-16T00:00:20.000000Z"); await m.cycle()
        assert len(sent) == 1 and "Price changed" in sent[0] and "was ֏100,000" in sent[0], sent
    asyncio.run(run())


def test_health_endpoint():
    async def run():
        import aiohttp, socket
        from listam.health import start_health
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
        class M: last_cycle_ok = 0.0
        runner = await start_health(port, M())
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("http://127.0.0.1:%d/health" % port) as r:
                    assert r.status == 200 and (await r.json())["status"] == "ok"
                async with s.head("http://127.0.0.1:%d/health" % port) as r:
                    assert r.status == 200          # pingers often use HEAD
        finally:
            await runner.cleanup()
    asyncio.run(run())


# ---------------- list.am snapshot (pushed by the GitHub runner) ----------------
def _snap(cards, details=None, age=0):
    import time as _t
    return {"received_at": _t.time() - age, "fetched_at": int(_t.time()) - age, "cards": cards, "details": details or {}}


def _c(i, price=100000, loc="Kentron", top=False, cur="֏"):
    return {"id": i, "url": "https://www.list.am/en/item/%d" % i, "title": "Ad %d" % i, "price_text": "%d" % price,
            "price": price, "currency": cur, "location": loc, "top": top}


def test_snapshot_source_states_and_filters():
    async def run():
        from listam.fetcher import Blocked, Waiting
        from listam.sources import SnapshotListAmSource
        h = {}
        src = SnapshotListAmSource(h, ["Avan"], max_age=3600, grace=600)
        with pytest.raises(Waiting): await src.fetch_cards("", 220000)            # just started, nothing yet
        src.started -= 1000
        with pytest.raises(Blocked): await src.fetch_cards("", 220000)            # runner never delivered
        h["snapshot"] = _snap([_c(1), _c(2, 300000), _c(3, loc="Avan"), _c(4, cur="$", price=900)])
        ids = [c.id for c in await src.fetch_cards("", 220000)]
        assert ids == [1, 4], ids                                               # over cap and excluded district dropped
        h["snapshot"] = _snap([_c(1)], age=7200)
        with pytest.raises(Blocked): await src.fetch_cards("", 220000)            # snapshot too old
    asyncio.run(run())


def test_snapshot_monitor_flow():
    async def run():
        import listam.monitor as mod
        from listam.sources import SnapshotListAmSource
        async def nosleep(*a, **k): pass
        mod.asyncio.sleep = nosleep
        sent = []
        async def send(chat, text): sent.append(text)
        h = {}
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db")); db.ensure_search(1, "l", "u")
        m = Monitor(Config(bot_token="x", allowed_chats=[1]), db, {"listam": SnapshotListAmSource(h, [])}, send)
        now = __import__("time").time()
        h["snapshot"] = _snap([_c(1), _c(2), _c(3)], {"1": {"desc": "text one", "posted": int(now) - 9999, "renewed": None}})
        await m.cycle(); assert "baseline" in sent[0], sent; sent.clear()
        await m.cycle(); assert sent == [], sent                                 # identical snapshot -> silence
        # new ad on top + price drop on ad 2
        h["snapshot"] = _snap([_c(9), _c(1), _c(2, 90000), _c(3)], {"1": {"desc": "text one", "posted": None, "renewed": None}})
        await m.cycle()
        assert sum("New" in s for s in sent) == 1 and sum("Price changed" in s for s in sent) == 1, sent
        sent.clear()
        # ad 3 jumps to the top, renewed, description edited
        r = int(now) - 60
        h["snapshot"] = _snap([_c(3), _c(9), _c(1), _c(2, 90000)], {"3": {"desc": "brand new text", "posted": None, "renewed": r}})
        await m.cycle()
        assert len(sent) == 1 and "Renewed" in sent[0] and "list.am" in sent[0], sent
        sent.clear()
        # ad 2 jumps to the top but the runner got no details for it: still reported as renewed
        h["snapshot"] = _snap([_c(2, 90000), _c(3), _c(9), _c(1)], {})
        await m.cycle()
        assert len(sent) == 1 and "Renewed" in sent[0] and "Ad 2" in sent[0], sent
    asyncio.run(run())


def test_ingest_endpoint_on_the_light_server():
    async def run():
        import aiohttp, socket
        from listam.health import start_health
        os.environ["INGEST_KEY"] = "k3y"
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
        class M: last_cycle_ok = 0.0
        holder = {}
        runner = await start_health(port, M(), holder)
        base = "http://127.0.0.1:%d" % port
        good = {"fetched_at": 1, "cards": [_c(1)], "details": {"1": {"desc": "x", "posted": None, "renewed": None}}}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(base + "/ingest", json=good) as r: assert r.status == 403           # no key
                async with s.post(base + "/ingest", json=good, headers={"X-Ingest-Key": "bad"}) as r: assert r.status == 403
                async with s.post(base + "/ingest", json={"cards": [{"id": 1}]}, headers={"X-Ingest-Key": "k3y"}) as r:
                    assert r.status == 400                                                              # bad payload
                assert "snapshot" not in holder
                async with s.post(base + "/ingest", json=good, headers={"X-Ingest-Key": "k3y"}) as r:
                    assert r.status == 200 and (await r.json())["cards"] == 1
                assert len(holder["snapshot"]["cards"]) == 1
                async with s.get(base + "/health") as r:
                    assert (await r.json())["snapshot_age_s"] is not None
        finally:
            await runner.cleanup()
            os.environ.pop("INGEST_KEY", None)
    asyncio.run(run())
