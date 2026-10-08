# DaVinci Resolve 预览/回放架构调研（用于对比自研「物化重剪代理」方案）

- 调研日期：2026-09-14（子代理串行调研，网络来源为主）
- 调研背景：Arknights 游戏录像（1080p60 H.264 长 GOP、B 帧占比 ~79%）自动剪辑工具；
  自研预览 = Rust 解码服务按段表选择性解码 + mpv 播放，4x 预览；
  计划方案 =「物化重剪代理」：按导出节奏把保留时间线离线重编码为高清代理（实测 CRF14 编码 6.8x 实时、2.26 GB/小时），预览直接播代理。
- 权威一手来源：DaVinci Resolve 18 Reference Manual Chapter 8《Improving Performance, Proxies, and the Render Cache》
  （镜像 PDF：https://lira.epac.to/DOCS-TECH/Graphics/Da%20Vinci%20Resolve/DaVinci%20Resolve%2018%20-%20Reference%20Manual%20-%20Chapter%208%20-%20Improving%20Performance,%20Proxies,%20and%20the%20Render%20Cache.pdf ，下称【手册Ch8】，页码 p.182–210）。
  其余为官方 techspecs、Avid 官方 KB、BMD 论坛、Reddit 等社区来源（均标注访问日期 2026-09）。
- 标注约定：[推断] = 由来源合理外推；「未找到公开数据」= 检索不到量化资料。

---

## 0. 架构总览（Resolve 提升回放性能的 5 条正交通道）

依【手册Ch8 p.182–209】，Resolve 把「预览流畅」与「成片质量」设计为**可分离**的两层，共 5 条通道：

| 通道 | 机制 | 新文件？ | 作用层 |
|---|---|---|---|
| Timeline Proxy Mode | 播放时实时降采样（Half/Quarter），不落盘 | 否 | 时间线处理分辨率 |
| Optimized Media | 把源转成易解码中间编码（存为专有 .dvcc 容器），Playback 菜单一键切换 | 是（专有格式、不可移植） | 源解码 |
| Proxy Media | 生成独立可移植代理文件（ProRes/DNxHR/H.264/H.265），经元数据链接 | 是（标准格式、可移植） | 源解码 |
| Render Cache（三层：Fusion Output Cache / Node Cache / Sequence Cache） | 把"难解码的源"或"重的效果/调色"预渲染落盘 | 是（仅本工程可用） | 解码 + 效果处理 |
| Performance Mode | 自动降低 sizing/解码/图像处理质量换交互性 | 否 | 处理精度 |

手册明确：所有方法「can be set up to have no effect on your delivered output」（p.183），即**预览优化与最终输出在架构上解耦**。
这与我们「预览播代理、导出走原始或高质量代理」的思路同构。

---

## 1. Optimized Media / Proxy 生成（Q1）

### 1.1 默认格式与分辨率
- **Proxy Media Resolution 默认 = "Choose Automatically"**（【手册Ch8 p.199 截图】）。
  语义：**只对分辨率高于时间线分辨率的素材降采样**；1080p 工程里 4K→半辨、8K→四分之一，
  **1080p 及以下的源不降分辨率**（p.189 "All clips that are 1080 and smaller generate optimized media at the same resolution as the source clips"——此句虽在 Optimized Media 节，但 Proxy Media 的 Choose Automatically 同规则，p.199）。
  **→ 对我们的 1080p60 素材：Resolve 默认代理仍是全 1080p，并不会默认减半。**「默认减半」是社区常见误传；减半需手动选 Half。
- **Proxy Media Format 选项**：ProRes 系（Proxy~4444 XQ）、DNxHR 系（LB~444）、H.264、H.265（p.199）。
  DR18 手册截图（Mac）默认显示 **ProRes 422 HQ**；Resolve 20 Editors Guide 亦记载 Mac 上默认 ProRes 422 HQ
  （https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Editors-Guide.pdf ，via 2026-09 搜索摘要）。
  Windows 默认：多个教程一致说 Windows 推荐/默认 DNxHR SQ（ProRes 在 Windows 不可编码）
  （https://creativevideotips.com/tutorials/davinci-resolve-render-cache-essentials 、https://davinciresolveclub.com/davinci-resolve-proxy-workflow/ ，访问 2026-09）。
  ⚠️ 来源冲突：DR20 Editors Guide 摘要说默认 ProRes 422 HQ（Mac 语境）；Windows 侧未见官方手册原文，只有教程一致口径 DNxHR SQ。并列如上。
