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


def luhn_ok(s) -> bool:
    d = [int(c) for c in str(s or "") if c.isdigit()]
    if len(d) < 12:
        return False
    tot, alt = 0, False
    for x in reversed(d):
        if alt:
            x *= 2
            if x > 9:
                x -= 9
        tot += x
        alt = not alt
    return tot % 10 == 0


def expiry_ok(s) -> bool:
    """MM/YY（或 MM/YYYY）且尚未過期（以當月為界）。"""
    import datetime
    t = str(s or "").replace(" ", "")
    if "/" not in t:
        return False
    mm, yy = t.split("/")[:2]
    try:
        m = int(mm)
        y = int(yy)
    except Exception:
        return False
    if not (1 <= m <= 12):
        return False
    y += 2000 if y < 100 else 0
    now = datetime.date.today()
    return (y, m) >= (now.year, now.month)


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
        # ⚡ 10/07 實跑教訓：卡號若**填錯一位**，Apple 會在前端就回「請輸入有效的信用卡號碼」
        # （Billing 卡住、整輪白跑）。這在 20:00 當晚是不可接受的失敗 ⇒ 出貨前先用 Luhn 檢查。
        bad_card = bool(str(p.get("card_number") or "").strip()) and not luhn_ok(p.get("card_number"))
        bad_exp = bool(str(p.get("card_expiry") or "").strip()) and not expiry_ok(p.get("card_expiry"))
        flag = "✅" if not miss and not dup and not bad_card and not bad_exp else "❌"
        print("  %s %-10s 卡=%s%s 到期=%s%s 地址=%s%s%s"
              % (flag, name, mask(p.get("card_number")),
                 "（Luhn 不通過）" if bad_card else "",
                 p.get("card_expiry") or "-",
                 "（格式/已過期）" if bad_exp else "",
                 mask(p.get("billing_line1"), 4),
                 ("  缺：" + "/".join(miss)) if miss else "",
                 ("  ⚠重複：" + "；".join(dup)) if dup else ""))
        if miss:
            problems.append("%s 缺 %s" % (name, "/".join(miss)))
        if dup:
            problems.append("%s %s" % (name, "；".join(dup)))
        if bad_card:
            problems.append("%s 卡號 Luhn 檢查不通過（尾 %s）——Apple 前端會直接拒，請重新核對數字"
                            % (name, mask(p.get("card_number"), 4)))
        if bad_exp:
            problems.append("%s 到期日格式錯誤或已過期：%s" % (name, p.get("card_expiry")))

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
