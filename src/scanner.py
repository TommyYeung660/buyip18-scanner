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

# 9/19 用戶放寬：256GB+512GB+1TB 都買（布根地紅/冰川色）——2TB 維持不掃不購
PARTS = ["MJXQ4ZA/A", "MJXR4ZA/A", "MJXV4ZA/A", "MJXW4ZA/A",
         "MJY04ZA/A", "MJY14ZA/A"]
MAX_PARTS = 3                       # pickup-message 上限，>3 會 541
PER_NODE_GAP_S = 15                 # 每節點兩次請求的最小間距（共享出口禮貌）
NODE_COOLDOWN_S = 900               # 節點單次失敗的冷卻秒數（15 分）
NODE_MAX_STRIKES = 3                # 累計失敗達此數 → 三振出局（該 job 內不再使用）
SWEEP_PAUSE = (2.0, 4.0)            # 每輪掃描間的基礎停頓
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "320"))
# 命中冷卻：同 (SKU, 門市) 在冷卻期內不再處理。
# 動機：命中 miss 之後不再拆艦隊（改 continue 續掃），但同一批庫存會被下一輪
# sweep 立刻再偵測到——沒有冷卻就會反覆觸發 bark（priority 2 需人工確認）＋
# 反覆跑後備引擎燒出口。冷卻長度對齊 keeper slot 的 exit 43 後重建時間（~2-3 分）。
HIT_COOLDOWN_S = int(os.environ.get("HIT_COOLDOWN_S", "300"))
MIHOMO_API = "http://127.0.0.1:9090"
PROXY = "http://127.0.0.1:7890"

START = time.time()
node_last_used = {}
node_dead = {}                      # name → 冷卻截止 unix-ts（過期即復活）
node_strikes = {}                   # name → 累計失敗次數（三振出局）
# 掃描心跳：每 HB_S 秒往帳本寫一列「這一輪掃了幾個節點、幾個已冷卻、命中幾個」。
# 為什麼需要：9/25 整窗 0 命中時，帳本裡**沒有任何掃描器健康的訊號**（哨兵通道自
# 9/22 起靜止），分不出「真沒貨」與「偵測壞了」——而後者是會靜默賠掉命中的失效模式。
# 放帳本而不是 status/latest.json：後者是「內容有變才推」，加一個每次掃描都變的
# 欄位會變成每 2-3 秒推一次、打爆 GitHub API；帳本 15 分鐘一列＝4 列/小時。
HB_S = int(os.environ.get("SCANNER_HB_S", "900"))
_hb_last = [0.0]


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


# ---------------------------------------------------------------- 駐場結帳（KEEPER 艦隊）
# 每個 SKU 一個駐場 slot：袋純一 SKU×BUY_COUNT、各自預熱會話停 review+RETAIL 待命。
# 命中哪個 SKU 就叫哪個 slot 就地選店下單（2-6 秒）——**永不需要換袋**
# （9/19 用戶：單 keeper 不中要換袋＝幾乎必敗；6 SKU→6 slot 全覆蓋）。
# 資源：每 slot ≈ 400MB Chromium（runner 16GB 充足）；分兩波錯開啟動免搶 CPU。
KEEPER_BASE = "/tmp/keeper"                      # slot 目錄 /tmp/keeper/k{i}/
KEEPER_ENGINE_BASE = "/tmp/checkout-engine"      # 基礎 clone（各 slot copytree 複製）
FALLBACK_ENGINE_DIR = "/tmp/checkout-fallback"   # 命中重走的後備引擎（勿與 keeper 共用目錄）
KEEPER_FLEET = list(PARTS)[:max(1, int(os.environ.get("KEEPER_SLOTS", "6") or 6))]
# 「武裝待命」的狀態字串。keeper 端現在統一寫 "ready"（停泊資訊走 park_store
# 正交欄位），這裡仍寬容接受 "parked"：狀態字串是跨檔案的契約，舊/新引擎混用
# 時若漏認，slot 會靜默退出派工並被建袋看門狗殺掉重開——代價太大，不值得省這行。
KEEPER_ARMED = ("ready", "parked")
# per-slot 專屬出口（供應商無關）：SLOT_PROXIES_JSON 第 i 項 → slot i 專用 listener
# 7892+i（scanner.yml 生成）；沒配到的 slot 或無 secret → 沿用免費 BUY 池 7891
try:
    SLOT_PROXY_COUNT = len(json.loads(os.environ.get("SLOT_PROXIES_JSON") or "[]"))
except Exception:
    SLOT_PROXY_COUNT = 0


def keeper_port(slot):
    return 7892 + slot if slot < SLOT_PROXY_COUNT else 7891


# 多卡就緒：SLOT_PROFILES（逗號分隔）第 i 項 → slot i 用哪個付款 profile
# （PROFILES_JSON 內多 profile；未設＝全部 billy01）。卡 2 到貨：加 profile 檔＋
# 設 secret 即可，無需改碼。
_SLOT_PROFILES = [p.strip() for p in
                  (os.environ.get("SLOT_PROFILES") or "").split(",") if p.strip()]


def keeper_profile(slot):
    return _SLOT_PROFILES[slot] if slot < len(_SLOT_PROFILES) else "billy01"


KEEPER_PROCS = {}                                # slot → Popen
KEEPER_LASTSTART = {}                            # slot → unix ts（冷卻用）


def _kslot_dir(i):
    d = os.path.join(KEEPER_BASE, "k%d" % i)
    os.makedirs(d, exist_ok=True)
    return d


def keeper_paths(i):
    d = _kslot_dir(i)
    return (os.path.join(d, "state.json"), os.path.join(d, "command.json"))


def keeper_state(i):
    try:
        return json.load(open(keeper_paths(i)[0]))
    except Exception:
        return None


def keeper_slot_for(sku):
    s = (sku or "").strip().upper()
    for i, p in enumerate(KEEPER_FLEET):
        if p.upper() == s:
            return i
    return None


def keeper_fire(i, sku, store, store_code=""):
    _, cmd = keeper_paths(i)
    json.dump({"action": "buy", "sku": sku, "store": store,
               "store_code": store_code},
              open(cmd, "w"), ensure_ascii=False)
    log("keeper%d 命令已下（%s @ %s/%s）— 等待結果"
        % (i, sku, store, store_code or "?"))