- **Optimized Media Format 选项**：Uncompressed 10-bit / 16-bit float、ProRes Proxy~4444 XQ、DNxHR LB~444；
  **存储为专有 .dvcc 镜像格式**，仅 Resolve 可读、不可导出（p.188、p.208）。跨工程共享（同项目库）（p.190）。
- Optimized Media 需手动对选中素材生成（p.187），社区口径：**对整条源片段生成**、因而占盘大
  （论坛共识：https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=207671 ；
  「只缓存时间线用到的部分」的是 Render Cache 里的 **Fusion Output Cache**（前称 Source Cache）——官方原文
  "caches the portion of each source media file that appears in the Timeline"（p.192），且明确
  "The advantage of the Fusion Output Cache over Optimized Media is that you only cache clips that are used in a timeline"（p.192）。
  Facebook 上「Optimized Media 只做时间线用到的部分」的说法与此冲突，判断为与 Source Cache 混淆，取官方口径。
  → 我们「按段表只物化保留段」在语义上最接近 Resolve 的 **Fusion Output Cache**，而非 Optimized Media / Proxy Media（两者均整源物化）。

### 1.2 码率与每小时磁盘占用（1080p60）
Avid 官方 DNxHR 码率表（1920×1080 光栅，每流；https://kb.avid.com/pkb/articles/en_US/Knowledge/DNxHR-Codec-Bandwidth-Specifications ，更新 2024-07，访问 2026-09）：
DNxHR 每帧字节数固定（按档位×光栅），59.94p 时：

| 档位 | MB/s | ≈Mb/s | ≈GB/小时(十进制) |
|---|---|---|---|
| DNxHR LB | 10.77 | 86.2 | **38.8** |
| DNxHR SQ（Windows 常用） | 34.42 | 275.4 | **123.9** |
| DNxHR HQ/HQX | 51.98 | 415.8 | **187.1** |
| DNxHR 444 | 104.19 | 833.5 | **375.1** |

- ProRes 422 HQ 1080p60：Apple 官方（https://support.apple.com/en-us/102207 ）"target data rate ≈220 Mbps at 1920x1080 and 29.97 fps" → 60p ≈440 Mb/s ≈ **198 GB/小时**（按帧率线性翻倍 [推断]）。
- **半分辨率（960×540，像素数=全辨 1/4）**：Avid 未定义 540p 档；按像素数线性折算：
  LB ≈ 21.5 Mb/s ≈ **9.7 GB/小时**、SQ ≈ 68.9 Mb/s ≈ **31 GB/小时** [推断]。
- **对照我们方案：CRF14 H.264 重剪代理 ≈ 2.26 GB/小时（≈5 Mb/s）**。
  → 比 Resolve 生态最省的 DNxHR LB 半辨代理（≈9.7 GB/h [推断]）仍小约 4 倍；
  比 Windows 常用 DNxHR SQ 半辨（≈31 GB/h [推断]）小约 14 倍；
  比"默认 Choose Automatically（1080p 源不降辨）+ DNxHR SQ"（123.9 GB/h）小约 **55 倍**。
  代价：我们用长 GOP（编码小但解码贵）替代行业代理惯用的帧内编码（编码大但解码便宜）——
  社区共识 H.264/H.265 不宜作代理（https://forum.blackmagicdesign.com/viewtopic.php?f=33&t=172202 ；
  The Post Flow：10 Mb/s H.264 代理"在本地重新引入长 GOP scrub 代价"，且"分辨率比编码格式更影响代理性能"，
  https://thepostflow.com/post-production/post-production-workflows/proxy-workflows-honestly/ ，访问 2026-09）。
  **→ 印证点：只要播放端解码器（我们 Rust 服务）能实时解 H.264 长 GOP（已实测 6.8x 编码、4x 预览可行），
  H.264 代理的磁盘经济性远超行业 DNxHR/ProRes 代理；行业弃用 H.264 代理主要因为通用 NLE 解码路径吃紧。**

