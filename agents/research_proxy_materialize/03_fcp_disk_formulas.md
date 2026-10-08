# 03 — FCP 预览架构 + 代理/中间编码磁盘公式调研

> 调研日期：2026-09-14。调研方式：WebSearch/WebFetch 串行调研。
> 用途：对照「物化重剪代理」方案（1080p60 H.264 长 GOP B帧79% 游戏录像；代理=H.264 CRF14，实测 6.8× 实时、2.26GB/小时；预览播代理、暂停真源帧）。
> 纪律：数字标来源+版本；冲突并列；查不到写「未找到公开数据」+[推断]；不编造。

## 状态
- [x] Q1 FCP 转码模型（代理生成/后台渲染/skimming/长GOP表现）
- [x] Q2 FCP 代理磁盘占用（1080p60 ProRes Proxy 每小时GB；优化媒体 ProRes 422 每小时GB）
- [x] Q3 磁盘公式全集（ProRes 全档位 1080p60 / DNxHR / H.264 CRF / All-I 倍数）
- [x] Q4 FCP 内存口碑 vs Windows NLE
- [x] Q5 FCP 倍速预览（JKL/丢帧/变速渲染）
- [x] Q6 FCP 剪辑即时性（剪切/修剪/磁性时间线）
- [x] Q7 预览=导出一致性

---

## Q1. FCP 转码模型：代理生成、后台渲染、skimming、长 GOP 表现

### 1.1 代理生成的格式与规格（官方文档）
来源：Apple《Create optimized and proxy files in Final Cut Pro for Mac》用户指南（当前页面版本为 FCP 12.3 文档），support.apple.com/guide/final-cut-pro/verb8e5f6fd/mac。
- 代理编码格式：**「Video is transcoded to either the Apple ProRes 422 Proxy or H.264 format」**（ProRes 422 Proxy 或 H.264 二选一）。
- 代理帧尺寸：**「You can create proxy media in a variety of frame sizes from 12.5 percent to 100 percent of the original」**（12.5%–100% 可选）。
- 版本沿革（来源：Apple 官方发布说明 support.apple.com/en-us/102825 + FCP Cafe 发布说明存档 fcp.cafe/releasenotes/）：
  - **FCP 10.6（2021-10）**：首次支持自选代理格式（ProRes Proxy 或 H.264）与自定义代理帧尺寸（1/8、1/4、1/2、全分辨率）。此前（10.0–10.5.4）代理固定为 ProRes 422 Proxy、固定小尺寸。
  - **FCP 12.3（2026-07，Apple 发布说明原文）**：**「Proxy media is now generated using HEVC instead of H.264, for improved compression efficiency and HDR support. Background rendering is now set to Off by default」**——H.264 代理选项被 HEVC 取代，且后台渲染默认改为关闭。
- 优化媒体（optimized media）：**「This option transcodes video to the Apple ProRes 422 format」**——是标准 ProRes 422，**不是** 422 HQ（官方 guide 同页）。若源格式本就能流畅编辑，该选项置灰。

### 1.2 转码触发时机（官方文档）
来源：同上 Apple guide。
- 导入窗口导入：「The files are transcoded in the background, after the import process is complete」（导入完成后后台转码）。
- Finder 拖入：「Media you drag from the Finder into an event or timeline is automatically organized, transcoded, and analyzed based on the import settings」。
- 事后补转码：菜单 File > Transcode Media。
- 媒体存储：可存入库包内或外部指定位置（外置盘可加速）。

### 1.3 后台渲染（timeline 渲染文件，注意与"转码"区分）
来源：Apple《Background rendering》guide（ver717f3ca3，FCP 12.3 版文档）。
- 渲染对象：「creating temporary video and audio render files for segments of your project that Final Cut Pro can't play in real time」——只渲染**效果/转场/生成器/字幕**等无法实时播放的片段，**不渲染剪切点**。
- 默认延迟：「By default, rendering begins in the background 0.3 seconds after you stop working and moving the pointer」（停止操作 0.3 秒后启动）。
- 可在 Settings > Playback 中关闭或调延迟。
- **版本冲突记录**：FCP 12.3 发布说明称「Background rendering is now set to Off by default」，而 12.3 版指南页面仍写 0.3 秒默认——两者并列（推测：指南描述的是开启时的默认延迟值，发布说明指首选项默认状态；以发布说明为版本行为权威）。
- 社区经验（Reddit r/finalcutpro 2024-02 帖「Turning off background rendering may be making things worse」）：完全关闭后台渲染可能让播放体验变差，建议改调大延迟而不是关掉。

