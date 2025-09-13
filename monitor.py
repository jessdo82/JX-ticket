# monitor.py — award-only JX detector (no aiohttp)
import os, re, json, time, asyncio
from datetime import datetime
import requests
from playwright.async_api import async_playwright

# ====== ENV ======
BOT  = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

ORIGIN = os.getenv("JX_ORIGIN", "TPE")
DEST   = os.getenv("JX_DEST",   "NRT")
DATE   = os.getenv("JX_DATE",   "2025-10-01")

INTERVAL = int(os.getenv("JX_INTERVAL_SEC", "1800"))       # 每輪間隔
COOLDOWN = int(os.getenv("JX_TG_COOLDOWN_SEC", "300"))     # TG 降噪冷卻（秒）
API_DUMP = os.getenv("JX_API_DUMP", "0") == "1"            # 除錯時設 1：傳回原始 JSON

HEADLESS = (os.getenv("HEADLESS", "1") == "1")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/123.0.0.0 Safari/537.36")

URL_WHITELIST = [
    r"/award", r"/awardshopping", r"/shopping", r"/availability", r"/offers", r"/flights"
]

last_tg_sent = 0

def log(msg: str):
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {msg}", flush=True)

# ====== Telegram（requests 版本，無 aiohttp）======
def tg_send(text: str):
    global last_tg_sent
    if not (BOT and CHAT): 
        log("[TG] not configured"); 
        return
    now = time.time()
    if now - last_tg_sent < COOLDOWN:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id": CHAT, "text": text, "disable_web_page_preview": True},
            timeout=15
        )
        log(f"[TG] sendMessage status={r.status_code}")
        last_tg_sent = now
    except Exception as e:
        log(f"[TG] send error: {e}")

def tg_file(path: str, caption: str = ""):
    if not (BOT and CHAT): 
        log("[TG] not configured for file")
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT}/sendDocument",
                data={"chat_id": CHAT, "caption": caption},
                files={"document": f},
                timeout=60
            )
        log(f"[TG] sendDocument {os.path.basename(path)} status={r.status_code}")
    except Exception as e:
        log(f"[TG] file error: {e}")

# ====== 判斷 & 摘要 ======
def url_allowed(url: str) -> bool:
    return any(re.search(p, url) for p in URL_WHITELIST)

def looks_like_award_json(obj) -> bool:
    if not isinstance(obj, (dict, list)): 
        return False
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return False
    # 有航班結構 或 直接出現 JX/STARLUX
    keys_hit = any(k in text for k in [
        "flights", "segments", "itineraries", "offers",
        "operatingCarrierCode", "marketingCarrierCode",
        "operatingAirlineCode", "marketingAirlineCode"
    ])
    brand_hit = ("\"JX\"" in text) or ("STARLUX" in text) or ("Starlux" in text)
    return keys_hit or brand_hit

def extract_awards_summary(obj) -> str:
    # 嘗試從常見欄位抓出航段/艙等/里程
    try:
        offers = None
        if isinstance(obj, dict):
            offers = obj.get("offers") or obj.get("data") or obj.get("results")
            if isinstance(offers, dict):
                offers = offers.get("offers") or offers.get("data")
        if not offers:
            return "Found possible JX award results (details in JSON)."
        if isinstance(offers, dict):
            offers = [offers]

        lines = []
        count = 0
        for off in offers:
            if not isinstance(off, dict): 
                continue
            carrier = (off.get("operatingCarrierCode")
                       or off.get("marketingCarrierCode")
                       or off.get("operatingAirlineCode")
                       or off.get("marketingAirlineCode"))
            if carrier and str(carrier).upper() != "JX":
                continue

            miles = (off.get("miles") or off.get("points") or off.get("awardMiles") 
                     or off.get("price") or off.get("lowestAwardMiles"))
            cabin = (off.get("cabin") or off.get("fareClass") or off.get("bookingClass"))

            segs = off.get("segments") or off.get("flights") or off.get("slices") or []
            if isinstance(segs, dict):
                segs = segs.get("segments") or segs.get("flights") or []

            route = []
            for s in segs:
                if not isinstance(s, dict): 
                    continue
                o = s.get("origin") or s.get("from") or s.get("departureAirport") or s.get("dep",{}).get("airport")
                d = s.get("destination") or s.get("to") or s.get("arrivalAirport") or s.get("arr",{}).get("airport")
                fno = s.get("flightNumber") or s.get("number") or s.get("id")
                if o and d:
                    route.append(f"{o}-{d} {fno or ''}".strip())
            if not route:
                continue

            count += 1
            lines.append(f"JX award: {' / '.join(route)} | {cabin or '?'} | {miles or '?'}")
        if count:
            return "\n".join(lines)
    except Exception:
        pass
    return "Found possible JX award results (details in JSON)."

