#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profiles／出口的**遮罩版**匯出（給用戶核對用）。

為什麼需要它：`buyip18-scanner` 是**公開 repo**，job log 全世界都看得到。原本的
`duo_preflight.py` 會把 profile 名、卡尾碼、地址尾碼直接印進公開 log（等於把
「誰、用哪張卡、住哪」的線索放上網）。10/15 起改成：

  · **stdout（公開）**：只有形狀與旗標，不含姓名/卡號/地址/email
  · **私有 repo**（`TommyYeung660/buyip18-checkout` 的 `status/profiles-masked.json`）：
    帶姓名的遮罩表，供用戶逐個核對「App 推上去的內容是不是我要的」
  · 密碼/完整卡號/完整地址**任何地方都不出現**

用法（runner 或本機）：
    PROFILES_JSON='...' SLOT_PROXIES_JSON='...' python3 tools/profiles_masked.py --out /tmp/profiles-masked.json
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from duo_preflight import expiry_ok, luhn_ok  # noqa: E402  （同一套檢查，不重寫一份）

# 卡組織（由卡號前綴判斷；只用於顯示與「安全碼位數對不對」的檢查）
def brand_of(pan: str) -> str:
    d = "".join(c for c in str(pan or "") if c.isdigit())
    if not d:
        return "?"
    if d[0] == "4":
        return "Visa"
    if d[:2] in ("34", "37"):
        return "Amex"
    if d[:2] in ("51", "52", "53", "54", "55") or (len(d) >= 4 and 2221 <= int(d[:4]) <= 2720):
        return "Mastercard"
    if d[:2] == "62":
        return "UnionPay"
    if d[:2] == "35":
        return "JCB"
    if d[:4] == "6011" or d[:2] == "65":
        return "Discover"
    return "其他(%s)" % d[:2]


def mask_card(pan: str) -> str:
    d = "".join(c for c in str(pan or "") if c.isdigit())
    return ("*" * max(0, len(d) - 4) + d[-4:]) if d else ""


def mask_email(v: str) -> str:
    v = str(v or "")
    if "@" not in v:
        return "*" * len(v)
    local, _, dom = v.partition("@")
    dl, _, tld = dom.rpartition(".")
    return "%s*@%s*.%s" % (local[:1] or "*", dl[:1] or "*", tld or "")


def mask_phone(v: str) -> str:
    d = "".join(c for c in str(v or "") if c.isdigit())
    return ("*" * max(0, len(d) - 4) + d[-4:]) if d else ""


def mask_addr(v: str, head=16, tail=4) -> str:
    v = str(v or "")
    if len(v) <= head + tail:
        return v[:head] + ("…" if len(v) > head else "")
    return v[:head] + "…" + v[-tail:]


def mask_user(u: str) -> str:
    u = str(u or "")
    return ("…" + u[-4:]) if len(u) > 4 else ("…" if u else "")