### 1.4 Skimming（悬停预览）实现与口碑
- 定义：指针划过时间线即播放对应帧，无需点击；Option-Command-S 开关（Apple guide《Play back and skim media》）。
- 实现方式：无专门的"skim 缓存"，就是实时解码 + 时间线合成显示（Apple 未公开实现细节[推断]：与正常播放共用解码/合成管线，仅指针驱动而非时钟驱动）。这也是长 GOP 素材 skimming 卡顿的根源——悬停触发随机 seek + 解码整条 GOP 链。
- 口碑/社区共识（Creative COW、fcp.co、Reddit r/finalcutpro、macprovideo 等多源一致）：
  - **1080p 单流长 GOP H.264**：多数现代机器可直编，「most computers and NLEs can handle it without proxies」。
  - **4K/高帧率长 GOP**：普遍卡顿，官方推荐方案就是建代理。
  - **Intel Mac**：长 GOP H.264 skimming 卡顿是老问题，Intel 用户普遍转 ProRes 代理/优化媒体。
  - **Apple Silicon**：内置 Media Engine 有专用 H.264/HEVC 硬解器（Softron 技术文档：不同芯片硬解器数量不同，M 系列每 die 含专用解码 ASIC），ProRes 播放/导出有专用加速引擎（apple.com/final-cut-pro：「The Media Engine in Apple silicon accelerates ProRes playback and export」），1080p60 长 GOP 直编体验明显好于 Intel 同代机器；但重时间线/多机位仍推荐代理。
  - 缓解手段（官方+社区）：Playback 质量设 Better Performance（约半分辨率）、开代理开关、关 skimming 排查。

### 1.5 与本项目的对照意义
- FCP 的模型 = 「预览播全 I 帧代理（ProRes Proxy）+ 导出用真源」，与本项目「物化重剪代理」同构；差别在 FCP 用 ProRes Proxy（帧内、解码零负担、体积大 6–7 倍）而非 H.264 长 GOP 代理。
- FCP 12.3 改用 HEVC 代理 + 默认关后台渲染，说明 Apple 也在权衡「代理磁盘体积」与「解码负担」，佐证本项目选 H.264 CRF14 折中路线有依据（体积小、Apple Silicon/现代 GPU 硬解长 GOP 也够用）。

**Q1 来源**：Apple FCP 用户指南（verb8e5f6fd / ver717f3ca3）、Apple 官方发布说明 102825、FCP Cafe 发布说明存档、Softron Apple Silicon 硬件加速文档、apple.com/final-cut-pro、Larry Jordan（larryjordan.com）、Creative COW/fcp.co/Reddit/macosprovideo 社区帖。

---

## Q2. FCP 代理磁盘占用：1080p60 ProRes Proxy 每小时 GB；优化媒体 ProRes 422 每小时 GB

### 2.1 官方锚点（码率目标值）
来源：Apple 官方支持文章《About Apple ProRes》（support.apple.com/en-us/102207，基于 Apple ProRes 白皮书），所有目标码率均标注「at 1920x1080 and 29.97 fps」：
| Codec | 官方目标码率 @1080p29.97 |
|---|---|
| ProRes 422 Proxy | ~45 Mbps |
| ProRes 422 LT | ~102 Mbps |
| ProRes 422 | ~147 Mbps |
| ProRes 422 HQ | ~220 Mbps |
| ProRes 4444 | ~330 Mbps（4:4:4 源） |
| ProRes 4444 XQ | ~500 Mbps（4:4:4 源） |

注：Apple 支持页只给 29.97p 单点，未发布 60p 表；ProRes 为变码率（VBR），码率随内容/尺寸/帧率浮动（页内明示「data rates vary based on codec type, image content, frame size, and frame rate」）。

### 2.2 换算到 1080p60（每秒 59.94 帧，逐帧整倍数换算 [推断]：ProRes 码率与像素吞吐近似线性，60p ≈ 2× 30p）
公式：GB/小时（十进制）= Mbps × 3600 ÷ 8 = Mbps × 0.45；GiB = GB ÷ 1.0737。
| 格式（1080p60 全分辨率） | 换算码率 | GB/小时（十进制） | GiB/小时 | 来源标注 |
|---|---|---|---|---|
| ProRes 422 Proxy（FCP 代理默认编码，100% 尺寸） | ~90 Mbps | **~40.5 GB** | ~37.7 GiB | 45 Mbps 官方锚点×2 换算 |
| ProRes 422 Proxy @ 半分辨率（10.6+ 可选 1/2） | ~22.5 Mbps | **~10.1 GB** | ~9.4 GiB | [推断] 码率≈像素数线性缩放 |
| ProRes 422（=FCP「优化媒体」，100% 尺寸） | ~294 Mbps | **~132.3 GB** | ~123.2 GiB | 147 Mbps 官方锚点×2 换算 |
| ProRes 422 LT | ~204 Mbps | ~91.8 GB | ~85.5 GiB | 同上 |

