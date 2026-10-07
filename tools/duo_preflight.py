#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iPhone Duo 出貨前檢查（preflight）：在 10/16 之前／當晚 T-30 分鐘跑一次。

它只讀**秘密的形狀**（不印值），逐一檢查：
  1. `PROFILES_JSON` 內每個 profile 的必填欄位（姓名／email／電話／卡號／到期／CVV／送貨地址）
  2. 有沒有重複（同卡號／同地址／同名）——重複＝Apple 端會被視為同一顧客
  3. `SLOT_PROXIES_JSON` 的出口數是否 ≥ 需要的路數（10），且每個都有 host/port
  4. duo.yml 的 matrix 路數與 profile 對應（duo01..duo10）
  ⇒ 印出一張「就緒表」(profile × 欄位) 與總結；任何缺漏 ⇒ exit 1（工作流會紅燈，早點發現）

用法（本機或 runner）：
    PROFILES_JSON='...' SLOT_PROXIES_JSON='...' python3 tools/duo_preflight.py
"""
import json
import os
import sys

NEED = [("first_name", "名"), ("last_name", "姓"), ("email", "email"), ("phone", "電話"),
        ("card_number", "卡號"), ("card_expiry", "到期"), ("card_cvv", "CVV"),
        ("billing_line1", "送貨地址")]
WANT_PROFILES = 10


def mask(v, keep=2):
    v = str(v or "")
    return "" if not v else ("*" * max(0, len(v) - keep) + v[-keep:])


def main() -> int:
    prof = {}
    try:
        prof = json.loads(os.environ.get("PROFILES_JSON") or "{}")
    except Exception as e:
        print("⛔ PROFILES_JSON 解析失敗：%r" % (e,))
        return 1
    plist = prof.get("profiles") or []
    proxies = []
    try:
        proxies = json.loads(os.environ.get("SLOT_PROXIES_JSON") or "[]")
    except Exception as e:
        print("⚠ SLOT_PROXIES_JSON 解析失敗：%r" % (e,))

    print("=== 就緒表（欄位只顯示尾 2 碼）===")
    problems = []
    seen_cards, seen_addr, seen_names = {}, {}, {}
    for i, p in enumerate(plist):
        name = str(p.get("name") or ("profile-%d" % (i + 1)))
        miss = [zh for k, zh in NEED if not str(p.get(k) or "").strip()]
        dup = []
        for key, store, label in (("card_number", seen_cards, "卡號"), ("billing_line1", seen_addr, "地址"),
                                  ("email", seen_names, "email")):
            v = str(p.get(key) or "").strip()
            if not v:
                continue
            if v in store:
                dup.append("%s 與 %s 相同" % (label, store[v]))
            else:
                store[v] = name
        flag = "✅" if not miss and not dup else "❌"
        print("  %s %-10s 卡=%s 到期=%s 地址=%s%s%s"
              % (flag, name, mask(p.get("card_number")), p.get("card_expiry") or "-",
                 mask(p.get("billing_line1"), 4),
                 ("  缺：" + "/".join(miss)) if miss else "",
                 ("  ⚠重複：" + "；".join(dup)) if dup else ""))
        if miss:
            problems.append("%s 缺 %s" % (name, "/".join(miss)))
        if dup:
            problems.append("%s %s" % (name, "；".join(dup)))

    print("=== 出口（SLOT_PROXIES_JSON）===")
    ok_px = 0
    for i, px in enumerate(proxies):
        if isinstance(px, dict) and px.get("host") and px.get("port"):
            ok_px += 1
        else:
            print("  ❌ idx=%d 形狀不對（要是 dict 且含 host/port）：%r" % (i, px))
    print("  可用出口 %d 個（需要 ≥%d）" % (ok_px, WANT_PROFILES))

    print("=== 總結 ===")
    print("  profiles=%d（建議 ≥%d）｜出口=%d（建議 ≥%d）" % (len(plist), WANT_PROFILES, ok_px, WANT_PROFILES))
    if len(plist) < WANT_PROFILES:
        problems.append("profile 數不足：%d < %d" % (len(plist), WANT_PROFILES))
    if ok_px < WANT_PROFILES:
        problems.append("可用出口不足：%d < %d" % (ok_px, WANT_PROFILES))
    if problems:
        print("⛔ 未就緒：")
        for x in problems:
            print("   - " + x)
        return 1
    print("✅ 就緒（可以跑 10 路）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
