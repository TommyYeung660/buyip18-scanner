#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iPhone Duo 送貨專案：單一 job 的執行器（10 路並發裡的一路）。

用法（在 runner 上，由 `.github/workflows/duo.yml` 呼叫）：
    python3 tools/duo_job.py

職責（全部由環境變數驅動，與 checkout.py 解耦）：
  1. 可選：等到 `DUO_WAIT_UNTIL`（ISO8601，例如 2026-10-16T20:00:00+08:00）前 ~90 秒
  2. 迴圈跑 `python3 checkout.py`（引擎一次一發）直到：
       - 引擎回報訂單成立（本機帳本出現 event=order）→ 成功
       - 或超過 `DUO_DEADLINE_MIN`（預設 25 分鐘）
     送貨鏈**不缺貨**，所以重試就是晚幾分鐘出貨，不需要毫秒級同步。
  3. 把本機 `status/hits.jsonl` 的新行**合併**進私有帳本（raw media type＋拒絕覆寫歷史，
     與掃描器 push_hits_ledger 同一套規則）；截圖由引擎自己在成立/未確認時上傳。

環境變數（必填見註）：
  ENGINE_DIR     引擎 checkout 目錄（預設 ./engine）
  PROFILE        * profile 名（對應 PROFILES_JSON 內 name）
  SKU            * 要買的料號（例 MK2F4ZA/A；留空＝照 AUTO_JSON candidates 順序）
  AUTO_JSON      預設 auto_duo.json
  FULFILLMENT    預設 HOME
  DRY_RUN        "1" 則只跑到 review 不下單
  PROFILES_JSON  * profiles JSON 字串（secret）
  GH_PAT         * 私有 repo token（推帳本用）
  DUO_PROXY_SERVER / DUO_PROXY_USER / DUO_PROXY_PASS  專屬出口（可空⇒直連）
  DUO_WAIT_UNTIL 例 2026-10-16T20:00:00+08:00（可空）
  DUO_DEADLINE_MIN 預設 25
  DUO_MAX_ATTEMPTS 預設 3（每 job 最多幾發；防同一卡重複送單）
  DUO_LEDGER_REPO  預設 TommyYeung660/buyip18-checkout
