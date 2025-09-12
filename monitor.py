import os
import re
import time
import asyncio
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ========= Env =========
ORIGIN = os.getenv("ALASKA_ORIGIN") or os.getenv("ORIGIN", "TPE")
DEST = os.getenv("ALASKA_DEST") or os.getenv("DEST", "SFO")
DATE = os.getenv("DATE")  # 單日
DATE_START = os.getenv("ALASKA_START_DATE") or os.getenv("DATE_START")
DATE_END = os.getenv("ALASKA_END_DATE") or os.getenv("DATE_END")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")

HEADLESS = (os.getenv("HEADLESS", "1") == "1")
RUN_ONCE = (os.getenv("RUN_ONCE", "0") == "1")
INTERVAL = int(os.getenv("POLL_INTERVAL_SEC") or os.getenv("INTERVAL") or "1800")
CABIN_FILTER = os.getenv("ALASKA_CABIN", "BUSINESS").upper()  # BUSINESS / ECONOMY / PREMIUM / FIRST / MAIN / ANY

DEBUG = os.getenv("DEBUG", "0") == "1"         # 存 HTML 到 /tmp
DEBUG_TG = os.getenv("DEBUG_TG", "0") == "1"   # 也用 Telegram 傳 HTML 檔

# ========= Utils =========
def log(msg: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{now}] {msg}", flush=True)

def send_telegram_text(text: str):
    if not (TG_TOKEN and TG_CHAT_ID):
        log("[TEL] not configured")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text})
    log(f"[TEL] sendMessage status={r.status_code}")

def send_telegram_file(path: str, caption: str = ""):
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
        with open(path, "rb") as f:
            r = requests.post(url, data={"chat_id": TG_CHAT_ID, "caption": caption}, files={"document": f})
        log(f"[TEL] sendDocument {path} status={r.status_code}")
    except Exception as e:
        log(f"[TEL] sendDocument error: {e}")

def format_message(results):
    lines = ["✨ JX Award Seat Found ✨"]
    for r in results:
        lines.append(
            f"• {r['date']} {r['origin']}→{r['dest']} "
            f"{r.get('flight','JX')} — {r.get('miles','?')} miles — {r.get('cabin','')}"
        )
    return "\n".join(lines)

# ========= Core =========
CARD_SELECTOR = "div.flight-card, div.akam-flight-card, [data-testid*='flight'], [class*='flight']"

async def search_one_date(p, date_str: str):
    """查詢單一天回傳結果清單；若 DEBUG 會存 HTML。"""
    browser = await p.chromium.launch(headless=HEADLESS)
    page = await browser.new_page()
    url = (
        "https://www.alaskaair.com/PlanBook/Flights"
        f"?origin={ORIGIN}&destination={DEST}&departureDate={date_str}&awardBooking=true"
    )
    log(f"[INFO] goto {url}")
    await page.goto(url, wait_until="load")

    # 等到航班卡片真的渲染出來（最多 20 秒）；失敗則再補等 5 秒
    try:
        await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except PwTimeout:
        log("[WARN] card selector not visible in 20s; fallback waiting 5s")
        await page.wait_for_timeout(5000)

    # 需要就把 HTML 存檔，方便對比官網
    results = []
    try:
        html = await page.content()
        if DEBUG:
            out = f"/tmp/page_{date_str}.html"
            with open(out, "w", encoding="utf-8") as f:
                f.write(html)
            log(f"[DEBUG] saved HTML -> {out} (len={len(html)})")
            if DEBUG_TG:
                send_telegram_file(out, caption=f"HTML {ORIGIN}->{DEST} {date_str}")
    except Exception as e:
        log(f"[DEBUG] save html failed: {e}")

    # 解析卡片
    cards = await page.query_selector_all(CARD_SELECTOR)
    log(f"[INFO] {date_str} card count = {len(cards)}")
    for c in cards:
        try:
            text = await c.inner_text()
        except Exception:
            continue
        u = text.upper()

        # 只要星宇承運
        if "STARLUX" not in u:
            continue

        # 判斷艙等（盡量涵蓋）
        cabin_names = ["BUSINESS", "FIRST", "PREMIUM CLASS", "PREMIUM", "ECONOMY", "MAIN"]
        found = [cn for cn in cabin_names if cn in u]
        cabin = found[0] if found else "UNKNOWN"

        # 艙等過濾
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
    log(f"[INFO] {date_str} found {len(results)} result(s)")
    return results

async def run_once():
    log("=== JX monitor started ===")
    log(f"Route: {ORIGIN}->{DEST}")
    if DATE_START and DATE_END:
        log(f"Date range: {DATE_START} ~ {DATE_END}")
    else:
        log(f"Date (single): {DATE or 'N/A'}")
    log(f"Headless={HEADLESS} Interval={INTERVAL}s Cabin={CABIN_FILTER} Debug={DEBUG} TgFile={DEBUG_TG}")

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
            ds = DATE or datetime.utcnow().strftime("%Y-%m-%d")
            log(f"[INFO] Checking {ORIGIN}->{DEST} on {ds} ...")
            try:
                results_all.extend(await search_one_date(p, ds))
            except Exception as e:
                log(f"[ERR] {ds} search failed: {e}")

    if results_all:
        msg = format_message(results_all)
        log("[FOUND] sending Telegram")
        send_telegram_text(msg)
    else:
        log("[NONE] No award seat found this run")

if __name__ == "__main__":
    if RUN_ONCE:
        asyncio.run(run_once())
    else:
        while True:
            asyncio.run(run_once())
            log(f"[SLEEP] waiting {INTERVAL}s for next run")
            time.sleep(INTERVAL)
