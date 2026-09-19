#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iPhone 18 Pro Max 秒級庫存掃描器
================================
單 job 內部循環（最長 ~5.5 小時），mihomo 載入訂閱節點並以 external-controller
秒切出口；每節點每輪 3 請求（連續），節點間距 ≥15 秒（禮貌頻率），N 節點聚合 ≈ 每 1-2 秒一次掃描。
見貨 → dispatch buyip18-checkout（帶 SKU+店精準打擊 payload）+ Bark → job 結束。

環境：SCAN_NODES_FILE（mihomo proxies 陣列 JSON）、GH_PAT、BARK_URL（可選）、
      MAX_MINUTES（預設 320）
"""
import json
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# 9/19 用戶放寬：256GB+512GB 都買（布根地紅/冰川色）——1TB/2TB 維持不掃不購
PARTS = ["MJXQ4ZA/A", "MJXR4ZA/A", "MJXV4ZA/A", "MJXW4ZA/A"]
MAX_PARTS = 3                       # pickup-message 上限，>3 會 541
PER_NODE_GAP_S = 15                 # 每節點兩次請求的最小間距（共享出口禮貌）
NODE_COOLDOWN_S = 900               # 節點單次失敗的冷卻秒數（15 分）
NODE_MAX_STRIKES = 3                # 累計失敗達此數 → 三振出局（該 job 內不再使用）
SWEEP_PAUSE = (2.0, 4.0)            # 每輪掃描間的基礎停頓
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "320"))
MIHOMO_API = "http://127.0.0.1:9090"
PROXY = "http://127.0.0.1:7890"

START = time.time()
node_last_used = {}
node_dead = {}                      # name → 冷卻截止 unix-ts（過期即復活）
node_strikes = {}                   # name → 累計失敗次數（三振出局）


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
    """選最久未用且存活的節點（跳過冷卻中／三振出局者）；保證每節點 ≥PER_NODE_GAP_S。"""
    while True:
        alive = [n for n in nodes
                 if node_dead.get(n, 0) <= time.time()
                 and node_strikes.get(n, 0) < NODE_MAX_STRIKES]
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
            try:
                mihomo_switch(node)
                _cur_node = node
                log("切換出口 → " + mask(node))
            except Exception as e:
                log("mihomo 切換失敗（%s…）: %s" % (mask(node), str(e)[:60]))
                # 記失敗但不標死節點（mihomo 本身問題，非出口問題）
                node_strikes[node] = node_strikes.get(node, 0)  # 不加擊
                time.sleep(2)
                continue
        try:
            hits = poll_sweep()
            if hits:
                return hits
        except Exception as e:
            log("節點失效（%s…）: %s" % (mask(node), str(e)[:60]))
            node_strikes[node] = node_strikes.get(node, 0) + 1
            if node_strikes[node] >= NODE_MAX_STRIKES:
                node_dead[node] = time.time() + 86400  # 該 job 內不再使用
                log("節點三振出局（%s…）" % mask(node))
            else:
                node_dead[node] = time.time() + NODE_COOLDOWN_S
                log("節點冷卻 15 分（%s…，第 %d 敗）" % (mask(node), node_strikes[node]))
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


# ---------------------------------------------------------------- 駐場結帳（KEEPER）
# 掃描 job 常駐一個「預熱結帳會話」：袋已填、訪客已過、停在履約頁待命。
# 命中且 SKU 吻合 → 掃描器經命令檔叫 keeper 就地選店下單（免預熱/入袋/訪客 ~45 秒）。
KEEPER_STATE = "/tmp/keeper/state.json"
KEEPER_CMD = "/tmp/keeper/command.json"
KEEPER_ENGINE_DIR = "/tmp/checkout-engine"      # keeper 常駐引擎
KEEPER_PROC = None                              # keeper 進程柄（死後即時重開）
FALLBACK_ENGINE_DIR = "/tmp/checkout-fallback"  # 命中重走的後備引擎（勿與 keeper 共用目錄）


def keeper_state():
    try:
        return json.load(open(KEEPER_STATE))
    except Exception:
        return None


def keeper_fire(sku, store, store_code=""):
    os.makedirs(os.path.dirname(KEEPER_CMD), exist_ok=True)
    json.dump({"action": "buy", "sku": sku, "store": store,
               "store_code": store_code},
              open(KEEPER_CMD, "w"), ensure_ascii=False)
    log("keeper 命令已下（%s @ %s/%s）— 等待結果" % (sku, store, store_code or "?"))


def keeper_wait(timeout_s):
    """輪詢 keeper state 直到真終態或逾時。
    keeper-swapping 是長流程（外部 d11bb25：換袋→重走→下單），提前當終態返回
    會令掃描器收工殺掉 keeper=換袋腰斬（9/18 晨 3 次命中的實測教訓）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        last = keeper_state()
        if last and last.get("state") not in (
                "ready", "buying", "selected", "keeper-swapping"):
            return last
        time.sleep(0.3)  # 命令拾取與結果回傳都快（原 3s）
    return last or {"state": "timeout"}


