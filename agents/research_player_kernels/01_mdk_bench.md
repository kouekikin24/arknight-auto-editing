# MDK（wang-bin）播放内核实测报告（对照 dsvc / mpv）

> 调研日期：2026-10-13（主会话串行，owner 令「按你的推荐执行」）
> 口径：同源（4.mp4 2.27GB/7069.9s/424176 帧）、同机（RTX 4060 Laptop + 16 核）、
> 同计时（seek 下发 → 首帧解码完成，wall clock）。
> 证据：`.cache/kernel_review/mdk_probe.cpp`（自建，MSVC 14.44 编译）+ 结果日志。
> 身份：MDK v0.39.0（git eed59da，2026-09-30 build），作者 WangBin（QtAV 作者，
> 其 mpv 提交 2015-2017 两条）。

## 0. 结论摘要（TL;DR）

**实测数字全面落在我们既有结论的一侧：MDK 没有任何超出我们物理墙的能力。**

1. **顺序解码吞吐**：MDK 软解 1273fps（60s chunk）/ **1903fps（全片 424k 帧）**；
   我们 dsvc 隔离 **2029fps**、本机 ffmpeg 软解 1835-2270fps——**同量级，MDK 略低**。
2. **硬解仍无优势**：D3D11=786fps、DXVA=660fps、CUDA=951fps，**全部低于其自家软解
   1273fps**——与 10-10 我们测 NVDEC 976fps<软解 2263fps 的结论同构（第 N 次印证）。
3. **随机 seek 延迟**：fast 45.5ms / accurate 61.7ms（全片 300 点）。
   **对照我们换段税 30-80ms/段——完全同量级。**
4. **官方「Optimized Continuous seeking」配方**（wiki 明确推荐用于 timeline preview：
   `setBufferRange(0)` + `continue_at_end=1` + `onSync→DBL_MAX`）实测：
   T3 碎段几何 42.3ms/次 vs 默认 42.7ms——**零实质加速**（差 1%）。
5. **连发 500 seek**：498/500 返回 Cancelled(-2)——SDK 语义=「被新 seek 取代」。
   即 MDK 的「连续 seek 优化」= **seek 合并/去抖**（保留最新目标，取消中间目标），
   **不是**把单次 seek 成本降低。碎段掠过场景（每 18ms 换段）下，合并的结果是
   **500 次目标只兑现 2 次**——对「跳过剪掉的片段」语义=大量帧被跳过不呈现。
6. **drop=nonref**：MDK 支持（`FFmpeg:drop=nonref`），但实测其口径与我们的
   AVDISCARD_NONREF 不同——帧数从 3602 掉到 752（我们 dsvc 是解码器级丢弃但
   **输出帧数不变**，只省解码成本）。MDK 这档是**不输出非参考帧**=改变呈现序列，
   不能用于我们的帧精确管线。

**裁决：不换内核（维持 10-13 上轮结论）。** MDK 的「时间轴预览优化」经实测
证明是 seek 合并（与 mpv 的 seek 队列天然行为同类），**没有解决换段税**，
也没有 40x 所需的能力；且核心闭源（私有仓库 + CLONE_PAT）、密钥制。

## 1. 测试矩阵与原始数字

### T1 顺序解码吞吐（60s chunk = 4.mp4[1800s,1860s]，3600 帧）

| 配置 | 帧数 | 墙钟 | fps |
|---|---|---|---|
| FFmpeg 软解 | 3602 | 2.83s | **1273** |
| D3D11 硬解 | 3602 | 4.58s | 786 |
| DXVA 硬解 | 3602 | 5.46s | 660 |
| CUDA 硬解 | 3627 | 3.81s | 951 |
| FFmpeg:drop=nonref | 752 | 2.36s | 318（输出帧数改变，不可比） |

### T1 全片顺序解码（4.mp4 全片，424176 帧）

| 配置 | 帧数 | 墙钟 | fps |
|---|---|---|---|
| FFmpeg 软解 | 424176 | 222.9s | **1903** |

对照（既有 T0）：dsvc 隔离 2029fps / GUI 实载 1380fps / 本机 ffmpeg 1835-2270fps。

### T2 随机 seek 延迟（4.mp4 全片均匀随机 300 点）

| 配置 | n | ok/err | mean | p50 | p90 | max |
|---|---|---|---|---|---|---|
| fast（KeyFrame\|InCache） | 300 | 300/0 | **45.5ms** | 43.5 | 55.8 | 62.0 |
| accurate（无 KeyFrame） | 300 | 300/0 | **61.7ms** | 60.2 | 76.8 | 122.6 |
| fast + 官方 Timeline 配方 | 300 | 300/0 | 48.1ms | 46.3 | 60.7 | 63.8 |
| accurate + 官方 Timeline 配方 | 300 | 300/0 | 65.2ms | 62.0 | 78.3 | 108.0 |

