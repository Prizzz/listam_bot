"""Runs on a GitHub Actions runner: scrape list.am with a real browser, push snapshots to the Space.

Env: SPACE_URL (where the bot listens: its /ingest), INGEST_KEY (shared secret),
     HF_TOKEN (only if the target is a private Hugging Face Space),
     MAX_PRICE (default 220000), DETAIL_COUNT (ad pages to open per snapshot, default 20),
     LOOP_MINUTES (keep running this long, one snapshot every INTERVAL seconds; 0 = a single snapshot),
     INTERVAL (default 300), CF_TIMEOUT (seconds to wait for Cloudflare's check, default 30).
Without SPACE_URL it only prints what it found (dry run).
"""
import asyncio
import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from listam.parser import parse_item_dates, parse_item_description, parse_listing  # noqa: E402

MAX_PRICE = int(os.environ.get("MAX_PRICE") or 220000)
URL = os.environ.get("LISTAM_URL") or (
    "https://www.list.am/en/category/56?n=2,3,5,6,7,8,9,13&price2=%d&crc=0&srt=0" % MAX_PRICE)
DETAIL_COUNT = int(os.environ.get("DETAIL_COUNT") or 20)
CHALLENGE_TIMEOUT = int(os.environ.get("CF_TIMEOUT") or 20)
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES") or 0)
INTERVAL = int(os.environ.get("INTERVAL") or 300)
PROFILE = "/tmp/prof"


class Blocked(Exception):
    pass


async def fetch(ctx, url, referer=None):
    page = await ctx.new_page()
    try:
        # Ad pages are only served without a challenge when they look like a click from the listing.
        await page.goto(url, wait_until="domcontentloaded", timeout=60000, **({"referer": referer} if referer else {}))
        waited = 0
        while waited < CHALLENGE_TIMEOUT and "just a moment" in (await page.title()).lower():
            await asyncio.sleep(1)
            waited += 1
        if "just a moment" in (await page.title()).lower():
            try:
                text = " ".join((await page.inner_text("body")).split())[:240]
                frames = [f.url[:80] for f in page.frames if "cloudflare" in f.url][:2]
                print("challenge page: %r frames=%s" % (text, frames), flush=True)
            except Exception:
                pass
            raise Blocked("Cloudflare challenge not passed in %ds" % CHALLENGE_TIMEOUT)
        return await page.content()
    finally:
        await page.close()


async def launch(p):
    shutil.rmtree(PROFILE, ignore_errors=True)  # a fresh profile = fresh Cloudflare session
    return await p.chromium.launch_persistent_context(
        PROFILE, headless=False, locale="en-US", viewport={"width": 1366, "height": 850},
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])


async def snapshot(ctx):
    t = time.time()
    cards = parse_listing(await fetch(ctx, URL))
    if not cards:
        raise Blocked("listing parsed to 0 cards")
    print("listing: %d cards in %.0fs" % (len(cards), time.time() - t), flush=True)
    # promoted ads first (their order is arbitrary), then the newest-updated regular ones
    wanted = [c for c in cards if c.top] + [c for c in cards if not c.top][:DETAIL_COUNT]
    details = {}
    for c in wanted:
        try:
            html = await fetch(ctx, c.url, referer=URL)
        except Blocked:
            break  # keep what we have; the Space copes with missing details
        posted, renewed = parse_item_dates(html)
        details[str(c.id)] = {
            "desc": parse_item_description(html),
            "posted": int(posted.timestamp()) if posted else None,
            "renewed": int(renewed.timestamp()) if renewed else None,
        }
    print("details: %d/%d in %.0fs" % (len(details), len(wanted), time.time() - t), flush=True)
    return {
        "fetched_at": int(time.time()),
        "cards": [{"id": c.id, "url": c.url, "title": c.title, "price_text": c.price_text, "price": c.price,
                   "currency": c.currency, "location": c.location, "top": c.top} for c in cards],
        "details": details,
    }


def push(payload):
    base = os.environ.get("SPACE_URL", "").rstrip("/")
    if not base:
        print("SPACE_URL not set: dry run, nothing sent")
        return
    req = urllib.request.Request(
        base + "/ingest", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Key": os.environ["INGEST_KEY"],
                 # only a private Hugging Face Space needs this; Render does not
                 **({"Authorization": "Bearer " + os.environ["HF_TOKEN"]} if os.environ.get("HF_TOKEN") else {})})
    for attempt in range(1, 5):  # the Space may be waking up
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                print("pushed to Space: HTTP %d %s" % (r.status, r.read()[:100].decode()), flush=True)
                return
        except (urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            print("push attempt %d failed: %s" % (attempt, code or e), flush=True)
            if code in (400, 401, 403):
                raise RuntimeError("Space rejected the snapshot (check INGEST_KEY / HF_TOKEN)")
            time.sleep(20)
    raise RuntimeError("could not push the snapshot to the Space")


async def main():
    from playwright.async_api import async_playwright
    end = time.time() + LOOP_MINUTES * 60
    ok = fails = 0
    async with async_playwright() as p:
        ctx = None
        while True:
            started = time.time()
            for attempt in range(1, 5):  # a fresh browser session often passes right after a refusal
                try:
                    if ctx is None:
                        ctx = await launch(p)
                    data = await snapshot(ctx)
                    await asyncio.to_thread(push, data)
                    ok += 1
                    break
                except Exception as e:  # Blocked, network, push errors: log, recycle the browser
                    print("attempt %d failed (%s: %s)" % (attempt, type(e).__name__, e), flush=True)
                    if ctx is not None:
                        await ctx.close()
                        ctx = None
                    if isinstance(e, RuntimeError) and "rejected" in str(e):
                        raise SystemExit(str(e))  # a config problem, retrying will not fix it
                    await asyncio.sleep(random.uniform(3, 8))
            else:
                fails += 1
                print("snapshot skipped after 4 attempts; ok=%d failed=%d" % (ok, fails), flush=True)
            if LOOP_MINUTES <= 0 or time.time() + INTERVAL > end:
                break
            await asyncio.sleep(max(5, INTERVAL - (time.time() - started) + random.uniform(-20, 20)))
        if ctx is not None:
            await ctx.close()
    print("finished: %d ok, %d failed" % (ok, fails))
    if ok == 0:
        raise SystemExit("no snapshot could be delivered")


if __name__ == "__main__":
    asyncio.run(main())
