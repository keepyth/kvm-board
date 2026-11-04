#!/usr/bin/env python3
"""NanoKVM-Go+ 后台状态服务：Web 仪表盘 + JSON API，端口 8123。
/memory —— 记忆织网面板（memfab 记忆与屏幕观察）"""

import base64
import hmac
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BJ = timezone(timedelta(hours=8))
ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?")

WEB_PORT = 8123
MEMFAB = "/var/lib/memfab"
SHOTS_DIR = "/var/lib/kvm-ocr/shots"   # kvm-ocr 归档的原始截图（1920x1080）
SHOT_RE = re.compile(r"^\d{8}T\d{6}Z\.jpg$")
BASE = os.path.dirname(os.path.abspath(__file__))
PAGE = open(os.path.join(BASE, "page.html"), encoding="utf-8").read()
MEM_PAGE = open(os.path.join(BASE, "memory.html"), encoding="utf-8").read()
SHOTS_PAGE = open(os.path.join(BASE, "shots.html"), encoding="utf-8").read()
FILES_PAGE = open(os.path.join(BASE, "files.html"), encoding="utf-8").read()
FILES_PASS = os.environ.get("FILES_PASS", "/etc/kvm/files.pass")   # 格式: 用户名:密码（一行）


def read_cpu():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    return vals[3] + vals[4], sum(vals)


def cpu_percent():
    i0, t0 = read_cpu()
    time.sleep(0.4)
    i1, t1 = read_cpu()
    dt, di = t1 - t0, i1 - i0
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))


def mem_info():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])
    total = info["MemTotal"]
    return total, total - info["MemAvailable"]


def temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return None


def uptime_s():
    with open("/proc/uptime") as f:
        return int(float(f.read().split()[0]))


def fmt_uptime(s):
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}天{h}小时{m}分" if d else f"{h}小时{m}分"


def disk_usage():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_bsize
    free = st.f_bavail * st.f_bsize
    return total, total - free


def services():
    try:
        with open("/etc/kvm/services.json") as f:
            return {k: bool(v.get("enabled")) for k, v in json.load(f).get("services", {}).items()}
    except Exception:
        return {}


def core_services_up():
    out = {}
    for name in ("nanokvm-go.service", "kvmcomm.service"):
        out[name] = os.system(f"systemctl is-active --quiet {name} 2>/dev/null") == 0
    return out


def dir_size_mb(path):
    total = 0
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return total / 1e6


def memfab_stats():
    obs_dir = os.path.join(MEMFAB, "observation-buffer")
    mem_dir = os.path.join(MEMFAB, "memory")
    n_obs = len([f for f in os.listdir(obs_dir) if f.endswith(".json")]) if os.path.isdir(obs_dir) else 0
    n_mem = len(os.listdir(mem_dir)) if os.path.isdir(mem_dir) else 0
    return {
        "timelapse_mb": round(dir_size_mb(os.path.join(MEMFAB, "timelapse")), 1),
        "buffer_mb": round(dir_size_mb(obs_dir), 2),
        "memory_mb": round(dir_size_mb(mem_dir), 3),
        "obs_count": n_obs,
        "mem_count": n_mem,
        "memfab_up": os.system("systemctl is-active --quiet memfab 2>/dev/null") == 0,
    }


_DEPS_CACHE = {"ts": 0.0, "data": None}


def _svc_up(name):
    return os.system("systemctl is-active --quiet %s 2>/dev/null" % name) == 0


def shot_files():
    """归档截图列表，新的在前。"""
    try:
        names = [n for n in os.listdir(SHOTS_DIR) if SHOT_RE.match(n)]
    except OSError:
        return []
    return sorted(names, reverse=True)


def shot_name_for(iso):
    """captured_at '2026-11-03T06:17:22.000000Z' → '20261103T061722Z.jpg'"""
    m = ISO_RE.search(str(iso or ""))
    if not m:
        return ""
    return (m.group(1).replace("-", "") + "T" + m.group(2).replace(":", "") + "Z.jpg")


def shot_url_for(iso):
    """若该观察有对应归档截图则返回 URL，否则空串。"""
    n = shot_name_for(iso)
    if n and os.path.isfile(os.path.join(SHOTS_DIR, n)):
        return "/shot?f=" + n
    return ""


