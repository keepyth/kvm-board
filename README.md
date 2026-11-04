# kvm-board — NanoKVM-Go+ 私有应用源

在 NanoKVM-Go 网页「设置 → 应用 → 应用源 → 添加服务器」填入本仓库 URL 即可安装应用。

## 应用列表
- **status-dashboard**：设备状态仪表盘。安装时自动部署 systemd 服务 `nanokvm-status`，浏览器访问 `http://<kvm-ip>:8123`（完整面板）+ `/api/status`（JSON API）。触摸屏应用本身仅显示服务状态、点按启停。`/memory` 为记忆织网面板（memfab 长期记忆 + 屏幕全文识别），`/shots` 为截图存档，`/files` 为**网页文件管理器**（浏览设备硬盘、点一下下载；Basic 认证，账号密码在 `/etc/kvm/files.pass`，只读，屏蔽 `/proc` `/sys` `/dev` `/run`）。

## /memory 面板的依赖安装

应用商店安装 `status-dashboard` 时，`post_script` 会调用应用包内的
`service/install_deps.sh` **一并安装全部依赖**；若沙箱权限不足（无法 `apt-get`、
写 `/etc/systemd/system` 或调用网页接口），会写 `DEPS_INSTALL_REQUIRED` 标记，
页面「📦 依赖安装与环境说明」折叠区块会给出依赖状态、一键命令和逐项手动步骤。

依赖清单：

| 组件 | 来源 / 作用 |
|---|---|
| `memfab` | 原厂 Go+ 自带 `/usr/bin/memfab`，开启后生成 `/var/lib/memfab` |
| MCP 密钥 | `/etc/kvm/ai.json`，网页「设置 → AI → MCP」开启，供 kvm-ocr 截图 |
| OCR 引擎 | `tesseract-ocr` + `tesseract-ocr-chi-sim` + `python3-pil` |
| `llm-proxy` | 127.0.0.1:1235 → 局域网 LM Studio（默认 192.168.110.71:1234） |
| `kvm-ocr` | tesseract v3 结构化识别 → 写入 memfab observation-buffer |
| `nanokvm-status` | 页面本体，8123 |

一键安装（SSH 登录设备，用 root 执行）：

```sh
sh /opt/status-dashboard/install_deps.sh -p '<网页admin密码>'
```

可选参数：`-u host:port` 改上游模型地址、`-i <秒>` 调整 OCR 间隔（默认 120）、`--no-ocr` 只装记忆链路（内存紧张时用）。
脚本需要 root；应用商店 `post_script` 权限不足时，改用 SSH root 执行同一条命令即可。

## 附带组件（手动部署，非应用）
- **kvm-ocr**：本地结构化 OCR 服务 v3（systemd `kvm-ocr`，部署到 `/opt/kvm-ocr/main.py`）。
  每 60 秒经 MCP 截图 → 灰度 1.5x 放大 → 自适应分栏 → tesseract psm4 TSV 识别（文字+每行坐标一次拿到）
  → 生成层次化 markdown（栏 → 区块（行间距聚类）→ 行，区块标注上部/中部/下部位置）→ 写入 memfab
  observation-buffer（engine=`tesseract_local`，ocr.meta 带行数/耗时/分栏模式）。
  文本质量与 v2 持平（同帧去空白 1126 字 / 关键词 18-21 命中），阅读顺序与页面结构一致。
  无视频信号（被控机睡眠/关机）时每 15 秒轻量重试并明确记日志。
- **status-service /memory 页面**：长期记忆默认只显示文件名列表（带来源 ID 徽标），点击展开正文与溯源
  （「从 ID#x~#y 中提取（北京时间范围，约 N 分钟，M 条）」）；屏幕观察默认一行摘要，点击弹窗显示
  结构化全文（markdown 渲染）；观察有全局稳定 ID#；所有时间统一北京时间 `YYYY-MM-DD HH:MM:SS`；
  页面响应 no-store 禁缓存。
- **llm-proxy**：systemd `llm-proxy`（监听 127.0.0.1:1235，转发到 LM Studio）。职责：剥离 `response_format: json_object`、剥 markdown 围栏、
  注入中文实体保真指令、**输入预算裁剪**（删除无用的 bbox 坐标 + 压缩超长 text 字段到 8500 字符预算内），
  防止 timeline 输入超出 LM Studio 8192 上下文（实测 3B/14B 的 timeline 失败率 82%/82% → 修复后 100% 成功）。
  配合 memfab 配置 `recent_context_blocks = 12`。