def keeper_start(nodes):
    """啟動駐場引擎：clone checkout repo → KEEPER 模式跑 checkout.py（背景、家寬出口 7891）。"""
    import shutil
    import subprocess
    pat = os.environ.get("GH_PAT", "").strip()
    if not pat or not os.environ.get("PROFILES_JSON", "").strip():
        log("keeper 未啟用（缺 GH_PAT/PROFILES_JSON secret）")
        return False
    if not [n for n in nodes if "家宽" in n and "香港" in n]:
        log("keeper 未啟用（節點池無香港家寬）")
        return False
    shutil.rmtree(KEEPER_ENGINE_DIR, ignore_errors=True)
    r = subprocess.run(
        ["git", "clone", "-q", "--depth", "1",
         "https://x-access-token:%s@github.com/TommyYeung660/buyip18-checkout" % pat,
         KEEPER_ENGINE_DIR],
        capture_output=True, text=True)
    if r.returncode != 0:
        log("keeper clone 失敗: " + (r.stderr or "")[-70:])
        return False
    os.makedirs("/tmp/keeper", exist_ok=True)
    for f in (KEEPER_STATE, KEEPER_CMD):
        try:
            os.remove(f)
        except Exception:
            pass
    keeper_skus = ",".join(PARTS[:2])  # 袋內 = 候選偏好前兩個（hint 排最前+次選）
    env = dict(os.environ, SKU=PARTS[0], KEEPER="1", KEEPER_SKUS=keeper_skus,
               KEEPER_STATE=KEEPER_STATE, KEEPER_CMD=KEEPER_CMD,
               PROFILE="billy01", DRY_RUN="", ADD_MODE="http",
               PROXY_PORT="7891", DISPLAY=":99", RUN_URL="",
               VNC_URL=os.environ.get("VNC_URL", ""),
               VNC_PW=(os.environ.get("VNC_PW")
                       or (open("/tmp/vncpw").read().strip()
                           if os.path.exists("/tmp/vncpw") else "")))
    global KEEPER_PROC
    proc = subprocess.Popen(
        ["python3", "checkout.py"], cwd=KEEPER_ENGINE_DIR, env=env,
        stdout=open("/tmp/keeper/keeper.log", "w"), stderr=subprocess.STDOUT)
    KEEPER_PROC = proc
    log("keeper 已啟動（pid %d）— 預熱結帳會話待命（袋內候選 %s）"
        % (proc.pid, keeper_skus))
    return True


def local_checkout(sku, store, nodes):
    """命中後免等新 runner：本 job 就地跑結帳引擎（家寬出口 7891，mihomo BUY 組釘死）。
    回傳 True=引擎已實際執行（不論成交與否，不再重複派工）；False=環境不備，交回派工路徑。"""
    import shutil
    import subprocess
    pat = os.environ.get("GH_PAT", "").strip()
    if not pat or not os.environ.get("PROFILES_JSON", "").strip():
        log("本地結帳缺 GH_PAT/PROFILES_JSON secret — 走派工")
        return False
    if subprocess.run(["pgrep", "-f", "Xvfb :99"],
                      capture_output=True).returncode != 0:
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x900x24"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
    tmp = FALLBACK_ENGINE_DIR
    shutil.rmtree(tmp, ignore_errors=True)
    r = subprocess.run(
        ["git", "clone", "-q", "--depth", "1",
         "https://x-access-token:%s@github.com/TommyYeung660/buyip18-checkout" % pat, tmp],
        capture_output=True, text=True)
    if r.returncode != 0:
        log("clone checkout 失敗: " + (r.stderr or "")[-70:])
        return False
    env = dict(os.environ, SKU=sku, STORE=store, PROFILE="billy01",
               DRY_RUN="", ADD_MODE="http", PROXY_PORT="7891",
               DISPLAY=":99", RUN_URL="",
               VNC_URL=os.environ.get("VNC_URL", ""),
               VNC_PW=(os.environ.get("VNC_PW")
                       or (open("/tmp/vncpw").read().strip()
                           if os.path.exists("/tmp/vncpw") else "")))
    log("本地結帳引擎啟動（命中就地執行，家寬出口 7891）")
    try:
        r = subprocess.run(["python3", "checkout.py"], cwd=tmp, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=900)
    except subprocess.TimeoutExpired as t:
        log("本地結帳引擎逾時（15 分）— 視為已執行不重複派工")
        return True
    open(os.path.join(tmp, "engine-run.log"), "w").write(r.stdout or "")
    for line in (r.stdout or "").strip().splitlines()[-12:]:
        log("引擎| " + line)
    log("本地結帳引擎結束 exit=%d" % r.returncode)
    return True


