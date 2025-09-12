import os, re, time, json, asyncio
from datetime import datetime, timedelta
import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ========= 環境變數 =========
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

# ========= 小工具 =========
def log(msg:str): print(f"[{datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {msg}", flush=True)

def send_tg_text(text:str):
    if not (TG_TOKEN and TG_CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id":TG_CHAT_ID,"text":text})
    except: pass

def send_tg_file(path:str, caption:str=""):
    if not (TG_TOKEN and TG_CHAT_ID): return
    try:
        with open(path,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id":TG_CHAT_ID,"caption":caption},
                          files={"document":f})
    except: pass

def mmddyyyy(iso:str)->str:
    return datetime.strptime(iso,"%Y-%m-%d").strftime("%m/%d/%Y")

def format_message(items):
    lines = ["✨ JX Award Seat Found (via Alaska) ✨"]
    for it in items:
        lines.append(f"• {it['date']} {it['origin']}→{it['dest']} {it.get('flight','JX')} — {it.get('miles','?')} miles — {it.get('cabin','')}")
    return "\n".join(lines)

# ========= 過濾條件 =========
KEY_URL_HINTS = ["award","awardshopping","availability","shop","search","calendar","price"]
ALASKA_API_HOST_HINTS = ["as.api.alaskaair.com","api.alaskaair.com","alaskaair.com"]

def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:120]

def _looks_like_award_api(url: str) -> bool:
    u = url.lower()
    return (any(h in u for h in ALASKA_API_HOST_HINTS)
            and any(k in u for k in KEY_URL_HINTS))

# ========= UI 操作 =========
CARD_SELECTOR = "div.flight-card, div.akam-flight-card, [data-testid*='flight']"

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
                if await lc.is_visible(timeout=1500):
                    await lc.click(); break
    except: pass

    # use miles
    try:
        for lc in [
            page.get_by_label(re.compile("Use miles|award", re.I)),
            page.get_by_role("checkbox", name=re.compile("Use miles|award", re.I)),
            page.locator("input[type='checkbox'][name*='award' i]")
        ]:
            if await lc.is_visible(timeout=1500):
                await lc.click(); break
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
        d = mmddyyyy(date_str)
        for lc in [page.get_by_label(re.compile("Depart|Departure", re.I)),
                   page.get_by_placeholder(re.compile("MM/|mm/", re.I))]:
            if await lc.is_visible(timeout=1500):
                await lc.fill(""); await lc.type(d, delay=40); await page.keyboard.press("Enter"); break
    except: pass

    # submit
    try:
        for lc in [page.get_by_role("button", name=re.compile("Find flights|Search", re.I))]:
            if await lc.is_visible(timeout=1500):
                await lc.click(); break
    except: pass

    await page.wait_for_load_state("networkidle")
    try: await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except: pass

# ========= 跑一天 =========
async def run_day_via_network(p, date_str:str):
    browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
    page    = await browser.new_page(locale="en-US", user_agent=UA)

    captured = []
    MAX_TG = 6

    async def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr","fetch"): return
            url = resp.url
            if not _looks_like_award_api(url): return
            status = resp.status
            host = _sanitize(url.split("//")[-1].split("/")[0])
            tag = f"{host}_{status}_{date_str}"
            try:
                data = await resp.json()
                path = f"/tmp/resp_{tag}.json"
                with open(path,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
                captured.append(path)
                if len(captured) <= MAX_TG: send_tg_file(path,f"API {status} {url}")
            except:
                txt = await resp.text()
                path = f"/tmp/resp_{tag}.txt"
                with open(path,"w",encoding="utf-8") as f: f.write(txt)
                captured.append(path)
                if len(captured) <= MAX_TG: send_tg_file(path,f"API {status} {url}")
        except Exception as e:
            log(f"[on_response] {e}")

    page.on("response", on_response)

    try:
        await do_search_use_miles(page, ORIGIN, DEST, date_str)
        await page.wait_for_timeout(6000)
    finally:
        await browser.close()

    log(f"[INFO] {date_str} captured {len(captured)} api files")
    return []

# ========= Orchestrator =========
async def run_once():
    send_tg_text("✅ JX monitor started (API dump mode)")
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
