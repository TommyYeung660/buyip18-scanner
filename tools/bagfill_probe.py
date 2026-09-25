#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入袋可及性 A/B：**UA × 出口**（唯讀：只載商品頁＋按加入購物袋，DRY_RUN、不結帳不下單）。

要解的矛盾：9/25 runner A/B 說「Safari UA 2/2、Chrome UA 0/6」（同一代理同一輪），
但生產換成 Safari UA 之後整夜仍有 7 次 `fetch=http541`。差異嫌疑＝**出口**：
那次 A/B 走免費池（209.9.200.33），生產 keeper 走 per-slot IPRoyal 專屬 IP。
本腳本把兩個維度交叉：{BUY 免費池, slot0 專屬, slot1 專屬} × {safari, chrome}。

出口由 mihomo listener 決定：7891=BUY，7892+i=slot i 專屬（見 bagfill-probe.yml）。
"""
import json
import os
import sys
import time
from pathlib import Path

CO = os.environ["CO_DIR"]
sys.path.insert(0, CO)
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("BUY_COUNT", "2")
os.environ.setdefault("ADD_RETRY_ROUNDS", "1")
os.environ.setdefault("ADD_RETRY_WAIT_S", "0")
os.environ.setdefault("SKU", "MJXQ4ZA/A")

import checkout as C  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

UA = {
    "safari": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"),
    "chrome": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
}
CAND = ["6.9", "布根地紅色", "256GB"]      # MJXQ4ZA/A（現行 4 SKU 之一）
OUT = Path(os.environ.get("PROBE_OUT", str(Path(__file__).resolve().parent / "out")))
OUT.mkdir(parents=True, exist_ok=True)
ARM = os.environ.get("ARM", "?")
UA_MODE = os.environ.get("UA_ARM", "safari")
N = int(os.environ.get("N", "6"))
RES = []


def rec(**kw):
    kw["arm"] = ARM; kw["ua"] = UA_MODE; kw["ts"] = time.strftime("%H:%M:%S")
    RES.append(kw)
    with open(OUT / "bagfill.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def main():
    with sync_playwright() as pw:
        args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        pp = (os.environ.get("PROXY_PORT") or "").strip()
        if pp:
            args.append("--proxy-server=http://127.0.0.1:%s" % pp)
        br = pw.chromium.launch(headless=True, args=args)
        ctx = br.new_context(viewport={"width": 1280, "height": 900}, locale="zh-HK",
                             user_agent=UA[UA_MODE])
        if UA_MODE == "safari":
            ctx.add_init_script("""
              Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
              Object.defineProperty(navigator, 'languages', {get: () => ['zh-HK','zh','en']});
              Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
              Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
              window.chrome = window.chrome || {runtime: {}};
            """)
        else:
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        oks = 0
        for i in range(1, N + 1):
            try:
                r0 = page.goto(C.PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
                st0 = r0.status if r0 else -1
            except Exception as e:
                st0 = -1; rec(i=i, phase="load", status=-1, exc=repr(e)[:50]); time.sleep(6); continue
            ok = False
            try:
                ok = bool(C.add_to_bag(page, ctx, CAND))
            except Exception as e:
                rec(i=i, phase="add", status=st0, exc=repr(e)[:60])
            oks += int(ok)
            rec(i=i, phase="add", status=st0, ok=ok, url=(page.url or "")[:70])
            time.sleep(6)
        rec(i=0, phase="summary", ok=oks, n=N)
        br.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback; traceback.print_exc(); rec(i=0, phase="fatal", exc=repr(e)[:120])