def pushover(title, body, priority=0):
    """Pushover（Android 主通道）。priority>=2=緊急：每 30 秒重響最長 3 小時直至確認。"""
    token = (os.environ.get("PUSHOVER_TOKEN") or "").strip()
    user = (os.environ.get("PUSHOVER_USER") or "").strip()
    if not token or not user:
        return
    d = {"token": token, "user": user, "title": title[:250],
         "message": body[:1000], "priority": str(priority),
         "sound": "siren" if priority >= 2 else "bugle"}
    if priority >= 2:
        d["retry"] = "30"
        d["expire"] = "10800"
    try:
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=urllib.parse.urlencode(d).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            log("Pushover 已送出 http=%d" % r.status)
    except Exception as e:
        log("Pushover 失敗: " + str(e)[:80])


def bark(title, body, priority=0):
    """通知分發：Bark（iOS，未設跳過）+ Pushover（未設跳過），兩通道並行。"""
    url = (os.environ.get("BARK_URL") or "").strip()
    if url:
        try:
            t = urllib.parse.quote(title)
            b = urllib.parse.quote(body)
            http(url.rstrip("/") + "/%s/%s?group=iPhone18&level=timeSensitive"
                 "&sound=alarm" % (t, b), timeout=10, proxy=False)
            log("Bark 已送出")
        except Exception as e:
            log("Bark 失敗: " + str(e)[:80])
    pushover(title, body, priority)


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
    keeper_on = keeper_start(nodes)
    last_kstart = time.time()
    sweep = 0
    while now_min() < MAX_MINUTES:
        sweep += 1
        # keeper 進程退出（判死自曝/讓位/崩潰）→ 冷卻 120s 即時重開，盲窗壓最短
        if (keeper_on and KEEPER_PROC is not None
                and KEEPER_PROC.poll() is not None
                and time.time() - last_kstart > 120):
            last_kstart = time.time()
            log("keeper 進程已退（exit=%s）— 即時重開" % KEEPER_PROC.returncode)
            keeper_on = keeper_start(nodes)
        try:
            hits = sweep_once(nodes)
        except RuntimeError as e:
            log("全部節點失效，10 分鐘後重試: %s" % e)
            node_dead.clear()  # 清冷卻讓節點復活（三振次數保留）
            time.sleep(600)
            continue
        if hits:
            # 命中收工的 run 生命常 <10 分（watchdog 班次晨間即中彈）——
            # 留 marker 讓 Self-resurrect 豁免防抖，免等 cron 遲到的 watchdog
            open("/tmp/hit-marker", "w").write("1")
            # SKU 優先序照 PARTS 定義（候選偏好），非字母序
            sku = next((p for p in PARTS if p in hits), sorted(hits)[0])
            # 結帳頁靠顯示名稱（Apple {店名}）點選，送店名；缺名時退回店號
            store = hits[sku][0][1]
            if not store:
                store = hits[sku][0][0]
            store_code = hits[sku][0][0]  # R###（方案 B HTTP selectStore 用）
            detail = "; ".join("%s@%s(%s)" % (s, n, q)
                               for s, n, q in [(h[1], h[0], h[2])
                                               for h in hits[sku]][:3])
            log("★ 有貨！ %s → %s" % (sku, detail))
            bark("iPhone 18 有貨！",
                 "%s @ %s — 自動下單已觸發" % (sku, detail[:60]), priority=2)
            # 優先序：keeper 駐場會話（秒級）→ 本地結帳（~60-90s）→ 派工（~3 分）
            if keeper_on:
                st = keeper_state()
                if not st:
                    log("keeper 預熱中 — 等待就緒（最長 100 秒）")
                    t0 = time.time()
                    while time.time() - t0 < 100:
                        st = keeper_state()
                        if st:
                            break
                        time.sleep(2)
                if st and st.get("state") == "ready":
                    keeper_fire(sku, store, store_code)
                    res = keeper_wait(600)
                    log("keeper 結果: %s" % json.dumps(res, ensure_ascii=False)[:140])
                    bark("iPhone 18 下單結果",
                         "%s %s" % (res.get("state", "?"),
                                    str(res.get("result", ""))[:60]), priority=1)
                    # 只在 keeper 真跑出結帳結果才收工；keeper 失敗（no-pickup/
                    # mismatch/dead/逾時）落到後備鏈（9/18 晨發現的設計缺陷修正）
                    if res and res.get("state") in ("ordered", "declined", "dry-run"):
                        return 0
                    if res and res.get("state") == "no-pickup":
                        # 全店無額已被 keeper HTTP 證實——8 分鐘後備引擎只會重演，
                        # 就地重開 keeper 續掃（盲窗 ~90 秒 vs 後備 8 分）
                        log("keeper no-pickup（全店無額）— 免後備，就地重開續掃")
                        keeper_on = keeper_start(nodes)
                        continue
                    log("keeper 未成（%s）— 落後備鏈" % res.get("state", "?"))
                if st:
                    log("keeper 狀態=%s 不可用 — 走後備" % st.get("state"))
            ran = False
            try:
                ran = local_checkout(sku, store, nodes)
            except Exception as e:
                log("本地結帳例外: %s" % str(e)[:80])
            if not ran:
                try:
                    dispatch_checkout(sku, store)
                except Exception as e:
                    log("dispatch 失敗: %s" % e)
                    bark("dispatch 失敗", str(e)[:80], priority=1)
            return 0
        if sweep % 20 == 0:
            log("sweep %d | %0.1f 分 | 存活節點 %d | 無貨 | keeper=%s"
                % (sweep, now_min(),
                   len([n for n in nodes
                        if node_dead.get(n, 0) <= time.time()
                        and node_strikes.get(n, 0) < NODE_MAX_STRIKES]),
                   (keeper_state() or {}).get("state", "-")))
        time.sleep(random.uniform(*SWEEP_PAUSE))
    log("到時收工，等 watchdog 重啟")
    return 0