def keeper_fire_park(i, store_code, store="", reason="post-hit"):
    """動態停泊命令：叫第 i 個 slot 把停泊狀態推進到「已選店 + contact + billing」。

    何時發：命中後立刻（該店剛出過貨，庫存可能還在，selectStore 最有機會成功）。
    停泊建立需要庫存——非窗口期做不出來（selectStore 會 302 sorry），所以只能
    趁命中後補發。見 docs/DYNAMIC_PARKING_DESIGN.md。
    """
    _, cmd = keeper_paths(i)
    json.dump({"action": "park", "store_code": (store_code or "").upper(),
               "store": store, "reason": reason},
              open(cmd, "w"), ensure_ascii=False)
    log("keeper%d 停泊命令已下（%s / %s）" % (i, store_code or "?", reason))


def keeper_wait(i, timeout_s):
    """輪詢第 i 個 keeper 的 state 直到真終態或逾時。
    進程死而無終態（裸崩沒寫 state）→ ~2 秒退回 keeper-failed，防 600 秒乾等。
    其餘 slot 不受影響（多 keeper 各自獨立）。"""
    t0 = time.time()
    last = None
    dead_polls = 0
    proc = KEEPER_PROCS.get(i)
    while time.time() - t0 < timeout_s:
        last = keeper_state(i)
        if last and last.get("state") not in (
                KEEPER_ARMED + ("buying", "selected", "keeper-swapping")):
            return last
        if proc is not None and proc.poll() is not None:
            dead_polls += 1
            if dead_polls >= 7:
                return {"state": "keeper-failed",
                        "reason": "proc-dead-no-terminal"}
        else:
            dead_polls = 0
        time.sleep(0.3)  # 命令拾取與結果回傳都快（原 3s）
    return last or {"state": "timeout"}


def keeper_slots_view():
    """回傳 (slots_dict, 武裝數)：latest.json 的 per-slot 可觀測性欄位。

    每個 slot 的字串：
      `-`              沒有狀態檔（重建中 / 剛被殺）
      `ready`          武裝待命，沒有停泊
      `ready@R409`     停泊在 R409（快路徑條件：命中 R409 時只剩 placeOrder）
      `ready!R409/原因` 嘗試停泊 R409 但失敗 ← **一定要看得見**：停泊必然發生在
                       keeper 重建之後，所以帳本上那一發命中的 park 欄位一定是空的；
                       若失敗不留痕，「停泊失敗」與「從未嘗試」在 latest.json 上
                       一模一樣，影子模式將無法歸因（9/22 第一發命中就是這樣）。

    抽成獨立函式是為了可測：這段原本內嵌在 status_pusher 的 try/except 裡，
    任何例外都會被吞掉、讓 latest.json 整個 slots 欄位消失＝靜默失去艦隊可觀測性。

    永不拋出例外：任何單一 slot 讀不到就退化為 '-'，不影響其他 slot。
    """
    slots = {}
    for i in range(len(KEEPER_FLEET)):
        try:
            st = keeper_state(i) or {}
            sv = str(st.get("state") or "-")
            if st.get("park_store"):
                sv += "@" + str(st["park_store"])
            elif st.get("park_try"):
                sv += "!" + str(st["park_try"])
                if st.get("park_fail"):
                    sv += "/" + str(st["park_fail"])[:16]
        except Exception:
            sv = "-"
        slots[str(i)] = sv
    # 武裝判準與派工一致（KEEPER_ARMED）：先前誤寫成 "review"，結果 6/6 全武裝時
    # 卻報 slots_ready=0 —— 這種「健康卻顯示 0」的指標比沒有更危險。
    ready = sum(1 for v in slots.values()
                if v.split("@")[0].split("!")[0] in KEEPER_ARMED)
    return slots, ready


def keeper_ready_count():
    n = 0
    for i in range(len(KEEPER_FLEET)):
        p = KEEPER_PROCS.get(i)
        if p is not None and p.poll() is None \
                and (keeper_state(i) or {}).get("state") in KEEPER_ARMED:
            n += 1
    return n


def keeper_start(nodes, slot):
    """啟動第 slot 個駐場引擎（純袋 = KEEPER_FLEET[slot]）。回傳 True=已起。"""
    import shutil
    import subprocess
    pat = os.environ.get("GH_PAT", "").strip()
    if not pat or not os.environ.get("PROFILES_JSON", "").strip():
        log("keeper 未啟用（缺 GH_PAT/PROFILES_JSON secret）")
        return False
    if not [n for n in nodes if "家宽" in n and "香港" in n]:
        log("keeper 未啟用（節點池無香港家寬）")
        return False
    if not os.path.exists(os.path.join(KEEPER_ENGINE_BASE, "checkout.py")):
        shutil.rmtree(KEEPER_ENGINE_BASE, ignore_errors=True)
        r = subprocess.run(
            ["git", "clone", "-q", "--depth", "1",
             "https://x-access-token:%s@github.com/TommyYeung660/buyip18-checkout" % pat,
             KEEPER_ENGINE_BASE],
            capture_output=True, text=True)
        if r.returncode != 0:
            log("keeper 基礎 clone 失敗: " + (r.stderr or "")[-70:])
            return False
    eng = os.path.join(_kslot_dir(slot), "engine")
    shutil.rmtree(eng, ignore_errors=True)
    try:
        shutil.copytree(KEEPER_ENGINE_BASE, eng, symlinks=True)
    except Exception as e:
        log("keeper%d 引擎複製失敗: %s" % (slot, repr(e)[:60]))
        return False
    state_p, cmd_p = keeper_paths(slot)
    try:
        os.remove(state_p)
    except Exception:
        pass
    # 命令檔不能無條件刪——**停泊命令正是要交給重建後的新 keeper 執行的**：
    # 命中失敗時 keeper 會退出，而 keeper_fire_park() 是在 keeper_wait() 之後才
    # 下命令，所以命令下達的當下該 slot 已經沒有 keeper 在讀。下一輪
    # keeper_restart_dead() → keeper_start() 若把命令檔刪掉，停泊命令就永遠
    # 沒有收件人（9/22 開影子模式前查出來的：整條停泊路徑會靜默空轉、零停泊）。
    # 但 buy 命令**必須**刪：新 keeper 重播一個舊 buy ＝重複下單。
    # 故只保留 park——它純推進結帳狀態、永不送 placeOrder，重播安全。
    _keep_cmd = False
    try:
        with open(cmd_p, encoding="utf-8") as f:
            _keep_cmd = (json.load(f) or {}).get("action") == "park"
    except Exception:
        _keep_cmd = False
    if not _keep_cmd:
        try:
            os.remove(cmd_p)
        except Exception:
            pass
    sku = KEEPER_FLEET[slot]
    env = dict(os.environ, SKU=sku, KEEPER="1", KEEPER_SKUS=sku,
               KEEPER_STATE=state_p, KEEPER_CMD=cmd_p,
               PROFILE=keeper_profile(slot), DRY_RUN="", ADD_MODE="http",
               PROXY_PORT=str(keeper_port(slot)), DISPLAY=":99", RUN_URL="",
               VNC_URL=os.environ.get("VNC_URL", ""),
               VNC_PW=(os.environ.get("VNC_PW")
                       or (open("/tmp/vncpw").read().strip()
                           if os.path.exists("/tmp/vncpw") else "")))
    proc = subprocess.Popen(
        ["python3", "checkout.py"], cwd=eng, env=env,
        stdout=open(os.path.join(_kslot_dir(slot), "keeper.log"), "w"),
        stderr=subprocess.STDOUT)
    KEEPER_PROCS[slot] = proc
    KEEPER_LASTSTART[slot] = time.time()
    log("keeper%d 已啟動（pid %d）— 純袋 %s 預熱待命（出口 %d%s）"
        % (slot, proc.pid, sku, keeper_port(slot),
           "=專屬 IP" if keeper_port(slot) != 7891 else "=免費 BUY 池"))
    return True


