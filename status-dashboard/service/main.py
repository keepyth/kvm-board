#!/usr/bin/env python3
"""NanoKVM-Go+ 后台状态服务：Web 仪表盘 + JSON API，端口 8123。
/memory —— 记忆织网面板（memfab 记忆与屏幕观察）"""

import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BJ = timezone(timedelta(hours=8))
ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?")

WEB_PORT = 8123
MEMFAB = "/var/lib/memfab"
BASE = os.path.dirname(os.path.abspath(__file__))
PAGE = open(os.path.join(BASE, "page.html"), encoding="utf-8").read()
MEM_PAGE = open(os.path.join(BASE, "memory.html"), encoding="utf-8").read()


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
        out.append({
            "id": pos,
            "time": fmt_bj(j.get("captured_at") or ""),
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


def render_memory_page():
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
            f'<div class="card memcard"><div class="memhead" onclick="toggleMem(this)">'
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
        if engine == "tesseract_local":
            badge = '<span class="badge tess">⚙ 本地结构化 v3</span>'
        elif engine == "nano_ocr_npu":
            badge = '<span class="badge npu">⚡ NPU 快速</span>'
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
        obs_cards += (
            f'<div class="card{" latest" if i == 0 and engine == "tesseract_local" else ""}">'
            f'<div class="obshead"><span class="oid">ID#{o["id"]}</span>'
            f'<span class="ot" data-full="{t}" onclick="toggleTime(this)">{esc(rel_time(o["time"]))}</span>'
            f'{badge}<span class="conf">{meta_txt}</span></div>'
            f'<div class="preview" onclick="showObs(this)" title="点击查看完整识别内容">{preview}</div>'
            f'<pre class="fulltext" hidden>{esc(o["text"])}</pre></div>')

    return (MEM_PAGE
            .replace("__STATS_CARDS__", stat_cards)
            .replace("__SVC__", svc)
            .replace("__MEM_CARDS__", mem_cards)
            .replace("__OBS_CARDS__", obs_cards)
            .replace("__TIME__", time.strftime("%Y-%m-%d %H:%M:%S")))


class Handler(BaseHTTPRequestHandler):
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
            elif path == "/memory":
                body = render_memory_page().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