def render_shots_page():
    """截图存档图库：网格缩略 + 点击看原图。"""
    items = shot_files()
    cards, total_mb = [], 0.0
    for n in items[:300]:
        try:
            total_mb += os.path.getsize(os.path.join(SHOTS_DIR, n)) / 1048576.0
        except OSError:
            pass
        # 20261103T061722Z.jpg → 2026-11-03 14:17:22（北京时间）
        try:
            dt = datetime.strptime(n[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone(BJ)
            label = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            label = n
        cards.append(
            '<a class="shot" href="/shot?f=%s" target="_blank">'
            '<img loading="lazy" src="/shot?f=%s" alt="%s">'
            '<span>%s</span></a>' % (n, n, label, label))
    if not cards:
        cards.append('<div class="empty">暂无截图归档。新入库的 OCR 观察会自动存一张原图（每 '
                     '180 秒最多一张）。</div>')
    return (SHOTS_PAGE
            .replace("__COUNT__", str(len(items)))
            .replace("__MB__", "%.1f" % total_mb)
            .replace("__CARDS__", "".join(cards))
            .replace("__TIME__", time.strftime("%Y-%m-%d %H:%M:%S")))


def mem_usage_info():
    """读取本机真实内存：硬件总量（MemTotal+内核与硬件预留）与当前可用量。"""
    info = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.split()[0]) if v.split() else 0
    except Exception:
        return {"total_mb": 0, "avail_mb": 0}
    total = info.get("MemTotal", 0)
    # dmesg 里的 "Memory: X/262132K available" 才是物理焊死值，这里用 MemTotal 向上取整到常见规格
    hw = 0
    for step in (128, 256, 512, 1024, 2048):
        if total * 1024 <= step * 1024 * 1024:
            hw = step
            break
    return {"total_mb": round(total / 1024),
            "avail_mb": round(info.get("MemAvailable", 0) / 1024),
            "hw_mb": hw or round(total / 1024),
            "swap_used_mb": round((info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1024)}


def deps_status():
    """/memory 面板所依赖的组件是否就绪（结果缓存 30 秒，避免每轮 AJAX 反复探测）。"""
    now = time.time()
    if _DEPS_CACHE["data"] is not None and now - _DEPS_CACHE["ts"] < 30:
        return _DEPS_CACHE["data"]

    def has_tess():
        if not shutil.which("tesseract"):
            return False
        try:
            r = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                               text=True, timeout=5)
            return "chi_sim" in (r.stdout + r.stderr)
        except Exception:
            return False

    def has_pil():
        try:
            import PIL  # noqa: F401
            return True
        except Exception:
            return False

    def port_open(host, port):
        try:
            s = socket.create_connection((host, port), 1.0)
            s.close()
            return True
        except Exception:
            return False

    d = {
        "dashboard": True,
        "memfab": _svc_up("memfab"),
        "mcp_key": os.path.isfile("/etc/kvm/ai.json") and os.path.getsize("/etc/kvm/ai.json") > 0,
        "tesseract": has_tess(),
        "pillow": has_pil(),
        "llm_proxy": _svc_up("llm-proxy"),
        "kvm_ocr": _svc_up("kvm-ocr"),
        "upstream": port_open("192.168.110.71", 1234),
        "installer": os.path.isfile(os.path.join(BASE, "install_deps.sh")),
        "manual_required": os.path.isfile(os.path.join(BASE, "DEPS_INSTALL_REQUIRED")),
    }
    _DEPS_CACHE.update(ts=now, data=d)
    return d


DEP_LABELS = [
    ("dashboard", "页面服务", "nanokvm-status · 8123"),
    ("memfab", "记忆织网", "原厂 /usr/bin/memfab · systemd memfab"),
    ("mcp_key", "MCP 密钥", "/etc/kvm/ai.json · kvm-ocr 截图凭据"),
    ("tesseract", "OCR 引擎", "tesseract-ocr + tesseract-ocr-chi-sim"),
    ("pillow", "图像处理", "python3-pil（Pillow）"),
    ("llm_proxy", "模型代理", "llm-proxy · 127.0.0.1:1235"),
    ("kvm_ocr", "屏幕识别", "kvm-ocr · 写入 observation-buffer"),
    ("upstream", "局域网模型", "192.168.110.71:1234 · LM Studio"),
]


def render_deps_html():
    d = deps_status()
    rows = []
    for key, name, desc in DEP_LABELS:
        ok = d.get(key, False)
        rows.append(
            '<div class="dep %s"><span class="dep-dot"></span>'
            '<b>%s</b><span class="dep-desc">%s</span>'
            '<span class="dep-state">%s</span></div>' %
            ("ok" if ok else "off", name, desc, "就绪" if ok else "缺失"))
    missing = sum(1 for k, _, _ in DEP_LABELS if not d.get(k, False))
    banner = ('<div class="dep-banner ok">✅ 全部依赖就绪</div>' if missing == 0 else
              '<div class="dep-banner off">⚠️ 有 %d 项依赖未就绪 —— 展开下方安装步骤处理</div>' % missing)
    if d.get("manual_required"):
        banner += ('<div class="dep-banner off">应用商店安装脚本权限不足，'
                   '请按下面「手动安装（SSH root）」执行。</div>')
    if not d.get("installer"):
        banner += '<div class="dep-banner off">未找到 install_deps.sh，请从应用源重新安装应用。</div>'
    return banner + "".join(rows)


def render_meminfo_html():
    """本机内存实况：硬件规格 / 系统可用 / 预留 / swap 占用。"""
    m = mem_usage_info()
    if not m["total_mb"]:
        return '<span class="dep-desc">内存信息读取失败</span>'
    reserved = max(0, m["hw_mb"] - m["total_mb"])
    swap = m["swap_used_mb"]
    level = "ok" if m["avail_mb"] >= 80 else ("warn" if m["avail_mb"] >= 40 else "off")
    return ('<span class="dep-desc">硬件焊死 <b>%(hw)d MB</b>（出厂固定，不可升级）'
            ' · 内核与视频采集预留 %(res)d MB · 系统可见 <b>%(tot)d MB</b>'
            ' · 当前可用 <b class="dep-%(lv)s">%(av)d MB</b>'
            ' · swap 已用 %(sw)d MB</span>' %
            {"hw": m["hw_mb"], "res": reserved, "tot": m["total_mb"],
             "av": m["avail_mb"], "sw": swap, "lv": level})


def fmt_bj(v):
    """任意 ISO 时间 → 北京时间字符串 'YYYY-MM-DD HH:MM:SS'（不带 TZ）。
    无偏移或 Z 视为 UTC；带 +08:00 等偏移时换算到北京时间。"""
    s = str(v or "").strip()
    m = ISO_RE.search(s)
    if not m:
        return s[:19] if s else "--"
    date, tm, off = m.group(1), m.group(2), m.group(3)
    try:
        base = datetime.strptime(date + " " + tm, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s[:19]
    if off in (None, "Z", "+00:00", "-00:00"):
        base = base.replace(tzinfo=timezone.utc).astimezone(BJ)
    elif off not in ("+08:00", "+0800"):
        sign = 1 if off[0] == "+" else -1
        try:
            delta = timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))
            base = base.replace(tzinfo=timezone(sign * delta)).astimezone(BJ)
        except (ValueError, IndexError):
            pass
    return base.strftime("%Y-%m-%d %H:%M:%S")