def status_pusher():
    """引擎 status/latest.json（keeper+本地後備取較新者）即時推送 checkout repo（private）。
    掃描 job 沒有 git commit 步驟，主控台讀 repo 檔案——靠此線程讓狀態/VNC 秒級可見。"""
    import base64
    pat = (os.environ.get("GH_PAT") or "").strip()
    if not pat:
        log("status 推送停用（缺 GH_PAT）")
        return
    api = ("https://api.github.com/repos/TommyYeung660/buyip18-checkout"
           "/contents/status/latest.json")
    heads = {"Authorization": "Bearer " + pat,
             "Accept": "application/vnd.github+json",
             "User-Agent": "buyip18-scanner"}
    last = None
    while True:
        try:
            cand = None
            for d in (os.path.join(KEEPER_ENGINE_DIR, "status", "latest.json"),
                      os.path.join(FALLBACK_ENGINE_DIR, "status", "latest.json")):
                try:
                    if os.path.exists(d) and (cand is None or
                            os.path.getmtime(d) > os.path.getmtime(cand)):
                        cand = d
                except Exception:
                    pass
            if cand:
                body = open(cand, "rb").read()
                if body != last:
                    sha = None
                    try:
                        req = urllib.request.Request(api, headers=heads)
                        with urllib.request.urlopen(req, timeout=15) as r:
                            sha = json.loads(r.read()).get("sha")
                    except Exception:
                        pass  # 404=檔案未建，直接建立
                    payload = {"message": "status: live from scanner",
                               "content": base64.b64encode(body).decode(),
                               "branch": "main"}
                    if sha:
                        payload["sha"] = sha
                    req = urllib.request.Request(
                        api, data=json.dumps(payload).encode(),
                        headers=heads, method="PUT")
                    with urllib.request.urlopen(req, timeout=15) as r:
                        log("status 已推送 checkout repo http=%d" % r.status)
                    last = body
        except Exception as e:
            log("status 推送失敗: " + str(e)[:80])
            time.sleep(10)
        time.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=status_pusher, daemon=True).start()
    sys.exit(main())
