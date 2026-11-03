#!/usr/bin/env python3
"""KVM 本地全文 OCR v3：约 60 秒一轮 截图 → 灰度放大 → 自适应分栏 → TSV 识别 → 结构化 markdown 入库。

v3 相比 v2：
  - 改用 tesseract TSV 输出，同时拿到文字与每行坐标/高度/置信度
  - 按栏 → 区块（行间距聚类）→ 行的层次生成 markdown，阅读顺序与页面结构一致
  - 每个区块标注大致位置（上部/中部/下部 + y 范围），保留版式信息
  - 文本质量与 v2 持平（同帧对比：去空白 1126 字 / 关键词 18-21 命中）
"""
import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.request

from PIL import Image

MCP_URL = "https://127.0.0.1/api/mcp"
INGEST_URL = "http://127.0.0.1:8743/v1/observations"
FRAME = "/var/lib/kvm-ocr/frame.jpg"
WORK_GRAY = "/var/lib/kvm-ocr/gray.png"
STATE = "/var/lib/kvm-ocr/last_text"
SHOTS = "/var/lib/kvm-ocr/shots"     # 入库观察的原始截图归档（与 captured_at 同名）
KEEP_SHOTS = 1200                    # 最多保留张数（1920x1080 约 245KB/张 ≈ 300MB）
INTERVAL = 180         # 秒，一轮间隔（install_deps.sh 的 -i 参数可覆盖）
SCALE = 1.5            # 放大倍数：小字号识别必需
PSM = "4"              # 单栏可变字号模式，对 UI 页面效果最好
GUTTER_SEARCH = (0.45, 0.85)
MAX_CHARS = 12000
SAME_RATIO = 0.80
CTX = ssl._create_unverified_context()
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def shingles(text, n=4):
    t = "".join(text.split())
    return {t[i:i + n] for i in range(0, max(0, len(t) - n))}


def similar(a, b):
    if not a or not b:
        return 0.0
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def mcp_session():
    key = json.loads(open("/etc/kvm/ai.json").read())["mcpApiKey"]
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    def rpc(payload, sid=None):
        h = dict(headers)
        if sid:
            h["mcp-session-id"] = sid
        req = urllib.request.Request(MCP_URL, json.dumps(payload).encode(), h, method="POST")
        r = urllib.request.urlopen(req, context=CTX, timeout=40)
        return r.headers.get("mcp-session-id", sid), r.read().decode()

    sid, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "kvm-ocr", "version": "3.0"}}})
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return rpc, sid


def capture(rpc, sid):
    _, body = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "screenshot", "arguments": {}}}, sid)
    payload = body.split("data: ", 1)[1].split("\n", 1)[0]
    result = json.loads(payload)["result"]
    if result.get("isError"):
        msg = ""
        for part in result.get("content", []):
            if part.get("type") == "text":
                msg = part.get("text", "")
        raise RuntimeError("NO_SIGNAL" if "timeout" in msg else ("截图失败: " + msg))
    for part in result.get("content", []):
        if part.get("type") == "image":
            data = base64.b64decode(part["data"])
            with open(FRAME, "wb") as f:
                f.write(data)
            return data
    raise RuntimeError("截图响应里没有图像")