交叉验证：
- Larry Jordan（larryjordan.com《The Brave New World of 8K Media》）：「ProRes 422 requires 66 GB per hour when shooting 1080p/30 media」→ ×2 = 132 GB/hr @60p，与换算一致。
- Larry Jordan《Media in FCP X》：代理 ≈ ProRes 422 的 1/4 体积（45/147 ≈ 0.31，社区口径 1/4 为约数）。
- Library of Congress（loc.gov ProRes 422 Proxy 格式档案 fdd000405）：45 Mbps @1080p29.97 ≈ 20 GB/小时，与 40.5 GB/hr @60p 一致。

### 2.3 实测口径与注意点
- 未找到 FCP 对 1080p60 源生成代理的逐文件公开实测数据（未找到公开数据）。上述 40.5 GB/hr 为官方锚点换算值；实测会因内容复杂度上下浮动（ProRes VBR）。
- **代理可能比原文件更大**（Frame.io 博客/FCPX 用户组多次讨论）：对高压缩长 GOP 源（如 4K HEVC ~50 Mbps），FCP 全尺寸 ProRes Proxy 反而比原片大。本项目场景：源为 1080p60 长 GOP H.264（码率通常 20–60 Mbps），全尺寸 ProRes Proxy（~90 Mbps）同样会大于源——这正是本项目选 H.264 CRF14 代理（2.26 GB/hr）的体积优势所在：**约为 FCP ProRes Proxy 方案的 1/18，ProRes 422 优化媒体的 1/59**。
- FCP 12.3 起 H.264 代理改 HEVC（官方发布说明 102825），进一步压代理体积（压缩效率提升幅度官方未给数字）。
- 代理可存库内或外置盘（官方 guide verb8e5f6fd）；渲染文件另有「Manage render files」清理入口。

**Q2 来源**：Apple《About Apple ProRes》102207（码率锚点）、Apple ProRes 白皮书（apple.com/final-cut-pro/docs/Apple_ProRes_White_Paper.pdf）、Library of Congress fdd000405、Larry Jordan 两篇文章、Frame.io 代理指南（blog.frame.io 2017/2023）、Reddit r/finalcutpro/r/fcpx 存储讨论帖。换算公式为本文计算，锚点数字均标注官方出处。

---

## Q3. 磁盘公式全集（重点）

**通用换算公式**：GB/小时（十进制）= Mbps × 3600 ÷ 8 = Mbps × 0.45；GiB/小时 = GB ÷ 1.0737；MiB/s × 3600 = MB 占用（÷1024 得 GiB）。

### 3.1 ProRes 全档位 1080p60 码率表
官方锚点：Apple《About Apple ProRes》（102207，与 ProRes 白皮书同源），全部标注 @1920×1080/29.97p。Apple 未公布 60p 表；60p 换算 = 29.97p 锚点 × 2（[推断]：ProRes 每 MB/帧 恒定、码率随帧率线性，Avid DNxHR 官方表亦证实同类帧内编码此规律，见 3.2）。
| 档位 | 官方锚点 @1080p29.97 | 1080p60 换算码率 | GB/小时 @60p | GiB/小时 |
|---|---|---|---|---|
| 422 Proxy | ~45 Mbps | ~90 Mbps | **~40.5 GB** | ~37.7 |
| 422 LT | ~102 Mbps | ~204 Mbps | ~91.8 GB | ~85.5 |
| 422 | ~147 Mbps | ~294 Mbps | **~132.3 GB** | ~123.2 |
| 422 HQ | ~220 Mbps | ~440 Mbps | ~198 GB | ~184.4 |
| 4444 | ~330 Mbps（4:4:4 源） | ~660 Mbps | ~297 GB | ~276.6 |
| 4444 XQ | ~500 Mbps（4:4:4 源） | ~1000 Mbps | ~450 GB | ~419.1 |