def build(profiles_json, proxies_json):
    try:
        plist = (json.loads(profiles_json or "{}")).get("profiles") or []
    except Exception:
        plist = []
    out = {"generated_at": datetime.datetime.now(
               datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
           "note": "遮罩版（卡號只留末 4 碼、email/電話/地址只留頭尾）。完整值只在 GitHub secret 內。",
           "count": len(plist), "profiles": []}
    seen_card, seen_addr, seen_email = {}, {}, {}
    for i, p in enumerate(plist):
        p = p or {}
        pan = str(p.get("card_number") or "")
        b = brand_of(pan)
        cvv = "".join(c for c in str(p.get("card_cvv") or "") if c.isdigit())
        flags = []
        for key, store, label in (("card_number", seen_card, "卡號"),
                                  ("billing_line1", seen_addr, "地址"),
                                  ("email", seen_email, "email")):
            v = str(p.get(key) or "").strip()
            if not v:
                continue
            if v in store:
                flags.append("⚠ %s 與 #%d 完全相同" % (label, store[v] + 1))
            else:
                store[v] = i
        if pan and not luhn_ok(pan):
            flags.append("⛔ 卡號 Luhn 不通過（Apple 前端會直接拒）")
        if p.get("card_expiry") and not expiry_ok(p.get("card_expiry")):
            flags.append("⛔ 到期日格式錯誤或已過期")
        if b == "Amex":
            if len(cvv) != 4:
                flags.append("⚠ Amex 安全碼應為 4 位（美國運通印在卡正面），現為 %d 位" % len(cvv))
            else:
                flags.append("Amex：安全碼 4 位（正常；Apple 支援美國運通）")
        elif cvv and len(cvv) != 3:
            flags.append("⚠ %s 安全碼通常 3 位，現為 %d 位" % (b, len(cvv)))
        miss = [k for k in ("first_name", "last_name", "email", "phone", "card_number",
                            "card_expiry", "card_cvv", "billing_line1")
                if not str(p.get(k) or "").strip()]
        if miss:
            flags.append("⛔ 缺欄位：" + ",".join(miss))
        out["profiles"].append({
            "idx": i,
            "name": str(p.get("name") or ""),
            "card": "%s %s" % (b, mask_card(pan)),
            "pan_digits": len("".join(c for c in pan if c.isdigit())),
            "expiry": str(p.get("card_expiry") or ""),
            "cvv_digits": len(cvv),
            "email": mask_email(p.get("email")),
            "phone": mask_phone(p.get("phone")),
            "address": mask_addr(p.get("billing_line1")),
            "address_chars": len(str(p.get("billing_line1") or "")),
            "flags": flags,
        })
    # 出口（只顯示形狀與遮罩後的 host/user）
    try:
        arr = json.loads(proxies_json or "[]")
    except Exception:
        arr = []
    slots = []
    for i, row in enumerate(arr):
        if not isinstance(row, dict):
            slots.append({"idx": i, "shape": "❌ 不是 dict"})
            continue
        h = str(row.get("host") or "")
        slots.append({"idx": i, "host": ("…" + h[-12:]) if len(h) > 12 else h,
                      "port": row.get("port"), "auth": bool(row.get("user")),
                      "user": mask_user(row.get("user")),
                      "pass_len": len(str(row.get("pass") or ""))})
    out["proxies"] = {"count": len(arr), "slots": slots,
                      "distinct_hosts": len({str((r or {}).get("host")) for r in arr
                                             if isinstance(r, dict) and r.get("host")})}
    return out


def stdout_summary(d):
    """公開 log 只印這個（無姓名/卡號/地址/email）。"""
    print("=== 遮罩匯出（公開 log 不含 PII；完整遮罩表寫入私有 repo）===")
    print("  profiles=%d｜出口=%d（不同 host %d 個）"
          % (d["count"], d["proxies"]["count"], d["proxies"]["distinct_hosts"]))
    from collections import Counter
    print("  品牌分佈：%s" % dict(Counter((p["card"].split() or ["?"])[0] for p in d["profiles"])))
    bad = [p for p in d["profiles"] if any(f.startswith("⛔") for f in p["flags"])]
    warn = [p for p in d["profiles"] if any(f.startswith("⚠") for f in p["flags"])]
    print("  ⛔ 有硬問題 %d 個：#%s" % (len(bad), ",".join(str(p["idx"] + 1) for p in bad)))
    print("  ⚠ 有提醒 %d 個：#%s" % (len(warn), ",".join(str(p["idx"] + 1) for p in warn)))
    for p in d["profiles"]:
        if p["flags"]:
            print("    #%-2d %s" % (p["idx"] + 1, "；".join(p["flags"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    _px = os.environ.get("SLOT_PROXIES_JSON") or "[]"
    try:   # 10/15：兩組 secret 串成一份（合併只能在 runner 內做）
        _a = json.loads(_px or "[]") + json.loads(os.environ.get("SLOT_PROXIES_JSON_NEW") or "[]")
        _px = json.dumps(_a)
    except Exception:
        pass
    d = build(os.environ.get("PROFILES_JSON"), _px)
    stdout_summary(d)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("  已寫入 %s（%d bytes）" % (a.out, os.path.getsize(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
