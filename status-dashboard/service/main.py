#!/usr/bin/env python3
"""NanoKVM-Go+ 后台状态服务：Web 仪表盘 + JSON API，端口 8123。"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_PORT = 8123


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


def snapshot():
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": round(cpu_percent(), 1),
        "mem_total_kb": None, "mem_used_kb": None,  # filled below
        "disk_total_gb": None, "disk_used_gb": None,
        "temp": temperature(),
        "uptime_s": uptime_s(), "uptime": fmt_uptime(uptime_s()),
        "services": services(),
        "core": core_services_up(),
        "loadavg": os.getloadavg(),
        "screen_timelapse_size_mb": timelapse_size(),
    }


def timelapse_size():
    total = 0
    for base in ("/var/lib/nanokvm", "/data/screen_timelapse", "/kvmcomm"):
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
    return round(total / 1e6, 1)


def snapshot_full():
    s = snapshot()
    mt, mu = mem_info()
    s["mem_total_kb"], s["mem_used_kb"] = mt, mu
    dt, du = disk_usage()
    s["disk_total_gb"], s["disk_used_gb"] = round(dt / 1e9, 1), round(du / 1e9, 1)
    return s


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"), encoding="utf-8").read()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == "/api/status":
                body = json.dumps(snapshot_full()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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
                html = (PAGE
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
                body = html.encode()
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
