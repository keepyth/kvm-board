# kvm-board — NanoKVM-Go+ 私有应用源

在 NanoKVM-Go 网页「设置 → 应用 → 应用源 → 添加服务器」填入本仓库 URL 即可安装应用。

## 应用列表
- **status-dashboard**：设备状态仪表盘。安装时自动部署 systemd 服务 `nanokvm-status`，浏览器访问 `http://<kvm-ip>:8123`（完整面板）+ `/api/status`（JSON API）。触摸屏应用本身仅显示服务状态、点按启停。`/memory` 为记忆织网面板（memfab 长期记忆 + 屏幕全文识别）。

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
