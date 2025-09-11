#!/usr/bin/env python3
# (same content as earlier; abbreviated comments removed for brevity)
import os, asyncio, time, re
from datetime import datetime, timedelta
from typing import List, Dict, Any
import requests
from playwright.async_api import async_playwright

ORIGIN = os.getenv("ALASKA_ORIGIN", "TPE").upper()
DEST = os.getenv("ALASKA_DEST", "LAX").upper()
START_DATE = os.getenv("ALASKA_START_DATE", "2025-10-01")
END_DATE = os.getenv("ALASKA_END_DATE", "2025-10-07")
TRIP_TYPE = os.getenv("ALASKA_TRIP_TYPE", "one_way")
PAX_ADT = int(os.getenv("ALASKA_PAX_ADT", "1"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "1800"))
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
HEADLESS = os.getenv("HEADLESS", "1") == "1"

def daterange(start: datetime, end: datetime):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)

def t_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured; skipping push.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
    if r.status_code != 200:
        print(f"[ERROR] Telegram push failed: {r.status_code} {r.text}")
    else:
        print("[OK] Telegram pushed.")

async def search_one_date(playwright, date_str: str) -> List[Dict[str, Any]]:
    browser = await playwright.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
    context = await browser.new_context(locale="en-US")
    page = await context.new_page()
    results = []
    try:
        await page.goto("https://www.alaskaair.com/", wait_until="domcontentloaded", timeout=60000)
        try:
            await page.get_by_role("button", name="Accept").click(timeout=3000)
        except: pass
        try:
            await page.get_by_label("Use miles").check(timeout=6000)
        except:
            try: await page.locator("label:has-text('Use miles')").click(timeout=3000)
            except: pass
        if TRIP_TYPE.lower() == "one_way":
            try: await page.get_by_label("One-way").check(timeout=5000)
            except:
                try: await page.locator("label:has-text('One-way') input[type=radio]").check(timeout=3000)
                except: pass
        await page.get_by_label("From").click()
        await page.get_by_label("From").fill(ORIGIN)
        await page.wait_for_timeout(800)
        await page.keyboard.press("Enter")
        await page.get_by_label("To").click()
        await page.get_by_label("To").fill(DEST)
        await page.wait_for_timeout(800)
        await page.keyboard.press("Enter")
        try:
            depart = page.get_by_label("Depart")
            await depart.click()
            await depart.fill(date_str)
            await page.keyboard.press("Enter")
        except: pass
        try:
            await page.get_by_role("button", name="Find flights").click(timeout=20000)
        except:
            await page.keyboard.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(6000)
        cards = page.locator("div").filter(has_text="Operated by")
        count = await cards.count()
        for i in range(count):
            card = cards.nth(i)
            text = (await card.inner_text()).strip()
            if "STARLUX" not in text.upper():
                continue
            if "BUSINESS" in text.upper():
                miles_match = re.search(r"(\d[\d,\.]+)\s*miles", text, re.IGNORECASE)
                miles = miles_match.group(1) if miles_match else "N/A"
                fn_match = re.search(r"\bJX\s?\d+\b", text, re.IGNORECASE)
                flight_no = fn_match.group(0) if fn_match else "JX"
                results.append({"date": date_str, "origin": ORIGIN, "dest": DEST, "flight": flight_no, "miles": miles})
    except Exception as e:
        print(f"[WARN] search error on {date_str}: {e}")
    finally:
        await context.close()
        await browser.close()
    return results

async def run_once() -> list:
    matches = []
    start = datetime.fromisoformat(START_DATE)
    end = datetime.fromisoformat(END_DATE)
    async with async_playwright() as p:
        for day in daterange(start, end):
            ds = day.strftime("%Y-%m-%d")
            print(f"[INFO] Checking {ORIGIN}->{DEST} on {ds}...")
            res = await search_one_date(p, ds)
            if res:
                print(f"[MATCH] {len(res)} result(s) on {ds}")
                matches.extend(res)
            else:
                print(f"[NONE] No JX Business award found on {ds}")
    return matches

def format_message(items: list) -> str:
    if not items: return "No JX Business awards found."
    lines = ["<b>Found STARLUX (JX) Business awards via Alaska</b>"]
    for r in items:
        lines.append(f"• {r['date']} {r['origin']}→{r['dest']} {r.get('flight','JX')} — {r.get('miles','?')} miles")
    return "\n".join(lines)

async def main_loop():
    while True:
        items = await run_once()
        if items:
            msg = format_message(items); print(msg); t_send(msg)
        else:
            print("[INFO] No matches this cycle.")
        if RUN_ONCE: break
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