def keeper_start_all(nodes):
    """兩波錯開啟動（6 個 Chromium 同時起跑會搶 CPU/觸發風控）。"""
    ok = 0
    fleet = KEEPER_FLEET
    for wave in (range(0, 3), range(3, len(fleet))):
        for i in wave:
            if keeper_start(nodes, i):
                ok += 1
            time.sleep(8)
    log("keeper 艦隊啟動：%d/%d 個（覆蓋 %s）"
        % (ok, len(fleet), ",".join(fleet)))
    return ok


KEEPER_FAILS = {}   # slot -> {"n": 連續開機即死次數, "until": 退避到期, "said": 已記錄到第幾次}
# 在途的「原地換 session」刷新命令：slot -> 發出時刻。看門狗＝超過
# REFRESH_TIMEOUT_S 仍未回武裝 ⇒ 退回「殺掉重建」（keeper 生命週期外的保險）。
KEEPER_REFRESH_SENT = {}
REFRESH_TIMEOUT_S = 180
# 原地換 session 成功的時刻（slot -> ts）。⛔ 9/25 實證明確：refresh 是**同一個進程**
# 的工作，不會更新 KEEPER_LASTSTART ⇒ 若不另外記「最近一次換 session」，slot 的齡
# 永遠 > 門檻 ⇒ 掃描器每一輪都再發一次 refresh（實測 slot2 在 100 秒內被發 5 次、
# slot3 三分鐘 6 次）⇒ 無限刷新迴圈：該 slot 永遠武裝不起來（最後 refresh-exhausted
# → exit 43），其他 slot 則因 refresh 名額被佔而等到會話過期。**換 session 必須
# 等價於「齡歸零」**才能讓這條路徑收斂。
KEEPER_RENEWED = {}


def _fleet_armed():
    """目前武裝中的 slot 數（斷路器安全閥用）。"""
    n = 0
    for j in range(len(KEEPER_FLEET)):
        p = KEEPER_PROCS.get(j)
        if p is None or p.poll() is not None:
            continue
        if (keeper_state(j) or {}).get("state") in KEEPER_ARMED:
            n += 1
    return n


def keeper_boot_break(i, age, prev, exit_code, short_life=300, base=120, cap=1800):
    """開機即死的斷路器：回傳 True＝正在退避、這一輪不要重開。

    為什麼（9/23 16:00 實證）：slot 0 的出口連商品頁都載不到
    （`Page.goto: net::ERR_CONNECTION_CLOSED`），它以 ~2 分鐘為週期無限重開
    （存活中位數 122-134 秒、3.5 小時 28 次死亡／22 次 recycle），卻永遠武裝
    不起來——持續燒 runner 資源與出口連線，整隊陪它一起耗。

    判準：存活 < short_life（300 秒）＝**從未武裝就死**；但「已做過事」的死
    不算（買入失敗／會話快驗失敗／sku 不符等都代表它武裝過）——那些要立刻重開。
    退避＝base × 2^(n-1)，上限 cap。活過 short_life 即歸零。
    ⛔ 安全閥：全隊 0 個武裝時一律只用 base（120 秒）持續試探——否則一次短暫
    的網路抖動會讓 6 個 slot 一起退避到 30 分鐘，反而比今天更難恢復。
    """
    st = KEEPER_FAILS.setdefault(i, {"n": 0, "until": 0.0, "said": 0,
                                     "life": None, "seen": 0.0})
    _r = str(prev.get("reason") or "")
    _worked = (_r.startswith(("http-buy", "buy-", "probe-exc", "sku-mismatch",
                              "advance-failed", "keepalive"))
               or bool(prev.get("buy_steps")))
    # ⚠ 同一個死進程會被每一輪迴圈重新觀察到，而 age（＝now − 上次啟動）會一路變大。
    # 9/23 晚的實作就是踩到這點：fails 每個 pass 都 +1（一路到 12）、直到 age 跨過
    # short_life 才歸零重開 → 退避從來沒有指數成長（實際等於固定 ~5 分鐘），而且
    # 每 ~10 秒噴一筆帳本（一夜 656 筆）。修法＝**記住這次死亡**：
    # life 在「第一次看到它死」時定案，之後的 pass 不重複計數、也不再寫事件。
    _first = st["life"] is None          # 這次死亡是不是第一次被看到
    if _first:
        st["life"] = age if age > 0 else 0
        st["seen"] = time.time()
    _life = st["life"]
    if _life >= short_life or _worked:
        st.update({"n": 0, "until": 0.0, "said": 0, "life": None})
        return False
    if _first:                                     # 只在第一次觀察時計數一次
        st["n"] += 1
        st["said"] = st["n"]
        _wait = base if _fleet_armed() == 0 else min(base * 2 ** (st["n"] - 1), cap)
        st["until"] = st["seen"] + _wait
        log("keeper%d 連續 %d 次開機即死（exit=%s 壽命 %ds reason=%s）— 退避 %ds 後再試"
            % (i, st["n"], exit_code, int(_life), _r[:30] or "-", _wait))
        hit_ledger(event="keeper-restart", slot=i, sku=KEEPER_FLEET[i],
                   state="backoff", exit=exit_code, prev=str(prev.get("state") or ""),
                   reason=_r[:70], up=int(_life), fails=st["n"],
                   backoff_s=_wait, restarted=False)
    if time.time() >= st["until"]:
        st["life"] = None          # 退避到期 → 放行重開，下次死亡重新起算
        return False
    return True