"""
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ENGINE = os.environ.get("ENGINE_DIR", "./engine")
LEDGER_REPO = os.environ.get("DUO_LEDGER_REPO", "TommyYeung660/buyip18-checkout")
LEDGER_PATH = "status/hits.jsonl"
API = "https://api.github.com/repos/" + LEDGER_REPO + "/contents/"
T0 = time.time()


def log(m):
    print("[duo_job %s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), m), flush=True)


def parse_iso(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        log("DUO_WAIT_UNTIL 解析失敗（%r）— 忽略" % s)
        return None


def wait_until(iso, lead_s=90):
    t = parse_iso(iso)
    if not t:
        return
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    fire = t - datetime.timedelta(seconds=lead_s)
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = (fire - now).total_seconds()
    if delta > 0:
        log("等到 %s（T-0=%s，提前 %ds 起跑）" % (fire.astimezone(t.tzinfo).isoformat(),
                                                t.isoformat(), lead_s))
        while delta > 0:
            time.sleep(min(delta, 30))
            delta = (fire - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    log("起跑（T-0 目標 %s）" % t.isoformat())


def engine_env():
    e = dict(os.environ)
    e.setdefault("AUTO_JSON", "auto_duo.json")
    e.setdefault("FULFILLMENT", "HOME")
    e.pop("KEEPER", None)          # 這條鏈不是 keeper 模式
    return e


ORDER_IN_STDOUT = re.compile(r"訂單號\s*=\s*([A-Z]?\d{6,})")


def order_from_stdout(text):
    """雙保險：引擎自己印出來的訂單號。帳本檔有時序/落地問題，stdout 不會騙人。"""
    m = ORDER_IN_STDOUT.search(text or "")
    return m.group(1) if m else None


def run_engine_once(n):
    log("── 第 %d 發：checkout.py（PROFILE=%s SKU=%s DRY_RUN=%s）"
        % (n, os.environ.get("PROFILE", ""), os.environ.get("SKU", "") or "(候選序)",
           os.environ.get("DRY_RUN", "")))
    t = time.time()
    p = subprocess.run([sys.executable, "-u", "checkout.py"], cwd=ENGINE,
                       env=engine_env(), capture_output=True, text=True, timeout=1800)
    # 印多一點（並優先印關鍵行）：CVV/卡號長度、訪客、履約、拒付等證據否則看不到
    lines = (p.stdout or "").strip().splitlines()
    keys = ("CVV", "卡號", "到期", "送貨鏈", "拒付", "明確拒絕", "未確認", "訂單號", "訪客",
            "KEEPER-B", "下單", "例外", "停")
    tail = [l for l in lines if any(k in l for k in keys)][-14:] or lines[-8:]
    for l in tail:
        log("   " + l[:160])
    log("── 第 %d 發結束：exit=%s、耗時 %.0fs" % (n, p.returncode, time.time() - t))
    return (p.stdout or "")


BASELINE = {"n": None}


def ledger_rows(all_rows=False):
    f = os.path.join(ENGINE, "status", "hits.jsonl")
    if not os.path.exists(f):
        return []
    out = []
    for line in open(f, encoding="utf-8", errors="replace").read().splitlines():
        if line.strip().startswith("{"):
            out.append(line.strip())
    if BASELINE["n"] is None:
        BASELINE["n"] = len(out)      # 第一次讀＝歷史，不算本 job 的痕跡
    return out if all_rows else out[BASELINE["n"]:]


def gh(method, url, data=None, raw=False):
    pat = (os.environ.get("GH_PAT") or os.environ.get("CHECKOUT_PAT") or "").strip()
    if not pat:
        return None
    hdr = {"Authorization": "Bearer " + pat,
           "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
           "User-Agent": "buyip18-duo"}
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(data).encode() if data else None, headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def push_ledger():
    """把本機新行合併進私有帳本（同一套規則：raw 讀底稿、讀不到就不推）。"""
    mine = ledger_rows()
    if not mine:
        log("本機無帳本行 ⇒ 不推")
        return
    try:
        base = gh("GET", API + LEDGER_PATH, raw=True) or ""
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 404:
            base = ""
        else:
            log("讀不到遠端帳本（%r）⇒ 本輪不推（避免覆寫歷史）" % (e,))
            return
    def key(l):
        try:
            d = json.loads(l)
            return json.dumps([d.get("ts"), d.get("event"), d.get("slot"),
                               d.get("sku"), d.get("state"), d.get("order_no")],
                              ensure_ascii=False)
        except Exception:
            return l
    seen, merged = set(), []
    for line in (base.splitlines() + mine):
        k = key(line)
        if k in seen:
            continue
        seen.add(k)
        merged.append(line)
    if len(merged) <= len(base.splitlines()):
        log("沒有新行可推（合併後 %d 行）" % len(merged))
        return
    body = {"message": "duo: ledger from job %s (profile=%s)" % (
                os.environ.get("GITHUB_RUN_ID", "local"), os.environ.get("PROFILE", "")),
            "content": base64.b64encode(("\n".join(merged) + "\n").encode()).decode(),
            "branch": "main"}
    # ⚠ 更新既有檔案必須帶 `sha`，否則 GitHub 回 422（10/07 實跑第一次推送就吃這個）
    try:
        meta = gh("GET", API + LEDGER_PATH) or "{}"
        body["sha"] = json.loads(meta).get("sha", "")
    except Exception:
        pass
    try:
        gh("PUT", API + LEDGER_PATH, data=body)
        log("帳本已推（合併後 %d 行，新增 %d 行）"
            % (len(merged), len(merged) - len(base.splitlines())))
    except Exception as e:
        log("帳本推送失敗：%r（不影響已成立的訂單）" % (e,))


def order_evidence():
    """回傳 (state, order_no)：任何「可能已成立」的證據都要讓迴圈停下來。

    ⚠ 安全：`unconfirmed`（200 但 body 沒確認頁）**不等於失敗**——它可能是已成立的單
    （本專案 9/29 誤報、10/02 三發未確認都是這型）。若在這裡繼續重試，就有雙單風險。
    ∴ 迴圈只在「完全沒有訂單痕跡」時才重試。
    """
    for line in ledger_rows():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") != "order":
            continue
        return (d.get("state") or "?", d.get("order_no") or "")
    return None


def ordered_ok():
    for line in ledger_rows():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") == "order" and d.get("state") == "ordered":
            return d.get("order_no") or "SUBMITTED"
    return None


def push_shots():
    """把本 job 產生的失敗/證據截圖推到私有 repo（公開 repo 絕不放截圖）。"""
    d = os.path.join(ENGINE, "status")
    if not os.path.isdir(d):
        return
    pat = (os.environ.get("GH_PAT") or os.environ.get("CHECKOUT_PAT") or "").strip()
    if not pat:
        return
    new_files = [f for f in sorted(os.listdir(d))
                 if f.startswith("shot-") and f.endswith(".png")
                 and os.path.getmtime(os.path.join(d, f)) >= T0]
    for f in new_files[:8]:
        try:
            with open(os.path.join(d, f), "rb") as fh:
                content = base64.b64encode(fh.read()).decode()
            name = "status/%s" % os.path.basename(f).replace(" ", "_")
            body = {"message": "duo job shot (profile=%s)" % os.environ.get("PROFILE", ""),
                    "content": content, "branch": "main"}
            # 同名檔已存在時必須帶 sha（否則 422，10/07 實跑吃過）
            try:
                meta = gh("GET", API + name) or "{}"
                body["sha"] = json.loads(meta).get("sha", "")
            except Exception:
                pass
            gh("PUT", API + name, data=body)
            log("截圖已推：%s" % name)
        except Exception as e:
            log("截圖推送失敗 %s：%r" % (f, e))
            break


def resolve_by_index():
    """⚡ 10/08：改用「路號」定位 profile。

    原因：原本 preflight 產生 matrix 再傳給 duo job，但 GitHub 會擋掉
    「可能含 secret」的 job output（實測警告：`Skip output 'matrix' since it may contain secret.`
    ——因為 matrix 裡的 profile 名是從 PROFILES_JSON 推出來的）⇒ matrix 變空 ⇒ 整個 run 失敗。
    改為：matrix 只帶**路號**（idx，靜態、不含密），每一路在**自己的 job 內**讀 secret 取第 idx 個
    profile；idx 超出現有數量就安靜結束（＝有幾張卡就跑幾路，且不必傳任何秘密出去）。
    """
    raw = os.environ.get("PROFILE_IDX", "").strip()
    if not raw.isdigit():
        return None
    i = int(raw)
    try:
        plist = (json.loads(os.environ.get("PROFILES_JSON") or "{}")).get("profiles") or []
    except Exception:
        return None
    if i < 0 or i >= len(plist):
        log("本路 idx=%d 沒有對應 profile（現有 %d 個）⇒ 結束（正常：有幾張卡就跑幾路）"
            % (i, len(plist)))
        return ""
    nm = str((plist[i] or {}).get("name") or "").strip()
    log("本路 idx=%d → profile=%s" % (i, nm))
    return nm


def profile_exists():
    """確認 PROFILE 真的在 PROFILES_JSON 內。

    ⚠ 引擎的行為是「找不到就用第一個 profile」——單路測試很方便，但**10 路並發時是災難**：
    9 路會全部退回同一張卡，Apple 端看起來就是同一顧客在同秒下 10 單。
    """
    want = os.environ.get("PROFILE") or ""
    try:
        plist = (json.loads(os.environ.get("PROFILES_JSON") or "{}")).get("profiles") or []
    except Exception:
        return None
    names = [str(p.get("name") or "") for p in plist]
    if not names:
        return None
    if want in names:
        return True
    if os.environ.get("DUO_ALLOW_FALLBACK") == "1":
        log("⚠ PROFILE=%s 不在 secret 內（%s）— 因 DUO_ALLOW_FALLBACK=1 才允許退回第一個"
            % (want, ",".join(names[:12])))
        return True
    log("⛔ PROFILE=%s 不在 PROFILES_JSON 內（現有：%s）⇒ 直接結束，不退回其他 profile"
        % (want, ",".join(names[:12])))
    return False


# ───────────────────────────── 假資料模式（10/15 用戶要求）─────────────────────────────
# 「10 卡並發 dry run 買 iPhone 18 Pro，要用 dummy data 代替，不要用真 profile 以免真的下單」。
# 目的：量測**並發 + 全鏈 + 出口**，不是買東西；真 profile（真卡真地址）一個都不碰。
#
# 兩道互鎖（缺一即拒跑，見 main）：
#   ① 卡號全部是各卡組織**公開測試號碼**（Luhn 合法、必被發卡行拒付，不屬於任何真人）
#   ② 必須同時 DRY_RUN 為真（引擎在 placeOrder 之前就停住）
# 即使兩道都被誤設成真下單，測試卡也不可能授權成功 ⇒ 不會產生真實訂單/扣款。
DUMMY_CARDS = [
    # (品牌, 測試卡號, 到期, 安全碼) — 4 Visa / 4 Mastercard / 2 Amex（AE 一定要有：
    # 用戶 10/15 要確認 AE 卡支援，AE＝15 位卡號＋4 位安全碼，與其他卡不同形狀）
    ("Visa",       "4111111111111111", "12/30", "123"),
    ("Mastercard", "5555555555554444", "12/30", "123"),
    ("Amex",       "378282246310005",  "12/30", "1234"),
    ("Visa",       "4012888888881881", "12/30", "123"),
    ("Mastercard", "5105105105105100", "12/30", "123"),
    ("Amex",       "371449635398431",  "12/30", "1234"),
    ("Visa",       "4242424242424242", "12/30", "123"),
    ("Mastercard", "5200828282828210", "12/30", "123"),
    ("Visa",       "4000056655665556", "12/30", "123"),
    ("Mastercard", "2223003122003222", "12/30", "123"),
]
DUMMY_ADDR = [
    "Flat A, 12/F, Block 3, Whampoa Garden, Hung Hom",
    "Room 801, 8/F, Tower 2, Harbour Place, Tai Kok Tsui",
    "Flat 5B, 21/F, Block D, Kornhill, Quarry Bay",
    "Unit 12, 15/F, Chelsea Court, Tsuen Wan",
    "Flat 3, 9/F, Mount Davis Road 45, Kennedy Town",
    "Room 1702, 17/F, Metro City Phase 2, Tseung Kwan O",
    "Flat C, 6/F, Fanling Centre Block 9, Fanling",
    "Unit 2203, 22/F, Sorrento Tower 6, West Kowloon",
    "Flat 7A, 14/F, Belvedere Garden Block 2, Tsuen Wan",
    "Room 505, 5/F, Laguna City Block 21, Lam Tin",
]


def dummy_profiles(n=10):
    """產生 n 個假 profile（形狀與真 profile 完全相同 ⇒ 引擎不需要任何分支）。"""
    out = []
    for i in range(n):
        brand, pan, exp, cvv = DUMMY_CARDS[i % len(DUMMY_CARDS)]
        nm = "DUMMY %02d" % (i + 1)
        out.append({
            "name": nm,
            "first_name": "TEST",
            "last_name": "DUMMY%02d" % (i + 1),
            "email": "duo.dummy%02d@example.com" % (i + 1),
            "phone": "5123%04d" % (i + 1),
            "card_number": pan,
            "card_expiry": exp,
            "card_cvv": cvv,
            "billing_line1": DUMMY_ADDR[i % len(DUMMY_ADDR)],
            "billing_line2": "",
            "_dummy_brand": brand,
        })
    return {"profiles": out}


def is_dry():
    return str(os.environ.get("DRY_RUN", "")).strip().lower() in ("1", "true", "yes")


def enable_dummy_if_requested():
    """回傳 None=可以繼續；非 None=要中止（並已說明理由）。"""
    if str(os.environ.get("DUMMY_PROFILES", "")).strip().lower() not in ("1", "true", "yes"):
        return None
    if not is_dry():
        log("⛔ DUMMY_PROFILES=1 必須搭配 DRY_RUN=true —— 用測試卡真下單沒有意義"
            "（且會把假資料留在 Apple 帳號上）⇒ 拒跑")
        return 9
    n = int(os.environ.get("DUMMY_COUNT", "10") or 10)
    os.environ["PROFILES_JSON"] = json.dumps(dummy_profiles(n), ensure_ascii=False)
    log("🧪 假資料模式：%d 個 dummy profile（公開測試卡號；Visa/MC 3 位、Amex 4 位安全碼）"
        "｜DRY_RUN=true 已在引擎內守住 placeOrder" % n)
    return None


EVID_MARKERS = (
    ("bag", re.compile(r"入袋\s*(\d+)/(\d+)（袋頁實測\s*(\d+)）")),
    ("add_retry", re.compile(r"入袋重試第\s*(\d+)/(\d+)\s*輪")),
    ("guest", re.compile(r"訪客完成 URL=(\S+)")),
    ("shipping_http", re.compile(r"Shipping 兩屏 HTTP 完成\s*([\d.]+)s")),
    ("shipping_ui", re.compile(r"Shipping HTTP 未果")),
    ("dry_review", re.compile(r"DRY_RUN ⇒ 停在 review")),
    ("order_no", ORDER_IN_STDOUT),
    ("declined", re.compile(r"拒付|declined")),
)


def summarize_out(text):
    """從引擎 stdout 抽幾個可驗收的欄位（彩排的驗收記錄靠這份，不靠人眼讀 log）。"""
    d = {"bag": "", "add_retry_rounds": 0, "reached_guest": False, "shipping": "",
         "dry_review": False, "order_no": "", "declined_seen": False}
    for line in (text or "").splitlines():
        m = EVID_MARKERS[0][1].search(line)
        if m:
            d["bag"] = "%s/%s（袋頁實測 %s）" % m.groups()
        m = EVID_MARKERS[1][1].search(line)
        if m:
            d["add_retry_rounds"] = max(d["add_retry_rounds"], int(m.group(1)))
        if EVID_MARKERS[2][1].search(line):
            d["reached_guest"] = True
        m = EVID_MARKERS[3][1].search(line)
        if m:
            d["shipping"] = "HTTP %ss" % m.group(1)
        if EVID_MARKERS[4][1].search(line):
            d["shipping"] = "UI 後備"
        if EVID_MARKERS[5][1].search(line):
            d["dry_review"] = True
        m = EVID_MARKERS[6][1].search(line)
        if m:
            d["order_no"] = m.group(1)
        if EVID_MARKERS[7][1].search(line):
            d["declined_seen"] = True
    return d


def push_private_json(name, obj, message):
    """把一份 JSON 推到**私有** repo（公開 repo 一個字都不放）。失敗只記 log，不影響流程。"""
    pat = (os.environ.get("GH_PAT") or os.environ.get("CHECKOUT_PAT") or "").strip()
    if not pat:
        return False
    try:
        content = json.dumps(obj, ensure_ascii=False, indent=1)
        body = {"message": message, "content": base64.b64encode(content.encode()).decode(),
                "branch": "main"}
        try:
            meta = gh("GET", API + name) or "{}"
            body["sha"] = json.loads(meta).get("sha", "")
        except Exception:
            pass
        gh("PUT", API + name, data=body)
        log("證據已推（私有）：%s" % name)
        return True
    except Exception as e:
        log("證據推送失敗 %s：%r" % (name, e))
        return False


def record_dry_evidence(idx, summary, out_text):
    """彩排（DUMMY/DRY_RUN）時，把每路的驗收記錄落成私有 repo 的一份 JSON。"""
    if not (is_dry() or os.environ.get("DUMMY_PROFILES")):
        return
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    try:
        latest = json.loads(open(os.path.join(ENGINE, "status", "latest.json"),
                                 encoding="utf-8").read())
    except Exception:
        latest = {}
    ev = {
        "run_id": run_id, "run_url": os.environ.get("RUN_URL", ""),
        "slot": idx, "profile": os.environ.get("PROFILE", ""), "sku": os.environ.get("SKU", ""),
        "auto_json": os.environ.get("AUTO_JSON", ""), "dummy": bool(os.environ.get("DUMMY_PROFILES")),
        "dry_run": True, "elapsed_s": round(time.time() - T0, 1),
        "summary": summary,
        "state": latest.get("state", ""), "review": latest.get("review", ""),
        "done_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))
                                        ).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "log_tail": (latest.get("log_tail") or [])[-30:],
        "shots": sorted(f for f in (os.listdir(os.path.join(ENGINE, "status"))
                                    if os.path.isdir(os.path.join(ENGINE, "status")) else [])
                        if f.startswith("shot-") and f.endswith(".png"))[-4:],
    }
    # 驗收判定（一路一列，人不用讀 log）
    ok = bool(summary.get("dry_review")) and latest.get("state") == "review"
    ev["verdict"] = "PASS" if ok else ("ORDER?" if summary.get("order_no") else "FAIL")
    push_private_json("status/dry-%s-slot%02d.json" % (run_id, idx), ev,
                      "dry rehearsal slot %02d (profile=%s)" % (idx, os.environ.get("PROFILE", "")))


def notify(title, msg, priority=0):
    """手機通知（Pushover 優先、Bark 次之）。任何失敗都不影響流程。"""
    import urllib.parse
    tok = (os.environ.get("PUSHOVER_TOKEN") or "").strip()
    usr = (os.environ.get("PUSHOVER_USER") or "").strip()
    bark = (os.environ.get("BARK_URL") or "").strip()
    try:
        if tok and usr:
            data = urllib.parse.urlencode({"token": tok, "user": usr, "title": title[:100],
                                           "message": msg[:900], "priority": priority}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://api.pushover.net/1/messages.json", data=data), timeout=15)
            return
        if bark:
            urllib.request.urlopen(bark.rstrip("/") + "/" + urllib.parse.quote(title + " " + msg)[:400],
                                   timeout=15)
    except Exception as e:
        log("通知失敗（不影響流程）：%r" % (e,))


def sku_for_index():
    """依路號配款式（與 tools/duo_preflight.py 的 VARIANTS 同一份；-1＝照設定檔候選序）。"""
    if os.environ.get("USE_APP_CONFIG") == "1":
        return ""
    raw = os.environ.get("PROFILE_IDX", "").strip()
    if not raw.isdigit():
        return ""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from duo_preflight import VARIANTS
        return VARIANTS[int(raw) % len(VARIANTS)][0]
    except Exception:
        return ""


def main():
    # ⚠⚠ 10/07 首戰教訓：BASELINE 必須在**任何一發之前**固定，否則第一發寫下的
    # `event=order` 會被當成「歷史」而看不到 ⇒ 迴圈以為沒成立 ⇒ 再打一發 ⇒ **雙單**。
    # 當晚真的發生了：W1948022250（第 1 發）＋ W1518897851（第 2 發）。
    ledger_rows()
    # 🧪 假資料模式（必須先於任何 profile 解析，因為它會直接替換 PROFILES_JSON）
    _abort = enable_dummy_if_requested()
    if _abort is not None:
        notify("Duo job 拒跑", "DUMMY_PROFILES=1 必須搭配 DRY_RUN=true（安全互鎖）")
        return _abort
    # 路號模式：先解出 profile 名與款式（都不需要跨 job 傳秘密）
    idx_name = resolve_by_index()
    if idx_name == "":
        return 0                     # 這一路沒有 profile ⇒ 正常結束
    if idx_name:
        os.environ["PROFILE"] = idx_name
        if not os.environ.get("SKU"):
            _sku = sku_for_index()
            if _sku:
                os.environ["SKU"] = _sku
                log("本路 idx=%s 指定款式 SKU=%s" % (os.environ.get("PROFILE_IDX"), _sku))
    if not os.environ.get("PROFILE"):
        log("⛔ 缺 PROFILE（也沒給 PROFILE_IDX）")
        return 2
    pe = profile_exists()
    if pe is False:
        notify("Duo job 中止", "PROFILE=%s 不在 PROFILES_JSON 內" % os.environ.get("PROFILE"))
        return 3
    wait_until(os.environ.get("DUO_WAIT_UNTIL", ""))
    deadline = T0 + int(os.environ.get("DUO_DEADLINE_MIN", "25")) * 60
    n = 0
    last_out = ""
    while True:
        n += 1
        out = ""
        try:
            out = run_engine_once(n) or ""
        except subprocess.TimeoutExpired:
            log("引擎逾時（30 分）⇒ 視為失敗，續試")
        if out:
            last_out = out
        o = order_from_stdout(out)
        if o:
            log("✅ 訂單成立（引擎 stdout）：%s（第 %d 發）" % (o, n))
            notify("Duo 下單成功", "%s｜訂單 %s" % (os.environ.get("PROFILE", ""), o), priority=1)
            break
        done = ordered_ok()
        if done:
            log("✅ 訂單成立：%s（第 %d 發）" % (done, n))
            notify("Duo 下單成功", "%s｜訂單 %s" % (os.environ.get("PROFILE", ""), done), priority=1)
            break
        # 卡資料本身有錯（Luhn/到期/CVV）⇒ 再跑幾次都一樣，直接收工並通知
        try:
            _st = json.loads(open(os.path.join(ENGINE, "status", "latest.json"),
                                  encoding="utf-8").read()).get("state")
        except Exception:
            _st = None
        if _st == "bad-card":
            log("⛔ 引擎回報 bad-card（卡片資料不合法）⇒ 收工，請更正 profile 後再跑")
            notify("Duo 卡片資料有誤", "%s：%s 的卡號/到期/CVV 不合法，已在開瀏覽器前中止"
                   % (os.environ.get("PROFILE", ""), os.environ.get("SKU", "")))
            break
        ev = order_evidence()
        if ev and ev[0] == "declined":
            # ⚡ 10/09：「被拒」＝**確定沒成立**（發卡機構回絕，Apple 頁面明講）⇒ 可以安全重試，
            # 不必把整輪收掉。多次嘗試正是用戶要的（有時銀行端是暫時性的）。
            # ⚠ 這裡**不可以 `continue`**：那會跳過下面的「上限／期限」檢查（10/09 實測：
            # 一次 dispatch 跑了 16 發、直到 40 分鐘 job 逾時被取消）。改為落到下面照常檢查。
            log("↻ 本發被拒（%s）⇒ 續試（不含雙單風險，但受上限與期限約束）" % (ev[1] or "declined"))
        if ev and ev[0] != "ordered":
            # 未確認（或任何非 ordered 的痕跡）⇒ **停手，交人判斷**，絕不重試
            log("⚠ 帳本出現訂單痕跡但狀態=%s（單號=%s）⇒ 停止重試（避免雙單）；"
                "請到 Apple 帳戶頁核對" % (ev[0], ev[1] or "(無)"))
            notify("Duo 下單結果：%s" % ev[0],
                   "%s｜%s｜請到 Apple 帳戶頁核對" % (os.environ.get("PROFILE", ""), ev[0]),
                   priority=1)
            break
        if str(os.environ.get("DRY_RUN", "")).strip().lower() in ("1", "true", "yes"):
            log("DRY_RUN ⇒ 一發即收工（不下單）")
            break
        cap = int(os.environ.get("DUO_MAX_ATTEMPTS", "3") or 3)
        if n >= cap:
            log("⛔ 已達重試上限 %d 發 ⇒ 收工（避免同一 profile/卡重複送單）" % cap)
            break
        if time.time() > deadline:
            log("⏰ 超過期限（%s 分鐘）⇒ 收工" % os.environ.get("DUO_DEADLINE_MIN", "25"))
            break
        time.sleep(15)
    push_ledger()
    push_shots()
    _sum = summarize_out(last_out)
    log("本路摘要：" + json.dumps(_sum, ensure_ascii=False))
    try:
        _idx = int(os.environ.get("PROFILE_IDX") or -1)
    except Exception:
        _idx = -1
    record_dry_evidence(_idx, _sum, last_out)
    log("本 job 結束（共 %d 發）" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