def fmt_bj_text(s):
    """把一段文本里出现的所有 ISO 时间统一替换为北京时间字符串。"""
    return ISO_RE.sub(lambda m: fmt_bj(m.group(0)), str(s or ""))


def obs_files():
    """observation-buffer 全部 JSON 文件名，字典序即时间序（文件名是 UTC 时间戳）。"""
    d = os.path.join(MEMFAB, "observation-buffer")
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".json"))


def obs_index():
    """全量观察索引 [{id, dt(北京时间 datetime)}]，ID 为持久稳定的全局序号。"""
    out = []
    d = os.path.join(MEMFAB, "observation-buffer")
    for i, name in enumerate(obs_files(), 1):
        try:
            with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                j = json.load(f)
            local = fmt_bj(j.get("captured_at") or "")
            dt = datetime.strptime(local, "%Y-%m-%d %H:%M:%S")
            out.append({"id": i, "dt": dt})
        except Exception:
            continue
    return out


def memfab_memories(limit=12):
    """读取 /var/lib/memfab/memory/*.md（YAML frontmatter + 正文），按文件名倒序。
    附带溯源：从哪些 ID# 原始 OCR 中总结而来。"""
    out = []
    d = os.path.join(MEMFAB, "memory")
    if not os.path.isdir(d):
        return out
    oidx = obs_index()
    for name in sorted(os.listdir(d), reverse=True)[:limit]:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except OSError:
            continue
        meta, body = {}, txt
        if txt.startswith("---"):
            parts = txt.split("---", 2)
            if len(parts) == 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
                body = parts[2]
        # 收集正文里每条记忆条目的时间戳（`## [时间]`），作为该条目覆盖的会话窗口
        times = []
        for m in ISO_RE.finditer(body):
            try:
                times.append(datetime.strptime(fmt_bj(m.group(0)), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
        src = ""
        segs = []
        for t in sorted(set(times)):
            lo, hi = t - timedelta(minutes=20), t + timedelta(minutes=2)
            ids = [o["id"] for o in oidx if lo <= o["dt"] <= hi]
            if ids:
                segs.append((min(ids), max(ids), lo, hi, len(ids)))
        # 合并时间上重叠/相邻（<3 分钟）的段
        merged = []
        for s in segs:
            if merged and s[2] <= merged[-1][3] + timedelta(minutes=3):
                p = merged[-1]
                merged[-1] = (min(p[0], s[0]), max(p[1], s[1]), p[2], max(p[3], s[3]), 0)
            else:
                merged.append(s)
        if merged:
            parts = []
            for a, b, lo, hi, _ in merged[-2:]:
                cnt = len([o for o in oidx if lo <= o["dt"] <= hi])
                parts.append("从 ID#%d~#%d 中提取（%s ~ %s，约 %d 分钟，%d 条）" % (
                    a, b, lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S"),
                    int((hi - lo).total_seconds() // 60), cnt))
            if len(merged) > 2:
                parts.insert(0, "共 %d 个时段，最近：" % len(merged))
            src = "；".join(parts)
        out.append({"name": name, "meta": meta, "body": fmt_bj_text(body.strip()[:4000]),
                    "created": fmt_bj(meta.get("created", "")), "updated": fmt_bj(meta.get("updated", "")),
                    "source": src,
                    "id_range": ("ID#%d~#%d" % (merged[-1][0], merged[-1][1])) if merged else ""})
    return out


def utc_to_local(ts):
    """兼容旧调用：统一交给 fmt_bj 输出北京时间字符串。"""
    return fmt_bj(ts)


def memfab_observations(limit=40):
    """读取 observation-buffer 最新 N 条 OCR 观察，倒序返回（含全局 ID、引擎、字数、全文）。"""
    out = []
    d = os.path.join(MEMFAB, "observation-buffer")
    if not os.path.isdir(d):
        return out
    files = obs_files()
    total = len(files)
    for pos, name in enumerate(files[-limit:], start=max(0, total - limit) + 1):
        try:
            with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                j = json.load(f)
        except Exception:
            continue
        ocr = j.get("ocr") or {}
        ctx = j.get("context") or {}
        src = j.get("source") or {}
        ev = j.get("event") or {}
        meta = ev.get("meta") or ocr.get("meta") or {}
        text = ocr.get("text", "") or ""
        raw_ts = j.get("captured_at") or ""
        out.append({
            "id": pos,
            "time": fmt_bj(raw_ts),
            "shot": shot_url_for(raw_ts),
            "app": ctx.get("app_name", ""),
            "title": ctx.get("title", ""),
            "engine": src.get("engine", "unknown"),
            "text": text[:30000],
            "chars": meta.get("chars") or len(text),
            "duration_ms": meta.get("duration_ms"),
            "split": meta.get("split", ""),
            "confidence": ocr.get("confidence"),
        })
    out.reverse()
    return out


def snapshot():
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": round(cpu_percent(), 1),
        "temp": temperature(),
        "uptime_s": uptime_s(), "uptime": fmt_uptime(uptime_s()),
        "services": services(),
        "core": core_services_up(),
        "loadavg": os.getloadavg(),
    }


def snapshot_full():
    s = snapshot()
    mt, mu = mem_info()
    s["mem_total_kb"], s["mem_used_kb"] = mt, mu
    dt, du = disk_usage()
    s["disk_total_gb"], s["disk_used_gb"] = round(dt / 1e9, 1), round(du / 1e9, 1)
    st = memfab_stats()
    s["screen_timelapse_size_mb"] = st["timelapse_mb"]
    s["memfab"] = st
    return s


def extract_conv(e, content):
    """从 trace 的出入参数里抽取人类可读的对话文本（去掉 JSON 结构）。"""
    conv = []
    req = e.get("request")
    if isinstance(req, str):
        try:
            req = json.loads(req)
        except Exception:
            req = None
    if isinstance(req, dict):
        for m in (req.get("messages") or []):
            if not isinstance(m, dict):
                continue
            text = m.get("content", "")
            if not isinstance(text, str):
                try:
                    text = json.dumps(text, ensure_ascii=False)
                except Exception:
                    text = str(text)
            text = text.strip()
            if text:
                conv.append({"role": m.get("role", "?"), "text": fmt_bj_text(text[:1500])})
    content = (content or "").strip()
    if content:
        conv.append({"role": "assistant", "text": fmt_bj_text(content[:1500])})
    return conv


def memfab_llm_status():
    """解析 llm-trace.jsonl：模型调用统计 + 最近一次对话详情。"""
    info = {"model": "", "last_time": "--", "last_stage": "", "today_calls": 0,
            "total_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "last_content": "", "last_request": "",
            "last_response": "", "last_ok": True, "last_duration_ms": None,
            "last_conv": []}
    path = os.path.join(MEMFAB, "logs", "llm-trace.jsonl")
    if not os.path.isfile(path):
        return info
    today = time.strftime("%Y-%m-%d")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                info["total_calls"] += 1
                ts = e.get("timestamp", "")
                if ts.startswith(today):
                    info["today_calls"] += 1
                content, usage = "", {}
                raw = e.get("response_raw") or ""
                parsed = None
                try:
                    parsed = json.loads(raw)
                    content = parsed["choices"][0]["message"]["content"]
                    usage = parsed.get("usage") or {}
                except Exception:
                    pass
                info["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
                info["completion_tokens"] += usage.get("completion_tokens", 0) or 0
                info["total_tokens"] += usage.get("total_tokens", 0) or 0
                info["last_time"] = fmt_bj(ts)
                info["last_stage"] = e.get("stage", "")
                info["model"] = e.get("model", info["model"])
                info["last_content"] = content[:600]
                info["last_request"] = e.get("request") or ""
                info["last_response"] = parsed if parsed is not None else raw
                info["last_ok"] = not e.get("error")
                info["last_duration_ms"] = e.get("duration_ms")
                info["last_conv"] = extract_conv(e, content)
    except OSError:
        pass
    return info


def esc(s):
    return html.escape(str(s))


def rel_time(ts):
    """本地时间字符串 → 'xx 秒/分钟/小时前'，供点击切换完整时间。"""
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        s = int(time.time() - t)
        if s < 0:
            return "刚刚"
        if s < 60:
            return f"{s} 秒前"
        if s < 3600:
            return f"{s // 60} 分钟前"
        if s < 86400:
            return f"{s // 3600} 小时前"
        return f"{s // 86400} 天前"
    except Exception:
        return ts


def render_memory_fragments():
    """只渲染页面的可变片段，供前端 AJAX 局部替换（避免整页刷新）。"""
    st = memfab_stats()
    mems = memfab_memories()
    obs = memfab_observations()

    stat_cards = "".join(
        f'<div class="card"><h2>{t}</h2><div class="big" style="font-size:22px">{v}<span class="unit"> {u}</span></div><div class="sub">{sub}</div></div>'
        for t, v, u, sub in [
            ("记忆条目", st["mem_count"], "条", "memfab 长期记忆文件"),
            ("屏幕观察", st["obs_count"], "条", "OCR 缓冲区待归档"),
            ("时间线占用", st["timelapse_mb"], "MB", "timelapse 录屏数据"),
            ("观察缓冲", st["buffer_mb"], "MB", "observation buffer"),
        ])
    svc = f'<span class="dot {"ok" if st["memfab_up"] else "off"}"></span>{"运行中" if st["memfab_up"] else "已停止"}'

    mem_cards = ""
    if not mems:
        mem_cards = '<div class="card"><div class="sub">暂无记忆。会话结束（屏幕长时间静止或重启 memfab）后 reducer 会自动生成。</div></div>'
    for m in mems:
        meta = m["meta"]
        desc = esc(meta.get("description", ""))
        tags = esc(meta.get("tags", "").strip("[]"))
        src = esc(m["source"]) if m["source"] else "暂无可关联的原始观察"
        idbadge = f'<span class="ids">{esc(m["id_range"])}</span>' if m.get("id_range") else ""
        mem_cards += (
            f'<div class="card memcard derived"><span class="tag tag-ai">AI 加工 · 非事实源</span>'
            f'<div class="memhead" onclick="toggleMem(this)">'
            f'<span class="fname">📄 {esc(m["name"])}</span>{idbadge}'
            f'<span class="caret">▸</span></div>'
            f'<div class="membody" hidden>'
            f'<div class="src">🔗 来源：{src}</div>'
            f'<div class="sub" style="margin:6px 0 8px">{desc} · 创建于 {esc(m["created"])} · 更新于 {esc(m["updated"])} · 标签: {tags}</div>'
            f'<pre class="body">{esc(m["body"])}</pre></div></div>')

    obs_cards = ""
    if not obs:
        obs_cards = '<div class="card"><div class="sub">暂无观察记录</div></div>'
    for i, o in enumerate(obs):
        engine = o.get("engine", "")
        lowq = ""
        if engine == "tesseract_local":
            badge = '<span class="badge tess">⚙ 本地结构化 v3 · 高精度</span>'
        elif engine == "nano_ocr_npu":
            badge = '<span class="badge npu" style="opacity:.6">⚡ NPU · 低精度（已停用）</span>'
            lowq = " lowq"
        else:
            badge = f'<span class="badge note">{esc(engine)}</span>'
        dur = o.get("duration_ms")
        dur_txt = f" · {dur/1000:.0f}s" if isinstance(dur, (int, float)) else ""
        split = f" · {esc(o['split'])}" if o.get("split") else ""
        conf = o.get("confidence")
        conf_txt = f" · 置信度 {conf:.2f}" if isinstance(conf, (int, float)) else ""
        t = esc(o["time"])
        meta_txt = f'{esc(o["app"])} · {esc(o["title"])} · {o["chars"]} 字{dur_txt}{split}{conf_txt}' if o.get("title") else f'{esc(o["app"])} · {o["chars"]} 字{dur_txt}{split}{conf_txt}'
        # 摘要：去掉 markdown 标记行（标题/引言/分隔线）后取前 110 字
        plain_lines = [ln for ln in o["text"].splitlines()
                       if ln.strip() and not re.match(r"^\s{0,3}(#{1,6}\s|>\s?|---+\s*$)", ln)]
        flat = " ".join(" ".join(plain_lines).split())
        preview = esc(flat[:110] + ("…" if len(flat) > 110 else ""))
        shot = o.get("shot") or ""
        shot_btn = (f'<a class="shotlink" href="{shot}" target="_blank" '
                    f'onclick="event.stopPropagation()" title="打开本帧原始截图 1920×1080">🖼 原图</a>'
                    if shot else "")
        obs_cards += (
            f'<div class="card raw{lowq}{" latest" if i == 0 and engine == "tesseract_local" else ""}"'
            f' data-shot="{shot}">'
            f'<span class="tag tag-raw">原始 · 引擎直出</span>'
            f'<div class="obshead"><span class="oid">ID#{o["id"]}</span>'
            f'<span class="ot" data-full="{t}" onclick="toggleTime(this)">{esc(rel_time(o["time"]))}</span>'
            f'{badge}<span class="conf">{meta_txt}</span>{shot_btn}</div>'
            f'<div class="preview" onclick="showObs(this)" title="点击查看完整识别内容">{preview}</div>'
            f'<pre class="fulltext" hidden>{esc(o["text"])}</pre></div>')

    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "svc": svc,
            "stats": stat_cards,
            "mem": mem_cards,
            "obs": obs_cards,
            "deps": render_deps_html(),
            "meminfo": render_meminfo_html()}


def render_memory_page():
    frag = render_memory_fragments()
    return (MEM_PAGE
            .replace("__STATS_CARDS__", frag["stats"])
            .replace("__SVC__", frag["svc"])
            .replace("__MEM_CARDS__", frag["mem"])
            .replace("__OBS_CARDS__", frag["obs"])
            .replace("__DEPS__", frag["deps"])
            .replace("__MEMINFO__", frag["meminfo"])
            .replace("__TIME__", frag["time"]))


# ---------- 文件浏览器（/files 浏览 + /dl 下载） ----------

BLOCKED_PREFIX = ("/proc", "/sys", "/dev", "/run")
_AUTH_CACHE = {"t": 0.0, "v": ("", "")}


def files_auth_conf():
    """读取 /etc/kvm/files.pass（user:pass），5 秒缓存。"""
    now = time.time()
    if now - _AUTH_CACHE["t"] > 5:
        try:
            line = open(FILES_PASS, encoding="utf-8", errors="replace").read().strip()
        except Exception:
            line = ""
        u, _, p = line.partition(":")
        _AUTH_CACHE["v"] = (u, p) if (u and p) else ("", "")
        _AUTH_CACHE["t"] = now
    return _AUTH_CACHE["v"]


def safe_path(p):
    """规范化并拦截虚拟/设备目录，返回 None 表示不可访问。"""
    if not p:
        p = "/"
    if not (p.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", p)):
        p = "/" + p
    try:
        rp = os.path.realpath(p)
    except Exception:
        return None
    for b in BLOCKED_PREFIX:
        if rp == b or rp.startswith(b + "/"):
            return None
    return rp


def fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "-"
    if n < 1024:
        return f"{int(n)} B"
    for u in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {u}"
    return f"{n:.1f} PB"


def file_icon(name, is_dir):
    if is_dir:
        return "📁"
    n = name.lower()
    if n.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
        return "🖼"
    if n.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm")):
        return "🎬"
    if n.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a")):
        return "🎵"
    if n.endswith((".zip", ".gz", ".tar", ".xz", ".7z", ".bz2", ".tgz")):
        return "📦"
    if n.endswith((".py", ".sh", ".js", ".json", ".yml", ".yaml", ".toml", ".conf",
                   ".ini", ".service", ".html", ".css", ".md", ".txt", ".log")):
        return "📄"
    if n.endswith((".db", ".sqlite", ".sqlite3")):
        return "🗄"
    return "📃"


def render_files_page(path, show_hidden=False):
    q = lambda s: urllib.parse.quote(s, safe="")
    crumbs = ['<a href="/files?p=/">根目录</a>']
    parts = [x for x in path.split("/") if x]
    acc = ""
    for seg in parts:
        acc += "/" + seg
        crumbs.append(f'<a href="/files?p={q(acc)}">{html.escape(seg)}</a>')
    crumb = '<span class="sep">/</span>'.join(crumbs)

    up = ""
    if path != "/":
        parent = os.path.dirname(path.rstrip("/")) or "/"
        up = f'<a class="btn" href="/files?p={q(parent)}">↑ 上级目录</a>'

    rows = []
    try:
        entries = list(os.scandir(path))
    except PermissionError:
        entries = []
        rows.append('<tr><td colspan="4" class="err">无权限读取该目录</td></tr>')
    except Exception as e:
        entries = []
        rows.append(f'<tr><td colspan="4" class="err">{html.escape(str(e))}</td></tr>')

    dirs, files = [], []
    for e in entries:
        if not show_hidden and e.name.startswith("."):
            continue
        try:
            is_dir = e.is_dir(follow_symlinks=False)
            st = e.stat(follow_symlinks=False)
        except Exception:
            continue
        (dirs if is_dir else files).append((e.name, is_dir, st))
    dirs.sort(key=lambda x: x[0].lower())
    files.sort(key=lambda x: x[0].lower())

    for name, is_dir, st in (dirs + files)[:800]:
        full = os.path.join(path, name) if path != "/" else "/" + name
        fq = q(full)
        nm = html.escape(name) + ("/" if is_dir else "")
        ts = datetime.fromtimestamp(st.st_mtime, BJ).strftime("%m-%d %H:%M")
        size = "—" if is_dir else fmt_size(st.st_size)
        if is_dir:
            link = f'<a class="nm" href="/files?p={fq}">{file_icon(name, True)} {nm}</a>'
            act = ""
        else:
            link = f'<span class="nm">{file_icon(name, False)} {nm}</span>'
            act = f'<a class="btn dl" href="/dl?p={fq}">下载</a>'
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".txt", ".log", ".json", ".md")):
                act += f' <a class="btn" href="/view?p={fq}" target="_blank">预览</a>'
        rows.append(f'<tr><td>{link}</td><td class="num">{size}</td>'
                    f'<td class="num">{ts}</td><td class="act">{act}</td></tr>')

    if not rows:
        rows.append('<tr><td colspan="4" class="err">空目录</td></tr>')

    return (FILES_PAGE
            .replace("__CRUMB__", crumb)
            .replace("__UP__", up)
            .replace("__P__", html.escape(path))
            .replace("__PQ__", q(path))
            .replace("__ROWS__", "".join(rows))
            .replace("__COUNT__", str(len(dirs) + len(files)))
            .replace("__HIDDEN__", "1" if show_hidden else "0")
            .replace("__HTOGGLE__", "0" if show_hidden else "1")
            .replace("__HIDDEN_LABEL__", "隐藏点文件" if show_hidden else "显示点文件")
            .replace("__TIME__", time.strftime("%Y-%m-%d %H:%M:%S")))


class Handler(BaseHTTPRequestHandler):
    # ---- 文件浏览用的 Basic 认证 + 流式下载 ----
    def _files_auth(self):
        user, pw = files_auth_conf()
        if not user:
            msg = ("未设置访问密码。请在 KVM 终端或 SSH 执行：\n"
                   "  echo 'kvm:你的密码' > /etc/kvm/files.pass && chmod 600 /etc/kvm/files.pass\n"
                   "  systemctl restart nanokvm-status\n")
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())
            return False
        raw = self.headers.get("Authorization", "")
        got = ""
        if raw.startswith("Basic "):
            try:
                got = base64.b64decode(raw[6:]).decode("utf-8", "replace")
            except Exception:
                got = ""
        if hmac.compare_digest(got, f"{user}:{pw}"):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="NanoKVM Files", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("需要登录（用户名/密码见 /etc/kvm/files.pass）".encode())
        return False

    def _not_found(self, msg="not found"):
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(msg.encode())

    def _send_file(self, fp, inline=False):
        """分块流式发送，避免把大文件读进内存（设备只有 256MB）。"""
        name = os.path.basename(fp)
        ext = os.path.splitext(name)[1].lower()
        inline_ok = ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                            ".txt", ".log", ".json", ".md", ".csv", ".conf",
                            ".ini", ".yml", ".yaml", ".service", ".sh", ".py", ".toml")
        if inline and not inline_ok:
            inline = False
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ext in (".txt", ".log", ".json", ".md", ".csv", ".conf", ".ini",
                   ".yml", ".yaml", ".service", ".sh", ".py", ".toml"):
            ctype = "text/plain; charset=utf-8"
        try:
            size = os.path.getsize(fp)
        except Exception:
            return self._not_found()
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
        disp = "inline" if inline else "attachment"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f"{disp}; filename=\"{ascii_name}\"; "
                             f"filename*=UTF-8''{urllib.parse.quote(name, safe='')}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path == "/api/status":
                body = json.dumps(snapshot_full()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif path == "/api/model-status":
                body = json.dumps(memfab_llm_status(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif path == "/api/memory":
                body = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                   "stats": memfab_stats(),
                                   "memories": memfab_memories(),
                                   "observations": memfab_observations()},
                                  ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif path == "/api/memory/fragment":
                body = json.dumps(render_memory_fragments(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif path == "/memory":
                body = render_memory_page().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif path == "/shots":
                body = render_shots_page().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif path in ("/files", "/dl", "/view"):
                if not self._files_auth():
                    return
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                fp = safe_path((qs.get("p") or ["/"])[0])
                if fp is None:
                    return self._not_found("该路径不可访问")
                if path == "/files":
                    if os.path.isfile(fp):
                        return self._send_file(fp, inline=False)
                    if not os.path.isdir(fp):
                        return self._not_found("目录不存在")
                    show_hidden = (qs.get("h") or ["0"])[0] == "1"
                    body = render_files_page(fp, show_hidden).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not os.path.isfile(fp):
                    return self._not_found()
                return self._send_file(fp, inline=(path == "/view"))
            elif path == "/shot/live":
                # 最近一轮抓到的原始帧（每轮覆盖写，最多 INTERVAL 秒旧）
                fp = "/var/lib/kvm-ocr/frame.jpg"
                if not os.path.isfile(fp):
                    self.send_response(404)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(b"no frame")
                    return
                body = open(fp, "rb").read()
                mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(fp)))
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Time", mtime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            elif path == "/shot":
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                name = (qs.get("f") or [""])[0]
                if not SHOT_RE.match(name):
                    self.send_response(400)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(b"bad name")
                    return
                fp = os.path.join(SHOTS_DIR, name)
                if not os.path.isfile(fp):
                    self.send_response(404)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                body = open(fp, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            else:
                s = snapshot_full()
                mt, mu = s["mem_total_kb"], s["mem_used_kb"]
                dt, du = s["disk_total_gb"], s["disk_used_gb"]
                temp = s["temp"]
                svc_rows = "".join(
                    f'<tr><td>{k}</td><td><span class="dot {"ok" if v else "off"}"></span>{"开启" if v else "关闭"}</td></tr>'
                    for k, v in s["services"].items())
                core_rows = "".join(
                    f'<tr><td>{k}</td><td><span class="dot {"ok" if v else "off"}"></span>{"运行中" if v else "已停止"}</td></tr>'
                    for k, v in s["core"].items())
                html_out = (PAGE
                            .replace("__TIME__", s["time"])
                            .replace("__CPU__", str(s["cpu"]))
                            .replace("__LOAD__", ", ".join(f"{x:.2f}" for x in s["loadavg"]))
                            .replace("__MEM_USED__", str(mu // 1024))
                            .replace("__MEM_TOTAL__", str(mt // 1024))
                            .replace("__MEM_PCT__", str(round(100.0 * mu / mt, 1)))
                            .replace("__TEMP__", f"{temp:.1f}" if temp else "--")
                            .replace("__TCLS__", "ok" if (temp or 0) < 65 else ("warn" if (temp or 0) < 75 else "bad"))
                            .replace("__DISK_USED__", str(du))
                            .replace("__DISK_TOTAL__", str(dt))
                            .replace("__DISK_PCT__", str(round(100.0 * du / dt, 1)))
                            .replace("__UPTIME__", s["uptime"])
                            .replace("__KERNEL__", os.uname().release)
                            .replace("__SVC_ROWS__", svc_rows)
                            .replace("__CORE_ROWS__", core_rows)
                            .replace("__TL_MB__", str(s["screen_timelapse_size_mb"])))
                body = html_out.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
            except Exception:
                pass

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler).serve_forever()