### T4 落点精度（seek 回调返回的首帧时间戳 - 目标，全片 300 点）

| 配置 | n | |dev| p50 | |dev| p90 | |dev| max | ~帧(p50) |
|---|---|---|---|---|
| fast（KeyFrame\|InCache） | 300 | **1060ms** | 1825ms | 2029ms | **63.6 帧**（落关键帧） |
| accurate（无 KeyFrame） | 300 | **7ms** | 14ms | 32ms | **0.4 帧**（帧级） |

**解读**：MDK 的 fast seek = 关键帧级（GOP 120 帧 → 中位偏 1060ms≈64 帧），
accurate seek 才能到帧级（7ms≈0.4 帧残差，即 PTS 量化+容器时基误差）。
对照我们：认证 PTS 轴 + seek-verified 闭环=**精确落帧 0 误差**（探针
verify_option2_prod 12/12 + T8 seek 误差 0 帧）；我们不需要在"快但粗"与
"准但慢"之间二选一。

### T3 碎段几何（0.18s 间隔连发 500 点）

| 配置 | 口径 | 结果 |
|---|---|---|
| 默认 | 串行（等回调） | mean **42.7ms**，p50 45.8，max 63.2 |
| 默认 | 连发（不等回调） | ok=2 / err=498，总 91.3ms，**per-seek 0.18ms** |
| 官方 Timeline 配方 | 串行 | mean **42.3ms**，p50 45.9，max 63.1 |
| 官方 Timeline 配方 | 连发 | ok=2 / err=498，总 107.3ms |
| 3.mp4 对照 | 串行 | mean **51.6ms**，p50 60.0，max 91.2 |
| 3.mp4 对照 | 连发 | ok=2 / err=498，总 121.9ms |

**连发口径解读（关键）**：err=498 的返回值是 `SeekError::Cancelled=-2`，SDK 头文件
原文="superseded by a newer seek, or unfinished previous seek dropped"。即：
- 连发时 MDK **取消中间目标、只兑现最新一个**（seek 合并）；
- 对「每 18ms 换段」的碎段掠过：**500 个目标只呈现 2 帧**——如果用它做业务
  变速预览，等于大量段被静默跳过；
- 对我们「帧精确 + 节奏=导出」的语义=不可用。

### 对照：MDK 与我们既有系统的定位差

| 维度 | MDK | 我们（dsvc + mpv + P1 代理） |
|---|---|---|
| 顺序解码 | 1903fps | 2029fps（隔离） |
| 硬解 | 660-951fps（劣于软解） | 976fps（劣于软解，同构） |
| 随机 seek | 45.5ms fast / 61.7ms accurate | 换段税 30-80ms/段（同量级） |
| 碎段连发 | 合并=只兑现最新（丢中间段） | 直通档逐段兑现（帧精确） |
| 40x 能力 | 无（顺序解码墙 + seek 合并语义） | **物化代理实测 40.0x 零停顿** |
| 源码 | 闭源核心（私有仓库+密钥） | 全自研可维护 |
| 帧精确语义 | seek 帧号 API 有，但连发合并 | 位级认证轴 + 代理映射 |

## 2. 方法学记录（R8 口径自证）

- **同源**：4.mp4（2.27GB/7069.9s/424176 帧）——与本项目全部 T0 数字同源。
- **同机**：与 dsvc 全部基准同机（RTX 4060 Laptop + 16 核）。
- **同计时**：seek 下发（wall clock）→ 回调（首帧解码完成）。与 dsvc 探针
  「seek 下发→seek_ok 事件」同构。
- **中文路径坑**：MDK 无法打开 `D:/qq下载/920/4.mp4`（status=0x80000000），
  用 NTFS 硬链接（零拷贝）绕开——**这不是我们的问题，但记录为使用限制**。
- **探针自建理由**（R2）：SDK 自带 DecodeFps.exe 每帧 printf+fflush（IO 拖慢，
  实测同文件 3.08s vs 我们探针 2.83s），且无 seek 计时；官方源码编译后同款。
- **公平性**：T2/T3 同时测「默认配置」与「官方 wiki 推荐 Timeline Preview 配方」
  （bufferRange=0 + continue_at_end）——**官方最优形态也没有加速**。

## 3. 对项目的影响

1. **「换 MDK」彻底关闭**：吞吐同量级（略低）、硬解同样无优势、seek 成本同量级
   （无黑科技）、碎段语义不兼容（seek 合并丢帧）、闭源+密钥制。