# ====== UI 操作 ======
CARD_SELECTOR = "div.flight-card, div.akam-flight-card, [data-testid*='flight']"

async def do_search_use_miles(page, origin, dest, date_str):
    await page.goto("https://www.alaskaair.com/", wait_until="load")
    await page.wait_for_load_state("networkidle")

    # one-way
    try:
        for lc in [
            page.get_by_role("radio", name=re.compile("one[- ]?way", re.I)),
            page.get_by_text(re.compile("^One[- ]?way$", re.I)).nth(0),
            page.locator("[aria-label*='One-way' i]")
        ]:
            if await lc.is_visible(timeout=1200):
                await lc.click(); break
    except: pass

    # Use miles
    try:
        for lc in [
            page.get_by_label(re.compile("Use miles|award", re.I)),
            page.get_by_role("checkbox", name=re.compile("Use miles|award", re.I)),
            page.locator("input[type='checkbox'][name*='award' i]")
        ]:
            if await lc.is_visible(timeout=1200):
                try:
                    if await lc.is_checked(): pass
                    else: await lc.click()
                except: await lc.click()
                break
    except: pass

    # From / To
    async def fill_any(label_regex, value):
        for lc in [page.get_by_label(label_regex),
                   page.get_by_placeholder(label_regex),
                   page.get_by_role("textbox", name=label_regex)]:
            try:
                if await lc.is_visible(timeout=1200):
                    await lc.click(); await lc.fill(""); await lc.type(value, delay=40)
                    await page.keyboard.press("Enter"); return True
            except: pass
        return False

    await fill_any(re.compile("From", re.I), origin)
    await fill_any(re.compile("To", re.I), dest)

    # Date
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y")
        for lc in [page.get_by_label(re.compile("Depart|Departure", re.I)),
                   page.get_by_placeholder(re.compile("MM/|mm/", re.I))]:
            if await lc.is_visible(timeout=1200):
                await lc.fill(""); await lc.type(d, delay=40); await page.keyboard.press("Enter"); break
    except: pass

    # Submit
    try:
        for lc in [page.get_by_role("button", name=re.compile("Find flights|Search", re.I))]:
            if await lc.is_visible(timeout=1500): await lc.click(); break
    except: pass

    await page.wait_for_load_state("networkidle")
    try: await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except: pass

# ====== 主流程（只在抓到 JX award 結果時通知；可選 dump）======
async def run_once():
    search_url = (f"https://www.alaskaair.com/planbook/flights?origin={ORIGIN}"
                  f"&destination={DEST}&departureDate={DATE}&awardBooking=true")
    log(f"[INFO] checking {ORIGIN}->{DEST} on {DATE}")
    if BOT and CHAT:
        tg_send(f"🔍 Checking {ORIGIN}->{DEST} on {DATE} (award)")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        page = await browser.new_page(locale="en-US", user_agent=UA)

        async def on_response(resp):
            try:
                req = resp.request
                if req.resource_type not in ("xhr","fetch"):
                    return
                url = req.url
                if not url_allowed(url):
                    return
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                text = await resp.text()
                if not text or text.strip()[0] not in "{[":
                    return
                data = json.loads(text)

                # 除錯用（你把 JX_API_DUMP=1 時才會傳 JSON）
                if API_DUMP:
                    fname = f"/tmp/resp_{int(time.time())}.json"
                    with open(fname, "w", encoding="utf-8") as f: f.write(text)
                    tg_file(fname, f"API {resp.status} | {url}")

                # 真正的通知：抓到 JX/STARLUX 的 award 結果
                if looks_like_award_json(data):
                    summary = extract_awards_summary(data)
                    tg_send(f"✅ JX award found\n{summary}")
            except Exception as e:
                log(f"[on_response] {e}")

        page.on("response", on_response)
        await page.goto(search_url, wait_until="networkidle")
        await page.wait_for_timeout(12000)  # 等 12 秒把內部 API 跑完
        await browser.close()

async def main():
    while True:
        try:
            await run_once()
        except Exception as e:
            log(f"[ERROR] {e}")
            tg_send(f"⚠️ error: {e}")
        await asyncio.sleep(INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
