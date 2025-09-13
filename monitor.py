# monitor.py  — clean award-only, JX-only, anti-spam
import os, asyncio, json, time, re
from datetime import datetime, timezone
from playwright.async_api import async_playwright

BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

ORIGIN  = os.getenv("JX_ORIGIN", "TPE")
DEST    = os.getenv("JX_DEST",   "NRT")
DATE    = os.getenv("JX_DATE",   "2025-10-01")
INTERVAL= int(os.getenv("JX_INTERVAL_SEC", "1800"))
COOLDOWN= int(os.getenv("JX_TG_COOLDOWN_SEC", "300"))

# 僅當這個 flag=1 時，才把所有 XHR/JSON 丟 TG（預設 0）
API_DUMP = os.getenv("JX_API_DUMP", "0") == "1"

# 只關注這些「可能是航班可賣/可兌換資料」的請求
URL_WHITELIST = [
    r"/award", r"/awardshopping", r"/shopping", r"/availability", r"/offers", r"/flights"
]

last_tg_sent = 0

async def tg_send(text: str):
    global last_tg_sent
    now = time.time()
    if now - last_tg_sent < COOLDOWN:
        return
    import aiohttp
    async with aiohttp.ClientSession() as s:
        await s.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                     json={"chat_id": CHAT, "text": text, "disable_web_page_preview": True})
    last_tg_sent = now

async def tg_file(path: str, caption: str = ""):
    # 只用於除錯；常態不會觸發
    import aiohttp
    data = {"chat_id": CHAT, "caption": caption}
    async with aiohttp.ClientSession() as s:
        with open(path, "rb") as f:
            form = aiohttp.FormData()
            for k,v in data.items(): form.add_field(k, str(v))
            form.add_field("document", f, filename=os.path.basename(path))
            await s.post(f"https://api.telegram.org/bot{BOT}/sendDocument", data=form)

def looks_like_award_json(obj: dict) -> bool:
    """非常保守地判斷是不是航班/兌換結果 JSON"""
    if not isinstance(obj, dict): return False
    text = json.dumps(obj, ensure_ascii=False)
    # 關鍵欄位（不同供應端名稱可能不同，所以用多組關鍵字）
    keys_hit = any(k in text for k in [
        "flights", "segments", "itineraries", "offers",
        "operatingCarrierCode", "marketingCarrierCode",
        "operatingAirlineCode", "marketingAirlineCode",
        "\"JX\"", "STARLUX", "Starlux"
    ])
    # 只要內容有 JX or STARLUX 或者有明顯航班結構，就算
    return keys_hit

def extract_awards_summary(obj: dict) -> str:
    """從 JSON 粗略萃取可讀摘要（艙等/航班/里程）"""
    lines = []
    def add(line): lines.append(line)

    # 常見字段嘗試
    try:
        offers = obj.get("offers") or obj.get("data") or []
        if isinstance(offers, dict):
            offers = offers.get("offers", [])
        cnt = 0
        for off in offers:
            # 航司 & 航班
            carrier = (off.get("operatingCarrierCode")
                       or off.get("marketingCarrierCode")
                       or off.get("operatingAirlineCode")
                       or off.get("marketingAirlineCode"))
            if carrier and str(carrier).upper() != "JX":
                continue
            # 里程/艙等
            miles = (off.get("miles") or off.get("points") or off.get("awardMiles") or off.get("price"))
            cabin = (off.get("cabin") or off.get("fareClass") or off.get("bookingClass"))
            segs  = off.get("segments") or off.get("flights") or []
            route = []
            for s in segs:
                o = s.get("origin") or s.get("from") or s.get("departureAirport") or s.get("dep",{}).get("airport")
                d = s.get("destination") or s.get("to") or s.get("arrivalAirport") or s.get("arr",{}).get("airport")
                fno = s.get("flightNumber") or s.get("number")
                route.append(f"{o}-{d} {fno}")
            if not route:
                continue
            cnt += 1
            add(f"JX award: {' / '.join(route)} | {cabin or '?'} | {miles}")
        if cnt:
            return "\n".join(lines)
    except Exception:
        pass
    # 後備：只說找到 JX 相關結果
    return "Found possible JX award results (details in JSON)."

def url_allowed(url: str) -> bool:
    return any(re.search(p, url) for p in URL_WHITELIST)

async def run_once():
    url = (f"https://www.alaskaair.com/planbook/flights?origin={ORIGIN}"
           f"&destination={DEST}&departureDate={DATE}&awardBooking=true")
    await tg_send(f"🔍 Checking {ORIGIN}->{DEST} on {DATE} (award only)")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        # 攔截 XHR
        async def on_response(resp):
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch"):  # 只看 API
                    return
                u = req.url
                if not url_allowed(u):
                    return
                ctype = (resp.headers or {}).get("content-type","")
                if "json" not in ctype:
                    return
                text = await resp.text()
                if not text or text.strip()[0] not in "{[":
                    return
                data = json.loads(text)

                if API_DUMP:
                    # 只有你把 JX_API_DUMP=1 才會送原始 JSON 做除錯
                    fname = f"/tmp/resp_{int(time.time())}.json"
                    with open(fname,"w",encoding="utf-8") as f: f.write(text)
                    await tg_file(fname, f"XHR 200 | {u}")

                # 真正通知條件（找到 JX 航班/兌換）
                if looks_like_award_json(data):
                    summary = extract_awards_summary(data)
                    await tg_send(f"✅ Award found (JX)\n{summary}")
            except Exception:
                pass

        page.on("response", on_response)
        await page.goto(url, wait_until="networkidle")
        # 給頁面一點時間跑完內部請求
        await page.wait_for_timeout(8000)
        await browser.close()

async def main():
    while True:
        try:
            await run_once()
        except Exception as e:
            await tg_send(f"⚠️ error: {e}")
        await asyncio.sleep(INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
