#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iPhone 18 Pro Max 秒級庫存掃描器
================================
單 job 內部循環（最長 ~5.5 小時），mihomo 載入訂閱節點並以 external-controller
秒切出口；每節點 ≥15 秒一次請求（禮貌頻率），N 節點聚合 ≈ 每 1-2 秒一次掃描。
見貨 → dispatch buyip18-checkout（帶 SKU+店精準打擊 payload）+ Bark → job 結束。

環境：SCAN_NODES_FILE（mihomo proxies 陣列 JSON）、GH_PAT、BARK_URL（可選）、
      MAX_MINUTES（預設 320）
"""
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

PARTS = ["MJXQ4ZA/A", "MJXR4ZA/A", "MJXV4ZA/A", "MJXW4ZA/A",
         "MJY04ZA/A", "MJY14ZA/A", "MJY44ZA/A", "MJY54ZA/A"]
MAX_PARTS = 3                       # pickup-message 上限，>3 會 541
PER_NODE_GAP_S = 15                 # 每節點兩次請求的最小間距（共享出口禮貌）
SWEEP_PAUSE = (2.0, 4.0)            # 每輪掃描間的基礎停頓
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "320"))
MIHOMO_API = "http://127.0.0.1:9090"
PROXY = "http://127.0.0.1:7890"

START = time.time()
node_last_used = {}
node_dead = set()


def mask(name):
    return (name[:10] + "…") if len(name) > 10 else name


def log(msg):
    print(time.strftime("%H:%M:%S") + "  " + msg, flush=True)


def now_min():
    return (time.time() - START) / 60


# ---------------------------------------------------------------- http
def http(url, method="GET", body=None, headers=None, timeout=12, proxy=True):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": PROXY, "https": PROXY}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, method=method,
                                 data=body.encode() if body else None,
                                 headers=headers or {})
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def mihomo_switch(node):
    req = urllib.request.Request(
        MIHOMO_API + "/proxies/SCAN", method="PUT",
        data=json.dumps({"name": node}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


# ---------------------------------------------------------------- 掃描
def poll_sweep():
    """3 個請求覆蓋 8 SKU；回傳 {sku: [(店號, 店名, quote)]}（僅 available）。"""
    hits = {}
    chunks = [PARTS[i:i + MAX_PARTS]
              for i in range(0, len(PARTS), MAX_PARTS)]
    for chunk in chunks:
        qs = "&".join("parts.%d=%s" % (i, urllib.parse.quote(p))
                      for i, p in enumerate(chunk))
        url = ("https://www.apple.com/hk-zh/shop/retail/pickup-message"
               "?pl=true&fae=true&mts.0=regular&" + qs +
               "&location=" + urllib.parse.quote("Hong Kong"))
        code, body = http(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-HK,zh-TW;q=0.9,en;q=0.7",
        })
        if code != 200:
            raise RuntimeError("pickup http=%d" % code)
        d = json.loads(body)
        stores = (d.get("body") or d).get("stores") or []
        for st in stores:
            pa = st.get("partsAvailability") or {}
            for part, info in pa.items():
                if info.get("pickupDisplay") == "available":
                    reg = (info.get("regular") or {})
                    hits.setdefault(part, []).append(
                        (st.get("storeNumber", ""),
                         st.get("storeName", ""),
                         reg.get("storePickupQuote", "")))
    return hits


def pick_node(nodes):
    """選最久未用且存活的節點；保證每節點 ≥PER_NODE_GAP_S。"""
    while True:
        alive = [n for n in nodes if n not in node_dead]
        if not alive:
            raise RuntimeError("全部節點失效")
        candidate = min(alive, key=lambda n: node_last_used.get(n, 0))
        gap = time.time() - node_last_used.get(candidate, 0)
        if gap >= PER_NODE_GAP_S or len(alive) == 1:
            node_last_used[candidate] = time.time()
            return candidate
        time.sleep(min(1.5, PER_NODE_GAP_S - gap))


def sweep_once(nodes):
    """一輪掃描 = 3 個請求，每請求換一個節點。"""
    global _cur_node
    hits = {}
    for _ in range(3):
        node = pick_node(nodes)
        if _cur_node != node:
            mihomo_switch(node)
            _cur_node = node
            log("切換出口 → " + mask(node))
        try:
            hits = poll_sweep()
            if hits:
                return hits
        except Exception as e:
            log("節點失效（%s…）: %s" % (mask(node), str(e)[:60]))
            node_dead.add(node)
    return hits


_cur_node = None


# ---------------------------------------------------------------- 動作
def dispatch_checkout(sku, store):
    pat = os.environ.get("GH_PAT", "")
    body = json.dumps({
        "event_type": "stock-hit",
        "client_payload": {"profile": "billy01", "dry_run": "",
                           "store": store, "sku": sku}})
    req = urllib.request.Request(
        "https://api.github.com/repos/TommyYeung660/buyip18-checkout/dispatches",
        method="POST", data=body.encode(),
        headers={"Authorization": "Bearer " + pat,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "buyip18-scanner",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        log("dispatch checkout → http=%d" % r.status)
        return r.status


def bark(title, body):
    url = (os.environ.get("BARK_URL") or "").strip()
    if not url:
        return
    try:
        t = urllib.parse.quote(title)
        b = urllib.parse.quote(body)
        http(url.rstrip("/") + "/%s/%s?group=iPhone18&level=timeSensitive"
             "&sound=alarm" % (t, b), timeout=10, proxy=False)
        log("Bark 已送出")
    except Exception as e:
        log("Bark 失敗: " + str(e)[:80])


# ---------------------------------------------------------------- 主循環
def main():
    nodes_file = os.environ.get("SCAN_NODES_FILE", "/tmp/nodes.json")
    nodes = [n["name"] for n in json.load(open(nodes_file))]
    # 預先剔除已知壞節點（信息節點/V6 壞線）
    bad_kw = ["建议", "建議", "V6【", "剩餘", "剩余", "过期", "過期", "官網", "群組"]
    dropped = [n for n in nodes if any(k in n for k in bad_kw)]
    nodes = [n for n in nodes if not any(k in n for k in bad_kw)]
    log("剔除壞節點 %d 個，可用 %d 個" % (len(dropped), len(nodes)))
    log("掃描器啟動：節點 %d 個，最長 %d 分鐘" % (len(nodes), MAX_MINUTES))
    sweep = 0
    while now_min() < MAX_MINUTES:
        sweep += 1
        try:
            hits = sweep_once(nodes)
        except RuntimeError as e:
            log("全部節點失效，10 分鐘後重試: %s" % e)
            node_dead.clear()
            time.sleep(600)
            continue
        if hits:
            sku = sorted(hits)[0]
            store = hits[sku][0][0]
            detail = "; ".join("%s@%s(%s)" % (s, n, q)
                               for n, q in [(h[1], h[0], h[2])
                                            for h in hits[sku]][:3])
            log("★ 有貨！ %s → %s" % (sku, detail))
            bark("iPhone 18 有貨！",
                 "%s @ %s — 自動下單已觸發" % (sku, detail[:60]))
            try:
                dispatch_checkout(sku, store)
            except Exception as e:
                log("dispatch 失敗: %s" % e)
                bark("dispatch 失敗", str(e)[:80])
            return 0
        if sweep % 20 == 0:
            log("sweep %d | %0.1f 分 | 存活節點 %d | 無貨"
                % (sweep, now_min(), len(nodes) - len(node_dead)))
        time.sleep(random.uniform(*SWEEP_PAUSE))
    log("到時收工，等 watchdog 重啟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
