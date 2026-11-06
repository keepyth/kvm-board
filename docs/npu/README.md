# NanoKVM-Go+ 的 NPU 到底能不能用？——实测、接入与边界

> 结论先行：**NPU 早就在用，只是厂商没暴露给我们。**
> 设备自带的 `/kvmcomm/ocr/nano_ocr_app` 就是跑在 NPU 上的 PP-OCR（det+rec），
> 我们把自己写的 `kvm-ocr` 从 tesseract（纯 CPU）切到了它：**单屏 40 秒 → 17 秒，文本质量还更好。**

---

## 1. 硬件事实（实测，非宣传口径）

| 项目 | 实测值 | 来源 |
|---|---|---|
| SoC | `axera,ax620e`（爱芯元智 AX620E 系列） | `/proc/device-tree/compatible` |
| CPU | 2 × ARMv7 (Cortex-A53) | `nproc` / `uname -m` |
| 内存 | 246 MB（可用 ~90 MB），Swap 122 MB | `free -m` |
| 内核 | 4.19.125 armv7l | `uname -a` |
| NPU 驱动 | `ax_npu.ko` 已加载，节点 `/dev/npu` | `/proc/modules`、`/dev` |
| NPU 运行时 | `/opt/lib/libax_engine.so`、`libax_interpreter.so` | `ldd` 厂商二进制 |
| 现成模型 | `/kvmcomm/models/pp_ocr/ch_PP_OCRv3_det_npu.axmodel`、`ch_PP_OCRv4_rec_npu.axmodel` | 文件系统 |
| 其他硬件单元 | IVE（图像处理）、VENC/VDEC（**这才是真正的"硬解"**）、GDC、AVS | `/dev/ax_*` 共 20 个节点 |

> 算力：爱芯官方页面只写"通元 4.0 混合精度 NPU"，未公布 AX620E 的 TOPS 具体值；
> "3.2 TOPS" 是 Sipeed 侧的宣传数字，**没有官方出处，别当硬指标引用**。

## 2. Tesseract 算不算"软解码"？

不算。"软解/硬解"是**视频编解码**的说法；OCR 没有这个分类。

- Tesseract 5.5 是**纯 CPU 软件实现**：图像二值化 → 连通域/版面分析 → LSTM 前向推理，最多用到 ARM NEON 指令集。
- 它**完全不碰 NPU**，也没有任何硬件加速路径可开。
- 所以之前一屏要 40 秒 —— 2 核 A53 硬跑神经网络，就是这个速度。

## 3. 同一张截图，两条链路的实测对比

样本：1920×1080 的财联社网页截图（`/var/lib/kvm-ocr/shots/20261105T072354Z.jpg`）

| 指标 | tesseract chi_sim psm4 | PP-OCR on NPU（1×） | PP-OCR on NPU（1.5× 放大） |
|---|---|---|---|
| 端到端耗时 | **39.8 s** | 8.0 s | 11.1 s |
| 经 kvm-ocr+分栏后 | 60 s | 12 s | **17 s** |
| 识别块/行数 | 36 行（混栏） | 74 块 | 91 块（分栏干净） |
| 典型错例 | `WERtA股24小时电报`、`httpsWwwwxclscn` | `C财跌社`、`办交部` | `财联社`、`外交部发言人郭嘉昆`（正确） |
| 版面 | 左右栏混在一行 | 按 y 排序 | 按 x 中心正确分左右栏 |

结论：**1.5× 放大喂 NPU 是甜点**——速度仍是 tesseract 的 2.3 倍，准确率反超。
1× 虽然只要 8 秒，但小字号（14 px 左右）识别明显退化，不建议。

## 4. 我们改了什么

`kvm-ocr/main.py` 增加引擎开关（默认 `npu`，失败自动回落 tesseract）：

```bash
# 默认（NPU）
systemctl restart kvm-ocr

# 临时切回 tesseract 做对照
OCR_ENGINE=tesseract systemctl restart kvm-ocr   # 或在 unit 里加 Environment=OCR_ENGINE=tesseract
```

实现要点（踩过的坑，改代码的人注意）：

1. `nano_ocr_app --image X.png` 会在**同目录**产出 `X_output_result.json`，结构是
   `{"ocr":{"text":..., "blocks":[{"text","bbox":[x0,y0,x1,y1],"confidence"}]}}`，坐标相对输入图。