def keeper_restart_dead(nodes, cooldown=120, build_deadline=420):
    """逐 slot 自癒：①進程死（過冷卻）→ 重開 ②進程活但卡住（建袋超 7 分未停泊，
    如 Playwright wedge）→ 殺掉重開。9/19 首次艦隊實證：2 個 slot 卡在建袋期
    永不自曝（進程活著、狀態檔無更新），死進程規則救不到。"""
    n = 0
    for i in range(len(KEEPER_FLEET)):
        proc = KEEPER_PROCS.get(i)
        if proc is None:
            continue
        age = time.time() - KEEPER_LASTSTART.get(i, 0)
        if proc.poll() is not None:
            _prev = keeper_state(i) or {}
            if keeper_boot_break(i, age, _prev, proc.returncode):
                continue                 # 開機即死 → 退避中（見 keeper_boot_break）
            if age <= cooldown:
                continue
            log("keeper%d 進程已退（exit=%s）— 即時重開" % (i, proc.returncode))
            # 帳本留痕：slot 死亡原本完全不入帳（帳本只記命中路徑事件），而
            # in-progress run 的 log 讀不到 → 「為什麼這個 slot 沒被派到」「重建
            # spell 從何而來」結構上答不出（9/23 08:47:31 slot4 那次「ready 一瞬
            # 即逝」＝補位後又死一次，只能靠 latest.json 時間線猜）。終態檔多半
            # 還在，把它的 state/reason 一起抄進來即可歸因。
            _ok = keeper_start(nodes, i)
            KEEPER_FAILS.setdefault(i, {}).update({"life": None})
            hit_ledger(event="keeper-restart", slot=i, sku=KEEPER_FLEET[i],
                       state="dead", exit=proc.returncode,
                       prev=str(_prev.get("state") or ""),
                       reason=str(_prev.get("reason") or "")[:70],
                       up=int(age), fails=KEEPER_FAILS.get(i, {}).get("n", 0),
                       restarted=bool(_ok))
            if _ok:
                n += 1
            continue
        st = keeper_state(i)
        if st and st.get("state") in KEEPER_ARMED:
            continue                     # 已停泊待命=正常
        # ⛔ 9/25 實證：正在**原地換 session** 的 slot 不可被建袋看門狗殺掉。
        # 這裡比的是 age＝「進程年齡」，而 refresh 是**同一個進程**的工作——那個
        # 進程必然已活 >build_deadline（keeper 待命 18 分鐘才換 session），
        # 所以它會被誤判成「建袋卡住」而殺掉：帳本實測 refresh 後 10-17 秒就
        # `state=build-timeout, prev='refreshing'`，換 session 永遠做不完，
        # 而且每 1-2 分鐘賠一個 slot 的重建（fleet 掉到 4/6）。
        # 它由主動回收的看門狗（REFRESH_TIMEOUT_S=180 秒 → 殺掉重建）負責。
        if i in KEEPER_REFRESH_SENT:
            continue
        if age > build_deadline:
            log("keeper%d 建袋超時（%ds 未停泊）— 殺掉重開" % (i, int(age)))
            try:
                proc.kill()
            except Exception:
                pass
            _ok = keeper_start(nodes, i)
            hit_ledger(event="keeper-restart", slot=i, sku=KEEPER_FLEET[i],
                       state="build-timeout", prev=str((st or {}).get("state") or ""),
                       reason=str((st or {}).get("reason") or "")[:70],
                       up=int(age), restarted=bool(_ok))
            if _ok:
                n += 1
    return n