def find_gutter(im):
    """在页面中部区域找主栏与侧栏之间的纵向留白，返回 1.5x 图上的 x 坐标或 None。"""
    w, h = im.size
    band = im.crop((0, int(h * 0.12), w, int(h * 0.92)))
    small = band.resize((w // 4, band.size[1] // 8))
    px = small.load()
    sw, sh = small.size
    lo, hi = int(sw * GUTTER_SEARCH[0]), int(sw * GUTTER_SEARCH[1])
    ink = [sum(1 for y in range(sh) if px[x, y] < 200) for x in range(lo, hi)]
    cur_start = None
    for i, v in enumerate(ink):
        white = v <= 2
        if white and cur_start is None:
            cur_start = i
        elif not white and cur_start is not None:
            if i - cur_start >= 2:
                return (lo + (cur_start + i) // 2) * 4
            cur_start = None
    if cur_start is not None and len(ink) - cur_start >= 2:
        return (lo + (cur_start + len(ink)) // 2) * 4
    return None


def join_tokens(tokens):
    """中文逐字 word 直接拼接，英文/数字保留空格。"""
    out = ""
    for t in tokens:
        if not out:
            out = t
            continue
        prev, nxt = out[-1], t[0]
        if CJK.match(nxt) and not re.match(r"[A-Za-z0-9]", prev):
            out += t
        elif prev in "([{/":
            out += t
        elif nxt in ")]},.;:!?%":
            out += t
        else:
            out += " " + t
    return out


def ocr_lines(path):
    """tesseract TSV → 行列表（含坐标、行高、平均置信度）。"""
    r = subprocess.run(["tesseract", path, "stdout", "-l", "chi_sim", "--psm", PSM, "tsv"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return []
    rows = r.stdout.splitlines()
    if not rows:
        return []
    hdr = rows[0].split("\t")
    idx = {k: i for i, k in enumerate(hdr)}
    groups = {}
    for ln in rows[1:]:
        f = ln.split("\t")
        if len(f) <= idx.get("text", 11) or f[idx["level"]] != "5":
            continue
        w = f[idx["text"]].strip()
        if not w:
            continue
        key = (f[idx["block_num"]], f[idx["par_num"]], f[idx["line_num"]])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"tokens": [], "x": int(f[idx["left"]]), "y": int(f[idx["top"]]),
                               "r": int(f[idx["left"]]) + int(f[idx["width"]]),
                               "b": int(f[idx["top"]]) + int(f[idx["height"]]), "conf": []}
        g["tokens"].append(w)
        g["x"] = min(g["x"], int(f[idx["left"]]))
        g["y"] = min(g["y"], int(f[idx["top"]]))
        g["r"] = max(g["r"], int(f[idx["left"]]) + int(f[idx["width"]]))
        g["b"] = max(g["b"], int(f[idx["top"]]) + int(f[idx["height"]]))
        try:
            g["conf"].append(float(f[idx["conf"]]))
        except ValueError:
            pass
    out = []
    for g in groups.values():
        text = join_tokens(g["tokens"]).strip()
        if not text:
            continue
        out.append({"text": text, "x": g["x"], "y": g["y"],
                    "w": g["r"] - g["x"], "h": g["b"] - g["y"],
                    "conf": round(sum(g["conf"]) / len(g["conf"])) if g["conf"] else 0})
    out.sort(key=lambda o: (o["y"], o["x"]))
    return out


def band_name(y0, y1, height):
    mid = (y0 + y1) / 2.0 / max(1, height)
    if mid < 0.18:
        return "顶部"
    if mid < 0.38:
        return "上部"
    if mid < 0.62:
        return "中部"
    if mid < 0.82:
        return "下部"
    return "底部"


def split_blocks(lines):
    """按行间距把一栏切成区块（模拟人类阅读的段落分组）。"""
    if not lines:
        return []
    hs = sorted(l["h"] for l in lines)
    med = max(8, hs[len(hs) // 2])
    blocks, cur = [], [lines[0]]
    for prev, ln in zip(lines, lines[1:]):
        gap = ln["y"] - (prev["y"] + prev["h"])
        if gap > max(med * 1.6, 14):
            blocks.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    blocks.append(cur)
    return blocks


def to_markdown(cols, split_x, elapsed, size):
    """cols: [(label, lines, x0, x1)] → 层次化 markdown。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    head = ["# 屏幕识别 · 结构化全文", "",
            "> 采集 %s · 引擎 tesseract chi_sim psm%s · 灰度 %.1fx · %s · 耗时 %.0fs · 画面 %dx%d"
            % (stamp, PSM, SCALE,
               ("分栏 @%dpx" % split_x) if split_x else "未分栏",
               elapsed, size[0], size[1]), ""]
    body = []
    for label, lines, x0, x1 in cols:
        if not lines:
            continue
        body.append("## %s（x %d–%d，共 %d 行）" % (label, x0, x1, len(lines)))
        for bi, blk in enumerate(split_blocks(lines), 1):
            y0 = int(blk[0]["y"] / SCALE)
            y1 = int((blk[-1]["y"] + blk[-1]["h"]) / SCALE)
            body.append("")
            body.append("### 区块 %d · %s（y %d–%d）" % (bi, band_name(blk[0]["y"], blk[-1]["y"], size[1] * SCALE), y0, y1))
            for ln in blk:
                body.append(ln["text"])
        body.append("")
    return "\n".join(head + body).strip()[:MAX_CHARS]


def ocr_frame():
    """灰度 → 1.5x 放大 → 自适应拆栏 → 逐栏 TSV → 结构化 markdown。
    返回 (markdown, 分栏模式, 原始行数)。"""
    started = time.time()
    im = Image.open(FRAME).convert("L")
    w, h = im.size
    up = im.resize((int(w * SCALE), int(h * SCALE)), Image.LANCZOS)
    up.save(WORK_GRAY)
    gx = find_gutter(up)
    cols, mode, split_x, total = [], "single", 0, 0
    if gx and w * 0.25 < gx / SCALE < w * 0.75:
        left = up.crop((0, 0, gx - 4, up.size[1]))
        right = up.crop((gx + 4, 0, up.size[0], up.size[1]))
        left.save("/var/lib/kvm-ocr/colL.png")
        right.save("/var/lib/kvm-ocr/colR.png")
        ll, rl = ocr_lines("/var/lib/kvm-ocr/colL.png"), ocr_lines("/var/lib/kvm-ocr/colR.png")
        split_x = int(gx / SCALE)
        cols = [("主栏 · 左侧内容区", ll, 0, split_x),
                ("侧栏 · 右侧边栏", rl, split_x, w)]
        total = len(ll) + len(rl)
        mode = "split@%d" % split_x
    else:
        lines = ocr_lines(WORK_GRAY)
        cols = [("全屏 · 单栏", lines, 0, w)]
        total = len(lines)
    md = to_markdown(cols, split_x, time.time() - started, (w, h))
    return md, mode, total


def shot_name(now):
    """captured_at '2026-11-03T06:17:22.000000Z' → 归档名 '20261103T061722Z.jpg'"""
    return now.replace("-", "").replace(":", "").split(".")[0] + "Z.jpg"


def archive_shot(now):
    """把本轮入库对应的原始截图留档，供网页按时间回溯查看清晰度。"""
    try:
        os.makedirs(SHOTS, exist_ok=True)
        dst = os.path.join(SHOTS, shot_name(now))
        shutil.copy2(FRAME, dst)
        names = sorted(os.listdir(SHOTS))
        if len(names) > KEEP_SHOTS:
            for old in names[:len(names) - KEEP_SHOTS]:
                try:
                    os.remove(os.path.join(SHOTS, old))
                except OSError:
                    pass
        return dst
    except Exception as e:
        log("截图归档失败: %s" % e)
        return None


def post(text, size, capture_id, mode, dur_ms, n_lines):
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    meta = {"pipeline": "v3-tsv-markdown", "split": mode,
            "duration_ms": dur_ms, "chars": len(re.sub(r"\s", "", text)), "lines": n_lines}
    body = {
        "schema_version": 1,
        "type": "ocr_observation",
        "captured_at": now,
        "capture_id": capture_id,
        "source": {"kind": "screen_ocr", "device": "nanokvm-go", "engine": "tesseract_local"},
        "screen": {"width": size[0], "height": size[1]},
        "context": {"app_name": "TesseractOCR", "title": "Local structured OCR v3"},
        "event": {"type": "ocr_capture", "reason": "interval", "meta": meta},
        "ocr": {"language": "zh", "text": text, "meta": meta},
    }
    req = urllib.request.Request(INGEST_URL, json.dumps(body, ensure_ascii=False).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()[:200], now


def main():
    os.makedirs("/var/lib/kvm-ocr", exist_ok=True)
    last_text = open(STATE, encoding="utf-8").read() if os.path.isfile(STATE) else ""
    last_hash = ""
    no_signal = False
    while True:
        started = time.time()
        try:
            rpc, sid = mcp_session()
            data = capture(rpc, sid)
            if no_signal:
                log("视频信号恢复")
            no_signal = False
            h = hashlib.sha1(data).hexdigest()
            if h == last_hash:
                log("画面像素未变，跳过")
            else:
                last_hash = h
                text, mode, n_lines = ocr_frame()
                ratio = similar(text, last_text)
                if len(re.sub(r"\s", "", text)) < 30:
                    log("识别文本过少（%d 字），不入库" % len(re.sub(r"\s", "", text)))
                elif ratio >= SAME_RATIO:
                    log("与上轮内容相同（相似度 %.2f），不入库" % ratio)
                else:
                    cid = "ocr-%s-%d" % (h[:12], int(started))
                    resp, stamp = post(text, Image.open(FRAME).size, cid, mode,
                                       int((time.time() - started) * 1000), n_lines)
                    archive_shot(stamp)
                    open(STATE, "w", encoding="utf-8").write(text)
                    last_text = text
                    log("已入库 %d 字 / %d 行 / 模式 %s / 耗时 %d 秒 / %s"
                        % (len(re.sub(r"\s", "", text)), n_lines, mode,
                           int(time.time() - started), resp[:50]))
        except Exception as e:
            msg = str(e)
            if "NO_SIGNAL" in msg:
                no_signal = True
                log("无视频信号（被控机可能睡眠/关机），15 秒后重试")
            else:
                log("本轮失败: %s: %s" % (type(e).__name__, msg[:160]))
        elapsed = time.time() - started
        if no_signal:
            time.sleep(15)
        else:
            time.sleep(max(3, INTERVAL - elapsed))


if __name__ == "__main__":
    main()
