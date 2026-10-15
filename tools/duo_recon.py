#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Duo 開賣前後的**偵察／臨場應變**工具。

為什麼需要（用戶 10/15 的第 4 點）：開賣前後 Apple 隨時可能把商品頁換成「請耐心等候／很快開賣」
那類等候頁，URL 與 DOM 也可能臨時改變。當晚 19:00 起我們需要一個**數十秒內**就能回答
「現在的頁面是什麼、還能不能買、DOM 有沒有變、SKU 有沒有變」的工具，而不是靠猜。

它做的事（每一輪）：
  1. 依 URL 候選梯逐一載入（預設三條：hk-zh／hk／帶 select 的變體）
  2. 判類：`configurator`（配置器活著）／`wait`（等候頁）／`blocked`（541/sorry）／`product-only`／`unknown`
  3. 抓：最終 URL、HTTP 狀態、頁面文字樣本（等候頁的真實文案是什麼，一次看清）、
     可見按鈕（id/data-autom/文字）、`partNumber`（SKU 清單有沒有變）、
     `dimensionColor`/`dimensionCapacity` 選項值、`add-to-cart` 是否可用
  4. 量流量（各資源類型 content-length 加總的上界）——買住宅流量（按 GB）時要用這個估預算
  5. 截圖 + JSON 推**私有** repo；公開 log 只印摘要（沒有 PII）
  6. `--watch-min` 模式：持續監看，只在**判類改變**時推檔並在 log 標記，用來等 20:00 那一瞬

用法：
    URLS="https://www.apple.com/hk-zh/shop/buy-iphone/iphone-duo" PROXY_IDX=0 \
        python3 tools/duo_recon.py --watch-min 15 --interval-s 30 --label t1945