def keeper_recycle_old(nodes, max_age_s):
    """主動錯開回收：在會話失明前，把太老的 keeper 逐一退掉重建。

    為什麼需要（9/23 實測）：keeper 的會話在齡滿 ~25 分鐘後失明——保活連續 3 次
    例外即自曝退出（exit 43）。證據是兩個死亡波次的年齡幾乎相同（1448-1535 秒），
    而第二波那批 process 是 09:42-09:46 才啟動的：若是「某個 wall-clock 外部事件」，
    它們應該在不同年齡死。被動等它自己死＝整隊同刻全滅（實測 5 個 slot 在 4 分鐘內
    一起死、可用率掉到 2/6，還有一發命中撞上已失明的會話、鏈一步都沒跑）。
    主動回收把「同刻全滅」換成「一次一個」。

    ⛔ 安全閘（**逐 slot** 判斷，任一不成立就跳過該 slot）：
      - 該 slot 在 `buying`（placeOrder 可能在途）→ 絕不動
      - 該 slot 進程已死 → 交給 keeper_boot_break／keeper_restart_dead
      - 該 slot 未武裝（建袋中／已失效）→ 沒東西可換
    另加「最多 2 個 refresh 在途」的節制（避免同時大量重填袋）。
    ⚠ 9/25 修正：這三條原本是**艦隊級**（任何 slot 死了／武裝 <5 就整輪不動），
    結果 refresh 被延到齡 1193-1544 秒才發、**已超過會話 20.3 分鐘 TTL**，
    必然失敗（refresh-exhausted → exit 43 → 重建）＝機制白做。
    回傳實際回收數。
    """
    if max_age_s <= 0:
        return 0
    now = time.time()
    for j in list(KEEPER_REFRESH_SENT):          # 刷新成功的清掉在途記錄
        if (keeper_state(j) or {}).get("state") in KEEPER_ARMED:
            KEEPER_REFRESH_SENT.pop(j, None)
            KEEPER_RENEWED[j] = time.time()      # 換 session 成功 ⇒ 齡歸零（見上）
    cands = sorted(range(len(KEEPER_FLEET)),
                   key=lambda j: KEEPER_LASTSTART.get(j, 0))
    # ⚠ 9/25 實證修正：原本這裡是**艦隊級**閘（任何 slot 進程死了、或武裝數 <5、
    # 或有 slot 在 buying 就整輪不動），結果 refresh 命令被延到齡 1193-1544 秒
    # 才發——**已超過會話的 20.3 分鐘 TTL**，於是 refresh 必然失敗
    # （refresh-exhausted → exit 43 → 重建），整套機制白做。
    # 改成**逐 slot**判斷：只看該 slot 自己（進程死／未武裝／在 buying 就跳過），
    # 不再被其他 slot 的狀態拖累；只保留「最多 2 個 refresh 在途」的節制。
    _in_flight = len(KEEPER_REFRESH_SENT)
    for i in cands:
        # 齡 = 距「進程啟動」或「最近一次成功換 session」較近者（原地換 session 不重啟
        # 進程，只有這樣才會收斂；見 KEEPER_RENEWED 的宣告處）。
        age = now - max(KEEPER_LASTSTART.get(i, 0), KEEPER_RENEWED.get(i, 0))
        if age < max_age_s:
            continue
        # ⛔ 在途檢查必須排在「未武裝就跳過」**之前**：refresh 進行中的 slot 正是
        # 未武裝（狀態 refreshing），若先跳過它，看門狗（超時 → 殺掉重建）永遠
        # 到不了 ⇒ 卡住的 slot 只能等 keeper_restart_dead 的 420 秒建袋看門狗才被救。
        _sent = KEEPER_REFRESH_SENT.get(i)
        if _sent and now - _sent <= REFRESH_TIMEOUT_S:
            return 0                     # 命令已發，等它完成（看門狗下一輪再看）
        if _sent:
            log("keeper%d 原地換 session 超時（%.0fs）— 退回殺掉重建"
                % (i, now - _sent))
            KEEPER_REFRESH_SENT.pop(i, None)
            try:
                KEEPER_PROCS[i].kill()
            except Exception:
                pass
            _ok = keeper_start(nodes, i)
            hit_ledger(event="keeper-recycle", slot=i, sku=KEEPER_FLEET[i],
                       up=int(age), state="refresh-timeout", restarted=bool(_ok))
            return 1 if _ok else 0
        p_i = KEEPER_PROCS.get(i)
        if p_i is None or p_i.poll() is not None:
            continue                      # 該 slot 進程已死 → 交自癒（keeper_boot_break）
        s_i = (keeper_state(i) or {}).get("state")
        if s_i == "buying":
            continue                      # 該 slot 的 placeOrder 可能在途 → 絕不動
        if s_i not in KEEPER_ARMED:
            continue                      # 該 slot 未武裝（建袋中／已失效）→ 沒東西可換
        # ⚡ 原地換 session（9/24，用戶批准）：Apple 的 guest 結帳會話有 ~20.3 分鐘
        # 伺服器端 TTL（本機實測；過期時 Apple 把頁面導去 /sorry/session_expired），
        # 保活無法延長。原本這裡是「殺進程 + 重建」＝2-4 分鐘空窗，而每次命中
        # 都可能落在那段空窗（9/24 08:40 那發就是被它吃掉）；改成發 refresh 命令，
        # 讓 keeper 在同一進程內清 session 重新武裝（~20-40 秒）。
        # 看門狗：超過 REFRESH_TIMEOUT_S 仍未武裝 ⇒ 退回殺掉重建（保險）。
        if _in_flight >= 2:
            return 0                     # 最多 2 個 refresh 在途（避免同時大量重填袋）
        try:
            _, cmd_p = keeper_paths(i)
            json.dump({"action": "refresh", "reason": "age %.0f min" % (age / 60.0)},
                      open(cmd_p, "w"), ensure_ascii=False)
        except Exception as e:
            log("keeper%d 刷新命令寫入失敗：%s — 退回殺掉重建" % (i, repr(e)[:50]))
            continue
        KEEPER_REFRESH_SENT[i] = now
        log("keeper%d 齡 %.1f 分 ≥ 上限 %.1f 分 — 發自我刷新命令（原地換 session）"
            % (i, age / 60.0, max_age_s / 60.0))
        hit_ledger(event="keeper-recycle", slot=i, sku=KEEPER_FLEET[i],
                   up=int(age), state="refresh-sent", restarted=False)
        return 0
    return 0


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
        log("引擎| " + _mask_secrets(line))
    log("本地結帳引擎結束 exit=%d" % r.returncode)
    return True


def _mask_secrets(line):
    """遮掉引擎輸出裡的敏感殘留後才回印到 job log。

    scanner repo 是 **public**，而本地引擎的 stdout 最後 12 行會被 log("引擎| ...")
    回印。引擎本身只印卡號末四碼與 CVV 的布林值（不印全號），但末四碼＋profile
    名稱出現在公開日誌仍是不必要的曝露——專案既有規則就是公開產物不帶帳單資訊。
    這裡做最後一道遮罩：任何 12 位以上數字串（卡號）、「尾四 N」、長 token 一律打碼。
    """
    import re as _re
    t = str(line)
    t = _re.sub(r"尾四\s*\d{2,}", "尾四 ****", t)
    t = _re.sub(r"\b\d{12,19}\b", "<CARD>", t)
    t = _re.sub(r"\b\d{3,4}\b(?=\s*(?:CVV|cvv))", "<CVV>", t)
    return t[:300]


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
def fresh_hits(hits, hit_cool, now=None):
    """濾掉冷卻期內的 (SKU, 門市)。

    hits 形狀：{sku: [(store_code, store_name, quote), ...]}
    hit_cool 形狀：{(sku, store_code): 到期 epoch}
    回傳 (fresh, suppressed)：fresh 同 hits 形狀；suppressed 是被濾掉的 key 集合。
    """
    now = time.time() if now is None else now
    fresh, suppressed = {}, set()
    for s, lst in hits.items():
        for h in lst:
            if hit_cool.get((s, h[0]), 0) > now:
                suppressed.add((s, h[0]))
                continue
            fresh.setdefault(s, []).append(h)
    return fresh, suppressed


