#!/bin/sh
# /memory 面板 —— 依赖一键安装脚本
#
# 用法（SSH 登录设备后，用 root 执行）：
#   sh /opt/status-dashboard/install_deps.sh -p '<网页 admin 密码>'
#
# 可选参数：
#   -u host:port   局域网 LM Studio 地址（默认 192.168.110.71:1234）
#   -i <秒>        OCR 轮间隔（默认 120；小内存机型建议 180）
#   --no-ocr       只装 memfab + llm-proxy + 页面，不启用 OCR（内存紧张时用）
#   -p <密码>      用于调用网页接口自动开启 MCP；不写入任何文件
#
# 需要 root：要写 /etc/systemd/system、/opt、/var/lib/memfab，并执行 apt-get。
# 若由应用商店的 post_script 调用且权限不足，会写 DEPS_INSTALL_REQUIRED 标记，
# 页面「依赖安装与环境说明」区块会提示改走 SSH root 手动执行。

set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PW="${KVM_WEB_PW:-}"
UPSTREAM="192.168.110.71:1234"
OCR_INTERVAL=120
NO_OCR=0
MARK="$APP_DIR/DEPS_INSTALL_REQUIRED"

while [ $# -gt 0 ]; do
  case "$1" in
    -p) PW="$2"; shift 2 ;;
    -u) UPSTREAM="$2"; shift 2 ;;
    -i) OCR_INTERVAL="$2"; shift 2 ;;
    --no-ocr) NO_OCR=1; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

log() { echo "[deps] $*"; }
warn() { echo "[deps][WARN] $*" >&2; }

# ---------- 0. 权限 ----------
if [ "$(id -u)" != "0" ]; then
  warn "需要 root 权限（当前 uid=$(id -u)）。请 SSH 登录后用 root 执行本脚本。"
  touch "$MARK" 2>/dev/null || true
  exit 1
fi
rm -f "$MARK" 2>/dev/null || true

# ---------- 1. 系统包 ----------
need_pkg=0
command -v tesseract >/dev/null 2>&1 || need_pkg=1
if command -v tesseract >/dev/null 2>&1; then
  tesseract --list-langs 2>/dev/null | grep -q chi_sim || need_pkg=1
fi
/usr/bin/python3 -c "import PIL" 2>/dev/null || need_pkg=1

if [ "$need_pkg" = "1" ]; then
  log "安装 tesseract / 中文语言包 / Pillow ..."
  apt-get update -qq 2>/dev/null || warn "apt-get update 失败，尝试直接安装"
  apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-chi-sim python3-pil || {
    warn "apt 安装失败（应用商店环境常无软件源权限）。请 SSH root 手动执行："
    warn "  apt-get update && apt-get install -y tesseract-ocr tesseract-ocr-chi-sim python3-pil"
    touch "$MARK"
    exit 1
  }
else
  log "系统包已齐全（tesseract + chi_sim + Pillow）"
fi

# ---------- 2. MCP 密钥（kvm-ocr 截图用） ----------
if [ ! -s /etc/kvm/ai.json ]; then
  if [ -n "$PW" ]; then
    log "通过网页接口开启 MCP ..."
    /usr/bin/python3 - "$PW" <<'PY' > /tmp/_mcp_login.json
import json, sys
print(json.dumps({"username": "admin", "password": sys.argv[1]}))
PY
    TOKEN=$(curl -sk -m 15 -X POST https://127.0.0.1/api/auth/login \
        -H 'Content-Type: application/json' -d @/tmp/_mcp_login.json \
        | /usr/bin/python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["token"])' 2>/dev/null || true)
    rm -f /tmp/_mcp_login.json
    if [ -n "$TOKEN" ]; then
      curl -sk -m 20 -X POST https://127.0.0.1/api/ai/config \
          -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
          -d '{"mcpEnabled":true}' >/dev/null 2>&1 || warn "MCP 开启请求失败"
    fi
  fi
  if [ ! -s /etc/kvm/ai.json ]; then
    warn "未检测到 /etc/kvm/ai.json。请在网页「设置 → AI → MCP」打开开关，或用 -p '<网页密码>' 重跑本脚本。"
    warn "kvm-ocr 依赖该密钥截图，缺它时 OCR 无法工作。"
  fi
else
  log "MCP 密钥已存在"
fi

# ---------- 3. 原厂 memfab（记忆织网） ----------
if [ -f /lib/systemd/system/memfab.service ] || [ -f /usr/lib/systemd/system/memfab.service ]; then
  systemctl enable --now memfab >/dev/null 2>&1 || warn "systemctl 启动 memfab 失败"
else
  warn "未找到 memfab.service（本机可能不是 Go+ 机型）"
fi
sleep 2
[ -f /var/lib/memfab/config.toml ] || warn "config.toml 未生成，请确认 memfab 已启动"

# ---------- 4. 配置 memfab 指向本地 llm-proxy ----------
/usr/bin/python3 - "$UPSTREAM" <<'PY'
import sys
p = "/var/lib/memfab/config.toml"
try:
    lines = open(p, encoding="utf-8").read().splitlines()
