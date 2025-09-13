import os, re, time, json, asyncio
from datetime import datetime, timedelta
import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ========= ENV =========
ORIGIN = os.getenv("ALASKA_ORIGIN") or "TPE"
DEST   = os.getenv("ALASKA_DEST")   or "NRT"
DATE = os.getenv("DATE")
DATE_START = os.getenv("ALASKA_START_DATE")
DATE_END   = os.getenv("ALASKA_END_DATE")
TRIP_TYPE = (os.getenv("ALASKA_TRIP_TYPE") or "one_way").lower()
CABIN_FILTER = (os.getenv("ALASKA_CABIN") or "ANY").upper()
TG_TOKEN   = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
HEADLESS = (os.getenv("HEADLESS","1")=="1")
RUN_ONCE = (os.getenv("RUN_ONCE","0")=="1")
INTERVAL = int(os.getenv("POLL_INTERVAL_SEC") or "1800")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/123.0.0.0 Safari/537.36")

# ========= utils =========
def log(msg:str): print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {msg}", flush=True)

def send_tg_text(text:str):
    if not (TG_TOKEN and TG_CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id":TG_CHAT_ID,"text":text}, timeout=15)
    except: pass

def send_tg_file(path:str, caption:str=""):
    if not (TG_TOKEN and TG_CHAT_ID): return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id":TG_CHAT_ID,"caption":caption},
                          files={"document":f}, timeout=60)
    except: pass

def mmddyyyy(iso:str)->str:
    return datetime.strptime(iso,"%Y-%m-%d").strftime("%m/%d/%Y")

# ========= API heuristics & parsing =========
URL_HINTS = [
    "award", "awardshopping", "availability", "shop", "search", "calendar",
    "offer", "pricing", "price", "shopping"
]
HOST_HINTS = ["alaskaair.com", "api.alaskaair.com", "as.api.alaskaair.com"]

JSON_FIELD_HINTS = [
    "segments", "itineraries", "flights", "offers", "slices", "legs",
    "operatingCarrier", "operatingCarrierCode", "carrierCode", "airlineCode",
    "marketingCarrier", "marketingCarrierCode"
]

def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:120]

def _url_looks_like_api(url:str)->bool:
    u = url.lower()
    return any(h in u for h in HOST_HINTS) and any(k in u for k in URL_HINTS)

def _json_has_flight_fields(payload)->bool:
    try:
        if isinstance(payload, dict):
            for k in payload.keys():
                ku = str(k).lower()
                if any(h in ku for h in [s.lower() for s in JSON_FIELD_HINTS]):
                    return True
            for v in payload.values():
                if _json_has_flight_fields(v): return True
        elif isinstance(payload, list):
            for v in payload:
                if _json_has_flight_fields(v): return True
    except: pass
    return False

CABIN_KEYS = ["BUSINESS","FIRST","PREMIUM CLASS","PREMIUM","ECONOMY","MAIN"]
CARRIER_KEYS = [
    "operatingCarrierCode","operatingCarrier","operatingAirline","operatingAirlineCode",
    "marketingCarrierCode","marketingCarrier","marketingAirline","marketingAirlineCode",
    "carrier","carrierCode","airlineCode","airline"
]
FLIGHTNUM_KEYS = ["flightNumber","operatingFlightNumber","marketingFlightNumber"]
MILES_KEYS  = ["miles","awardMiles","priceMiles","lowestAwardMiles","loyaltyMiles","totalMiles"]

def _first_str(v):
    if v is None: return None
    if isinstance(v,(int,float)): return str(v)
    if isinstance(v,str): return v
    return None

def _pick_from_dict(d:dict):
    carrier=None
    for k in CARRIER_KEYS:
        if k in d:
            s=_first_str(d[k]); 
            if s: 
                s=s.strip().upper()
                if s=="JX" or "STARLUX" in s: carrier="JX"
                break
    miles=None
    for k in MILES_KEYS:
        if k in d:
            s=_first_str(d[k]); 
            if s: miles=s.replace(",",""); break
    flight=None
    for k in FLIGHTNUM_KEYS:
        if k in d:
            s=_first_str(d[k]); 
            if s:
                num=re.sub(r"\D","",s)
                if num: flight=f"JX{num}"; break
    cabin=None
    for k,v in d.items():
        s=_first_str(v); 
        if not s: continue
        u=s.upper()
        for c in CABIN_KEYS:
            if c in u: cabin=c; break
        if cabin: break
    return carrier, miles, flight, cabin

def walk_json(obj, date_str, out):
    try:
        if isinstance(obj, dict):
            carrier, miles, flight, cabin = _pick_from_dict(obj)
            if carrier=="JX":
                if cabin is None: cabin="UNKNOWN"
                if CABIN_FILTER=="ANY" or CABIN_FILTER in cabin:
                    out.append({"date":date_str,"origin":ORIGIN,"dest":DEST,
                                "flight":flight or "JX","miles":miles or "N/A","cabin":cabin})
            for v in obj.values(): walk_json(v,date_str,out)
        elif isinstance(obj, list):
            for v in obj: walk_json(v,date_str,out)
    except: pass

# ========= UI =========
CARD_SELECTOR = "div.flight-card, div.akam-flight-card, [data-testid*='flight']"

