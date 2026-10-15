#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐出口體檢（exit health）：對每個出口量三件事，一張表看完。

  1. **egress IP／地區**：透過該出口打 ipinfo ⇒ 確認真的從香港出去、IP 不重複
  2. **真站可達性**：透過該出口 GET 商品頁 ⇒ HTTP 狀態、位元組數、耗時
  3. **頁面指紋**：`partNumber`／`dimensionColor`／`add-to-cart` 是否在（商品頁有沒有正常）

為什麼要這支：10/15 實測「同一出口同時跑兩個 session ⇒ 其中一個可能整路被 541 擋死」，
而 541 又是**機率閘**（出口 2 一度 24/24 全滅、一小時後恢復）⇒ 當晚 18:30 逐個出口先體檢一次，
不合格的不要派上去。**只讀網頁、不下單、不碰 profile。**

用法：
    SLOT_PROXIES_JSON='[...]' python3 tools/exit_health.py --url https://www.apple.com/hk-zh/shop/buy-iphone/iphone-duo
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


def proxy_url(row):
    h, p = str(row.get("host") or ""), str(row.get("port") or "")
    if not h:
        return ""
    u, w = str(row.get("user") or ""), str(row.get("pass") or "")
    if u:
        return "http://%s:%s@%s:%s" % (urllib.parse.quote(u, safe=""),
                                       urllib.parse.quote(w, safe=""), h, p)
    return "http://%s:%s" % (h, p)


def opener(px, timeout):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": px, "https": px}))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-HK,zh;q=0.9,en;q=0.8")]
    return op


def get(op, url, timeout):
    t = time.time()
    try:
        with op.open(url, timeout=timeout) as r:
            body = r.read()
            return {"status": r.status, "bytes": len(body),
                    "ms": int((time.time() - t) * 1000), "body": body}
    except Exception as e:
        code = getattr(e, "code", None)
        return {"status": code or 0, "bytes": 0, "ms": int((time.time() - t) * 1000),
                "error": repr(e)[:90], "body": b""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.apple.com/hk-zh/shop/buy-iphone/iphone-duo")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--ipinfo", default="https://ipinfo.io/json")
    a = ap.parse_args()
    try:
        arr = json.loads(os.environ.get("SLOT_PROXIES_JSON") or "[]")
    except Exception:
        arr = []
    if not arr:
        print("⛔ SLOT_PROXIES_JSON 空或解析失敗 ⇒ 沒得測")
        return 2
    print("=== 逐出口體檢（出口 %d 個｜商品頁 %s）===" % (len(arr), a.url))
    bad = 0
    seen_ip = {}
    for i, row in enumerate(arr):
        if not isinstance(row, dict) or not row.get("host"):
            print("  #%d ❌ 形狀不對" % i)
            bad += 1
            continue
        px = proxy_url(row)
        op = opener(px, a.timeout)
        info = get(op, a.ipinfo, a.timeout)
        ip = city = cc = "?"
        if info.get("status") == 200 and info.get("body"):
            try:
                d = json.loads(info["body"].decode("utf-8", "replace"))
                ip, city, cc = d.get("ip", "?"), d.get("city", "?"), d.get("country", "?")
            except Exception:
                pass
        page = get(op, a.url, a.timeout)
        html = (page.get("body") or b"").decode("utf-8", "replace")
        marks = []
        for k in ('"partNumber"', "dimensionColor", "add-to-cart", "你想如何付款"):
            if k in html:
                marks.append(k)
        dup = ("（與 #%d 同 IP！）" % seen_ip[ip]) if ip in seen_ip and ip != "?" else ""
        if ip != "?":
            seen_ip[ip] = i
        ok = info.get("status") == 200 and page.get("status") == 200
        if not ok:
            bad += 1
        print("  #%d %s egress=%s %s/%s%s｜商品頁 http=%s %dKB %dms｜指紋=%s%s"
              % (i, "✅" if ok else "❌", ip, city, cc, dup,
                 page.get("status"), (page.get("bytes") or 0) // 1024, page.get("ms"),
                 ",".join(marks) or "（無）",
                 ("｜err=" + str(page.get("error"))) if page.get("error") else ""))
    print("=== 合計：可用 %d／%d（不同 egress IP %d 個）===" % (len(arr) - bad, len(arr), len(seen_ip)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
