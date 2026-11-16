"""Runs on a GitHub Actions runner: scrape list.am with a real browser, push the snapshot to the Space.

Env: SPACE_URL, HF_TOKEN (read access to the private Space), INGEST_KEY (shared secret),
     MAX_PRICE (default 220000), DETAIL_COUNT (ad pages to open, default 25).
Without SPACE_URL it only prints what it found (dry run).
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from listam.parser import parse_item_dates, parse_item_description, parse_listing  # noqa: E402

MAX_PRICE = int(os.environ.get("MAX_PRICE") or 220000)
URL = os.environ.get("LISTAM_URL") or (
    "https://www.list.am/en/category/56?n=2,3,5,6,7,8,9,13&price2=%d&crc=0&srt=0" % MAX_PRICE)
DETAIL_COUNT = int(os.environ.get("DETAIL_COUNT") or 25)
CHALLENGE_TIMEOUT = 60


class Blocked(Exception):
    pass


async def fetch(ctx, url):
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        waited = 0
        while waited < CHALLENGE_TIMEOUT and "just a moment" in (await page.title()).lower():
            await asyncio.sleep(1)
            waited += 1
        if "just a moment" in (await page.title()).lower():
            raise Blocked("Cloudflare challenge not passed in %ds" % CHALLENGE_TIMEOUT)
        return await page.content()
    finally:
        await page.close()


async def scrape():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        kw = dict(user_data_dir="/tmp/prof", headless=False, locale="en-US", viewport={"width": 1366, "height": 850},
                  args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        try:
            ctx = await p.chromium.launch_persistent_context(channel="chrome", **kw)  # Chrome preinstalled on the runner
        except Exception as e:
            print("Chrome channel unavailable (%s), using bundled Chromium" % str(e).splitlines()[0])
            ctx = await p.chromium.launch_persistent_context(**kw)
        try:
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
                    html = await fetch(ctx, c.url)
                except Blocked:
                    break  # keep what we have; the Space copes with missing details
                posted, renewed = parse_item_dates(html)
                details[str(c.id)] = {
                    "desc": parse_item_description(html),
                    "posted": int(posted.timestamp()) if posted else None,
                    "renewed": int(renewed.timestamp()) if renewed else None,
                }
            print("details: %d/%d in %.0fs" % (len(details), len(wanted), time.time() - t), flush=True)
        finally:
            await ctx.close()
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
                 "Authorization": "Bearer " + os.environ["HF_TOKEN"]})
    for attempt in range(1, 5):  # the Space may be waking up
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                print("pushed to Space: HTTP %d %s" % (r.status, r.read()[:100].decode()))
                return
        except (urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            print("push attempt %d failed: %s" % (attempt, code or e), flush=True)
            if code in (400, 401, 403):
                raise SystemExit("Space rejected the snapshot (check INGEST_KEY / HF_TOKEN)")
            time.sleep(20)
    raise SystemExit("could not push the snapshot to the Space")


if __name__ == "__main__":
    start = time.time()
    try:
        data = asyncio.run(scrape())
    except Blocked as e:
        raise SystemExit("BLOCKED: %s" % e)
    push(data)
    print("done in %.0fs" % (time.time() - start))
