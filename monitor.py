import os
import re
import time
import asyncio
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ========= 環境變數 =========
ORIGIN = os.getenv("ALASKA_ORIGIN") or os.getenv("ORIGIN", "TPE")
DEST = os.getenv("ALASKA_DEST") or os.getenv("DEST", "LAX")

# 單日或區間，擇一（YYYY-MM-DD）
DATE = os.getenv("DATE")
DATE_START = os.getenv("ALASKA_START_DATE") or os.getenv("DATE_START")
DATE_END = os.getenv("ALASKA_END_DATE") or os.getenv("DATE_END")

# 行程型態：one_way / round_trip
TRIP_TYPE = (os.getenv("ALASKA_TRIP_TYPE") or "one_way").lower()

# 乘客數
PAX_ADT = int(os.getenv("ALASKA_PAX_ADT") or "1")

# 艙等過濾：BUSINESS / ECONOMY / PREMIUM / FIRST / MAIN / ANY（ANY = 全艙等）
CABIN_FILTER = (os.getenv("ALASKA_CABIN") or "BUSINESS").upper()

# Telegram
TG_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
TG_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID") or "").strip()

# 其他
HEADLESS = (os.getenv("HEADLESS", "1") == "1")
RUN_ONCE = (os.getenv("RUN_ONCE", "0") == "1")
INTERVAL = int(os.getenv("POLL_INTERVAL_SEC") or os.getenv("INTERVAL") or "1800")

# 除錯：存 HTML/截圖；把檔案（可選）傳到 Telegram
DEBUG = (os.getenv("DEBUG", "0") == "1")
DEBUG_TG = (os.getenv("DEBUG_TG", "0") == "1")

# ========= 小工具 =========
def log(msg: str):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{now}] {msg}", flush=True)

def send_telegram_text(text: str):
    if not (TG_TOKEN and TG_CHAT_ID):
        log("[TEL] not configured")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text}
        )
        log(f"[TEL] sendMessage status={r.status_code}")
    except Exception as e:
        log(f"[TEL] sendMessage error: {e}")

def send_telegram_file(path: str, caption: str = ""):
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT_ID, "caption": caption},
                files={"document": f}
            )
        log(f"[TEL] sendDocument {path} status={r.status_code}")
    except Exception as e:
        log(f"[TEL] sendDocument error: {e}")

def format_message(results):
    lines = ["✨ JX Award Seat Found (Alaska Award) ✨"]
    for r in results:
        lines.append(
            f"• {r['date']} {r['origin']}→{r['dest']} "
            f"{r.get('flight','JX')} — {r.get('miles','?')} miles — {r.get('cabin','')}"
        )
    return "\n".join(lines)