交叉验证：Larry Jordan「ProRes 422 = 66 GB/小时 @1080p30」→ ×2 = 132 ✓；LoC「Proxy 45 Mbps ≈ 20 GB/小时」→ ×2 = 40.5 ✓。注：ProRes 为 VBR，实际随内容浮动；斜杠修正：ProRes 码率亦随分辨率/位深/透明通道变化（白皮书明示）。

### 3.2 DNxHR 全档位（Avid 官方精确表，非估算）
来源：Avid KB《DNxHR Codec Bandwidth Specifications》（kb.avid.com，每帧字节数固定 → 码率精确、随帧率线性）。HD=1920×1080。**59.94p 行为官方原始数据**：
| 档位（HD 1080） | 字节/帧 | 59.94p 官方值 | 换算 Mbps | GB/小时 @60p | GiB/小时 |
|---|---|---|---|---|---|
| LB | 188,416 B | 10.77 MB/s | ~90.3 Mbps | ~40.7 GB | ~37.9 |
| SQ | 602,112 B | 34.42 MB/s | ~288.7 Mbps | ~129.9 GB | ~121.0 |
| HQ | 909,312 B | 51.98 MB/s | ~436.0 Mbps | ~196.2 GB | ~182.7 |
| HQX | 909,312 B | 51.98 MB/s | ~436.0 Mbps | ~196.2 GB | ~182.7 |
| 444 | 1,822,720 B | 104.19 MB/s | ~874.0 Mbps | ~393.3 GB | ~366.3 |

注：官方表按 23.976/25/29.97/50/59.94 五档帧率给出，50p≈59.94p×(50/59.94)；HQ 与 HQX 每帧字节相同（HQX 仅多了 10/12-bit 打包定义）；表中 MB/s 为二进制 MiB/s（Avid 按字节/帧 ÷1,048,576 计算）。DNxHR 逐帧恒码率（CBG），实测即官方值。

### 3.3 H.264 CRF 14/16/18/20 @1080p60 游戏内容典型码率
**规则来源（官方文档级）**：FFmpeg Trac Wiki《Encode/H.264》：「The range is exponential, so increasing the CRF value +6 results in roughly half the bitrate / file size, while -6 leads to roughly twice the bitrate / file size」（CRF 对数刻度，±6 ≈ 体积×2 或 ÷2）；slhck《CRF Guide》(slhck.info, 2017) 同口径；goughlui.com HandBrake 实测（2016）：实际翻倍点在 CRF +5.68~+6.42 之间，规则成立但逐片浮动。

**以本项目实测为锚**（00_local_baseline.md：CRF14 veryfast 1080p60 = 2.26 GB/小时 = **5.0 Mbps**）+ FFmpeg 规则推算：
| CRF | 相对 CRF14 | 典型码率 [推断] | GB/小时 [推断] |
|---|---|---|---|
| 14（项目实测） | 1× | **5.0 Mbps（实测）** | **2.26 GB（实测）** |
| 16 | ×1.26 | ~6.3 Mbps | ~2.8 GB |
| 18 | ×1.59 | ~7.9 Mbps | ~3.6 GB |
| 20 | ×2.0 | ~10 Mbps | ~4.5 GB |

**社区对照区间**（说明内容依赖极大，本项目数值显著偏低的原因见下）：
- r/letsplay 社区共识（2024）：1080p 录制下限 12–16 Mbps，推荐 CRF 16–20 而非固定码率。
- OBS 论坛实测帖（obsproject.com/forum/threads/40202，2016）：CRF22 veryfast ≈ 13 Mbps（1080p，帧率未标明）；另一用户 1080p60 superfast CRF20 无体积数字。OBS 用户组实例：30GB/1.5h ≈ 45 Mbps（高质量 CRF）。
- **本项目 CRF14 只有 5.0 Mbps 的解释 [推断]**：源为 NVENC/硬件编码器输出（20.6 Mbps，压缩效率低于 x264），且游戏画面大量静态 UI + 长 GOP B帧79% 结构利于重压缩；x264 veryfast 同质量码率可比硬件编码低 2–4 倍。**结论：CRF 绝对值不可跨内容迁移，同一 CRF 的码率必须以本项目实测为准；上表 CRF16/18/20 换算仅在本内容分布内可信。**