except OSError:
    sys.exit("config.toml 不存在，跳过配置")
up = sys.argv[1]
want = {
    "models.default": {
        "model": '"mistralai/ministral-3-14b-reasoning"',
        "base_url": '"http://127.0.0.1:1235/v1"',
        "api_key": '"local"',
        "max_tokens": "4096",
        "timeout_seconds": "180",
    },
    "llm_trace": {
        "enabled": "true",
        "path": '"/var/lib/memfab/logs/llm-trace.jsonl"',
    },
    "timeline": {"recent_context_blocks": "12"},
}
out, sec = [], None
for ln in lines:
    s = ln.strip()
    if s.startswith("[") and s.endswith("]"):
        if sec in want:
            for k, v in want.pop(sec).items():
                out.append("%s = %s" % (k, v))
        sec = s[1:-1]
        out.append(ln)
        continue
    if sec in want and s and not s.startswith("#") and "=" in s:
        k = s.split("=", 1)[0].strip()
        if k in want[sec]:
            out.append("%s = %s" % (k, want[sec].pop(k)))
            continue
    out.append(ln)
if sec in want:
    for k, v in want.pop(sec).items():
        out.append("%s = %s" % (k, v))
for name, kv in want.items():
    out.append("")
    out.append("[%s]" % name)
    for k, v in kv.items():
        out.append("%s = %s" % (k, v))
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
print("memfab 配置已更新：模型经 127.0.0.1:1235 转发到 %s，llm_trace 开启，上下文块数 12" % up)
PY
mkdir -p /var/lib/memfab/logs
systemctl restart memfab >/dev/null 2>&1 || true

# ---------- 5. 部署 llm-proxy / kvm-ocr ----------
mkdir -p /opt/llm-proxy /opt/kvm-ocr /var/lib/kvm-ocr
cp "$APP_DIR/llm_proxy.py" /opt/llm-proxy/main.py
cp "$APP_DIR/kvm_ocr.py"   /opt/kvm-ocr/main.py
chmod 644 /opt/llm-proxy/main.py /opt/kvm-ocr/main.py
sed -i "s#http://192.168.110.71:1234#http://$UPSTREAM#" /opt/llm-proxy/main.py

cat > /etc/systemd/system/llm-proxy.service <<'UNIT'
[Unit]
Description=memfab LLM proxy (LM Studio compatibility shim)
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/llm-proxy/main.py
Restart=always
RestartSec=3
MemoryHigh=32M
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/kvm-ocr.service <<'UNIT'
[Unit]
Description=NanoKVM local full-text OCR (tesseract)
After=network-online.target memfab.service
[Service]
ExecStart=/usr/bin/python3 /opt/kvm-ocr/main.py
Restart=always
RestartSec=5
MemoryHigh=140M
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/nanokvm-status.service <<'UNIT'
[Unit]
Description=NanoKVM Go+ Status Dashboard
After=multi-user.target
[Service]
ExecStart=/usr/bin/python3 /opt/status-dashboard/main.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# 用 restart 而非 enable --now：重复安装时才能加载新版本的 Python/HTML
systemctl enable llm-proxy nanokvm-status >/dev/null 2>&1 || true
systemctl restart llm-proxy >/dev/null 2>&1 || warn "llm-proxy 启动失败"
systemctl restart nanokvm-status >/dev/null 2>&1 || warn "nanokvm-status 启动失败"
if [ "$NO_OCR" = "1" ]; then
  log "按 --no-ocr 跳过 kvm-ocr"
  systemctl disable kvm-ocr >/dev/null 2>&1 || true
  systemctl stop kvm-ocr >/dev/null 2>&1 || true
else
  # 小内存机型：拉长 OCR 间隔，避免 tesseract 与 4K 采集同时把 CPU/交换分区打满
  sed -i "s/^INTERVAL = .*/INTERVAL = $OCR_INTERVAL          # 秒，一轮间隔（由 install_deps.sh 设置）/" /opt/kvm-ocr/main.py
  systemctl enable kvm-ocr >/dev/null 2>&1 || true
  systemctl restart kvm-ocr >/dev/null 2>&1 || warn "kvm-ocr 启动失败"
fi

# ---------- 6. 结果 ----------
log "=== 依赖状态 ==="
log "OCR 间隔：${OCR_INTERVAL}s（可用 -i 调整；--no-ocr 关闭）"
for s in memfab llm-proxy kvm-ocr nanokvm-status; do
  printf '  %-18s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || true)"
done
[ -s /etc/kvm/ai.json ] && log "  MCP 密钥        已生成" || warn "  MCP 密钥        缺失（OCR 无法截图）"
tesseract --list-langs 2>/dev/null | grep -q chi_sim && log "  chi_sim         已安装" || warn "  chi_sim         缺失"
log "页面：http://<kvm-ip>:8123/memory"
log "若某项未就绪，展开页面「依赖安装与环境说明」查看手动步骤。"
