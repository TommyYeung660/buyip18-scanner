#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""導航鏈失效回歸：用**生產真實的配對**重測「導航是否清零節流閘」。

背景：9/25 三次真命中全部 nav=2、nav_stk=False，鏈仍 17.4 秒（選店快、聯絡人與
billing 都被墊 ~7.6-9.2 秒）⇒ 生產上導航沒有清閘。但 9/22 在同型主機
（secure7.../apw/checkout）量到 goto 後 228ms（有清），9/23 在 secure11 同操作
卻 6685ms（沒清）。同一實驗隔天相反 ⇒ 必須在今天就地重測。

本腳本：建立真實訪客結帳會話（真的入袋 → 結帳 → 訪客），先在會話上跑一次
真實的「選店 + 取貨聯絡人」把狀態推進，之後所有實驗臂都送**同一發已變成
no-op 的取貨聯絡人 POST**（生產同一字串）——這樣各臂的差異只剩「導航」，
不會混到步驟本身的思考時間。

臂（每臂先睡 _COOL 秒讓閘自然過期，再動作、再量）：
  A 對照。不導航、睡 _COOL → 參考值（無閘延遲）
  B 同頁連發（睡 _GAP）→ 預期 ~10s（證明閘存在）
  C goto 當前結帳頁（同 URL 重載）→ ?
  D goto ?_s=Shipping-init（生產在「取貨聯絡人」前用的目標）→ ?
  E goto ?_s=Billing-init （生產在 billing 前用的目標）→ ?
  F goto www 基底 /shop/checkout → ?（跨 origin）
每臂記錄：stk（前/後）、導航耗時、導航後實際 URL（抓 302 落點）、POST ms、
status、head.status。3 輪、每輪臂序輪轉以抵銷漂移。