### 1.3 Blackmagic Proxy Generator（独立代理生成器）
- 随 Resolve 安装的独立小程序，watch-folder 自动转码，代理落在源旁 "Proxy" 子目录（【手册Ch8 p.201–202】）。
- **免费版捆绑 Lite 版：不支持 H.264/H.265 代理输出；Studio 才有完整版**（https://whoismatt.com/how-to-use-proxy-files-to-edit-super-fast-in-davinci-resolve/ ；https://usermanual.com/support/blackmagicdesign/web-manual/cloud-store/proxy-generator ，访问 2026-09）。
- 有教程称 Windows 版 Proxy Generator 只出 H.264/H.265（https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=178458 ，访问 2026-09）；
  与 Lite 版"无 H.264"的说法存在版本/平台口径冲突，并列待核。

---

## 2. Render Cache（智能缓存/手动缓存）（Q2）

### 2.1 三层缓存与触发条件（官方）
依【手册Ch8 p.192–196】，"Render Cache" 实为三层独立缓存：
1. **Fusion Output Cache**（前称 Source Cache）：把时间线中源媒体"调色前"状态落盘。Smart 模式自动缓存：
   - **H.264、H.265、DCP、JPEG2K、相机 RAW 的剪辑进时间线即缓存**（p.195）——注意：**纯 H.264 素材默认就会被 Smart Cache 缓存源**，这正对应我们素材类型；
   - Speed（变速）效果在**源级**缓存（"cached at the source level, which makes it possible to move cached speed effects clips on the Timeline without needing to re-cache them"，p.195）——**变速缓存可随剪辑移动而不失效**；
   - Fusion 特效/合成的剪辑。
2. **Node Cache**（调色节点级）：Smart 自动缓存 Noise Reduction（降噪）、Motion Blur、任何 ResolveFX/OFX 插件节点（p.193、p.195）。
3. **Sequence Cache**（时间线级）：Smart 自动缓存所有非 Normal 合成模式叠加、含不透明度或 **Speed/Retime 效果**的剪辑、以及所有转场（p.196）。
- User 模式默认**不**自动缓存 H.264 等格式，只有手动标记；仅"Automatically cache Fusion Effects in User Mode"默认开（p.195）。
- 后台缓存默认开，用户停止操作 N 秒后自动开始（N 可在 Master Settings 调，p.193）；**默认 5 秒**、可低至 1 秒
  （BMD 论坛：https://forum.blackmagicdesign.com/viewtopic.php?f=22&t=116680 、https://forum.blackmagicdesign.com/viewtopic.php?f=33&t=167360 ，访问 2026-09）。

### 2.2 缓存格式与体积
- Render Cache Format 选项（Master Settings）：DNxHR LB/SQ/HQ/HQX/444、Uncompressed 10-bit / 16-bit float、ProRes 系（Mac）；
  **较新版本（约 17/18+）加入 H.264/H.265 作为缓存/代理格式**（https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=225949 中用户将 Proxy/Cache 设为 H.264；教程 https://creativevideotips.com/tutorials/the-best-proxy-format-for-resolve 用 H.265 代理，访问 2026-09）。
  默认值：教程口径 Windows=DNxHR SQ、Mac=ProRes 422 系（https://creativevideotips.com/tutorials/davinci-resolve-render-cache-essentials ，访问 2026-09）；
  Reddit 有用户称默认 DNxHR HQX "文件无谓地大"（https://www.reddit.com/r/davinciresolve/comments/m3iklu/ ，访问 2026-09）——并列。
- **体积量级（1080p60 时间线，全分辨率缓存）**：DNxHR SQ ≈ 124 GB/小时、LB ≈ 38.8 GB/小时、HQX ≈ 187 GB/小时（按 Avid 表换算，同 §1.2）。
  切到 H.264 缓存可小约一个数量级，但播放时要再解长 GOP，社区普遍认为这"违背缓存初衷"
  （https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=225949 ，访问 2026-09）。