def mmddyyyy(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return dt.strftime("%m/%d/%Y")

# ========= 主要流程：完整模擬官網查「Use miles」 =========
CARD_SELECTOR = "div.flight-card, div.akam-flight-card, [data-testid*='flight'], [class*='flight']"

async def fill_origin_dest_date_and_search(page, origin: str, dest: str, date_str: str):
    """在首頁操作：One-way/RT、From/To、勾 Use miles、填日期、查詢"""
    # 1) 開首頁
    await page.goto("https://www.alaskaair.com/", wait_until="load")
    await page.wait_for_timeout(1000)

    # 2) Trip type
    try:
        if TRIP_TYPE == "one_way":
            # 按 One-way（各種可能文字/aria）
            one_way_locators = [
                page.get_by_role("radio", name=re.compile("one[- ]?way", re.I)),
                page.get_by_text(re.compile("^One[- ]?way$", re.I)).nth(0),
                page.locator("[aria-label*='One-way' i]"),
            ]
            for lc in one_way_locators:
                try:
                    if await lc.is_visible(timeout=1000):
                        await lc.click()
                        break
                except Exception:
                    pass
    except Exception as e:
        log(f"[WARN] trip type set failed: {e}")

    # 3) 勾 Use miles（Award）
    try:
        award_locators = [
            page.get_by_label(re.compile("Use miles|award", re.I)),
            page.get_by_role("checkbox", name=re.compile("Use miles|award", re.I)),
            page.locator("input[type='checkbox'][name*='award' i]"),
        ]
        clicked = False
        for lc in award_locators:
            try:
                if await lc.is_visible(timeout=1200):
                    checked = await lc.is_checked()
                    if not checked:
                        await lc.click()
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            # 有些版本是按鈕
            try:
                btn = page.get_by_text(re.compile("Use miles|Award", re.I))
                await btn.first.click(timeout=1200)
            except Exception:
                pass
    except Exception as e:
        log(f"[WARN] award toggle failed: {e}")

    # 4) From / To
    # 盡量匹配多種實作
    async def safe_fill(label_regex, value):
        cands = [
            page.get_by_label(label_regex),
            page.get_by_placeholder(label_regex),
            page.get_by_role("textbox", name=label_regex),
            page.locator(f"input[aria-label*='{value}' i]"),
        ]
        for lc in cands:
            try:
                if await lc.is_visible(timeout=1200):
                    await lc.click()
                    await lc.fill("")
                    await lc.type(value, delay=30)
                    await page.wait_for_timeout(600)
                    # 選第一個建議
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
        return False

    ok_from = await safe_fill(re.compile("From|From where|from airport", re.I), origin)
    ok_to = await safe_fill(re.compile("To|To where|to airport", re.I), dest)
    if not (ok_from and ok_to):
        log("[WARN] from/to fill fallback")
        # 後備：某些布局用 data-testid
        try:
            await page.locator("[data-testid*='from']").fill(origin)
            await page.keyboard.press("Enter")
            await page.locator("[data-testid*='to']").fill(dest)
            await page.keyboard.press("Enter")
        except Exception:
            pass

    # 5) 日期（多半可直接輸入 mm/dd/yyyy）
    date_mmdd = mmddyyyy(date_str)
    filled_date = False
    date_locators = [
        page.get_by_label(re.compile("Depart|Departure", re.I)),
        page.get_by_placeholder(re.compile("MM/|mm/", re.I)),
        page.locator("input[type='text'][name*='depart' i]"),
        page.locator("input[type='text'][aria-label*='Depart' i]"),
    ]
    for lc in date_locators:
        try:
            if await lc.is_visible(timeout=1200):
                await lc.click()
                await lc.fill("")
                await lc.type(date_mmdd, delay=30)
                await page.keyboard.press("Enter")
                filled_date = True
                break
        except Exception:
            pass

    if not filled_date:
        # 後備：開日曆用鍵盤選
        try:
            await page.keyboard.press("Tab")
            await page.keyboard.type(date_mmdd, delay=30)
            await page.keyboard.press("Enter")
        except Exception:
            pass

    # 6) Submit / Find flights
    # 不同版本可能叫 "Find flights" / "Search" / "Continue"
    submitted = False
    submit_candidates = [
        page.get_by_role("button", name=re.compile("Find flights|Search|Continue", re.I)),
        page.get_by_text(re.compile("^Find flights$|^Search$", re.I)),
        page.locator("button[type='submit']"),
    ]
    for lc in submit_candidates:
        try:
            if await lc.is_visible(timeout=1500):
                await lc.click()
                submitted = True
                break
        except Exception:
            pass

    if not submitted:
        # 退而求其次：Enter
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    # 等待結果頁
    try:
        await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except PwTimeout:
        log("[WARN] results card not visible in 20s; +5s")
        await page.wait_for_timeout(5000)

async def parse_results(page, date_str: str):
    """解析結果頁，抓 Operated by STARLUX 的航班與里程/艙等"""
    results = []

    # Debug: 存一份 HTML/截圖
    try:
        if DEBUG:
            html = await page.content()
            out_html = f"/tmp/page_{ORIGIN}-{DEST}_{date_str}.html"
            with open(out_html, "w", encoding="utf-8") as f:
                f.write(html)
            log(f"[DEBUG] saved HTML -> {out_html} (len={len(html)})")
            if DEBUG_TG:
                send_telegram_file(out_html, caption=f"{ORIGIN}->{DEST} {date_str} HTML")

            shot = f"/tmp/screen_{ORIGIN}-{DEST}_{date_str}.png"
            await page.screenshot(path=shot, full_page=True)
            log(f"[DEBUG] saved screenshot -> {shot}")
            if DEBUG_TG:
                send_telegram_file(shot, caption=f"{ORIGIN}->{DEST} {date_str} screenshot")
    except Exception as e:
        log(f"[DEBUG] save debug assets failed: {e}")

    cards = await page.query_selector_all(CARD_SELECTOR)
    log(f"[INFO] {date_str} card count = {len(cards)}")

    for c in cards:
        text = ""
        try:
            text = await c.inner_text()
        except Exception:
            continue
        u = text.upper()

        # 只要星宇（通常結果會顯示 "Operated by STARLUX" 或 "STARLUX"）
        if "STARLUX" not in u:
            # 也試試 "OPERATED BY STARLUX"
            if "OPERATED BY" in u and "STARLUX" not in u:
                continue
            else:
                continue

        # 艙等辨識（盡量涵蓋）
        cabin_names = ["BUSINESS", "FIRST", "PREMIUM CLASS", "PREMIUM", "ECONOMY", "MAIN"]
        found = [cn for cn in cabin_names if cn in u]
        cabin = found[0] if found else "UNKNOWN"

        # 艙等過濾
        if CABIN_FILTER != "ANY" and CABIN_FILTER not in u:
            continue

        # 里程數
        miles_match = re.search(r"(\d[\d,\.]+)\s*miles", text, re.IGNORECASE)
        miles = miles_match.group(1) if miles_match else "N/A"

        # 班號
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

    log(f"[INFO] {date_str} found {len(results)} result(s)")
    return results

async def run_one_day(p, date_str: str):
    browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
    page = await browser.new_page(locale="en-US")  # 英文界面比較穩
    try:
        await fill_origin_dest_date_and_search(page, ORIGIN, DEST, date_str)
        res = await parse_results(page, date_str)
        return res
    finally:
        await browser.close()

async def run_once():
    # 啟動心跳
    send_telegram_text("✅ JX award monitor started (Use miles)")
    log("=== JX award monitor started ===")
    log(f"Route: {ORIGIN}->{DEST}  Trip={TRIP_TYPE}  PAX={PAX_ADT}")
    if DATE_START and DATE_END:
        log(f"Date range: {DATE_START} ~ {DATE_END}")
    else:
        log(f"Date (single): {DATE or 'N/A'}")
    log(f"Cabin={CABIN_FILTER}  Headless={HEADLESS}  Interval={INTERVAL}s  Debug={DEBUG}/{DEBUG_TG}")

    results_all = []
    async with async_playwright() as p:
        if DATE_START and DATE_END:
            start = datetime.strptime(DATE_START, "%Y-%m-%d")
            end = datetime.strptime(DATE_END, "%Y-%m-%d")
            cur = start
            while cur <= end:
                ds = cur.strftime("%Y-%m-%d")
                log(f"[INFO] Checking {ORIGIN}->{DEST} on {ds} (award)...")
                try:
                    results_all.extend(await run_one_day(p, ds))
                except Exception as e:
                    log(f"[ERR] {ds} search failed: {e}")
                cur += timedelta(days=1)
        else:
            ds = DATE or datetime.utcnow().strftime("%Y-%m-%d")
            log(f"[INFO] Checking {ORIGIN}->{DEST} on {ds} (award)...")
            try:
                results_all.extend(await run_one_day(p, ds))
            except Exception as e:
                log(f"[ERR] {ds} search failed: {e}")

    if results_all:
        msg = format_message(results_all)
        log("[FOUND] sending Telegram")
        send_telegram_text(msg)
    else:
        log("[NONE] No award seat found this run")

# ========= 入口 =========
if __name__ == "__main__":
    if RUN_ONCE:
        asyncio.run(run_once())
    else:
        while True:
            asyncio.run(run_once())
            log(f"[SLEEP] waiting {INTERVAL}s for next run")
            time.sleep(INTERVAL)
