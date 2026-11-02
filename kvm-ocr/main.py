#!/usr/bin/env python3
"""KVM 本地全文 OCR v2：约 60 秒一轮 截图 → 预处理 → 双栏拆分识别 → 推入 memfab。

v2 相比 v1 的改进（实测同一帧 1920x1080 财联社页面）：
  - 关键词召回 7/28 → 23/28，正文接近逐字正确
  - 阅读顺序正确（自适应分栏，左栏先、右栏后，不再左右穿插）
  - 管线：灰度 → 1.5x LANCZOS 放大 → 找纵向留白沟槽拆栏 → psm4 逐栏识别
  - 元数据入库：字数、耗时、配置版本
"""
import base64
import hashlib
import json
import os
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
INTERVAL = 60          # 秒，一轮间隔（识别耗时超过则立刻进入下一轮）
SCALE = 1.5            # 放大倍数：小字号识别必需
PSM = "4"              # 单栏可变字号模式，对 UI 页面效果最好
GUTTER_SEARCH = (0.45, 0.85)  # 在宽度的这个区间内找分栏沟槽
MAX_CHARS = 9000
SAME_RATIO = 0.80      # 与上一轮文本相似度超过此值视为同一屏，不入库
CTX = ssl._create_unverified_context()


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
                             "clientInfo": {"name": "kvm-ocr", "version": "2.0"}}})
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
    """在页面中部区域找主栏与侧栏之间的纵向留白，返回 1.5x 图上的 x 坐标或 None。
    - 只分析 12%~92% 高度带（顶部导航条/底部栏是通栏元素，会污染墨水计数）
    - 墨水 = 像素 <200（抗锯齿后的灰字也算墨），留白列要求整列几乎全白
    - 在搜索区间内取最左侧的合格沟槽（跳过左边距与栏内小空隙）"""
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
            if i - cur_start >= 2:  # 至少 ~8 原始像素宽
                return (lo + (cur_start + i) // 2) * 4
            cur_start = None
    if cur_start is not None and len(ink) - cur_start >= 2:
        return (lo + (cur_start + len(ink)) // 2) * 4
    return None


def ocr_img(path):
    r = subprocess.run(["tesseract", path, "stdout", "-l", "chi_sim", "--psm", PSM],
                       capture_output=True, text=True, timeout=600)
    return r.stdout if r.returncode == 0 else ""


def clean(text):
    lines = []
    for ln in text.replace("\x0c", "\n").splitlines():
        ln = ln.strip()
        if ln and (not lines or ln != lines[-1]):
            lines.append(ln)
    return "\n".join(lines)[:MAX_CHARS]


def ocr_frame():
    """灰度 → 1.5x 放大 → 自适应拆栏 → 逐栏 psm4。返回 (全文, 分栏模式)。"""
    im = Image.open(FRAME).convert("L")
    w, h = im.size
    up = im.resize((int(w * SCALE), int(h * SCALE)), Image.LANCZOS)
    up.save(WORK_GRAY)
    gx = find_gutter(up)
    parts, mode = [], "single"
    if gx and w * 0.25 < gx / SCALE < w * 0.75:
        # 沟槽在画面中部：左主栏 + 右侧栏
        left = up.crop((0, 0, gx - 4, up.size[1]))
        right = up.crop((gx + 4, 0, up.size[0], up.size[1]))
        left.save("/var/lib/kvm-ocr/colL.png")
        right.save("/var/lib/kvm-ocr/colR.png")
        parts.append(ocr_img("/var/lib/kvm-ocr/colL.png"))
        parts.append(ocr_img("/var/lib/kvm-ocr/colR.png"))
        mode = "split@%d" % int(gx / SCALE)
    else:
        parts.append(ocr_img(WORK_GRAY))
    return clean("\n".join(parts)), mode


def post(text, size, capture_id, mode, dur_ms):
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    body = {
        "schema_version": 1,
        "type": "ocr_observation",
        "captured_at": now,
        "capture_id": capture_id,
        "source": {"kind": "screen_ocr", "device": "nanokvm-go", "engine": "tesseract_local"},
        "screen": {"width": size[0], "height": size[1]},
        "context": {"app_name": "TesseractOCR", "title": "Local full-text OCR"},
        "event": {"type": "ocr_capture", "reason": "interval",
                  "meta": {"pipeline": "v2-gray-up15-psm4", "split": mode,
                           "duration_ms": dur_ms, "chars": len(text)}},
        "ocr": {"language": "zh", "text": text},
    }
    req = urllib.request.Request(INGEST_URL, json.dumps(body, ensure_ascii=False).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()[:200]


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
                text, mode = ocr_frame()
                ratio = similar(text, last_text)
                if len(text) < 20:
                    log("识别文本过少（%d 字），不入库" % len(text))
                elif ratio >= SAME_RATIO:
                    log("与上轮内容相同（相似度 %.2f），不入库" % ratio)
                else:
                    cid = "ocr-%s-%d" % (h[:12], int(started))
                    resp = post(text, Image.open(FRAME).size, cid, mode,
                                int((time.time() - started) * 1000))
                    open(STATE, "w", encoding="utf-8").write(text)
                    last_text = text
                    log("已入库 %d 字 / 模式 %s / 耗时 %d 秒 / %s"
                        % (len(text), mode, int(time.time() - started), resp[:60]))
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