### 3.4 All-I / 短 GOP 相对长 GOP 的体积倍数
- **All-I（keyint=1）vs 长 GOP（含 B 帧）同质量**：无官方单一倍数。社区共识区间 **3–6×**（Doom9 论坛 GOP 讨论 forum.doom9.org/showthread.php?t=183818；Reddit r/AV1；Streaming Learning Center B 帧效率文章）。x264 CRF 下自动给 I 帧更多比特，质量恒定、体积上涨，是最干净的对比方式。
- **摄像机官方码率对照（同厂同格式）**：Sony 官方规格（sony.com FX6 规格页 + Sony 支持文章 00253978）：**XAVC-I HD 59.94p = 223 Mbps（All-I）vs XAVC-L HD 59.94p = VBR 最高 150 Mbps（长 GOP）→ 倍数 ≈ 1.5×**。此为摄像机档位配平值（两者均 10-bit 4:2:2），低于 x264 语境的 3–6×，因为厂商把 All-I 档的量化也调粗了。[推断] 同档量化下 All-I 惩罚更接近 3–6×。
- **短 GOP（GOP≤30，即关键帧间隔 ≤0.5s）vs 2s 长 GOP**：r/ffmpeg 社区共识 **+10–20% 体积**（reddit.com/r/ffmpeg/comments/1gz72ky）；I 帧最大可到 P 帧的 ~10×（Dolby OptiView LL-HLS 博客）；OBS 社区 60fps 常用 2s GOP（obsproject 论坛）。本项目源 GOP=120 帧（2s）：若代理改 GOP≤30，预计体积 +10~20%（[推断]，r/ffmpeg 区间映射到本项目内容）。
- **对照 FCP**：ProRes Proxy/DNxHR 全部为 All-I；这解释了 FCP 代理 ~90 Mbps vs 本项目 H.264 长 GOP 代理 5 Mbps 的 ~18× 差距（其中约 3–6× 来自 All-I vs 长 GOP 编码效率差，其余来自像素吞吐与量化档位）。

### 3.5 公式速查（按本项目 1080p60 游戏内容）
| 方案 | 码率 | GB/小时 | 相对本项目代理倍数 |
|---|---|---|---|
| 本项目 H.264 CRF14 长 GOP 代理 | 5.0 Mbps（实测） | **2.26（实测）** | 1× |
| H.264 All-I（同质量） | ~15–30 Mbps | ~7–13.5 | ~3–6× [推断] |
| ProRes 422 Proxy（FCP 代理） | ~90 Mbps | ~40.5 | ~18× |
| ProRes 422（FCP 优化媒体） | ~294 Mbps | ~132.3 | ~59× |
| ProRes 422 HQ | ~440 Mbps | ~198 | ~88× |
| DNxHR LB | ~90 Mbps | ~40.7 | ~18× |
| DNxHR SQ | ~289 Mbps | ~129.9 | ~57× |
| DNxHR HQ/HQX | ~436 Mbps | ~196.2 | ~87× |
| DNxHR 444 | ~874 Mbps | ~393.3 | ~174× |

**Q3 来源**：Apple 102207 + ProRes 白皮书（29.97p 锚点）；Avid KB DNxHR 带宽规格页（59.94p 精确表）；FFmpeg Trac Wiki Encode/H.264（CRF ±6 规则）；slhck.info CRF Guide；goughlui.com HandBrake 实测；00_local_baseline.md（CRF14 实测锚）；Sony FX6 规格页/支持文章 00253978（XAVC-I/L 官方码率）；Doom9、r/AV1、r/ffmpeg、Streaming Learning Center、Dolby OptiView、AWS Media Blog、OBS 论坛（All-I/GOP 倍数）。所有 ×2 倍帧率换算、CRF16/18/20 外推与倍数综合均为标注 [推断] 的计算，锚点数字皆有出处。

---

## Q4. FCP 内存口碑（Apple Silicon 统一内存）与 Windows NLE 对照

### 4.1 FCP 内存口碑（多源社区一致口径）
- 门槛低：macprovideo《FCP X Basic Hardware Guide》基准 = 8GB 起步可剪；社区（MacRumors、Reddit r/finalcutpro）大量 8GB M1 机型剪 1080p 没问题的实例。
- 甜点：**16GB = 社区公认实用甜点**（r/finalcutpro「How much RAM do I need」2025-06 帖与 MacRumors 多帖一致）。
- 重负载：4K/多机位/大工程建议 24GB+；16GB 机型也有「out of application memory」报错案例（FCPX 用户组帖），说明长会话/大库仍会触顶。
- 结构性原因（[推断]+官方佐证）：Apple Silicon 统一内存（CPU/GPU 共享，免拷贝）+ Media Engine **硬解 H.264/HEVC/ProRes 不占系统 RAM 大头**，解码路径内存放大远小于软解；FCP 逐帧缓存策略激进但 macOS 内存压缩/交换强。
- 注意：Apple Silicon 内存焊死不可升级，社区共识「买时多留余量」。

