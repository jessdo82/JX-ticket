import os, re, time, json, asyncio
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ----------------- ENV -----------------
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

DEBUG   = (os.getenv("DEBUG","0")=="1")
DEBUG_TG= (os.getenv("DEBUG_TG","0")=="1")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/123.0.0.0 Safari/537.36")

# ----------------- utils -----------------
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

# ----------------- JSON extract helpers -----------------
CABIN_KEYS = ["BUSINESS","FIRST","PREMIUM CLASS","PREMIUM","ECONOMY","MAIN"]
CARRIER_KEYS = [
    "operatingCarrierCode","operatingCarrier","operatingAirline","operatingAirlineCode",
    "marketingCarrierCode","marketingCarrier","marketingAirline","marketingAirlineCode",
    "carrier","carrierCode","airlineCode","airline"
]
FLIGHTNUM_KEYS = ["flightNumber","operatingFlightNumber","marketingFlightNumber"]
MILES_KEYS  = ["miles","awardMiles","priceMiles","lowestAwardMiles","loyaltyMiles","totalMiles"]

def first_str(v):
    if v is None: return None
    if isinstance(v, (int,float)): return str(v)
    if isinstance(v, str): return v
    return None

def try_extract_from_dict(d:dict):
    """盡量從單一 dict 取出 carrier/miles/flight/cabin。"""
    carrier = None
    for k in CARRIER_KEYS:
        if k in d:
            s = first_str(d[k])
            if s and s.strip():
                carrier = s.strip().upper()
                break
    # 允許 STARLUX 或 JX 任一
    if carrier and not (("STARLUX" in carrier) or (carrier == "JX")):
        # 有些 API 會給 IATA 二碼小寫
        if carrier.upper() != "JX":
            carrier = None

    miles = None
    for k in MILES_KEYS:
        if k in d:
            s = first_str(d[k])
            if s:
                miles = s.replace(",", "")
                break

    flight = None
    for k in FLIGHTNUM_KEYS:
        if k in d:
            s = first_str(d[k])
            if s:
                s = re.sub(r"\D", "", s)
                if s:
                    flight = f"JX{s}"  # 我們只關注 JX
                    break

    cabin = None
    # 艙等可能在多個欄位（name/code/description）
    for k,v in d.items():
        s = first_str(v)
        if not s: continue
        u = s.upper()
        for c in CABIN_KEYS:
            if c in u:
                cabin = c
                break
        if cabin: break

    return carrier, miles, flight, cabin

def walk_json(obj, date_str:str, out:list):
    """深度尋找有 JX/STARLUX 航段的節點，組合成結果。"""
    try:
        if isinstance(obj, dict):
            carrier, miles, flight, cabin = try_extract_from_dict(obj)
            if carrier == "JX" or (carrier and "STARLUX" in carrier):
                # 艙等過濾
                if cabin is None: cabin = "UNKNOWN"
                if CABIN_FILTER != "ANY" and CABIN_FILTER not in cabin:
                    pass
                else:
                    out.append({
                        "date": date_str, "origin": ORIGIN, "dest": DEST,
                        "flight": flight or "JX", "miles": miles or "N/A", "cabin": cabin
                    })
            # 繼續下探
            for v in obj.values(): walk_json(v, date_str, out)
        elif isinstance(obj, list):
            for v in obj: walk_json(v, date_str, out)
        elif isinstance(obj, str):
            u = obj.upper()
            if ("STARLUX" in u) or (" JX" in u) or u.startswith("JX"):
                # 從自由文字兜一筆候選
                cabin = None
                for c in CABIN_KEYS:
                    if c in u: cabin = c; break
                if CABIN_FILTER != "ANY" and (not cabin or CABIN_FILTER not in u):
                    return
                out.append({
                    "date": date_str, "origin": ORIGIN, "dest": DEST,
                    "flight": "JX", "miles": None, "cabin": cabin or "UNKNOWN"
                })
    except Exception:
        pass

# ----------------- UI steps + network capture -----------------
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
    if not (ok1 and ok2):
        log("[WARN] from/to fallback attempted")

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

    # 等待請求
    await page.wait_for_load_state("networkidle")
    try: await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except PwTimeout: pass

async def run_day_via_network(p, date_str:str):
    """操作 UI + 監聽 JSON，並把回應檔案存/傳；從欄位直接找 JX/STARLUX。"""
    browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
    page    = await browser.new_page(locale="en-US", user_agent=UA)

    captured = []  # (kind, url, payload)
    idx = 0

    async def on_response(resp):
        nonlocal idx
        try:
            url = resp.url
            rt  = resp.request.resource_type
            if rt not in ("xhr","fetch"): return
            if "alaskaair.com" not in url: return
            if not any(k in url.lower() for k in ["shop","search","offer","flight","award","availability","price","calendar"]):
                return

            ctype = resp.headers.get("content-type","")
            if "application/json" in ctype:
                data = await resp.json()
                captured.append(("json", url, data))
                # 存檔（前幾筆傳 TG）
                if DEBUG:
                    idx += 1
                    pth = f"/tmp/resp_{date_str}_{idx}.json"
                    with open(pth,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
                    log(f"[DEBUG] saved {pth}")
                    if DEBUG_TG and idx <= 5: send_tg_file(pth, f"resp {idx} {date_str}")
            else:
                text = await resp.text()
                # 嘗試就算 content-type 不是 json 也 parse 一下
                payload = None
                try:
                    payload = json.loads(text)
                    captured.append(("json", url, payload))
                    if DEBUG:
                        idx += 1
                        pth = f"/tmp/resp_{date_str}_{idx}.json"
                        with open(pth,"w",encoding="utf-8") as f: json.dump(payload,f,ensure_ascii=False,indent=2)
                        log(f"[DEBUG] saved {pth}")
                        if DEBUG_TG and idx <= 5: send_tg_file(pth, f"resp {idx} {date_str}")
                except Exception:
                    captured.append(("text", url, text))
                    if DEBUG:
                        idx += 1
                        pth = f"/tmp/resp_{date_str}_{idx}.txt"
                        with open(pth,"w",encoding="utf-8") as f: f.write(text)
                        log(f"[DEBUG] saved {pth}")
                        if DEBUG_TG and idx <= 3: send_tg_file(pth, f"resp {idx} {date_str}")
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await do_search_use_miles(page, ORIGIN, DEST, date_str)
        await page.wait_for_timeout(5000)

        # Debug：頁面也存一下
        if DEBUG:
            html_path = f"/tmp/page_{ORIGIN}-{DEST}_{date_str}.html"
            with open(html_path,"w",encoding="utf-8") as f: f.write(await page.content())
            log(f"[DEBUG] saved HTML -> {html_path}")
            if DEBUG_TG: send_tg_file(html_path, f"{ORIGIN}->{DEST} {date_str} HTML")

        # 從 captured 的 JSON/Text 萃取 JX/STARLUX
        results=[]
        for kind, url, payload in captured:
            if kind=="json":
                walk_json(payload, date_str, results)
            else:
                u = payload.upper()
                if ("STARLUX" in u) or (" JX" in u) or u.startswith("JX"):
                    results.append({
                        "date": date_str, "origin": ORIGIN, "dest": DEST,
                        "flight": "JX", "miles": None, "cabin": "UNKNOWN"
                    })

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

# ----------------- Orchestrator -----------------
async def run_once():
    send_tg_text("✅ JX award monitor started (enhanced JSON mode)")
    log("=== JX award monitor started (enhanced JSON mode) ===")
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
