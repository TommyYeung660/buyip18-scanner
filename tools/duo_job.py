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
    return p.returncode


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


def main():
    if not os.environ.get("PROFILE"):
        log("⛔ 缺 PROFILE")
        return 2
    pe = profile_exists()
    if pe is False:
        notify("Duo job 中止", "PROFILE=%s 不在 PROFILES_JSON 內" % os.environ.get("PROFILE"))
        return 3
    wait_until(os.environ.get("DUO_WAIT_UNTIL", ""))
    deadline = T0 + int(os.environ.get("DUO_DEADLINE_MIN", "25")) * 60
    n = 0
    while True:
        n += 1
        try:
            run_engine_once(n)
        except subprocess.TimeoutExpired:
            log("引擎逾時（30 分）⇒ 視為失敗，續試")
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
        if ev and ev[0] != "ordered":
            # 未確認（或任何非 ordered 的訂單痕跡）⇒ **停手，交人判斷**，絕不重試
            log("⚠ 帳本出現訂單痕跡但狀態=%s（單號=%s）⇒ 停止重試（避免雙單）；"
                "請到 Apple 帳戶頁核對" % (ev[0], ev[1] or "(無)"))
            notify("Duo 下單結果：%s" % ev[0],
                   "%s｜%s｜請到 Apple 帳戶頁核對" % (os.environ.get("PROFILE", ""), ev[0]),
                   priority=1)
            break
        if os.environ.get("DRY_RUN") == "1":
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
    log("本 job 結束（共 %d 發）" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