### 4.2 与 Windows NLE 对照（引用本仓库已有调研 01/02 号文档的带源数据）
| NLE | 官方建议 | 社区实测口径 | 来源 |
|---|---|---|---|
| FCP（macOS） | 无官方 RAM 建议数 | 8GB 可用 / 16GB 甜点 / 24GB+ 重载 | Reddit r/finalcutpro 1l1dudo、MacRumors 2319826、Apple Discussions 254642236 |
| Premiere（Win） | **最低 32GB、重度 64GB** | 16GB 相比 64GB 整体性能 −9%（复杂时间线），32GB≈64GB | Puget Systems 硬件建议 +《When Does RAM Capacity Impact Performance?》（见 02_premiere.md §4） |
| Resolve（Win） | **16GB 最低（Fusion 32GB）**、GPU ≥4GB VRAM | 1080p 流畅 32GB 舒适；硬解 H.265 时 RAM 16→32GB 飙升；4K 长 GOP VRAM 可 >12GB | BMD 官方规格 + BMD 论坛 t=82810 + Reddit 1hbzwoi/1gdgfqc（见 01_davinci_resolve.md §5） |
| Resolve（macOS） | 8GB RAM 即达官方最低线 | — | BMD 官方规格（同上） |

### 4.3 结论与本项目映射
- FCP 的「省内存」口碑 = 统一内存 + 硬解 + 精简功能面三重叠加，并非纯软件效率；Windows NLE 官方建议普遍 32–64GB，差距量级 2–4×。
- 本项目（GUI 650–950MB + dsvc 150MB + mpv，00_local_baseline.md）走「按需选择性解码」路线，内存模型接近 FCP 硬解思路（解码负担转给专用资源、不囤帧缓存），显著低于 Windows NLE 主流预期——构成差异化优势。

**Q4 来源**：Reddit r/finalcutpro（1l1dudo）、MacRumors 论坛（2319826）、Apple Discussions（254642236）、macprovideo 硬件指南、Puget Systems（Premiere 硬件建议/RAM 容量实测）、BMD 官方规格与论坛帖 t=82810、Reddit r/davinciresolve（1hbzwoi、1gdgfqc）——后三者转引自本仓库 01_davinci_resolve.md / 02_premiere.md（含原始链接）。

---

## Q5. FCP 倍速预览：JKL 档位、高倍速丢帧、变速片段渲染

### 5.1 JKL 档位
- L = 正向播放；连按 L 逐档升速：**2x → 4x → 8x → … 最高 32x**；J 反向同理（-2x、-4x…-32x）；K 暂停；K+J/K+L 微速爬行。来源：fcpxtemplates.com《J,K,L and beyond》（社区教程，与 Apple 键位一致）。官方未公布逐档倍速上限文档（未找到公开数据；32x 为社区口径）。

### 5.2 高倍速丢帧行为
- 高倍速/大分辨率下 FCP 保时钟、丢帧：Neat Video《The way to smooth editing in Final Cut Pro》明确指出高倍速 shuttle 或 4K/8K 分辨率下 FCP 无法实时刷新预览、会出现跳帧——shuttle 本质是 best-effort 抽帧预览（与 Premiere 同策略，见 02_premiere.md §6）。
- 官方给出的顺滑手段：Playback 质量设 Better Performance（约半分辨率显示，可能暂停部分效果）、代理媒体、关 skimming（Apple guide《Control playback quality》ver2fd7a8b94）。官方未逐档披露 2x/4x/8x 下精确丢帧策略（未找到公开数据）[推断]：高倍速为「抽帧保速」，非完整逐帧。
- 4K60 素材时间线跳帧的社区解法首选也是代理（FCPX 用户组、Reddit 1g4yxdo）。