2. **「换内核」类调研第三次闭环**（09-12 后端文献 / 10-10 FilmCraft / 10-13 MDK）：
   三次独立实测都指向同一结论——**问题不在内核，在碎段拼接的解码账；
   解=物化**（我们已落地 P1）。
3. **MDK 唯一值得留意的一点**：它把「时间轴预览」写进 wiki 推荐配方=行业独立
   佐证「剪辑预览需要专门优化」这一需求真实存在；其做法（seek 合并）我们已
   通过 dsvc 世代抢占+mpv 队列天然吸收。

## 4. 复现命令

```bash
# 环境：MSVC BuildTools 2022 + MDK SDK v0.39.0
cd .cache/kernel_review
cmd //c build_probe.bat                      # 编译 mdk_probe.exe
export PATH="$PWD/mdk/mdk-sdk/bin/x64:$PATH" # mdk.dll/ffmpeg-9.dll 等

# T1 顺序吞吐
./mdk_probe.exe seq chunk_1800_60s.mp4 FFmpeg
./mdk_probe.exe seq src4.mp4 FFmpeg          # 全片 424k 帧

# T2 随机 seek（300 点）
./mdk_probe.exe seek src4.mp4 300
MDK_TIMELINE=1 ./mdk_probe.exe seek src4.mp4 300   # 官方 Timeline 配方

# T3 碎段几何（500 连发）
./mdk_probe.exe frag src4.mp4 500
MDK_TIMELINE=1 ./mdk_probe.exe frag src4.mp4 500
```

`src4.mp4`/`src3.mp4` = 中文路径源的 NTFS 硬链接（零拷贝，`mklink /H` 的
PowerShell `New-Item -ItemType HardLink` 等价物）。

---

## 附录 A：liberika（erika）实跑烟测（2026-10-13，T0）

**动机**：10-13 首轮调研对 liberika 只做了源码级阅读（无剪辑语义）未实跑；
owner 令「都补」→ 用其官方 v0.2.1 Windows x64 **C ABI 预编译包**
（`erika_capi.dll`，ctypes 驱动，同源 4.mp4 硬链接）。

**关键方法学**：liberika 无"直接读 position"API，位置只能从
`PositionChanged` 事件流获得；且本项目对 mpv 有过「seek 后瞬态报目标值」
的教训 → 探针设计**回显判别器**：取帧栅格之间的目标（半帧偏移），
真落帧必被量化偏离目标，回显则精确 0.0ms。

**结果**：

| 项 | 实测 |
|---|---|
| 打开→Ready | **333.7ms**（open 调用 281.7ms + 事件等待）|
| 随机 seek 延迟（30 点，seek→最后 position 事件） | mean **196.5ms** / max 360.6ms |
| 落点精度 | p50 **12.4ms**（0.75 帧）/ max 31.6ms |
| 回显判别（帧间目标） | settled 300.5333s vs target 300.5083s → **dev 25ms=real_landing**（非回显，真实落帧）|
| 顺序播放推进（2s 窗） | 1.92s/2.04s=**0.94x**（正常）|

**判定**：
1. **落点非帧精确**：p50 0.75 帧、max 1.9 帧——落在最近帧而非"目标帧"，
   与我们认证 PTS 轴 + seek-verified 的 0 误差不可比；且其 API 只有
   `seek(position_micros)`，**无帧号寻址**（源码级确认）。
2. **seek 延迟 196ms** 比 MDK（45-62ms）慢一个量级、比我们直通档换段
   （30-80ms）慢；比我们代理模式 exact seek（误差 0、亚秒级）无优势。
3. **无剪辑语义**（源码级：无 EDL/段表/包级跳过/帧步进）——与首轮结论一致。
4. 复现：`python .cache/kernel_review/liberika_probe.py`
   （结果 `liberika_probe_results.json`）。

**结论**：liberika 作为通用播放内核可工作，但对本项目的痛点（碎段拼接、
帧精确、40x）**零贡献**，与首轮源码级判定一致——「不引入」结论不变。

## 附录 B：本报告未覆盖项（诚实登记）

- MDK 带窗口的真实渲染路径（瓶颈在解码/seek 不在渲染，未测）；
- MDK 更多硬解变体（MFT/cuvid/Vulkan；"硬解更慢"已在 3 变体+自家软解对照成立）；
- MDK 与我们 GUI 同跑并发（连隔离档都不如我们，未测）；
- MDK 长时稳定性/内存足迹/4K 源（不改变"换不换"决策，未测）；
- liberika 的弹幕/字幕/HDR 能力（与本项目无关，未测）。

