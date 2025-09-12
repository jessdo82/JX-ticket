import os, re, time, json, asyncio
from datetime import datetime, timedelta
import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ========= 環境變數 =========
ORIGIN = os.getenv("ALASKA_ORIGIN") or os.getenv("ORIGIN", "TPE")
DEST   = os.getenv("ALASKA_DEST")   or os.getenv("DEST", "NRT")
DATE = os.getenv("DATE")
DATE_START = os.getenv("ALASKA_START_DATE") or os.getenv("DATE_START")
DATE_END   = os.getenv("ALASKA_END_DATE")   or os.getenv("DATE_END")

TRIP_TYPE = (os.getenv("ALASKA_TRIP_TYPE") or "one_way").lower()
CABIN_FILTER = (os.getenv("ALASKA_CABIN") or "ANY").upper()  # BUSINESS/ECONOMY/PREMIUM/FIRST/MAIN/ANY

TG_TOKEN   = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
TG_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID") or "").strip()

HEADLESS = (os.getenv("HEADLESS","1")=="1")
RUN_ONCE = (os.getenv("RUN_ONCE","0")=="1")
INTERVAL = int(os.getenv("POLL_INTERVAL_SEC") or os.getenv("INTERVAL") or "1800")

DEBUG   = (os.getenv("DEBUG","1")=="1")   # 預設開
DEBUG_TG= (os.getenv("DEBUG_TG","0")=="1")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/123.0.0.0 Safari/537.36")

# ========= 小工具 =========
def log(msg:str): print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {msg}", flush=True)