### 5.3 变速片段是否需要渲染（与 Premiere 的关键差异）
- FCP 变速三档质量法：**Normal（帧抽样）、Frame Blending（帧混合）、Optical Flow（光流插帧）**。来源：Apple FCP 用户指南「Retime clips to create speed effects」章节（教程生态一致转述：TunesKit/Motion Array/BorisFX 均列此三档）。
- **纯速度改变（Normal 抽样）本身不进渲染条**：FCP 只对「无法实时播放」的段落打渲染标记（官方 Background rendering 页定义），普通恒定变速以帧抽样方式实时播放，无需渲染。
- **Optical Flow / Frame Blending 是重计算效果**：光流最高质量但「expect longer render times」（教程口径）；播放时未渲染段以降质/抽帧方式尽力预览，后台渲染完成后变顺滑——与 Q1.3「0.3 秒闲置后台渲染」衔接。
- 官方另有《Play back slow-motion clips》专页（慢速播放质量说明，未逐字抓取）。
- 对照 Premiere（02_premiere.md §6）：变速/光流在 Premiere 是**典型红色渲染条效果**，官方排障建议就是渲染预览文件；FCP 的 Normal 变速免渲染、光流才需后台渲染——FCP 即时性口碑更好。

### 5.4 与本项目映射
- 本项目「物化重剪代理」把变速节奏烤进代理（0.2x 段 10×、1x 段 2×，00_local_baseline.md），比 FCP 的「实时抽帧变速」更彻底：任何倍速恒 60fps、零丢帧。FCP 对照说明：主流 NLE 在高倍速一律牺牲帧完整性保时间轴，本项目方案在该维度超越 FCP。

**Q5 来源**：fcpxtemplates.com（JKL 档位）、Neat Video 博客（丢帧机制）、Apple guide ver2fd7a8b94/ver717f3ca3（播放质量/背景渲染）、FCPX 用户组 + Reddit r/finalcutpro（4K60 跳帧）、TunesKit/Motion Array/BorisFX 教程（retime 三法与渲染代价）。

---

## Q6. FCP 剪辑即时性：剪切/修剪（代理就绪时）与磁性时间线影响

### 6.1 剪切/修剪即时性
- 结构性事实：FCP 的**剪切点不产生渲染文件**——渲染文件只服务于「无法实时播放」的效果段（Apple《Background rendering》定义，见 Q1.3），剪切/修剪是纯元数据操作，代理（或易解素材）就绪时即时生效，修剪时 skimming 提供实时画面反馈。
- 前提是素材可解：长 GOP 原生素材上，trim/scrub 的反复随机解码仍有惩罚（Frame.io《Editing codecs》论点，Premiere/FCP 通用，见 02_premiere.md §6）——「代理让剪辑手感即时」在两家同为动因；Larry Jordan《New! Improved Proxy Workflow》：代理提升性能、降低存储带宽压力。
- 极限情况：超大工程时间线响应劣化（Reddit 1klhidy「FCP is driving me crazy with big projects」），解法仍是管理代理/渲染文件；Apple 发布说明（102825）多年迭代「very long projects 编辑性能改善」。

### 6.2 磁性时间线的影响
- 行为：主故事线片段自动吸附、删除后自动涟漪闭合、不留黑段（Frame.io 2017《How The Magnetic Timeline Keeps You Focused》：唯一内置磁性时间线的主流 NLE，宣称提速编辑）。
- 提速面：拖拽重排不产生间隙、插入自动让位——减少手动清理，社区普遍认其为速度优势（macprovideo《Speed Trimming Techniques》：磁性主故事线快速重排 + Position 工具 P 绕过涟漪）。
- 代价面：涟漪不可预期移动下游片段（新手争议点）；多故事线场景管理复杂。与渲染/代理无交互：磁性重排只改元数据引用，不触发媒体重编码（[推断]+官方模型一致：转码只与源媒体挂钩）。
- Windows 侧对照：Premiere/Resolve 均无原生磁性时间线（需手动涟漪或第三方工具）。

### 6.3 与本项目映射
- 本项目删减 = ms 级 timeline plan 重建 + 直通段表热更新（<0.5s 续播，00_local_baseline.md），达到甚至超过 FCP 代理就绪时的即时性；但本项目无涟漪语义（段表模型），磁性时间线的「自动闭合」由用户/规则层决定——设计上可选择性借鉴。

**Q6 来源**：Apple《Background rendering》ver717f3ca3、Apple 发布说明 102825、Frame.io 博客（2017 磁性时间线、editing codecs）、Larry Jordan 代理工作流文章、macprovideo 修剪技法、Reddit r/finalcutpro 1klhidy。

---

## Q7. 预览 = 导出一致性：FCP 导出是否复用渲染/代理

