#!/usr/bin/env python3
"""NanoKVM-Go+ 后台状态服务：Web 仪表盘 + JSON API，端口 8123。
/memory —— 记忆织网面板（memfab 记忆与屏幕观察）"""

import html
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def memfab_memories(limit=12):
    """读取 /var/lib/memfab/memory/*.md（YAML frontmatter + 正文），按文件名倒序。"""
    out = []
    d = os.path.join(MEMFAB, "memory")
    if not os.path.isdir(d):
        return out
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
        out.append({"name": name, "meta": meta, "body": body.strip()[:2000]})
    return out


def memfab_observations(limit=25):
    """读取 observation-buffer 最新 N 条 OCR 观察，倒序返回。"""
    out = []
    d = os.path.join(MEMFAB, "observation-buffer")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d))[-limit:]:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                j = json.load(f)
        except Exception:
            continue
        ocr = j.get("ocr") or {}
        ctx = j.get("context") or {}
        out.append({
            "time": (j.get("captured_at") or "")[:19].replace("T", " "),
            "app": ctx.get("app_name", ""),
            "title": ctx.get("title", ""),
            "text": ocr.get("text", ""),
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


def memfab_llm_status():
    """解析 llm-trace.jsonl：模型调用统计 + 最近一次对话详情。"""
    info = {"model": "", "last_time": "--", "last_stage": "", "today_calls": 0,
            "total_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "last_content": "", "last_request": "",
            "last_response": "", "last_ok": True, "last_duration_ms": None}
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
                info["last_time"] = ts[:19].replace("T", " ")
                info["last_stage"] = e.get("stage", "")
                info["model"] = e.get("model", info["model"])
                info["last_content"] = content[:600]
                info["last_request"] = e.get("request") or ""
                info["last_response"] = parsed if parsed is not None else raw
                info["last_ok"] = not e.get("error")
                info["last_duration_ms"] = e.get("duration_ms")
    except OSError:
        pass
    return info


def esc(s):
    return html.escape(str(s))


def render_model_section():
    """模型工作状态卡片区（数据来自 llm-trace）。"""
    s = memfab_llm_status()
    if not s["model"]:
        return '<div class="card"><div class="sub">暂无模型调用记录（llm-trace 未开启或无数据）</div></div>'
    ok_badge = '<span class="ok">✓ 成功</span>' if s["last_ok"] else '<span class="bad">✗ 失败</span>'
    dur = f"{s['last_duration_ms']}ms" if s["last_duration_ms"] is not None else "--"
    last_content = esc(s["last_content"] or "(无文本返回)")
    req_json = esc(json.dumps(s["last_request"], ensure_ascii=False, indent=2)[:4000]) if s["last_request"] else "(无)"
    resp = s["last_response"]
    resp_json = esc(json.dumps(resp, ensure_ascii=False, indent=2)[:6000]) if resp else "(无)"
    stage_cn = {"timeline": "时间线整理", "reducer": "长期记忆归约", "compact": "记忆压缩"}.get(s["last_stage"], s["last_stage"] or "--")
    return f'''<div class="grid">
<div class="card"><h2>模型</h2><div class="big" style="font-size:15px;word-break:break-all">{esc(s["model"])}</div>
<div class="sub">经 llm-proxy → LM Studio (192.168.110.71:1234)</div></div>
<div class="card"><h2>上次对话</h2><div class="big" style="font-size:15px">{esc(s["last_time"])}</div>
<div class="sub">{esc(stage_cn)} · {ok_badge} · 耗时 {dur}</div></div>
<div class="card"><h2>今日对话</h2><div class="big">{s["today_calls"]}<span class="unit"> 次</span></div>
<div class="sub">累计 {s["total_calls"]} 次</div></div>
<div class="card"><h2>Token 消耗</h2><div class="big" style="font-size:22px">{s["total_tokens"]:,}<span class="unit"> tokens</span></div>
<div class="sub">输入 {s["prompt_tokens"]:,} / 输出 {s["completion_tokens"]:,}</div></div>
</div>
<div class="card"><h2>上次对话内容（模型返回）</h2><pre class="body">{last_content}</pre></div>
<details class="card"><summary>上次对话原始出/入参数（点击展开）</summary>
<div class="kv"><b>请求 (request)：</b><pre class="body">{req_json}</pre>
<b>响应 (response_raw)：</b><pre class="body">{resp_json}</pre></div>
</details>'''


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
        upd = esc(meta.get("updated", ""))
        tags = esc(meta.get("tags", "").strip("[]"))
        mem_cards += (f'<div class="card"><h2>{esc(m["name"])}</h2>'
                      f'<div class="sub">{desc} · 更新于 {upd} · 标签: {tags}</div>'
                      f'<pre class="body">{esc(m["body"])}</pre></div>')

    obs_rows = ""
    if not obs:
        obs_rows = '<tr><td colspan="3">暂无观察记录</td></tr>'
    for o in obs:
        conf = f"{o['confidence']:.2f}" if isinstance(o["confidence"], (int, float)) else "--"
        text = esc(o["text"][:160])
        obs_rows += (f'<tr><td class="nowrap">{esc(o["time"])}</td>'
                     f'<td class="nowrap">{esc(o["app"])} · {esc(o["title"])}</td>'
                     f'<td>{text}<span class="conf"> ({conf})</span></td></tr>')

    return (MEM_PAGE
            .replace("__STATS_CARDS__", stat_cards)
            .replace("__SVC__", svc)
            .replace("__MODEL_SECTION__", render_model_section())
            .replace("__MEM_CARDS__", mem_cards)
            .replace("__OBS_ROWS__", obs_rows)
            .replace("__TIME__", time.strftime("%Y-%m-%d %H:%M:%S")))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path == "/api/status":
                body = json.dumps(snapshot_full()).encode()
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