全程 DRY_RUN、不下單、不填卡、不碰 billing。
"""
import json
import os
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("PROBE_OUT", str(HERE / "out")))
OUT.mkdir(exist_ok=True)

CO = os.environ.get("CO_DIR",
                 "/Users/admin/Documents/projects/BuyIphone18/repos/buyip18-checkout")
sys.path.insert(0, CO)
os.environ["DRY_RUN"] = "true"
os.environ["KEEPER"] = "1"
os.environ["BUY_COUNT"] = "2"
os.environ["ADD_RETRY_ROUNDS"] = "1"
os.environ["ADD_RETRY_WAIT_S"] = "0"
os.environ["SKU"] = "MJXQ4ZA/A"
os.environ["KEEPER_NAV_CHAIN"] = "1"
os.environ["KEEPER_NAV_ANY"] = "1"

import checkout as C  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

UA_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15")
UA_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
COOL = 12          # 秒；讓節流閘自然過期（>10s）
GAP = 1            # 秒；刻意踩閘
CARDS = [("MJXQ4ZA/A", ["6.9", "布根地紅色", "256GB"]),
         ("MJXN4ZA/A", ["6.9", "黑色", "256GB"])]
STORE = "R485"

LOG = []
RES = []


def log(m):
    s = "%s %s" % (time.strftime("%H:%M:%S"), m)
    print(s, flush=True)
    LOG.append(s)


def rec(**kw):
    kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    RES.append(kw)
    with open(OUT / "nav_pair_real.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def stk(page):
    try:
        return C._keeper_stk(page) or ""
    except Exception as e:
        return "ERR:" + repr(e)[:20]


def post(page, qs, body=""):
    t0 = time.time()
    try:
        r = C._keeper_cx(page, qs, body)
    except Exception as e:
        return {"ms": int((time.time() - t0) * 1000), "status": -2, "err": repr(e)[:60]}
    ms = int((time.time() - t0) * 1000)
    head = ((r.get("body") or {}).get("head") or {})
    return {"ms": ms, "status": r.get("status"),
            "head_status": head.get("status"),
            "next": (((head.get("data") or {}).get("url") or "")[:70])}


def nav(page, url):
    t0 = time.time()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return int((time.time() - t0) * 1000), "ERR:" + repr(e)[:40]
    return int((time.time() - t0) * 1000), page.url


def main():
    with sync_playwright() as pw:
        # runner 上走 HK 代理（家網封高位端口、且本機出口已被標記 541）
        args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        _pp = os.environ.get("PROXY_PORT", "").strip()
        if _pp:
            args.append("--proxy-server=http://127.0.0.1:%s" % _pp)
        log("瀏覽器代理=%s" % ("127.0.0.1:" + _pp if _pp else "（直連）"))
        br = pw.chromium.launch(headless=True, args=args)
        # ⚠ 9/25 定案（記憶 apple-risk-541）：入袋 fetch 被 541 閘的是 UA/指紋，
        # 不是出口——生產 checkout.py 自己用 Chrome/131 且無 stealth 就是「被閘的
        # 形狀」；實測 Safari UA 才 200，只加 stealth（留 Chrome UA）仍然 541。
        # 本 harness 走同一條入袋路徑 ⇒ 必須用 Safari UA + stealth。
        ctx = br.new_context(
            viewport={"width": 1280, "height": 900}, locale="zh-HK",
            user_agent=(UA_SAFARI if os.environ.get("UA_ARM", "safari") == "safari"
                        else UA_CHROME))
        ctx.add_init_script("""
          Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
          Object.defineProperty(navigator, 'languages', {get: () => ['zh-HK','zh','en']});
          Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
          window.chrome = window.chrome || {runtime: {}};
          Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
        """)
        page = ctx.new_page()

        # ---- 前置驗證：只載商品頁，確認 UA/出口可及（541 是可及性閘，先量到才算）----
        try:
            r0 = page.goto(C.PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
            st0 = r0.status if r0 else -1
        except Exception as e:
            st0 = -1
            log("  前置載入例外 %s" % repr(e)[:60])
        u0 = (page.url or "")[:90]
        bad0 = (st0 != 200) or ("541" in u0) or ("/shop/go/404" in u0) or ("sorry" in u0)
        log("  前置：status=%s bad=%s url=%s" % (st0, bad0, u0))
        rec(phase="preflight", status=st0, bad=bad0, url=u0,
            ua="Safari/605.1.15")
        if bad0:
            log("!! 前置即被擋（541/404）⇒ 後續無法進行，只回報 UA 結論")
            rec(phase="abort", why="preflight-blocked", status=st0)
            return

        # ---- 真實入袋 ----
        log("→ 入袋 " + CARDS[0][0])
        page.goto(C.PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
        n = 0
        for sku, cand in CARDS:
            for _try in range(6):
                # 每輪先重載商品頁（生產 fill_bag 就是這樣做；541 綁「每次文件載入」
                # 而非 session，且失敗後頁面會被導去 go/404，不重載就必然一直失敗）
                try:
                    page.goto(C.PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    log("  重載商品頁失敗 %s" % repr(e)[:50])
                try:
                    if C.add_to_bag(page, ctx, cand):
                        n += 1
                        break
                except Exception as e:
                    log("  入袋例外 %s" % repr(e)[:60])
                time.sleep(8)
        log("  入袋 %d 件" % n)
        if n == 0:
            log("!! 入袋 0 件，中止"); rec(phase="abort", why="bag-empty"); return

        # ---- 結帳 → 訪客（複製生產入口邏輯）----
        def _entry():
            """與生產 checkout_flow 的入口邏輯逐字對齊（元素級等待 + click_text 後備）。"""
            page.goto(f"{C.SHOP}/bag", wait_until="domcontentloaded", timeout=45000)
            try:
                btn = page.locator(
                    "button:has-text('結帳'), a:has-text('結帳'), "
                    "[role=button]:has-text('結帳'), "
                    "input[type=submit][value*='結帳'], "
                    "input[type=button][value*='結帳'], "
                    "button:has-text('Check Out'), a:has-text('Check Out'), "
                    "[role=button]:has-text('Check Out')").first
                btn.wait_for(state="visible", timeout=10000)
                t = (btn.text_content() or "").strip()
                if "Apple Pay" not in t and "Pay" not in t:
                    btn.click(timeout=3000)
                    log("  點擊結帳入口「%s」" % t[:20])
                    return True
            except Exception as e:
                log("  入口元素點擊失敗 %s" % repr(e)[:40])
            return C.click_text(page, ["結帳", "立即結帳", "Check Out"],
                                exclude=["Apple Pay", "Pay"], timeout=8000)

        ok_entry = _entry()
        log("  入口已點=%s url=%s" % (ok_entry, page.url[:80]))
        # ⚠ 只有在 URL 真的到 Fulfillment 才算進到結帳（9/25 實證：click_text 子串
        # 會命中別的含「以訪客身份繼續」的元素，點完落在 /signIn ——「點到就 break」
        # 會帶著 signIn 頁往下走，後面每一步都必然失敗）。
        _guest_clicks = 0
        for _i in range(18):
            page.wait_for_timeout(1500)
            if "Fulfillment" in (page.url or ""):
                break
            if C.click_text(page, ["以訪客身份繼續", "繼續以訪客身份結帳",
                                   "Continue as Guest"]):
                _guest_clicks += 1
                log("  點到訪客按鈕（第 %d 輪，第 %d 次）url=%s"
                    % (_i + 1, _guest_clicks, page.url[:70]))
                page.wait_for_timeout(1500)
                if "Fulfillment" in (page.url or ""):
                    break
            if _i in (4, 9, 13) and ("/bag" in (page.url or "")
                                     or "signIn" in (page.url or "")):
                log("  未到 Fulfillment，重按入口（第 %d 次）url=%s"
                    % (_i // 5, page.url[:70]))
                _entry()
                page.wait_for_timeout(1200)
        log("  訪客 URL=%s（訪客點擊 %d 次）" % (page.url[:100], _guest_clicks))
        if "Fulfillment" not in (page.url or ""):
            try:
                txt = page.evaluate("""() => Array.from(document.querySelectorAll(
                    'button,a[role=button],[role=button],a')).slice(0,40)
                    .map(e => (e.innerText||'').trim().slice(0,18)).filter(Boolean)
                    .join(' | ')""")
            except Exception as e:
                txt = "ERR " + repr(e)[:40]
            log("!! 沒進訪客結帳 url=%s 當頁按鈕/連結=%s" % (page.url[:90], txt[:300]))
            rec(phase="abort", why="guest", url=page.url[:110], buttons=txt[:400])
            return

        host = (page.url or "").split("/")[2]
        base = (page.url or "").split("?")[0]
        log("  host=%s base=%s stk=%s" % (host, base, stk(page)))
        rec(phase="session", host=host, base=base, stk=stk(page),
            bag=n, url=page.url[:120], ua=os.environ.get("UA_ARM", "safari"),
            guest_clicks=_guest_clicks)

        # ---- 真實第一步：選店（生產字串）----
        q_store = ("_a=continue&_m=checkout.fulfillment"
                   "&checkout.fulfillment.fulfillmentOptions"
                   "&checkout.fulfillment.pickupTab.pickup.storeLocator.showAllStores=false"
                   "&checkout.fulfillment.pickupTab.pickup.storeLocator.selectStore=" + STORE +
                   "&checkout.fulfillment.pickupTab.pickup.storeLocator.searchInput="
                   "%E9%A6%99%E6%B8%AF")
        r = post(page, q_store)
        log("  選店 %s" % json.dumps(r, ensure_ascii=False))
        rec(phase="step", step="選店" + STORE, nav=None, **r)
        page.wait_for_timeout(1500)

        # ---- 真實第二發：取貨聯絡人（profile 由生產碼組）----
        # ⛔ 一律用假 profile：實驗不需要真資料，而 public repo 的 log 絕不可
        # 出現真 email/手機（這發 POST 是 no-op 重送，值不影響節流量測）。
        prof = {"email": "probe@example.com", "phone": "51234567",
                "first_name": "Probe", "last_name": "Test"}
        _contact = (
            "checkout.pickupContact.selfPickupContact.selfContact.address.emailAddress="
            + prof["email"] +
            "&checkout.pickupContact.selfPickupContact.selfContact.address.mobilePhone="
            + "".join(ch for ch in str(prof.get("phone", "")) if ch.isdigit())[-8:] +
            "&checkout.pickupContact.selfPickupContact.selfContact.address"
            ".isDaytimePhoneSelected=false"
            "&checkout.pickupContact.selfPickupContact.selfContact.address.lastName="
            + prof["last_name"] +
            "&checkout.pickupContact.selfPickupContact.selfContact.address.firstName="
            + prof["first_name"])
        q_contact = "_a=continue&_m=checkout.pickupContact&" + _contact
        r = post(page, q_contact)
        log("  取貨聯絡人（推進）%s stk=%s" % (json.dumps(r, ensure_ascii=False), stk(page)))
        rec(phase="step", step="取貨聯絡人-推進", nav=None, **r)

        # 之後所有臂都送這一發（已 no-op）——差異只剩導航
        arms = {
            "A_對照不導航": None,
            "B_同頁連發": "__none__",
            "C_goto同URL": base + "?_s=Fulfillment-init",
            "D_goto_Shipping-init": base + "?_s=Shipping-init",
            "E_goto_Billing-init": base + "?_s=Billing-init",
            "F_goto_www基底": "https://www.apple.com/hk-zh/shop/checkout?_s=Shipping-init",
        }
        order = list(arms)
        for rep in range(1, 4):
            rnd = order[rep % len(order):] + order[:rep % len(order)]
            for name in rnd:
                tgt = arms[name]
                log("--- 第%d輪 %s" % (rep, name))
                s0 = stk(page)
                time.sleep(COOL)                      # 讓閘過期
                nv_ms, nv_url = None, None
                if tgt == "__none__":
                    time.sleep(GAP)                   # 刻意踩閘：同頁連發
                elif tgt:
                    nv_ms, nv_url = nav(page, tgt)
                    if isinstance(nv_url, str) and nv_url.startswith("http"):
                        base_new = nv_url.split("?")[0]
                        if nv_url.split("?")[0] != base:
                            log("    ⚠ 導航換了基底：%s" % base_new[:80])
                            base = base_new
                    s1 = stk(page)
                    log("    導航 %sms → %s" % (nv_ms, str(nv_url)[:90]))
                else:
                    pass                              # A：純等 12 秒
                r = post(page, q_contact)
                log("    量測 %s ms  stk %s→%s  %s"
                    % (r["ms"], s0[:10], stk(page)[:10], json.dumps(r, ensure_ascii=False)))
                rec(phase="arm", rep=rep, arm=name, target=tgt,
                    nav_ms=nv_ms, nav_url=(nv_url if isinstance(nv_url, str) else None),
                    stk_before=s0, stk_after=stk(page), **r)

        # ---- 摘要 ----
        log("\n=== 摘要（量測 POST ms，中位數）===")
        for name in arms:
            v = [x["ms"] for x in RES if x.get("phase") == "arm" and x.get("arm") == name]
            if v:
                v.sort()
                log("  %-22s %s" % (name, v))
        br.close()


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        rec(phase="fatal", err=repr(e)[:200])
    log("總耗時 %.0f 秒" % (time.time() - t0))


# end