### 7.1 代理与导出：**代理只服务播放，导出永远从原片/优化媒体合成**
- 「Proxy Preferred」是播放设置，仅影响监视器取流来源；Share/导出流程使用原始全分辨率媒体（Apple《Manage Media with Final Cut Pro Libraries》官方 PDF 生成媒体模型 + Larry Jordan 代理工作流 + FCP 用户指南转述一致）。FCP 无「导出代理成片」常规选项（未找到官方开关；Compressor 端另有玩法）。
- 质量语义：预览（代理=低清）≠ 导出（原片=全清），但**时间轴结构/效果参数一致**；切回原始/优化模式时旧代理相关状态作废（Larry Jordan：切换会删除代理渲染文件并重渲）。
- 后果：代理不能给导出提速（导出仍要解原片长 GOP）；这是与本项目「导出节奏=代理节奏」最大的架构分歧。

### 7.2 渲染文件与导出：**匹配时复用，可显著加速**
- 官方模型：渲染文件/优化文件/代理文件同属库内「generated media」；**复制工程共享同一批渲染文件**（Apple 官方 PDF《Manage Media with Final Cut Pro Libraries》）。
- 复用条件（Larry Jordan 实测文《How Rendering Affects Export Speed》）：**把工程渲染编解码设为与导出目标一致**（如渲染=ProRes 422、导出=ProRes 422 Master File）→ 后台渲染好的效果段直接复用，导出跳过重算；编解码不匹配则导出时重渲。
- 社区验证：已后台渲染的工程导出更快（FCPX 用户组帖 3529790293799116、Reddit ltqgc8、Apple Discussions 256008063 一致口径）。
- 效果段的预览=导出一致性因此成立：后台渲染完成的效果段，预览画面与导出画面同源（同一渲染文件）；未渲染完时预览是尽力播放，导出才出全质——存在短暂不一致窗口。

### 7.3 与本项目对照（预览=导出一致性的两种路线）
| 维度 | FCP | 本项目（物化重剪代理） |
|---|---|---|
| 预览取流 | 代理（低清） | 代理（CRF14 高清，≈源画质）+ 暂停真源帧位级精确 |
| 导出取流 | **原片**（解长 GOP 全量） | **保留时间线本身已物化**：剪辑即导出素材序列，无二次合成 |
| 节奏一致性 | 预览节奏=时间线参数，导出需重新执行变速/效果 | 节奏烤进代理，「所见即导出」按构造成立（00_local_baseline.md） |
| 一致性风险 | 未渲染效果段、VFR 源、导出编解码不匹配 | 段表/代理重建窗口期（秒级） |
| 结论 | FCP 用「播放代理 + 导出原片 + 渲染文件复用」保导出质量，代价=导出仍吃长 GOP 解码与全量渲染 | 本项目把 FCP 的「优化媒体」思想推到极致：直接把保留时间线物化为交付级代理，磁盘代价 2.26GB/h（FCP 优化媒体 132GB/h 的 1/59，见 Q3.5） |

**Q7 来源**：Apple《Manage Media with Final Cut Pro Libraries》官方 PDF（generated media 模型、渲染文件跨工程共享）、Apple FCP 用户指南（Proxy Preferred 播放设置）、Larry Jordan《How Rendering Affects Export Speed in Apple Final Cut Pro》（渲染编解码匹配复用实测）、FCPX 用户组/Reddit/Apple Discussions 导出提速帖。

---

## 总结（对「物化重剪代理」方案的印证与差异点）

1. **架构同构**：FCP=「预览播帧内代理 + 导出原片」；本项目=「预览播重剪代理 + 代理即导出」。本项目把一致性做到了 FCP 没有的程度（导出零重合成），代价是必须自己管理代理增量重建。
2. **磁盘公式定论**：1080p60 全分辨率下，ProRes Proxy ≈40.5GB/h（Apple 锚点换算）、ProRes 422 ≈132GB/h、DNxHR LB≈40.7GB/h~444≈393GB/h（Avid 官方精确表）；本项目 CRF14=2.26GB/h 实测，比所有帧内中间格式小 1–2 个数量级；All-I 惩罚 3–6×、短 GOP(0.5s) 惩罚 10–20%（社区共识）——若本项目代理 seek 不足可加 GOP≤30，磁盘代价可控。
3. **FCP 的教训**：12.3 默认关后台渲染、代理转 HEVC，说明即使 Apple 也在压缩后台资源占用与代理体积；「0.3 秒闲置即渲染」对交互延迟敏感的工程同样适用（本项目段级增量重建=秒级，更优）。
4. **内存**：FCP 硬解+统一内存 16GB 甜点 vs Windows NLE 32–64GB 官方建议；本项目 650–950MB GUI 为最小内存路线。