def send_tg_text(text:str):
    if not (TG_TOKEN and TG_CHAT_ID): 
        log("[TEL] not configured"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id":TG_CHAT_ID,"text":text})
        log(f"[TEL] sendMessage status={r.status_code}")
    except Exception as e:
        log(f"[TEL] error {e}")

def send_tg_file(path:str, caption:str=""):
    if not (TG_TOKEN and TG_CHAT_ID): return
    try:
        with open(path,"rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                              data={"chat_id":TG_CHAT_ID,"caption":caption},
                              files={"document":f})
        log(f"[TEL] sendDocument {path} status={r.status_code}")
    except Exception as e:
        log(f"[TEL] file error {e}")

def mmddyyyy(iso:str)->str:
    return datetime.strptime(iso,"%Y-%m-%d").strftime("%m/%d/%Y")

def format_message(items):
    lines = ["✨ JX Award Seat Found (via Alaska) ✨"]
    for it in items:
        lines.append(f"• {it['date']} {it['origin']}→{it['dest']} {it.get('flight','JX')} — {it.get('miles','?')} miles — {it.get('cabin','')}")
    return "\n".join(lines)

# ========= 解析工具（等拿到 JSON 後就能對欄位） =========
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
    if isinstance(v, (int,float)): return str(v)
    if isinstance(v, str): return v
    return None

def _try_pick_from_dict(d:dict):
    carrier = None
    for k in CARRIER_KEYS:
        if k in d:
            s = _first_str(d[k])
            if s and s.strip():
                carrier = s.strip().upper()
                break
    if carrier and not (("STARLUX" in carrier) or (carrier == "JX")):
        if carrier.upper() != "JX":
            carrier = None

    miles = None
    for k in MILES_KEYS:
        if k in d:
            s = _first_str(d[k])
            if s:
                miles = s.replace(",", "")
                break

    flight = None
    for k in FLIGHTNUM_KEYS:
        if k in d:
            s = _first_str(d[k])
            if s:
                s = re.sub(r"\D", "", s)
                if s:
                    flight = f"JX{s}"
                    break

    cabin = None
    for k,v in d.items():
        s = _first_str(v)
        if not s: continue
        u = s.upper()
        for c in CABIN_KEYS:
            if c in u: cabin = c; break
        if cabin: break

    return carrier, miles, flight, cabin

def walk_json(obj, date_str:str, out:list):
    try:
        if isinstance(obj, dict):
            carrier, miles, flight, cabin = _try_pick_from_dict(obj)
            if carrier == "JX" or (carrier and "STARLUX" in carrier):
                if cabin is None: cabin = "UNKNOWN"
                if CABIN_FILTER == "ANY" or (cabin and CABIN_FILTER in cabin):
                    out.append({
                        "date": date_str, "origin": ORIGIN, "dest": DEST,
                        "flight": flight or "JX", "miles": miles or "N/A", "cabin": cabin
                    })
            for v in obj.values(): walk_json(v, date_str, out)
        elif isinstance(obj, list):
            for v in obj: walk_json(v, date_str, out)
        elif isinstance(obj, str):
            u = obj.upper()
            if ("STARLUX" in u) or (" JX" in u) or u.startswith("JX"):
                cabin = None
                for c in CABIN_KEYS:
                    if c in u: cabin = c; break
                if CABIN_FILTER == "ANY" or (cabin and CABIN_FILTER in (cabin or "")):
                    out.append({"date":date_str,"origin":ORIGIN,"dest":DEST,"flight":"JX","miles":None,"cabin":cabin or "UNKNOWN"})
    except Exception:
        pass

# ========= UI 操作 =========
CARD_SELECTOR = "div.flight-card, div.akam-flight-card, [data-testid*='flight'], [class*='flight']"

async def do_search_use_miles(page, origin:str, dest:str, date_str:str):
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
                try:
                    if await lc.is_visible(timeout=1200):
                        await lc.click(); break
                except Exception: pass
    except Exception as e: log(f"[WARN] trip type: {e}")

    # use miles
    try:
        toggled=False
        for lc in [
            page.get_by_label(re.compile("Use miles|award", re.I)),
            page.get_by_role("checkbox", name=re.compile("Use miles|award", re.I)),
            page.locator("input[type='checkbox'][name*='award' i]")
        ]:
            try:
                if await lc.is_visible(timeout=1200):
                    if hasattr(lc,"is_checked"):
                        if not await lc.is_checked(): await lc.click()
                    else:
                        await lc.click()
                    toggled=True; break
            except Exception: pass
        if not toggled:
            try: await page.get_by_text(re.compile("Use miles|Award", re.I)).first.click(timeout=1200)
            except Exception: pass
    except Exception as e: log(f"[WARN] award: {e}")

    # from/to
    async def fill_any(label_regex, value):
        for lc in [page.get_by_label(label_regex),
                   page.get_by_placeholder(label_regex),
                   page.get_by_role("textbox", name=label_regex)]:
            try:
                if await lc.is_visible(timeout=1200):
                    await lc.click(); await lc.fill(""); await lc.type(value, delay=40)
                    await page.wait_for_timeout(600); await page.keyboard.press("Enter"); return True
            except Exception: pass
        return False
    ok1 = await fill_any(re.compile("From|From where|from airport", re.I), origin)
    ok2 = await fill_any(re.compile("To|To where|to airport", re.I), dest)
    if not (ok1 and ok2): log("[WARN] from/to fallback attempted")

    # date
    date_mmdd = mmddyyyy(date_str)
    set_date=False
    for lc in [page.get_by_label(re.compile("Depart|Departure", re.I)),
               page.get_by_placeholder(re.compile("MM/|mm/", re.I)),
               page.locator("input[type='text'][name*='depart' i]"),
               page.locator("input[type='text'][aria-label*='Depart' i]")]:
        try:
            if await lc.is_visible(timeout=1200):
                await lc.click(); await lc.fill(""); await lc.type(date_mmdd, delay=40)
                await page.keyboard.press("Enter"); set_date=True; break
        except Exception: pass
    if not set_date:
        try: await page.keyboard.press("Tab"); await page.keyboard.type(date_mmdd, delay=40); await page.keyboard.press("Enter")
        except Exception: pass

    # submit
    submitted=False
    for lc in [page.get_by_role("button", name=re.compile("Find flights|Search|Continue", re.I)),
               page.get_by_text(re.compile("^Find flights$|^Search$", re.I)),
               page.locator("button[type='submit']")]:
        try:
            if await lc.is_visible(timeout=1500): await lc.click(); submitted=True; break
        except Exception: pass
    if not submitted:
        try: await page.keyboard.press("Enter")
        except Exception: pass

    await page.wait_for_load_state("networkidle")
    try: await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except PwTimeout: pass

# ========= 跑一天（含：強制 dump/傳 XHR 回應） =========
def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:120]

