# kvm-board — NanoKVM-Go+ 私有应用源

在 NanoKVM-Go 网页「设置 → 应用 → 应用源 → 添加服务器」填入本仓库 URL 即可安装应用。

## 应用列表
- **status-dashboard**：设备状态仪表盘。安装时自动部署 systemd 服务 `nanokvm-status`，浏览器访问 `http://<kvm-ip>:8123`（完整面板）+ `/api/status`（JSON API）。触摸屏应用本身仅显示服务状态、点按启停。