2. 它的 JSON **偶尔含非法 UTF-8 字节**，必须 `decode("utf-8","replace")` 或退 `gb18030`，否则整轮回落。
3. 它的 stdout 是二进制日志流，`subprocess` **不能带 `text=True`**，否则直接抛解码异常。
4. 坐标随放大倍数变化，`to_markdown()` 里的 `/SCALE` 必须跟着引擎走。
5. NPU 一次全帧跑完（PP-OCR 自带版面检测），再按行的 x 中心分左右栏，比"先切成两张图各跑一次"快一倍。

## 5. 会不会和现有功能冲突？

**不会，实测过并发场景。** 依据：

- NPU 由 `ax_engine` 统一调度，支持多进程；设备上 `kvm_vin`（采集）本来就一直映射着 `libax_engine`，
  我们又同时跑 `nano_ocr_app`，两边都正常，没有 `device busy`。
- 厂商自带的 `nano_ocr.service`（守护式连续 OCR）现在是 **failed 且 disabled**：
  它在 13 分钟里烧了 6 分 20 秒 CPU，还因为"上传 MCP 观测失败"反复重试被 systemd 超时杀掉。
  我们走的是"画面像素有变化才识别 + 180 秒一轮"的按需模式，负载低得多，**不建议再把它打开**。
- 真正的风险是**内存**，不是 NPU 本身：本机只有 246 MB，NPU 进程按 CMM（连续内存）分配，
  和 `kvm-ocr.service` 的 `MemoryHigh=140M` 同一个 cgroup。压测时留意 `free -m` 和
  `systemctl show kvm-ocr -p MemoryCurrent`，别的没什么要注意的。
- **PC 上装 AXERA 工具链不会影响设备**：Pulsar2 是离线量化编译工具，产出 `.axmodel` 再拷进设备，两边零耦合。

## 6. 除了 OCR，这块 NPU 还能干什么

按"对 KVM 场景的性价比"排序（都在 3 TOPS 量级内，模型可量化到 1~5 MB）：

| 用途 | 价值 | 备注 |
|---|---|---|
| **UI 元素检测**（按钮/输入框/图标 bbox） | 高 | 解决 claw "看得见字、点不准位置"的老问题，可接 `press_keys`/点击坐标 |
| **屏幕变化/异常检测** | 高 | 比逐像素哈希更抗噪（弹窗、加载动画、蓝屏），可用于告警 |
| **验证码/二维码识别** | 中 | 二维码已有纯 CPU 方案，验证码才需要模型 |
| **表格/表单结构化** | 中 | PP-Structure 类模型，先把 OCR 结果给出 bbox 后处理也能凑 |
| **人脸/人体检测** | 低 | KVM 场景基本用不上 |
| **跑 VLM / 小语言模型** | 不建议 | 算力与 246 MB 内存都不够，本地大模型仍走 192.168.110.71 |

> 注意区分：**IVE / VENC / VDEC 是独立硬件单元，不算 NPU 算力**。
> 屏幕视频流能 1080p 流畅，靠的是 VENC/VDEC 硬编硬解，跟 NPU 无关。

## 7. 什么时候才需要装 Pulsar2 工具链

只有**要跑自己训练/换新的模型**时才需要：PC 上装 AXERA Pulsar2（Docker 镜像），
ONNX → 量化 → 编译成 `.axmodel`，再拷到设备由 `libax_engine` 加载。

如果只是想跑 OCR / 检测这类通用能力，**AXERA ModelZoo 里有现成 `.axmodel`**，
拷进去就能用，连工具链都不用碰。本次改造就属于这一类：**零新增依赖、零新装组件**。

## 8. 复现与验证

```bash
# 设备侧：单帧离线跑一遍（不依赖视频信号）
cp /opt/kvm-ocr/main.py /tmp/m_npu.py
cd /tmp && python3 -c "import sys;sys.path.insert(0,'/tmp');import m_npu as m;\
md,mode,n=m.ocr_frame('/var/lib/kvm-ocr/shots/<任意截图>.jpg');print(mode,n);print(md[:600])"

# 直接调厂商二进制看耗时明细
/kvmcomm/ocr/nano_ocr_app --image /var/lib/kvm-ocr/shots/<x>.jpg --time_log --log error
```

回滚：`cp /opt/kvm-ocr/main.py.tessbak /opt/kvm-ocr/main.py && systemctl restart kvm-ocr`

---

_最后更新：2026-11-06 · 实测设备 NanoKVM-Go+（192.168.110.193，AX620E）_