async def run_day_via_network(p, date_str:str):
    browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
    page    = await browser.new_page(locale="en-US", user_agent=UA)

    captured = []          # (kind, url, local_path)
    MAX_SAVE = 20          # 最多保存 20 個回應
    MAX_TG   = 6           # 最多傳 6 個到 Telegram

    async def on_response(resp):
        """無條件保存所有 XHR/Fetch 回應（JSON 或文字），並把前幾個直接送到 TG。"""
        try:
            rt = resp.request.resource_type
            if rt not in ("xhr", "fetch"): return

            url = resp.url
            status = resp.status
            host_part = _sanitize(url.split("//")[-1].split("/")[0])
            path_part = _sanitize("/".join(url.split("//")[-1].split("/")[1:]) or "root")
            tag = f"{host_part}_{path_part}_{status}_{date_str}"

            # 優先存 JSON；失敗就存 text
            try:
                data = await resp.json()
                pth = f"/tmp/resp_{tag}.json"
                with open(pth, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                saved = ("json", url, pth, data)
            except Exception:
                try:
                    txt = await resp.text()
                except Exception:
                    body = await resp.body()
                    txt = f"<<binary {len(body)} bytes>>"
                pth = f"/tmp/resp_{tag}.txt"
                with open(pth, "w", encoding="utf-8") as f:
                    f.write(txt)
                saved = ("text", url, pth, txt)

            captured.append(saved)
            log(f"[DUMP] wrote {saved[2]}")

            # 直接送前 MAX_TG 個到 TG（不受 DEBUG_TG 限制）
            if len([c for c in captured if os.path.exists(c[2])]) <= MAX_TG and TG_TOKEN and TG_CHAT_ID:
                send_tg_file(saved[2], f"XHR {status} | {url}")

            # 控制數量
            if len(captured) > MAX_SAVE:
                captured.pop(0)
        except Exception as e:
            log(f"[DUMP] on_response error: {e}")

    page.on("response", on_response)

    try:
        await do_search_use_miles(page, ORIGIN, DEST, date_str)
        await page.wait_for_timeout(5000)

        # 從已抓到的 JSON 裡找 JX/STARLUX
        results=[]
        for kind, url, pth, payload in captured:
            if kind == "json":
                try: walk_json(payload, date_str, results)
                except Exception: pass

        # 去重
        uniq={}
        for r in results:
            key=(r["date"], r.get("flight"), r.get("cabin"), r.get("miles"))
            uniq[key]=r
        results=list(uniq.values())

        log(f"[INFO] {date_str} network-captured {len(captured)} items; candidates={len(results)}")
        return results
    finally:
        await browser.close()

# ========= Orchestrator =========
async def run_once():
    send_tg_text("✅ JX award monitor started (dump mode)")
    log("=== JX award monitor started (dump mode) ===")
    log(f"Route: {ORIGIN}->{DEST}  Trip={TRIP_TYPE}  Date={DATE or (DATE_START+'~'+DATE_END)}")
    log(f"Cabin={CABIN_FILTER} Headless={HEADLESS} Interval={INTERVAL}s Debug={DEBUG}/{DEBUG_TG}")

    items=[]
    async with async_playwright() as p:
        if DATE_START and DATE_END:
            cur = datetime.strptime(DATE_START,"%Y-%m-%d")
            end = datetime.strptime(DATE_END,"%Y-%m-%d")
            while cur<=end:
                ds = cur.strftime("%Y-%m-%d")
                log(f"[INFO] checking {ds} ...")
                try:
                    items.extend(await run_day_via_network(p, ds))
                except Exception as e:
                    log(f"[ERR] {ds} failed: {e}")
                cur += timedelta(days=1)
        else:
            ds = DATE or datetime.utcnow().strftime("%Y-%m-%d")
            log(f"[INFO] checking {ds} ...")
            items.extend(await run_day_via_network(p, ds))

    if items:
        send_tg_text(format_message(items))
        log(f"[FOUND] {len(items)} item(s) sent")
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