def main():
    nodes_file = os.environ.get("SCAN_NODES_FILE", "/tmp/nodes.json")
    nodes = [n["name"] for n in json.load(open(nodes_file))]
    # 預先剔除已知壞節點（信息節點/V6 壞線）
    bad_kw = ["建议", "建議", "V6【", "剩餘", "剩余", "过期", "過期", "官網", "群組"]
    dropped = [n for n in nodes if any(k in n for k in bad_kw)]
    nodes = [n for n in nodes if not any(k in n for k in bad_kw)]
    log("剔除壞節點 %d 個，可用 %d 個" % (len(dropped), len(nodes)))
    log("掃描器啟動：節點 %d 個，最長 %d 分鐘" % (len(nodes), MAX_MINUTES))
    keeper_on = keeper_start_all(nodes) > 0
    sweep = 0
    hit_cool = {}   # (SKU, 店號) -> 冷卻到期 epoch
    _last_sup = ()  # 上次記錄的抑制集合（變化才記帳，免洗版）
    while now_min() < MAX_MINUTES:
        sweep += 1
        # keeper 艦隊自癒：逐 slot 檢查死亡進程（冷卻 120s）即時重開
        if keeper_on:
            keeper_restart_dead(nodes)
            # 主動錯開回收（KEEPER_RECYCLE_S，0=關）。會話齡滿 ~25 分鐘就失明
            # （9/23 兩波死亡年齡都 1448-1535 秒＝年齡觸發），被動等它自己死會
            # 讓整隊同刻重建；這裡在失明前逐個退掉，天然錯開。
            keeper_recycle_old(nodes, int(os.environ.get("KEEPER_RECYCLE_S", "0")))
        try:
            hits = sweep_once(nodes)
            if time.time() - _hb_last[0] > HB_S:
                _hb_last[0] = time.time()
                hit_ledger(event="sweep", nodes_total=len(nodes),
                           nodes_dead=sum(1 for n in nodes
                                          if node_dead.get(n, 0) > time.time()),
                           nodes_strike=sum(1 for n in nodes if node_strikes.get(n)),
                           hits_in_round=len(hits), cur_node=mask(_cur_node) if _cur_node else "")
                try:
                    push_hits_ledger()      # 心跳要即時可見，不等命中
                except Exception:
                    pass
        except RuntimeError as e:
            log("全部節點失效，10 分鐘後重試: %s" % e)
            node_dead.clear()  # 清冷卻讓節點復活（三振次數保留）
            time.sleep(600)
            continue
        if hits:
            # 冷卻過濾：只留不在冷卻期內的 (SKU, 門市)。全部被冷卻＝本輪不處理、
            # 續掃（艦隊保持武裝），以低頻率記帳免洗版。
            _fresh, _suppressed = fresh_hits(hits, hit_cool)
            if not _fresh:
                # 只在抑制集合變化時記一次（免每輪洗版）
                _k = tuple(sorted(_suppressed))
                if _k != _last_sup:
                    _last_sup = _k
                    _txt = ",".join("%s@%s" % x for x in _k)
                    log("命中仍在冷卻期（%s）— 續掃（不拆艦隊）" % _txt[:80])
                    hit_ledger(event="hit-cooldown", state="suppressed",
                               detail=_txt[:80])
                time.sleep(random.uniform(*SWEEP_PAUSE))
                continue
            _last_sup = ()
            hits = _fresh
            # SKU 優先序照 PARTS 定義（候選偏好），非字母序
            sku = next((p for p in PARTS if p in hits), sorted(hits)[0])
            # 結帳頁靠顯示名稱（Apple {店名}）點選，送店名；缺名時退回店號
            store = hits[sku][0][1]
            if not store:
                store = hits[sku][0][0]
            store_code = hits[sku][0][0]  # R###（方案 B HTTP selectStore 用）
            # 立刻把這一對放進冷卻：keeper exit 43 後需 ~2-3 分重建，期間重複偵測
            # 只會重複發緊急通知＋重複跑後備。其他門市／其他 SKU 不受影響。
            hit_cool[(sku, store_code)] = time.time() + HIT_COOLDOWN_S
            detail = "; ".join("%s@%s(%s)" % (s, n, q)
                               for s, n, q in [(h[1], h[0], h[2])
                                               for h in hits[sku]][:3])
            log("★ 有貨！ %s → %s" % (sku, detail))
            hit_ledger(event="hit", sku=sku, store=store, code=store_code,
                       detail=detail[:80])
            bark("iPhone 18 有貨！",
                 "%s @ %s — 自動下單已觸發" % (sku, detail[:60]), priority=2)
            # 優先序：命中 SKU 的專屬 keeper slot（秒級）→ 本地結帳（~60-90s）→ 派工（~3 分）
            if keeper_on:
                slot = keeper_slot_for(sku)
                st = keeper_state(slot) if slot is not None else None
                # 不等預熱：slot 未就緒（狀態 None=建袋中/剛崩）即走本地引擎——
                # 100 秒乾等只會把命中→下單拖成 160+ 秒（9/19 晨 #18/#19 敗因）；
                # 命中落在 slot 推進期時命令檔會被停泊後 0.3 秒拾取，無需等
                if slot is not None and st and st.get("state") in KEEPER_ARMED:
                    keeper_fire(slot, sku, store, store_code)
                    res = keeper_wait(slot, 600)
                    log("keeper%d 結果: %s（其餘 slot 照常待命）"
                        % (slot, json.dumps(res, ensure_ascii=False)[:140]))
                    _st0 = keeper_state(slot) or {}
                    hit_ledger(event="keeper", slot=slot, sku=sku,
                               state=(res or {}).get("state", "timeout"),
                               reason=(res or {}).get("reason", ""),
                               result=str((res or {}).get("result", ""))[:60],
                               parked_at=str(_st0.get("park_store") or "")[:8],
                               posts_used=int(_st0.get("posts") or 0),
                               # 本次買入的逐步耗時（checkout BUY_TRACE）——命中鏈驗收的
                               # 核心數字。latest.json 是滾動快照、in-progress log 讀不到，
                               # 只有抄進 append-only 帳本才永久可查。
                               buy_steps=str(_st0.get("buy_steps") or "")[:240],
                               # 導航鏈自證（checkout NAV_FACT）：nav＝導航次數、
                               # nav_skip＝沒導航的原因、nav_stk＝導航後 stk 有無換。
                               nav=_st0.get("nav"), nav_skip=str(_st0.get("nav_skip") or "")[:24],
                               nav_stk=_st0.get("nav_stk"), nav_ms=_st0.get("nav_ms"),
                               park_survived=_st0.get("park_survived"))
                    # 動態停泊：無論這次下單成敗，都趁熱把該店停起來。
                    # 只發命令、不改本次流程——keeper 下單後會自己重建再停泊。
                    if os.environ.get("KEEPER_PARK_STORE", "0").strip() == "1" \
                            and store_code:
                        try:
                            keeper_fire_park(slot, store_code, store)
                        except Exception as _e:
                            log("停泊命令失敗: " + repr(_e)[:60])
                    bark("iPhone 18 下單結果",
                         "%s %s" % (res.get("state", "?"),
                                    str(res.get("result", ""))[:60]), priority=1)
                    # 只在 keeper 真跑出結帳結果才收工；keeper 失敗落到後備鏈
                    if res and res.get("state") in ("ordered", "declined", "dry-run"):
                        # 只有真的產出結帳結果才收工。marker 讓 Self-resurrect 豁免
                        # 10 分防抖（命中收工的 run 生命常 <10 分）
                        open("/tmp/hit-marker", "w").write("1")
                        push_hits_ledger()  # 收工前同步沖帳（daemon 線程會被 exit 殺）
                        return 0
                    if res and res.get("state") == "no-pickup":
                        # 全店無額已被 keeper HTTP 證實——就地重開該 slot 續掃
                        log("keeper%d no-pickup（全店無額）— 免後備，就地重開續掃" % slot)
                        keeper_start(nodes, slot)
                        continue
                    # buying 逾時＝placeOrder 可能已在途——加時 180 秒等真終態；
                    # 仍不明則不落本地引擎（雙訂單風險），緊急通知交人工確認
                    if res and res.get("state") == "buying":
                        log("keeper%d 停在 buying — 加時 180 秒等終態（防重複下單）" % slot)
                        res = keeper_wait(slot, 180)
                    if res and res.get("state") == "buying":
                        bark("⚠️ 下單結果未明",
                             "keeper 停在 buying——不重複下單，請查 Apple 訂單/郵件",
                             priority=2)
                        open("/tmp/hit-marker", "w").write("1")
                        push_hits_ledger()
                        return 0
                    log("keeper%d 未成（%s）— 落後備鏈" % (slot, res.get("state", "?")))
                elif slot is None:
                    log("命中 SKU %s 無專屬 slot（艦隊外）— 落後備鏈" % sku)
                    hit_ledger(event="keeper-skip", sku=sku, state="no-slot")
                elif st:
                    log("keeper%d 狀態=%s 不可用 — 走後備" % (slot, st.get("state")))
                    hit_ledger(event="keeper-skip", slot=slot, sku=sku,
                               state=st.get("state"))
                else:
                    # slot 有、但沒有狀態檔＝正在重建（keeper_start 會先刪狀態檔，
                    # 新 keeper 走完建袋才寫 ready）。原本這條靜默落後備、不寫帳本
                    # → 事後完全無法回答「這一發為何沒派到 keeper」（9/22 08:40
                    # 那發就是這樣消失的：前一次失敗後 slot 重建中，命中撞上重建窗）。
                    log("keeper%d 無狀態檔（重建中）— 走後備" % slot)
                    hit_ledger(event="keeper-skip", slot=slot, sku=sku,
                               state="rebuilding")
            else:
                log("keeper 艦隊未啟用 — 直接走後備鏈")
                hit_ledger(event="keeper-skip", sku=sku, state="keeper-off")
            # 補位即時化：命中用過 keeper 後它會自己退出，而下一輪的自癒
            # （keeper_restart_dead，在主迴圈頂）要等整段本地後備跑完（~2-3 分）
            # 才輪到 → 該 SKU 的 slot 每次命中後空缺 3-5 分鐘。9/22 08:03-08:14
            # 實測：熱門 SKU 的 slot4 只有 50% 時間可用、該窗 30.9 次重建/小時；
            # 08:07 那發更是直接撞上重建窗（keeper-skip rebuilding）白丟一發。
            # 只補真的死掉的進程（活著在建袋的不動）；keeper_start 保留 park 命令、
            # 刪 buy 命令，所以「趁熱停泊」也一併提早約 2 分鐘執行。
            if keeper_on and slot is not None:
                _p = KEEPER_PROCS.get(slot)
                if _p is not None and _p.poll() is None:
                    for _ in range(10):      # 最多等 5 秒讓 keeper 寫完終態自己收工
                        if _p.poll() is not None:
                            break
                        time.sleep(0.5)
                if _p is None or _p.poll() is not None:
                    log("keeper%d 失敗後即時補位（不等下一輪自癒）" % slot)
                    # 帳本留痕：否則「補位有沒有真的提早發生」只能靠事後比對
                    # latest.json 的 slot 時間線反推（9/23 08:45 那發就是這樣驗的）。
                    hit_ledger(event="relay", slot=slot, sku=sku,
                               state="restarted" if keeper_start(nodes, slot)
                               else "restart-failed")
            ran = False
            try:
                ran = local_checkout(sku, store, nodes)
            except Exception as e:
                log("本地結帳例外: %s" % str(e)[:80])
            _lst = ""
            try:
                _lst = json.load(open(os.path.join(
                    FALLBACK_ENGINE_DIR, "status", "latest.json"))).get("state", "")
            except Exception:
                pass
            hit_ledger(event="local", sku=sku,
                       state=_lst or ("ran" if ran else "skipped"))
            if not ran:
                try:
                    dispatch_checkout(sku, store)
                    hit_ledger(event="dispatched", sku=sku, store=store)
                except Exception as e:
                    log("dispatch 失敗: %s" % e)
                    bark("dispatch 失敗", str(e)[:80], priority=1)
            if _lst in ("ordered", "declined", "dry-run"):
                # 真的產出結帳結果 → 收工（與 keeper 路徑同語義）
                open("/tmp/hit-marker", "w").write("1")
                push_hits_ledger()
                return 0
            push_hits_ledger()  # 沖帳
            # 後備鏈用完仍未成交 = miss。原本這裡 return 0 會拆掉整個艦隊，
            # 重建需 ~3 分鐘——而晨間命中是叢集出現的（9/20 06:53/07:04/07:08
            # 三個在 15 分鐘內），第 2/3 發因此必定落在重建盲窗裡。9/20 07:04
            # 實證：keeper2 開機 34 秒被判定未就緒而跳過，直接走後備 no-pickup。
            # 改為續掃：艦隊保持武裝，其他 SKU／門市立刻可戰；剛處理的這一對
            # 已在冷卻期內，不會重複觸發通知或重複跑後備。
            log("後備鏈未成交（miss）— 續掃，不拆艦隊（keeper 待命保持）")
            hit_ledger(event="continue-scan", sku=sku, store=store,
                       state=_lst or ("ran" if ran else "skipped"))
            continue
        if sweep % 20 == 0:
            log("sweep %d | %0.1f 分 | 存活節點 %d | 無貨 | keeper=%d/%d 待命"
                % (sweep, now_min(),
                   len([n for n in nodes
                        if node_dead.get(n, 0) <= time.time()
                        and node_strikes.get(n, 0) < NODE_MAX_STRIKES]),
                   keeper_ready_count(), len(KEEPER_FLEET)))
        time.sleep(random.uniform(*SWEEP_PAUSE))
    log("到時收工，等 watchdog 重啟")
    return 0


