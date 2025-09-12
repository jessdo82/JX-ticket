import os
import re
import time
import asyncio
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright

# ---- Env (支援 ALASKA_* 及簡短名稱，兩者擇一皆可) ----
ORIGIN = os.getenv("ALASKA_ORIGIN") or os.getenv("ORIGIN", "TPE")
DEST = os.getenv("ALASKA_DEST") or os.getenv("DEST", "SFO")

DATE = os.getenv("DATE")  # 單日模式可用
DATE_START = os.getenv("ALASKA_START_DATE") or os.getenv("DATE_START")
DATE_END = os.getenv("ALASKA_END_DATE") or os.getenv("DATE_END")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")

HEADLESS = (os.getenv("HEADLESS", "1") == "1")
RUN_ONCE = (os.getenv("RUN_ONCE", "0") == "1")
INTERVAL = int(os.getenv("POLL_INTERVAL_SEC") or os.getenv("INTERVAL") or "1800")

# 艙等過濾：BUSINESS / ECONOMY / PREMIUM / FIRST / MAIN / ANY
CABIN_FILTER = os.getenv("ALASKA_CABIN", "BUSINESS").upper()

# ---- Helpers ----
def log(msg: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{now}] {msg}", flush=True)

def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        log("[WARN] Telegram not configured (skip send)")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg})
        if r.ok:
            log("[TEL] sent")
        else:
            log(f"[TEL] failed status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        log(f"[TEL] exception: {e}")

def format_message(results):
    lines = ["✨ JX Award Seat Found ✨"]
    for r in results:
        lines.append(
            f"• {r['date']} {r['origin']}→{r['dest']} "
            f"{r.get('flight','JX')} — {r.get('miles','?')} miles — {r.get('cabin','')}"
        )
    return "\n".join(lines)

# ---- Core ----
async def search_one_date(p, date_str: str):
    """查詢單一天回傳結果清單"""
    browser = await p.chromium.launch(headless=HEADLESS)
    page = await browser.new_page()
    url = (
        "https://www.alaskaair.com/PlanBook/Flights"
        f"?origin={ORIGIN}&destination={DEST}"
        f"&departureDate={date_str}&awardBooking=true"
    )
    log(f"[INFO] goto {url}")
    await page.goto(url, wait_until="load")
    await page.wait_for_timeout(7000)  # 等前端資料載入

    cards = await page.query_selector_all("div.flight-card, div.akam-flight-card, body")
    results = []

    for c in cards:
        try:
            text = await c.inner_text()
        except Exception:
            continue
        u = text.upper()

        # 只要星宇承運
        if "STARLUX" not in u:
            continue

        # 推測艙等
        cabin_names = ["BUSINESS", "FIRST", "PREMIUM CLASS", "PREMIUM", "ECONOMY", "MAIN"]
        found_cabins = [cn for cn in cabin_names if cn in u]
        cabin = found_cabins[0] if found_cabins else "UNKNOWN"

        # 依環境變數過濾
        if CABIN_FILTER != "ANY" and CABIN_FILTER not in u:
            continue

        # miles 與班號
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
    log(f"[INFO] {date_str} found {len(results)} result(s)")
    return results

async def run_once():
    # 啟動時印出設定摘要
    log("=== JX monitor started ===")
    log(f"Route: {ORIGIN}->{DEST}")
    if DATE_START and DATE_END:
        log(f"Date range: {DATE_START} ~ {DATE_END}")
    else:
        log(f"Date (single): {DATE or (DATE_START or 'N/A')}")
    log(f"Headless={HEADLESS}  Interval={INTERVAL}s  Cabin={CABIN_FILTER}")
    log(f"Telegram configured: {bool(TG_TOKEN and TG_CHAT_ID)}")

    results_all = []
    async with async_playwright() as p:
        if DATE_START and DATE_END:
            start = datetime.strptime(DATE_START, "%Y-%m-%d")
            end = datetime.strptime(DATE_END, "%Y-%m-%d")
            cur = start
            while cur <= end:
                ds = cur.strftime("%Y-%m-%d")
                log(f"[INFO] Checking {ORIGIN}->{DEST} on {ds} ...")
                try:
                    results_all.extend(await search_one_date(p, ds))
                except Exception as e:
                    log(f"[ERR] {ds} search failed: {e}")
                cur += timedelta(days=1)
        else:
            ds = DATE or (DATE_START or datetime.utcnow().strftime("%Y-%m-%d"))
            log(f"[INFO] Checking {ORIGIN}->{DEST} on {ds} ...")
            try:
                results_all.extend(await search_one_date(p, ds))
            except Exception as e:
                log(f"[ERR] {ds} search failed: {e}")

    if results_all:
        msg = format_message(results_all)
        log("[FOUND] sending Telegram")
        send_telegram(msg)
    else:
        log("[NONE] No award seat found in this run")

# ---- Entry ----
if __name__ == "__main__":
    if RUN_ONCE:
        asyncio.run(run_once())
    else:
        while True:
            asyncio.run(run_once())
            log(f"[SLEEP] waiting {INTERVAL}s for next run")
            time.sleep(INTERVAL)
