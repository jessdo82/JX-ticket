# monitor.py — JX(Starlux) award detector on Alaska (precise JSON parse, quiet)
import os, re, json, time, asyncio
from datetime import datetime, timedelta
import requests
from playwright.async_api import async_playwright

# ========= Config via ENV =========
BOT  = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

ORIGIN = os.getenv("JX_ORIGIN", "TPE")
DEST   = os.getenv("JX_DEST",   "NRT")
DATE   = os.getenv("JX_DATE",   "2025-10-01")              # 單日；想跑區間見最下方多日註解
INTERVAL = int(os.getenv("JX_INTERVAL_SEC", "1800"))       # 每輪間隔(秒)
HEADLESS = (os.getenv("HEADLESS", "1") == "1")
DEBUG_DUMP = (os.getenv("JX_DEBUG", "0") == "1")           # 1=傳回原始JSON做除錯

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/123.0.0.0 Safari/537.36")

# 僅攔這些看起來像查票/兌換的 API
URL_HINTS = [
    "award", "awardshopping", "availability", "shopping", "search", "offers", "pricing"
]
HOST_HINTS = ["alaskaair.com", "api.alaskaair.com", "as.api.alaskaair.com"]

# 降噪：同一個結果(日期+航班+艙等+里程) 6小時內只通知一次
DEDUP_TTL_SEC = 6 * 3600
dedup_cache = {}

def log(msg: str):
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {msg}", flush=True)

def tg_send(text: str):
    if not (BOT and CHAT): 
        log("[TG] not configured"); 
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id": CHAT, "text": text, "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        log(f"[TG] send error: {e}")

def tg_file(path: str, caption: str = ""):
    if not (BOT and CHAT): 
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT}/sendDocument",
                data={"chat_id": CHAT, "caption": caption},
                files={"document": f},
                timeout=60
            )
    except Exception as e:
        log(f"[TG] file error: {e}")

def looks_like_api(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in HOST_HINTS) and any(k in u for k in URL_HINTS)

# ---------------- JSON 解析：抓出 JX 有位/候補 ----------------
CARRIER_KEYS = [
    "operatingCarrierCode","marketingCarrierCode","operatingAirlineCode","marketingAirlineCode",
    "carrierCode","airlineCode","carrier","airline"
]
SEAT_KEYS = ["availability","available","seatsRemaining","inventory","seats","remainingSeats"]
WAITLIST_HINT = ["WAIT", "WL", "REQUEST"]
STATUS_KEYS = ["status","availabilityStatus","bookingStatus"]

MILES_KEYS = ["miles","points","awardMiles","priceMiles","lowestAwardMiles","loyaltyMiles","totalMiles","price"]
CABIN_KEYS = ["FIRST","BUSINESS","PREMIUM","ECONOMY","MAIN","COACH","CABIN","CLASS","BOOKINGCLASS","FARECLASS"]
FLIGHT_KEYS = ["flightNumber","operatingFlightNumber","marketingFlightNumber","number","flight"]

def _to_str(x):
    if x is None: return None
    if isinstance(x,(int,float)): return str(x)
    if isinstance(x,str): return x
    return None

def _has_jx(d: dict) -> bool:
    for k in CARRIER_KEYS:
        if k in d:
            v = _to_str(d[k])
            if v and ("JX" in v.upper() or "STARLUX" in v.upper()):
                return True
    return False

def _seats_value(d: dict):
    # 回傳 (has_explicit_info, seats/int, is_waitlist/bool)
    has_info=False; seats=None; wait=False
    for k in SEAT_KEYS:
        if k in d:
            has_info=True
            try:
                seats = int(str(d[k]).replace(",",""))
            except:
                # true/false 也視為 1/0
                if str(d[k]).lower()=="true": seats=1
                elif str(d[k]).lower()=="false": seats=0
    for k in STATUS_KEYS:
        if k in d:
            has_info=True
            sv = _to_str(d[k]) or ""
            if any(w in sv.upper() for w in WAITLIST_HINT): wait=True
    return has_info, seats, wait

def _pick(d: dict, keys):
    for k in keys:
        if k in d:
            v = _to_str(d[k])
            if v: return v
    return None

def _collect_items(obj, out: list, date_str: str):
    """深度走訪 JSON，搜集 JX 相關 offer/segment，推斷是否有位"""
    try:
        if isinstance(obj, dict):
            if _has_jx(obj):
                has_info, seats, is_wl = _seats_value(obj)
                miles = _pick(obj, MILES_KEYS)
                cabin = _pick(obj, CABIN_KEYS)
                flight = _pick(obj, FLIGHT_KEYS)
                if miles:
                    miles = re.sub(r"[^\d]", "", miles) or miles
                item = {
                    "date": date_str,
                    "flight": (flight or "JX").replace(" ",""),
                    "cabin": cabin or "?",
                    "miles": miles or "?",
                    "seats": seats,
                    "waitlist": bool(is_wl),
                    "has_info": has_info,
                }
                # 只有有資訊(座位數/狀態)才比較可信；若沒有但有 miles 也先收
                out.append(item)
            for v in obj.values():
                _collect_items(v, out, date_str)
        elif isinstance(obj, list):
            for v in obj:
                _collect_items(v, out, date_str)
    except Exception:
        pass

