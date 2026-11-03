# kvm-board — NanoKVM-Go+ 私有应用源

在 NanoKVM-Go 网页「设置 → 应用 → 应用源 → 添加服务器」填入本仓库 URL 即可安装应用。

## 应用列表
- **status-dashboard**：设备状态仪表盘。安装时自动部署 systemd 服务 `nanokvm-status`，浏览器访问 `http://<kvm-ip>:8123`（完整面板）+ `/api/status`（JSON API）。触摸屏应用本身仅显示服务状态、点按启停。`/memory` 为记忆织网面板（memfab 长期记忆 + 屏幕全文识别）。

## 附带组件（手动部署，非应用）
- **kvm-ocr**：本地全文 OCR 服务（systemd `kvm-ocr`，部署到 `/opt/kvm-ocr/main.py`）。
  每 60 秒经 MCP 截图 → 灰度 1.5x 放大 → 自适应分栏 → tesseract psm4 中文识别 → 写入 memfab
  observation-buffer（engine=`tesseract_local`）。v2 管线关键词召回 7/28 → 23/28（同一帧对比）。
  无视频信号（被控机睡眠/关机）时每 15 秒轻量重试并明确记日志。
- **llm-proxy**：systemd `llm-proxy`（监听 127.0.0.1:1235，转发到 LM Studio）。除剥离 `response_format: json_object` 和 markdown 围栏外，
  会向 system prompt 注入「中文专有名词逐字原样照抄」的保真指令，防止小模型把「东山精密」改写成「东山若」之类的错误。
  配合 memfab 配置 `[models.default] model = "mistralai/ministral-3-14b-reasoning"`（中文保真优于 3B），`timeout_seconds = 180`。