HITS_LEDGER = "/tmp/hits.jsonl"


def hit_ledger(**ev):
    """命中事件永久帳：本 job 逐行累加，status_pusher 合併去重後推
    checkout repo status/hits.jsonl。latest.json 會被 keeper 重建覆蓋、
    本地處理的命中不進 run list——體檢/主控台查命中史唯讀此帳
    （9/19-20 的 #15/#16/#18/#19 就是快照式檢查漏掉的）。"""
    try:
        ev["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(HITS_LEDGER, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


LEDGER_API = ("https://api.github.com/repos/TommyYeung660/buyip18-checkout"
              "/contents/status/hits.jsonl")
LEDGER_PUSHED = set()


def push_hits_ledger():
    """合併去重所有 hits.jsonl（scanner+各 slot 引擎+後備）→ 推 checkout repo。
    status_pusher 週期呼叫；命中路徑收工前【同步】呼叫——進程 exit 會殺掉
    daemon 線程，不同步推最後幾行就會丟（hit#20 實證丟了 local/dispatched）。"""
    import base64
    import glob
    pat = (os.environ.get("GH_PAT") or "").strip()
    if not pat:
        return False
    heads = {"Authorization": "Bearer " + pat,
             "Accept": "application/vnd.github+json",
             "User-Agent": "buyip18-scanner"}
    try:
        lines = []
        for p in ([HITS_LEDGER]
                  + glob.glob("/tmp/keeper/k*/engine/status/hits.jsonl")
                  + [os.path.join(FALLBACK_ENGINE_DIR, "status", "hits.jsonl")]):
            try:
                if os.path.exists(p):
                    lines += [x for x in open(p).read().splitlines() if x.strip()]
            except Exception:
                pass
        if not lines:
            return False

        def _k(l):
            try:
                d = json.loads(l)
                return (d.get("ts", ""), d.get("event", d.get("state", "")),
                        d.get("sku", ""), d.get("store", ""))
            except Exception:
                return l
        base = ""
        try:
            req = urllib.request.Request(LEDGER_API, headers=heads)
            with urllib.request.urlopen(req, timeout=15) as r:
                base = base64.b64decode(json.loads(r.read())["content"]).decode()
        except Exception:
            base = ""  # 404=帳本未建
        merged, seen = [], set()
        for x in base.splitlines() + lines:
            k = _k(x)
            if x.strip() and k not in seen:
                seen.add(k)
                merged.append(x)
        if not (set(map(_k, merged)) - LEDGER_PUSHED):
            return False
        sha = None
        try:
            req = urllib.request.Request(LEDGER_API, headers=heads)
            with urllib.request.urlopen(req, timeout=15) as r:
                sha = json.loads(r.read()).get("sha")
        except Exception:
            sha = None
        payload = {"message": "hits ledger",
                   "content": base64.b64encode(
                       ("\n".join(merged) + "\n").encode()).decode(),
                   "branch": "main"}
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(
            LEDGER_API, data=json.dumps(payload).encode(),
            headers=heads, method="PUT")
        with urllib.request.urlopen(req, timeout=15) as r:
            log("hits 帳本已推送 http=%d（%d 行）" % (r.status, len(merged)))
        LEDGER_PUSHED.clear()
        LEDGER_PUSHED.update(map(_k, merged))
        return True
    except Exception as e:
        log("hits 帳本推送失敗: " + str(e)[:80])
        return False


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
            # 全艦隊 slot 的 status + 本地後備，取 mtime 最新者＝正在動的那個
            paths = [os.path.join(FALLBACK_ENGINE_DIR, "status", "latest.json")]
            for i in range(len(KEEPER_FLEET)):
                paths.append(os.path.join(_kslot_dir(i), "engine",
                                          "status", "latest.json"))
            for d in paths:
                try:
                    if os.path.exists(d) and (cand is None or
                            os.path.getmtime(d) > os.path.getmtime(cand)):
                        cand = d
                except Exception:
                    pass
            if cand:
                body = open(cand, "rb").read()
                # 附上「每個 slot 現在什麼狀態」：latest.json 是 6 個 slot 取 mtime
                # 最新者覆蓋而成的單一檔，光看它分不出「艦隊有幾個真的武裝待命」。
                # 而命中當下剛好有幾個 slot 在 review，才是決定成敗的量。
                # 只加欄位、不改既有語義；失敗就照舊推送不阻斷。
                try:
                    _slots, _ready = keeper_slots_view()
                    _d = json.loads(body.decode("utf-8"))
                    _d["slots"] = _slots
                    _d["slots_ready"] = _ready
                    _d["slots_total"] = len(_slots)
                    body = json.dumps(_d, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass
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
            # ---- 命中帳本：週期合併去重推 repo（收工前另有同步呼叫）
            push_hits_ledger()
        except Exception as e:
            log("status 推送失敗: " + str(e)[:80])
            time.sleep(10)
        time.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=status_pusher, daemon=True).start()
    sys.exit(main())