- 缓存管理：Playback > Delete Render Cache > All/Unused/Selected；还有跨工程 Cache Manager 窗口（p.198，截图显示单工程缓存可达 GB 级，如样例工程 3.54 GB）。
- **对我们 2.26 GB/小时的物化代理：Resolve 的帧内编码缓存在 1080p60 下是它的 17–83 倍体积** [按 §1.2 表换算]。

### 2.3 命中/未命中的回放差异与状态条
- 时间线上方状态条：**红=待缓存、蓝=已命中（直接读盘播放）、橙=混合**（BMD 官方论坛：https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=103711 ，访问 2026-09）。
- 未命中时回放质量由 Performance Mode/Timeline Proxy 决定，优先保音频丢视频帧（默认 Show All Video Frames off，p.184）；
  命中后重段秒回实时。社区定性一致：缓存段流畅、未缓存段卡（https://davinciresolveclub.com/davinci-resolve-render-cache/ ，访问 2026-09）。

### 2.4 ≥4x 快进表现
- **未找到公开的系统性量化实测**（FPS/丢帧率数据）。
- 相关定性证据：Smart 缓存填满（红→蓝）后再快进是社区标准操作；加速片段（如 1600x）不缓存则回放极卡
  （https://www.reddit.com/r/davinciresolve/comments/n3k12j/ ；https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=83753 ，访问 2026-09）。
- [推断] 缓存命中段（帧内编码落盘）快进时每帧都可独立解码，>4x 只受磁盘顺序读限制，表现稳定；
  未缓存长 GOP 源在 4x+ 时解码压力≈4 倍且跳跃取帧，必然丢帧或降速（与 p.184 默认丢帧策略一致）。
  我们的 4x 预览播帧内可随机解码的 H.264 代理仍需连续解 GOP，但保留段连续播放时等同正常回放，实测可行。

---

## 3. Seek / Scrub 实现（Q3）

### 3.1 缩略图缓存
- Resolve 为 Media Pool/时间线生成缩略图并落缓存盘（Media Storage 首盘/Cache Files Location；
  https://www.reddit.com/r/colorists/comments/9ipm1s/ ；https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=47639 ，访问 2026-09）。
- 已知痛点：缩放时间线时缩略图重载导致数秒卡顿，常见解法是关掉缩略图显示
  （https://www.reddit.com/r/davinciresolve/comments/1i11ilp/ ；https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=48893 ，访问 2026-09）。
- **未找到官方文档描述缩略图的密度/调度算法**。