def parse_award_json(payload, date_str: str):
    """回傳三組：avail(有位)、wl(候補)、others(無明確資訊)"""
    bucket = []
    _collect_items(payload, bucket, date_str)
    avail=[]; wl=[]; others=[]
    for it in bucket:
        seats = it.get("seats")
        wait = it.get("waitlist")
        has_info = it.get("has_info")
        # 明確有位： seats>0 或 available=True
        if seats is not None and seats > 0:
            avail.append(it); continue
        if wait:
            wl.append(it); continue
        # 沒明確資訊但有 miles → 放 others（可視情況當成有位，但我保守處理）
        if it.get("miles") not in (None,"?"):
            others.append(it)
    return avail, wl, others

def fmt_items(prefix, items):
    lines=[prefix]
    for it in items[:8]:
        lines.append(f"• {it['date']} {ORIGIN}->{DEST} {it.get('flight','JX')} | {it.get('cabin','?')} | {it.get('miles','?')} miles"
                     + (f" | seats≈{it['seats']}" if it.get('seats') is not None else ""))
    return "\n".join(lines)

def dedup_key(it):
    return f"{it.get('date')}|{ORIGIN}-{DEST}|{it.get('flight')}|{it.get('cabin')}|{it.get('miles')}"

def not_recently_notified(items):
    """過濾掉 6小時內發過的項目"""
    now = time.time()
    kept=[]
    for it in items:
        k = dedup_key(it)
        ts = dedup_cache.get(k, 0)
        if now - ts >= DEDUP_TTL_SEC:
            kept.append(it)
            dedup_cache[k] = now
    return kept

# ----------------- Page flow & capture -----------------
async def search_and_capture(date_str: str):
    url = (f"https://www.alaskaair.com/planbook/flights"
           f"?origin={ORIGIN}&destination={DEST}"
           f"&departureDate={date_str}&awardBooking=true")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        page = await browser.new_page(locale="en-US", user_agent=UA)

        found_avail=[]; found_wl=[]; found_others=[]
        got_any=False

        async def on_response(resp):
            nonlocal found_avail, found_wl, found_others, got_any
            try:
                req = resp.request
                if req.resource_type not in ("xhr","fetch"): return
                if not looks_like_api(req.url): return
                ctype = (resp.headers or {}).get("content-type","").lower()
                if "json" not in ctype: return
                text = await resp.text()
                if not text or text.strip()[0] not in "{[": return
                payload = json.loads(text)

                if DEBUG_DUMP:
                    fpath = f"/tmp/award_{int(time.time())}.json"
                    with open(fpath,"w",encoding="utf-8") as f: f.write(text)
                    tg_file(fpath, f"debug dump | {req.url}")

                avail, wl, others = parse_award_json(payload, date_str)
                if avail or wl or others:
                    got_any=True
                    found_avail += avail
                    found_wl    += wl
                    found_others+= others
            except Exception as e:
                log(f"[on_response] {e}")

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="networkidle")
            # 等待網頁把航班API都打完
            await page.wait_for_timeout(15000)
        finally:
            await browser.close()

    return got_any, found_avail, found_wl, found_others

# ----------------- Runner -----------------
async def run_once(date_str: str):
    got, avail, wl, others = await search_and_capture(date_str)

    # 只在「有位」或「候補」時通知；平常安靜
    msg_parts=[]

    avail = not_recently_notified(avail)
    wl    = not_recently_notified(wl)

    if avail:
        msg_parts.append(fmt_items("✅ JX award AVAILABLE", avail))
    elif wl:
        msg_parts.append(fmt_items("🟡 JX award WAITLIST (no confirmed seats)", wl))
    elif got and others:
        # 有抓到 JX 結構但沒有明確 seats；若你想把它當作「可能有位」，把這一段改成✅
        pass  # 目前不通知，保持安靜；除非你要放寬

    if msg_parts:
        tg_send("\n\n".join(msg_parts))
        log("[INFO] sent notification")
    else:
        log("[INFO] no JX award availability this run")

async def main():
    while True:
        try:
            await run_once(DATE)
        except Exception as e:
            log(f"[ERROR] {e}")
            tg_send(f"⚠️ monitor error: {e}")
        await asyncio.sleep(INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