"""
import argparse
import datetime
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_URLS = [
    "https://www.apple.com/hk-zh/shop/buy-iphone/iphone-duo",
    "https://www.apple.com/hk/shop/buy-iphone/iphone-duo",
    "https://www.apple.com/hk-zh/shop/buy-iphone/iphone-duo/select",
]
# 等候頁常見文案（中英；寫成清單是因為我們**還沒見過**當晚那一頁的真實字樣，
# 這支工具存在的意義之一就是把它第一次照下來）
WAIT_WORDS = ["請耐心", "很快", "即將", "準備", "稍後", "敬請", "暫未", "開賣", "預告",
              "請稍候", "回到這裡", "再回來", "coming soon", "check back", "we'll be back",
              "stay tuned", "available soon", "be right back", "notify me", "通知我"]
LIVE_WORDS = ["add-to-cart", "加入購物袋", "Add to Bag", "選購", "立即購買"]

BTN_JS = """() => Array.from(document.querySelectorAll(
    'button, a[role=button], [role=button], input[type=submit]'))
    .slice(0, 40)
    .map(e => [(e.id || ''), (e.getAttribute('data-autom') || ''),
               (e.innerText || e.value || '').trim().slice(0, 22),
               e.disabled ? 'disabled' : (e.offsetParent ? 'visible' : 'hidden')])
    .filter(r => r[2] || r[0] || r[1])"""


def proxy_cfg(idx_txt):
    arr = []
    try:
        arr = json.loads(os.environ.get("SLOT_PROXIES_JSON") or "[]")
    except Exception:
        pass
    if idx_txt == "" or not str(idx_txt).strip().isdigit():
        return None, ""
    i = int(idx_txt)
    if i >= len(arr) or not isinstance(arr[i], dict):
        return None, ""
    row = arr[i]
    if not row.get("host"):
        return None, ""
    srv = "%s:%s" % (row.get("host"), row.get("port"))
    px = {"server": "http://" + srv}
    if row.get("user"):
        px["username"] = str(row["user"])
        px["password"] = str(row.get("pass") or "")
    return px, "slot%d" % i


def classify(status, final_url, html, text):
    if status in (403, 541) or "/shop/sorry" in (final_url or ""):
        return "blocked"
    if "dimensionColor" in html or '"partNumber"' in html:
        return "configurator"
    hits = [w for w in WAIT_WORDS if w.lower() in text.lower()]
    if hits:
        return "wait:" + hits[0]
    if "add-to-cart" in html:
        return "product-only"
    return "unknown"


def one_cycle(pw, urls, px, wait_s, save_html):
    from playwright.sync_api import sync_playwright  # noqa: F401  (呼叫端已 import)
    out = {"ts": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
                      .strftime("%Y-%m-%dT%H:%M:%S+08:00"),
           "cycles": []}
    for u in urls:
        rec = {"url": u}
        browser = pw.chromium.launch(headless=True,
                                     args=["--disable-blink-features=AutomationControlled",
                                           "--no-sandbox"],
                                     proxy=px)
        ctx = browser.new_context(locale="zh-HK", timezone_id="Asia/Hong_Kong",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        bytes_by_type = {}
        html = ""
        try:
            resp = page.goto(u, wait_until="domcontentloaded", timeout=60000)
            rec["status"] = resp.status if resp else None

            def on_resp(r):
                try:
                    n = int((r.headers or {}).get("content-length") or 0)
                except Exception:
                    n = 0
                t = r.request.resource_type
                bytes_by_type[t] = bytes_by_type.get(t, 0) + n
            page.on("response", on_resp)
            page.wait_for_timeout(int(wait_s * 1000))
            # 再重整一次：等候頁 → 開賣頁的切換常要重載才看得到
            rec["final_url"] = page.url
            rec["title"] = (page.title() or "")[:80]
            html = page.content() or ""
            try:
                text = page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                text = ""
            rec["text_sample"] = " ".join((text or "").split())[:400]
            rec["html_len"] = len(html)
            rec["verdict"] = classify(rec["status"], rec["final_url"], html, text)
            rec["wait_words"] = [w for w in WAIT_WORDS if w.lower() in (text or "").lower()][:6]
            rec["live_words"] = [w for w in LIVE_WORDS if w.lower() in (html or "").lower()][:6]
            rec["part_numbers"] = sorted(set(__import__("re").findall(r'"partNumber":"([A-Z0-9]{5,8}ZA/A)"', html)))[:14]
            rec["colors"] = sorted(set(__import__("re").findall(r'dimensionColor=([a-z]+)', html)))[:8]
            rec["caps"] = sorted(set(__import__("re").findall(r'dimensionCapacity=([0-9a-z]+)', html)))[:8]
            rec["add_to_cart"] = ("disabled" if 'aria-disabled="true"' in html and "add-to-cart" in html
                                  else ("present" if "add-to-cart" in html else "absent"))
            try:
                rec["buttons"] = page.evaluate(BTN_JS)
            except Exception:
                rec["buttons"] = []
            rec["bytes_by_type"] = bytes_by_type
            rec["bytes_total_kb"] = round(sum(bytes_by_type.values()) / 1024.0, 1)
            try:
                shot = page.screenshot(full_page=False)
            except Exception:
                shot = None
        except Exception as e:
            rec["verdict"] = "error"
            rec["error"] = repr(e)[:160]
            shot = None
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
        rec["_shot"] = shot
        rec["_html"] = html if save_html else ""
        out["cycles"].append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-min", type=float, default=0.0)
    ap.add_argument("--interval-s", type=float, default=30.0)
    ap.add_argument("--wait-s", type=float, default=6.0)
    ap.add_argument("--label", default="recon")
    ap.add_argument("--save-html", action="store_true")
    a = ap.parse_args()
    urls = [u.strip() for u in (os.environ.get("URLS", "") or "").split(",") if u.strip()] \
        or DEFAULT_URLS
    px, px_name = proxy_cfg(os.environ.get("PROXY_IDX", ""))
    from duo_job import push_private_bytes, push_private_json   # 復用私有 repo 推送（D6）
    from playwright.sync_api import sync_playwright
    tag = "%s-%s" % (a.label, datetime.datetime.now().strftime("%H%M%S"))
    print("=== 偵察（%d 條 URL｜出口=%s｜等候頁/開賣頁判類）===" % (len(urls), px_name or "直連"))
    print("    URL: %s" % " , ".join(urls))
    t_end = time.time() + a.watch_min * 60
    last = None
    n = 0
    while True:
        n += 1
        with sync_playwright() as pw:
            res = one_cycle(pw, urls, px, a.wait_s, a.save_html)
        for rec in res["cycles"]:
            line = ("[%s] #%d %s｜http=%s｜len=%sKB｜verdict=%s｜SKU=%d｜色=%s｜袋鈕=%s"
                    % (res["ts"][11:19], n, rec["url"].split("apple.com")[-1][:52],
                       rec.get("status"), rec.get("bytes_total_kb"), rec.get("verdict"),
                       len(rec.get("part_numbers") or []), ",".join(rec.get("colors") or [])[:24],
                       rec.get("add_to_cart")))
            print(line, flush=True)
            if rec.get("wait_words"):
                print("      等候字樣：%s" % ",".join(rec["wait_words"]))
            if rec.get("text_sample"):
                print("      文字：%s" % rec["text_sample"][:180])
            if rec.get("error"):
                print("      錯誤：%s" % rec["error"])
            shot = rec.pop("_shot", None)
            html = rec.pop("_html", "")
            verdict = rec.get("verdict", "")
            changed = (verdict != last)
            if changed or n == 1:
                last = verdict
                nm = "status/recon/%s-u%d.json" % (tag, res["cycles"].index(rec))
                push_private_json(nm, rec, "recon %s verdict=%s" % (a.label, verdict))
                ui = res["cycles"].index(rec)
                if shot:
                    push_private_bytes("status/recon/%s-u%d.png" % (tag, ui), shot,
                                       "recon shot verdict=%s" % verdict)
                if html:
                    push_private_bytes("status/recon/%s-u%d.html.gz" % (tag, ui),
                                       gzip.compress(html.encode()),
                                       "recon html verdict=%s" % verdict)
        # 滾動狀態檔（當晚我只要讀這一個檔就知道現在什麼情況）
        push_private_json("status/recon-latest.json",
                          {"ts": res["ts"], "label": a.label, "cycle": n, "proxy": px_name,
                           "results": [{k: v for k, v in r.items() if not k.startswith("_")}
                                       for r in res["cycles"]]},
                          "recon latest (%s)" % a.label)
        if time.time() >= t_end:
            break
        time.sleep(max(2.0, a.interval_s))
    print("=== 偵察結束（%d 輪）===" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