async def do_search_use_miles(page, origin, dest, date_str):
    await page.goto("https://www.alaskaair.com/", wait_until="load")
    await page.wait_for_load_state("networkidle")

    # one-way
    try:
        if TRIP_TYPE=="one_way":
            for lc in [
                page.get_by_role("radio", name=re.compile("one[- ]?way", re.I)),
                page.get_by_text(re.compile("^One[- ]?way$", re.I)).nth(0),
                page.locator("[aria-label*='One-way' i]")
            ]:
                if await lc.is_visible(timeout=1500): await lc.click(); break
    except: pass

    # use miles
    try:
        for lc in [
            page.get_by_label(re.compile("Use miles|award", re.I)),
            page.get_by_role("checkbox", name=re.compile("Use miles|award", re.I)),
            page.locator("input[type='checkbox'][name*='award' i]")
        ]:
            if await lc.is_visible(timeout=1500): await lc.click(); break
    except: pass

    # from/to
    async def fill_any(label_regex, value):
        for lc in [page.get_by_label(label_regex),
                   page.get_by_placeholder(label_regex),
                   page.get_by_role("textbox", name=label_regex)]:
            try:
                if await lc.is_visible(timeout=1500):
                    await lc.click(); await lc.fill(""); await lc.type(value, delay=40)
                    await page.keyboard.press("Enter"); return True
            except: pass
        return False

    await fill_any(re.compile("From", re.I), origin)
    await fill_any(re.compile("To", re.I), dest)

    # date
    try:
        d=mmddyyyy(date_str)
        for lc in [page.get_by_label(re.compile("Depart|Departure", re.I)),
                   page.get_by_placeholder(re.compile("MM/|mm/", re.I))]:
            if await lc.is_visible(timeout=1500):
                await lc.fill(""); await lc.type(d, delay=40); await page.keyboard.press("Enter"); break
    except: pass

    # submit
    try:
        for lc in [page.get_by_role("button", name=re.compile("Find flights|Search", re.I))]:
            if await lc.is_visible(timeout=1500): await lc.click(); break
    except: pass

    await page.wait_for_load_state("networkidle")
    try: await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except: pass

# ========= Run one day with robust capture =========
async def run_day_via_network(p, date_str):
    browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
    page    = await browser.new_page(locale="en-US", user_agent=UA)

    candidate_sent = 0
    any_saved = 0
    fallback_bucket = []   # (path, url, status, kind)

    async def on_response(resp):
        nonlocal candidate_sent, any_saved, fallback_bucket
        try:
            if resp.request.resource_type not in ("xhr","fetch"):
                return
            url = resp.url
            status = resp.status
            host = _sanitize(url.split("//")[-1].split("/")[0])
            path_tag = _sanitize("/".join(url.split("//")[-1].split("/")[1:]) or "root")
            tag = f"{host}_{path_tag}_{status}_{date_str}"

            # 先嘗試 JSON
            payload = None
            kind = "text"
            try:
                payload = await resp.json()
                kind = "json"
                pth = f"/tmp/resp_{tag}.json"
                with open(pth,"w",encoding="utf-8") as f: json.dump(payload,f,ensure_ascii=False,indent=2)
            except:
                try:
                    txt = await resp.text()
                except:
                    body = await resp.body()
                    txt = f"<<binary {len(body)} bytes>>"
                pth = f"/tmp/resp_{tag}.txt"
                with open(pth,"w",encoding="utf-8") as f: f.write(txt)

            any_saved += 1
            # 先丟到備援桶（最多存 10 筆最新的）
            fallback_bucket.append((pth, url, status, kind))
            if len(fallback_bucket) > 10:
                fallback_bucket.pop(0)

            # 判斷是不是「像航班 API」：URL 或 JSON 內容命中
            looks_api = _url_looks_like_api(url)
            has_fields = _json_has_flight_fields(payload) if (kind=="json" and payload is not None) else False

            if looks_api or has_fields:
                # 直接傳 TG（最多傳 6 個）
                if candidate_sent < 6 and TG_TOKEN and TG_CHAT_ID:
                    send_tg_file(pth, f"API {status} | {url}")
                    candidate_sent += 1
        except Exception as e:
            log(f"[on_response] {e}")

    page.on("response", on_response)

    try:
        await do_search_use_miles(page, ORIGIN, DEST, date_str)
        # 最多等 10 秒讓請求都回來
        await page.wait_for_timeout(10000)
    finally:
        await browser.close()

    # 備援：若一個候選都沒傳，從 fallback 挑 3 個（以 JSON 優先）傳出去
    if candidate_sent == 0 and TG_TOKEN and TG_CHAT_ID:
        # JSON 優先排序
        fallback_bucket.sort(key=lambda x: 0 if x[3]=="json" else 1)
        for i, (pth, url, status, kind) in enumerate(fallback_bucket[:3], start=1):
            send_tg_file(pth, f"Fallback {i} ({kind}) {status} | {url}")
        log(f"[FALLBACK] sent {min(3,len(fallback_bucket))} files (no candidate matched)")
    log(f"[INFO] {date_str} saved={any_saved} candidate_sent={candidate_sent}")
    return []

# ========= Orchestrator =========
async def run_once():
    send_tg_text("✅ JX monitor started (robust API capture)")
    async with async_playwright() as p:
        if DATE_START and DATE_END:
            cur = datetime.strptime(DATE_START,"%Y-%m-%d")
            end = datetime.strptime(DATE_END,"%Y-%m-%d")
            while cur<=end:
                ds = cur.strftime("%Y-%m-%d")
                log(f"[INFO] checking {ds}")
                await run_day_via_network(p, ds)
                cur += timedelta(days=1)
        else:
            ds = DATE or datetime.utcnow().strftime("%Y-%m-%d")
            await run_day_via_network(p, ds)

if __name__=="__main__":
    if RUN_ONCE: asyncio.run(run_once())
    else:
        while True:
            asyncio.run(run_once())
            time.sleep(INTERVAL)
