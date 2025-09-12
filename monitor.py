import os
import re
import time
import requests
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
import asyncio

# 環境變數
ORIGIN = os.getenv("ORIGIN", "TPE")      # 出發地
DEST = os.getenv("DEST", "NRT")          # 目的地
DATE = os.getenv("DATE", "2025-10-01")   # 出發日期（單日模式）
DATE_START = os.getenv("DATE_START")     # 起始日期（區間模式，可選）
DATE_END = os.getenv("DATE_END")         # 結束日期（區間模式，可選）
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
HEADLESS = os.getenv("HEADLESS", "1") == "1"
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"
INTERVAL = int(os.getenv("INTERVAL", "1800"))  # 預設每 30 分鐘跑一次
CABIN_FILTER = os.getenv("ALASKA_CABIN", "BUSINESS").upper()  # BUSINESS / ECONOMY / PREMIUM / FIRST / MAIN / ANY

async def search_once(p, date_str):
    """查詢單一天的可用艙等"""
    browser = await p.chromium.launch(headless=HEADLESS)
    page = await browser.new_page()
    url = f"https://www.alaskaair.com/PlanBook/Flights?origin={ORIGIN}&destination={DEST}&departureDate={date_str}&awardBooking=true"
    await page.goto(url)
    await page.wait_for_timeout(8000)

    content = await page.inner_text("body")
    results = []

    cards = await page.query_selector_all("div.flight-card, div.akam-flight-card, body")
    for c in cards:
        text = await c.inner_text()
        u = text.upper()
        if "STARLUX" not in u:   # 只要星宇
            continue

        # 嘗試判斷艙等
        cabin_names = ["BUSINESS", "FIRST", "PREMIUM", "PREMIUM CLASS", "ECONOMY", "MAIN"]
        found_cabins = [c for c in cabin_names if c in u]
        cabin = found_cabins[0] if found_cabins else "UNKNOWN"

        # 依照設定過濾艙等
        if CABIN_FILTER != "ANY" and CABIN_FILTER not in u:
            continue

        miles_match = re.search(r"(\d[\d,\.]+)\s*miles", text, re.IGNORECASE)
        miles = miles_match.group(1) if miles_match else "N/A"

        fn_match = re.search(r"\bJX\s?\d+\b", text, re.IGNORECASE)
        flight_no = fn_match.group(0) if fn_match else "JX"

        results.append({
            "date": date_str,
            "origin": ORIGIN,
            "dest": DEST,
            "flight": flight_no,
            "miles": miles,
            "cabin": cabin
        })

    await browser.close()
    return results

def format_message(results):
    lines = ["✨ JX Award Seat Found ✨"]
    for r in results:
        lines.append(f"• {r['date']} {r['origin']}→{r['dest']} {r['flight']} — {r['miles']} miles — {r['cabin']}")
    return "\n".join(lines)

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[WARN] Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg})

async def main():
    async with async_playwright() as p:
        results_all = []
        if DATE_START and DATE_END:
            start = datetime.strptime(DATE_START, "%Y-%m-%d")
            end = datetime.strptime(DATE_END, "%Y-%m-%d")
            cur = start
            while cur <= end:
                ds = cur.strftime("%Y-%m-%d")
                print(f"[INFO] Checking {ORIGIN}->{DEST} on {ds}...")
                results = await search_once(p, ds)
                results_all.extend(results)
                cur += timedelta(days=1)
        else:
            print(f"[INFO] Checking {ORIGIN}->{DEST} on {DATE}...")
            results = await search_once(p, DATE)
            results_all.extend(results)

        if results_all:
            msg = format_message(results_all)
            print("[FOUND]", msg)
            send_telegram(msg)
        else:
            print("[NONE] No award seat found")

if __name__ == "__main__":
    if RUN_ONCE:
        asyncio.run(main())
    else:
        while True:
            asyncio.run(main())
            time.sleep(INTERVAL)