### 3.2 长 GOP vs 帧内素材的定位延迟
- **未找到公开的量化延迟数据（毫秒级实测）**。
- 机理与社区共识：帧内编码（DNxHR/ProRes/BRAW）任意帧可独立解码，scrub 立即；长 GOP（H.264/H.265）定位需回溯到前一 I 帧并解到目标帧，
  B 帧（我们素材 ~79%）还需先解其参考帧，跳跃 scrub 成本高
  [机理推断，社区旁证：https://www.reddit.com/r/davinciresolve/comments/tvnlns/ "为什么 Resolve 播 H.264 这么差"；BMD 论坛 https://forum.blackmagicdesign.com/viewtopic.php?f=33&t=172202 ，访问 2026-09]。
- 行业对策与我们的方案一致：**转代理/中间编码**是几乎所有教程对长 GOP 素材 scrub 卡顿的第一处方
  （https://davinciresolveclub.com/davinci-resolve-slow-playback/ ；https://cutsio.com/blog/best-way-to-optimize-playback-in-davinci-resolve ，访问 2026-09）。
- 差异点：Resolve 的代理是**整源替换**（scrub 也在代理上），我们的方案是**原始解码服务按段表选择性解码**——
  scrub 落在删除段时可直接跳到下一保留段 GOP 边界，定位语义甚至可以比"全源代理"更聪明。

### 3.3 JKL 梭式播放
- 倍速档位（官方快捷键行为，多源一致）：L 逐次 1x→2x→4x→8x→16x→**32x**（shuttle 档位 0.5x/1x/2x/4x/8x/16x/32x，正反同构；Shift+K 降一档）
  （https://forum.blackmagicdesign.com/viewtopic.php?uid=16&f=33&t=158477 ；https://www.tourboxtech.com/en/news/how-to-speed-up-playback-in-davinci-resolve.html ，访问 2026-09）。
- 丢帧行为：**未找到官方逐档说明**。默认 "Show All Video Frames off"=算力不足时**保音频、丢视频帧**（p.184）；
  [推断] 高倍速梭式播放按此策略丢帧/跳帧，不保证逐帧显示；音频会变速播放（Reddit 用户讨论 2x/4x 音频变调与 buffer 设置，https://www.reddit.com/r/davinciresolve/comments/vktzr4/ ，访问 2026-09）。
- 对比：我们 4x 预览是固定档位 + 解码服务选择性跳过删除段；Resolve 没有对应的"跳过删除段"概念（它不删数据，只是时间线不引用）。

---

## 4. 删减/修剪的用时模型（Q4）

- **剪辑操作本身是元数据操作**：Resolve 全程非破坏性（剪辑点、修剪都是元数据），不会触发源重编码；
  Optimized/Proxy/Cache 媒体存在时播放走缓存。手册明确三层缓存设计目标即
  "改时间线不需要重缓存调色、改调色不需要重缓存时间线"（p.192）。
- **但 Render Cache 与剪辑实例强绑定，改位置/剪切会使缓存失效**：
  - 用户实测：仅把剪辑左移一点，Smart/User 模式下缓存条变红、重渲（https://www.reddit.com/r/davinciresolve/comments/tgrzf0/ ，访问 2026-09）；
  - 刀片切开/分割剪辑会使两侧缓存失效重渲（https://www.reddit.com/r/davinciresolve/comments/127pu8z/ ；https://www.reddit.com/r/davinciresolve/comments/j7kuz5/ ，访问 2026-09）；
  - 小改动引发整段时间线重缓存的报告（https://www.reddit.com/r/davinciresolve/comments/1vhaelj/ ；https://www.reddit.com/r/davinciresolve/comments/1n7wsck/ ，访问 2026-09）；
  - 例外（官方）：**源级缓存（Fusion Output Cache 的 H.264 源缓存与 Speed 效果源级缓存）可随剪辑在时间线上移动而不重缓存**（p.195）。
- **重渲耗时量级：未找到公开量化数据**。[推断] 缓存重渲成本 ≈ 首次缓存该段成本（效果处理 + 帧内编码落盘，1080p 通常快于实时），
  量级为"秒级/十秒级每分钟素材"；但长片多次微调会反复触发，社区把它列为最大痛点之一（见 §7）。
- **对比我们**：剪切=改段表，O(1) 元数据生效；预览代理按导出节奏物化，**剪切根本不会使已物化代理失效**（代理是"保留时间线视图"的只读快照）。
  这一点上我们的模型规避了 Resolve 最被诟病的"剪一下、缓存作废"问题——代价是重剪后需要重新物化（6.8x 实时，可控）。

---

## 5. 内存与 GPU（Q5）

### 5.1 官方推荐（Blackmagic techspecs，访问 2026-09：https://www.blackmagicdesign.com/products/davinciresolve/techspecs ）
- Windows：**16 GB RAM**（用 Fusion 则 32 GB）；GPU ≥ **4 GB VRAM**，OpenCL 1.2 / CUDA 12.8；分辨率档位官方不再细分（统一最低线）。
- macOS：8 GB RAM（Fusion 16 GB）。
- 社区口径补充：4K 流畅需 32 GB RAM、8 GB+ VRAM（论坛/测评共识，访问 2026-09）。

### 5.2 社区实测（RAM/VRAM 与素材格式关系）
- 1080p 剪辑 32 GB 舒适、16 GB 可用（https://www.reddit.com/r/davinciresolve/comments/1hbzwoi/ ，访问 2026-09）。
- **GPU 硬解 H.265 时系统 RAM 从 16 GB 飙到 32 GB**（BMD 论坛实测帖：https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=82810 ，访问 2026-09）——长 GOP 硬解路径内存放大明显。
- 4K H.264/H.265 剪辑 VRAM 可瞬时 >12 GB，而 DNxHR/ProRes 下 10 GB VRAM 富余（https://www.reddit.com/r/davinciresolve/comments/1gdgfqc/ ，访问 2026-09）。
- Resolve Studio 的 H.264/H.265 硬解只覆盖特定 profile/色度/位深组合，不匹配则回落到慢的软解（Puget Systems：https://www.pugetsystems.com/labs/articles/what-h-264-and-h-265-hardware-decoding-is-supported-in-davinci-resolve-studio-2122/ ，访问 2026-09）。
- **对我们的启示**：我们素材 1080p60 H.264 长 GOP 在 Resolve 里属"难解码格式"（官方原话，p.187/p.195），
  官方路径就是转 Optimized/Proxy；我们的 Rust 解码服务按需解长 GOP + 播低码率 H.264 代理，内存占用模型完全不同（解码池按段表上限可控）。

---

## 6. 预览 = 导出一致性（Q6）

官方口径（【手册Ch8】）：
- **默认导出不用任何预览替代物，保证最高质量**：
  - Proxy："By default, the Deliver page always reverts proxies to the original source media for final output to ensure the highest quality render."（p.206）
  - Optimized Media：Deliver 页有 "Use Optimized Media" 复选项，勾上才用优化媒体换渲染时间，且建议配高质量格式（p.190）。
  - Render Cache：Deliver 页 "Use Render Cached Images" 复选项，勾上才"直接从缓存写帧而非重渲效果"（p.197），手册建议先设高质量缓存格式"to guarantee the best results"。
  - Performance Mode/Timeline Proxy/解码降质：输出不受影响（p.184–186；Deliver 有 "Force debayer/sizing to highest quality" 兜底）。
- **逐位一致性：官方未承诺 "bit-exact"**。缓存帧是按所选缓存格式（如 8-bit DNxHR SQ）落盘的有损中间体，
  勾选复用缓存导出时会引入该格式的量化差 [推断，由 p.197 "选择高质量格式以保证最佳结果"反推]。
  未找到官方逐位一致性声明。
- **含义**：行业成熟做法 = **预览代理永不默认进入成片链路**；要快导出必须显式勾选并接受质量折损（"dailies/prosumer 快出片"场景，p.206）。
- **对我们**：若成片直接用 CRF14 物化代理导出，相当于 Resolve 的"Use proxy media"模式（质量封顶在代理）；
  若要"预览=导出逐位一致"，应在导出时按段表从**原始流**做流级剪辑/或接受 CRF14 作为最终质量并验证视觉无损。
  我们的 CRF14（≈5 Mb/s，低复杂度游戏画面）视觉上接近无损，但与"从原流失级剪辑"不逐位一致 [推断]。

---

## 7. 长时间线（小时级）的真实用户痛点（Q7）

 Reddit/BMD 论坛已知问题（均访问 2026-09）：
1. **缓存体积与反复重建**：Render Cache 填盘、小改动大面积重渲、"缓存是唯一解但每次重跑很痛苦"
   （https://www.reddit.com/r/davinciresolve/comments/1btszed/ "Render Cache is a mess"；
   https://www.reddit.com/r/davinciresolve/comments/1r90422/ "playback lag phenomenon"）。
2. **长会话内存泄漏感**：Resolve 20 播放随时间劣化、重启恢复（https://www.reddit.com/r/davinciresolve/comments/1lh3qei/ "Editing in Resolve Studio 20 is a pain"）。
3. **好机器仍卡**：高配机在纯 H.264/4K 时间线导航 CPU 100%、切点卡顿（https://www.reddit.com/r/davinciresolve/comments/10nkl9u/ ；https://www.reddit.com/r/davinciresolve/comments/1j8v27v/ —— quarter 代理分辨率+NVMe 仍卡，社区归因长 GOP 源）。
4. **缩略图/时间线缩放卡顿**（§3.1）。
5. **长片标配解法 = 代理**：几乎所有长片工作流教程要求生成代理/优化媒体 + 缓存放 NVMe
   （https://davinciresolveclub.com/davinci-resolve-slow-playback/ ；https://support.suitestudios.io/en/articles/11876623 ）。
6. **免费版尤甚**：无 H.264/HEVC 硬解（Windows/Linux），长 GOP 全软解（§8）。

---

## 8. 免费版 vs Studio：与 proxy / render cache 相关的差异（Q8）

| 功能 | 免费版 | Studio | 来源 |
|---|---|---|---|
| Render Cache（Smart/User） | 有，行为一致 | 有 | https://davinciresolve21.com/blog/davinciresolve-render-cache-explained-smart-vs-user （访问 2026-09） |
| Optimized / Proxy Media 生成 | 有 | 有 | 同上 + 手册 |
| ProRes 编码（含 ProRes 代理/缓存） | Windows/Linux **无**（macOS 有） | 全平台有 | Puget Systems https://www.pugetsystems.com/blog/2025/03/25/prores-encoding-in-davinci-resolve-19-1-4/ （2025-03） |
| **H.264/H.265 硬件解码（Windows/Linux）** | **无（软解）** | 有（受 profile/位深/色度限制） | BMD techspecs；Puget Systems（访问 2026-09） |
| Blackmagic Proxy Generator | **Lite 版（无 H.264/H.265 代理输出）** | 完整版 | https://whoismatt.com/how-to-use-proxy-files-to-edit-super-fast-in-davinci-resolve/ ；usermanual.com Cloud Store 手册 |
| 降噪（Spatial/Temporal NR） | 无 | 有 | BMD 论坛 https://forum.blackmagicdesign.com/viewtopic.php?f=32&t=90284 |
| Speed Warp（光流高级档） | 无 | 有 | 同上 |

**对我们的影响评估**：我们素材正是 1080p60 H.264 长 GOP——免费版 Resolve 在 Windows/Linux 上软解它，
恰是其最弱场景；Studio 硬解又受 profile 限制。即 Resolve 处理我们这类素材的成本天然偏高，
而我们自研方案对长 GOP 的段表选择性解码是按需、可并行的，比较基准应计入这一点。

---

## 9. 对「物化重剪代理」方案的印证与反驳

### 印证（行业做法同向）
1. **"难解的源 → 落盘成易播中间体"是行业正统**：Resolve 五条通道里两条（Optimized/Proxy）+ 缓存一条都是这个思路；
   长 GOP H.264 被官方点名（p.187/p.195）。我们把保留时间线物化成代理属于同一正统。
2. **"预览质量 ≠ 导出质量"的分层是行业默认**（§6）；我们以"高清代理兼做成片源"上探到 Resolve 里需显式勾选的
   "Use proxy media"档位——CRF14 的质量余量让这一步风险可控 [推断]。
3. **磁盘账**：Resolve 标准代理 9.7–124 GB/小时 vs 我们 2.26 GB/小时，我们省 1–2 个数量级（§1.2）。
4. **按需物化**：Resolve 只做"时间线用到的部分"的缓存通道正是 Fusion Output Cache（p.192），与我们的"按段表只物化保留段"同源；
   我们按导出节奏增量物化，与 Proxy Generator 的 watch-folder 后台转码节奏等价。

### 反驳 / 风险（行业弃路的原因）
1. **H.264 代理是社区公认的坏代理格式**：长 GOP scrub 代价不随"变成代理"消失（The Post Flow；BMD 论坛 t=172202）。
   我们的方案之所以可行，前提是**自研解码服务能持续 4x+ 实时解 CRF14 长 GOP**——这在通用 NLE 里不成立，在我们管线里已实测成立；
   但需注意 4x 预览时解码余量、seek 回溯 I 帧的延迟（B 帧 79% 加重回溯）。
2. **剪切→缓存失效**：Resolve 的痛点（§4）提示我们：物化代理必须定义为"只读快照 + 版本化重物化"，
   否则会在反复改剪时陷入 Resolve 式重渲循环；6.8x 实时意味着 1 小时保留内容重物化 ≈ 9 分钟（实测线性外推 [推断]）。
3. **导出一致性**：Resolve 默认导出永远回原素材。我们若用代理直接成片，需建立"代理即交付物"的质量验收
   （CRF14 视觉无损验证），或导出时回原流按段表剪辑——后者才能拿到 Resolve 默认档的"最高质量渲染"语义。
4. **音频**：Resolve 代理可嵌同步音频（p.200）；我们的物化代理需确认音轨随行，否则预览/导出要另走音频链路。

### 结论
物化重剪代理 = Resolve「Proxy Media（按需版）+ Use proxy media 导出」的组合，方向与行业一致；
差异在代理编码选择（我们 H.264 长 GOP vs 行业帧内），这是**用自研解码能力换 10–55 倍磁盘空间**的交换，
其成立条件（解码实时性、seek 延迟、重物化代价、导出质量语义）均已在 §1–§6 找到行业对照点，无根本性冲突。

---

## 10. 来源清单（访问日期均为 2026-09-14）

一手/官方：
1. DaVinci Resolve 18 Reference Manual Ch.8（PDF 镜像，p.182–210）：https://lira.epac.to/DOCS-TECH/Graphics/Da%20Vinci%20Resolve/DaVinci%20Resolve%2018%20-%20Reference%20Manual%20-%20Chapter%208%20-%20Improving%20Performance,%20Proxies,%20and%20the%20Render%20Cache.pdf
2. Blackmagic techspecs：https://www.blackmagicdesign.com/products/davinciresolve/techspecs
3. Resolve 20 Editors Guide：https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Editors-Guide.pdf
4. Avid DNxHR Codec Bandwidth Specifications（2024-07 更新）：https://kb.avid.com/pkb/articles/en_US/Knowledge/DNxHR-Codec-Bandwidth-Specifications
5. Puget Systems：H.264/H.265 硬解支持矩阵 https://www.pugetsystems.com/labs/articles/what-h-264-and-h-265-hardware-decoding-is-supported-in-davinci-resolve-studio-2122/ ；ProRes 19.1.4 https://www.pugetsystems.com/blog/2025/03/25/prores-encoding-in-davinci-resolve-19-1-4/
6. Blackmagic Cloud Store 手册（Proxy Generator）：https://usermanual.com/support/blackmagicdesign/web-manual/cloud-store/proxy-generator

社区/论坛：
7. BMD 论坛：代理格式讨论 t=172202；缓存条颜色 t=103711；Smart/User t=138423；Fusion 缓存随修剪失效 t=203618；H.264 缓存失效 t=225949；RAM 异常 t=82810；VRAM t=222597；梭式倍速 t=158477；加速片段缓存 t=83753；Proxy Generator 平台差异 t=178458、t=168814；Optimized vs Proxy vs Cache t=178600
8. Reddit r/davinciresolve：tvnlns（H.264 回放差）、tgrzf0（移动剪辑重缓存）、127pu8z（刀片切割重缓存）、j7kuz5（分割丢缓存）、1n7wsck/1vhaelj（小改全量重缓存）、1btszed（缓存乱）、1r90422（缓存唯一解但慢）、1lh3qei（v20 会话劣化）、10nkl9u/1j8v27v（高配机卡）、1i11ilp（缩略图卡）、1hbzwoi（RAM 民调）、1gdgfqc（VRAM 实测）、vktzr4（梭式音频）、m3iklu（缓存格式默认）、179guao（Proxy Generator Lite）
9. 教程/行业：whoismatt.com（Lite 无 H.264）、creativevideotips.com（缓存格式推荐、H.265 代理）、davinciresolveclub.com（代理工作流/缓存指南）、davinciresolve21.com（Smart vs User、缓存占盘）、thepostflow.com（代理工作流真相：H.264 代理重新引入长 GOP 代价）、cutsio.com、tourboxtech.com（JKL 倍速、导出用缓存）、support.suitestudios.io、workflow.frame.io、neatvideo.com（内存）

未尽事项：
- Render Cache Format / Optimized Media Format 的**官方默认值**原文（DR20 全手册 209MB 下载中，落地后可补 grep 验证；当前以教程口径并列）。
- ≥4x 快进的丢帧率量化、seek 延迟毫秒级数据：未找到公开数据，仅机理推断。
