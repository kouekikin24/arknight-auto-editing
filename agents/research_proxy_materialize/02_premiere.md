# 02 · Adobe Premiere Pro 预览/回放架构调研

> 调研日期：2026-09-14。方式：WebSearch + WebFetch/curl(本地代理 127.0.0.1:7890)。
> 纪律：数字尽量给来源链接与版本；冲突并列；查不到写「未找到公开数据」并标 [推断]。
> 适用对象：自研 Arknights 录制剪辑工具「物化重剪代理」方案对齐参考。

---

## 1. Proxy 工作流（ProRes Proxy / H.264 代理）

**生成方式**（Adobe 官方）：
- 入口：Media Browser 勾选 Ingest → 「Create Proxies」；或对已有素材右键 Proxy → Create Proxies。**Ingest 默认关闭**。
- 代理由 **Adobe Media Encoder 在后台排队编码**，编辑不被阻塞；可暂停队列、可在编码中退出 Premiere（重开项目自动补挂载）；但中途终止未完成文件会断开挂载。
  来源：[Adobe: Ingest and proxy workflows](https://helpx.adobe.com/premiere/desktop/organize-media/ingest-proxy-workflow/ingest-and-proxy-workflow.html)、[Adobe: Create proxies](https://helpx.adobe.com/premiere/desktop/organize-media/ingest-proxy-workflow/create-proxies.html)、[Frame.io 2024 代理指南](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/)
- 默认预设 = **Apple ProRes QuickTime Proxy**；默认 **Frame Size = Half（1/2 原始分辨率）**（亦可 1/4）。代理文件存于「Proxies」文件夹，文件名加 `_Proxy` 后缀。
- 代理硬约束：帧率/场序/时长/声道数必须与原片一致；分辨率须能整除原片（如 1920x1080 原片可配 960x540）。来源：[Frame.io 2024](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/)

**默认分辨率与推荐**：
- 内置 ProRes Low-Resolution Proxy 示例 = **1024x540, 10-bit**（面向 4K 原片，即 1/4 偏下）。来源：[Frame.io 2024](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/)
- 社区第三方教程对 4K 原片亦常推荐 1024x540（≈1/4）。来源：[Filmora 教程](https://filmora.wondershare.com/adobe-premiere/premiere-pro-proxy.html)

**码率与磁盘（1080p60 原片换算）**：
- Apple 官方白皮书锚点：**ProRes Proxy 目标码率 ≈ 45 Mbps @ 1920x1080/29.97**（[Wikipedia 汇总](https://en.wikipedia.org/wiki/Apple_ProRes)、[ER Media ToolKit 手册引用](https://audiospot-creativetools.com/wp-content/uploads/2024/07/ER2-Pro-User-Guide-2245-Mac.pdf)）。ProRes 码率随帧率线性缩放 →
  - **ProRes Proxy @1080p60 ≈ 90 Mbps ≈ 40.5 GB/小时** [计算：45×2 ÷ 8 × 3600]
  - （参考）ProRes 422 LT @1080p60 ≈ 204 Mbps ≈ 92 GB/h；ProRes 422 @1080p60 ≈ 294 Mbps ≈ 132 GB/h [计算，锚点 102/147 Mbps @29.97]
- **Adobe H.264 代理：未找到官方固定码率**，由 AME 代理预设决定（社区极少用 H.264 代理，首选 ProRes Proxy；Frame.io 明言「没有好理由不要选 ProRes Proxy」，H.264 仅用于笔记本便携等边缘场景）。锚点：1080p60 @ 5 Mbps ≈ **2.25 GB/小时**——与我们 CRF14 实测 2.26 GB/小时同量级。
- 官方对代理的建议场景（Adobe 播放优化文档）：H.264/H.265 等重解码格式、4K/高分辨率素材。来源：[Adobe: Optimize playback performance for H.264 and H.265 media](https://helpx.adobe.com/premiere/desktop/troubleshooting/playback-issues/optimize-playback-performance-for-h264-h265-media.html)

**Toggle Proxy 语义**：Program/Source 监视器的蓝色「Toggle Proxies」按钮**全局切换**播放源=代理或原片（二选一，暂停画面同样显示当前源）；导出**始终用原片**（除非原片离线），另有导出端「Use Proxies」覆盖项。时间线片段有挂载状态角标（缺失/禁用等）。来源：[Frame.io 2024](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/)

---

## 2. Preview Files（序列预览文件，.prvfile/.pek）

**是什么**：时间线段落（Sequence > Render In to Out / Enter）渲染出的**按时间锚定的中间文件**，存于暂存盘的 **「Adobe Premiere Pro Preview Files」文件夹**（路径可在 Scratch Disks 配置）。`.pek` 不是视频预览——它是**音频波形峰值缓存**（另有 `.cfa`=音频 conform 缓存），属媒体缓存体系、不可关闭只能换位置。
来源：[Adobe: Use preview files when rendering](https://helpx.adobe.com/premiere/desktop/render-and-export/render-sequences-for-playback/use-preview-files-when-rendering.html)、[Adobe Community: pek files](https://community.adobe.com/questions-729/pek-files-1425972)、[Creative COW: cfa/pek](https://creativecow.net/forums/thread/cfa-and-pek-file-madness/page/3/)

**什么内容会进预览文件**：
- 播放引擎**无法实时播放的段落（红色渲染条）**：重度效果、变速（Speed ramp / Rate Stretch / Time Remapping）、转场、光流插帧等；
- 以及用户手动渲染的段落（含实时可播但建议渲染的黄色段）。「Render Effects In to Out」只渲染效果段；「Render In to Out」连黄色一起渲染。
- 社区共识：**变速片段不渲染就卡顿**是典型场景（见 §5）。
来源：[Boris FX: How to Render in Premiere](https://borisfx.com/blog/how-to-render-in-premiere-pro/)、[Motion Array](https://motionarray.com/learn/premiere-pro/how-to-render-video-adobe-premiere-pro/)、[Adobe Community: speed ramps not playing back](https://community.adobe.com/questions-729/speed-ramps-not-playing-back-correctly-1412662)

**格式**：
- 序列设置 > Video Previews 中选 Preview File Format。历史默认 = **I-Frame Only MPEG**（帧内编码、类似 M-JPEG 的 MPEG 家族帧内编解码，Windows 容器 .avi / Mac .mov）。
- **Premiere Pro 23.0（2022-10）起，多数「从素材新建序列」的默认预览编码改为 Apple ProRes 422 LT**（I-frame 中间码）。来源：[Adobe 社区官方讨论: Use ProRes as the default codec for sequence rendering](https://community.adobe.com/questions-734/discuss-use-prores-as-the-default-codec-for-sequence-rendering-313242)
- 扩展名：新版 Premiere 渲染预览文件在 Preview Files 文件夹中（用户可见 `.prvfile`）；**未找到 Adobe 官方对 `.prvfile` 格式的公开文档**（格式不透明、跨版本兼容性由 Premiere 内部管理）。
- 编码质量由 Video Previews 的 **Quality 滑杆**控制（数值越高码率/体积越大；刻度各资料描述不一：1–5 与 0.1–2.0 两种说法并存，未找到官方精确口径，以软件实际为准）。来源：[Adobe FAQ: How to Change the Codec for Video Preview Files](https://community.adobe.com/questions-729/faq-how-to-change-the-codec-for-video-preview-files-1393003)

**磁盘占用公式**：**未找到官方公式**。
- [推断/计算锚点] ProRes 422 LT 预览 @1080p60 ≈ 204 Mbps ≈ **92 GB/小时**；I-Frame Only MPEG 为质量驱动编码，社区有「预览文件异常巨大」的实测抱怨（[Creative COW 实例帖](https://creativecow.net/forums/thread/unusually-large-preview-files-rendered/)），数量级通常低于 ProRes 422 但无公开精确值。
- 重要行为：**Premiere 不会自动删除失效的旧预览文件**——反复改动重渲染会持续吃盘（[Reddit 实测抱怨](https://www.reddit.com/r/premiere/comments/14cywku/massive_rendering_preview_files_are_making_my/)）；需手动 Sequence > Delete Render Files。

**失效规则**（官方口径分散，以下为文档+社区综合）：
- 预览文件与「序列设置 + 渲染的时间区间」绑定：区间内**剪辑被移动/修剪/波纹/滑动、效果参数变更、变速变更** → 该区间渲染条重新变黄/红，对应预览作废；
- **序列设置（帧尺寸/帧率/预览编码）变更 → 全部预览失效**；
- 预览文件被删或路径失效 → 时间线不显示视频预览（官方排障页：[Timeline doesn't show video preview](https://helpx.adobe.com/premiere/desktop/troubleshooting/playback-issues/timeline-doesnt-show-video-preview.html)）。
- [推断] 预览按时间线位置寻址，未改动但整体平移也会造成区间错位（社区常见「渲染变绿后又红」报告，如 [Facebook 讨论](https://www.facebook.com/groups/premierepro/posts/2177764555634594/)）。

**导出时「Use Previews」语义**：见 §7。

---

## 3. 长 GOP 素材的 seek/scrub 痛点

**官方/权威解释（为什么建议转代理）**：
- H.264/HEVC 为帧间/长 GOP：帧多为 P/B 帧，单帧显示需回溯解码至 I 帧再链式重建；随机访问（scrub、倒放、变速、多机位）是「随机访问模式」，与解码器假设的顺序播放相悖。[Puget Systems 技术文（2019）](https://www.pugetsystems.com/support/guides/why-cant-my-new-workstation-edit-h-264-media-smoothly-1367/)、[Adobe 社区 FAQ: Intraframe vs Long GOP](https://community.adobe.com/questions-729/the-difference-between-intraframe-like-prores-and-long-gop-like-h-264-codecs-1406869)、[Frame.io 编辑编解码指南](https://workflow.frame.io/guide/editing-codecs)
- Adobe 已知问题页**官方承认** GOP 结构会导致 poor seek and scrub performance（[Adobe: Known and fixed issues](https://helpx.adobe.com/premiere/desktop/troubleshooting/limitations-and-known-issues/known-and-fixed-issues.html)）。
- 官方缓解路径：建代理 / 降播放分辨率 / 硬件解码（[Adobe: Optimize playback performance for H.264/H.265](https://helpx.adobe.com/premiere/desktop/troubleshooting/playback-issues/optimize-playback-performance-for-h264-h265-media.html)）。

**实测定位延迟数据**：**未找到公开的 ms 级 seek 延迟测量**。相关可引用行为数据：
- Puget 2020 播放实测（4K H.264/HEVC，i9-10900K + RTX 3080）：软件解码下**正向 ≤2x 可用，2x 以上/倒放必须硬件解码**；scrub 软解「很卡」硬解「顺滑」（详见 §8）。来源：[Puget: GPU Decoding — Is it Faster?](https://www.pugetsystems.com/labs/articles/premiere-pro-gpu-decoding-for-h-264-and-hevc-media-is-it-faster-1908/)
- 社区观察：shuttle 停止后回跳约 30 帧（音频 pitch 维持功能相关，关闭可解）（[Reddit: Shuttle Right skips ~30 frames](https://www.reddit.com/r/premiere/comments/1ahn12p/shuttle_right_skips_about_30_frames_before_video/)）。
- [推断] 定位延迟 ≈ 「目标帧与前一 I 帧的 GOP 距离 + 缓存状态」的函数：冷缓存、长 GOP、B 帧多时延迟显著高于 I-frame 代理——这正是 Adobe/Frame.io 首推 ProRes 类帧内代理的结构性原因。

---

## 4. 内存模型

**偏好设置**：Edit > Preferences > Memory。面板显示三行：Installed RAM / **RAM reserved for other applications** / RAM available for Premiere（Premiere 可用 = 总量 − 保留量）。来源：[Adobe: Memory preferences](https://helpx.adobe.com/premiere/desktop/get-started/preferences-and-settings/memory-preferences.html)
- 关键语义（Digital Rebellion）：「保留给其他应用」是**上限而非预分配**——只有 OS/其他程序真正需要时才占用；Premiere 按需取用。来源：[Digital Rebellion: Premiere Pro Memory Settings Explained](https://www.digitalrebellion.com/blog/posts/premiere_pro_memory_settings_explained)
- 8GB 机器社区建议保留 2–3 GB（[Scribd 设置单](https://www.scribd.com/document/994323224/Premiere-Pro-8GB-RAM-Settings-2)）；大内存机器社区多保留一个适度固定值。

**实测内存占用 vs 素材/时间线复杂度**：
- Puget 跨应用实测：**16GB 相比 64GB 整体性能 −9%**（复杂时间线/特效回放受影响最大），**32GB ≈ 64GB**；官方建议 **Premiere 最低 32GB、重度（4K+/动态链接/多机位）64GB**。来源：[Puget: When Does RAM Capacity Impact Performance?](https://www.pugetsystems.com/labs/articles/when-does-ram-capacity-impact-performance/)、[Puget: Premiere 硬件建议](https://www.pugetsystems.com/solutions/video-editing-workstations/adobe-premiere-pro/hardware-recommendations/)
- 趋势 [Puget 定性]：内存占用随分辨率、素材数量、特效/动态链接、GPU 加速开启快速上升；逐帧缓存回放使其成为「内存饥渴」型应用。素材格式（长 GOP vs 帧内）对**瞬时解码内存**影响相对小，主要差在 CPU/GPU 解码开销（§8）。

**GPU（Mercury）角色**：Mercury Playback Engine（CUDA/Metal/OpenCL）负责**合成管线**：缩放、调色、特效、混合模式的 GPU 加速；**H.264/HEVC 的解码不走 Mercury 渲染器**，走独立的专用硬解单元（NVDEC/Quick Sync/AMF，见 §8）。二者角色互补而非同一回事。来源：[Adobe 硬件加速文档簇](https://helpx.adobe.com/premiere/desktop/get-started/technical-requirements/hardware-accelerated-decoding-and-encoding.html)、[Puget 2020 解码实测](https://www.pugetsystems.com/labs/articles/premiere-pro-gpu-decoding-for-h-264-and-hevc-media-is-it-faster-1908/)

---

## 5. 倍速预览

**JKL 档位**：Premiere 的 J/K/L 是 **Shuttle（线性加速/减速）模型**：L 连按逐级加速、J 反向、K 停；**Shift+L 以 0.1x 步进微调**（如 150%）；K+L 组合为慢放（社区口径 0.25x 级步进，未找到官方精确档位表）。**未找到官方「最高倍速/档位表」公开文档**。来源：[Adobe 社区: JKL shuttle vs playback speed](https://community.adobe.com/questions-729/j-k-l-keys-are-assigned-to-shuttle-but-used-to-be-playback-speed-how-do-i-change-it-back-1342501)、[Reddit: 调速讨论](https://www.reddit.com/r/premiere/comments/zh95e3/speed_up_the_video_for_preview_in_the_timeline_to/)、[LinkedIn: Shift+L 自定义速度](https://www.linkedin.com/posts/d-labs-studio_how-to-get-custom-playback-speeds-in-adobe-activity-7254084154139582464-7qnE)

**高倍速丢帧行为**：best-effort 保时钟：解码/渲染跟不上时**跳帧维持播放速度**，音频 shuttle 可选保音调（关掉可消除部分丢帧/回跳）。降播放分辨率（1/2、1/4、1/8、1/16）与代理是官方/社区标准解法。来源：[Reddit: shuttle skips ~30 frames](https://www.reddit.com/r/premiere/comments/1ahn12p/shuttle_right_skips_about_30_frames_before_video/)、[Creative COW: 105% 速度跳帧](https://creativecow.net/forums/thread/105-speed-video-jumps-frames-on-output/)、[Adobe 播放优化文档](https://helpx.adobe.com/premiere/desktop/troubleshooting/playback-issues/optimize-playback-performance-for-h264-h265-media.html)
- Puget 实测锚点：4K 长 GOP **软解 2x 封顶，>2x 与倒放需硬解**（[Puget 2020](https://www.pugetsystems.com/labs/articles/premiere-pro-gpu-decoding-for-h-264-and-hevc-media-is-it-faster-1908/)）。

**变速片段（Speed ramp / Time remapping）**：
- Time Remapping 上限 1000%（再高需嵌套套娃）([Stack Exchange](https://video.stackexchange.com/questions/22977/how-to-time-remap-faster-than-1000-in-premiere))；
- 时间插值可选 Optical Flow（光流）等；
- **流畅预览需要渲染预览文件**：官方排障建议=渲染（变绿）、删旧渲染文件、换光流插值——即变速是触发红色渲染条与预览文件依赖的典型效果。来源：[Adobe Community: speed ramps not playing back correctly](https://community.adobe.com/questions-729/speed-ramps-not-playing-back-correctly-1412662)、[Adobe 官方 speed ramp 教程](https://www.adobe.com/creativecloud/video/hub/guides/premiere-pro-speed-ramp.html)

---

## 6. 剪辑即时性（cut/trim/ripple 的响应模型）

**双轨模型**：
1. **元数据即时**：cut/trim/ripple/移动等时间线操作只改工程元数据，立即生效、不触发任何编码——编辑手感为即时。
2. **渲染惰性**：改动区域后该区间渲染条回黄/红；只有当播放引擎（GPU+解码+分辨率组合）无法实时时才「需要渲染」。渲染条颜色语义（社区/教程综合）：
   - **红** = 无法实时播放（重效果/变速等），Render Selection 只对红生效；
   - **黄** = 可实时但建议渲染（换机器/降配可能不保）；
   - **绿** = 已有预览文件；
   - **无条** = 素材+效果全实时无压力。
   Enter / Sequence > Render In to Out 渲染红+黄并转绿。来源：[Adobe Community: Yellow render line](https://community.adobe.com/questions-729/yellow-render-line-premiere-pro-1340829)、[Reddit: 为什么变红](https://www.reddit.com/r/premiere/comments/1de40nd/why_does_my_render_bar_turn_red_all_i_did_was/)、[Boris FX](https://borisfx.com/blog/how-to-render-in-premiere-pro/)
- **对长 GOP 素材的即时性惩罚**：即使纯剪辑（无效果），scrub/trim 中的反复随机解码在软解下也会卡（Frame.io：强机器上 4K 压缩素材 trim 仍会丢帧）——这是「代理让剪辑手感即时」的动因。来源：[Frame.io 2024](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/)

---

## 7. 预览=导出一致性（Use Previews 的质量争议）

**语义**：导出勾选「Use Previews」（默认不勾选）→ 已渲染区间直接取预览文件参与合成，不再实时重渲染。若预览编码与导出编码**完全匹配**（编解码器/帧尺寸/帧率/码率等），触发 **Smart Rendering**（帧直拷、不重编码），Adobe 宣称理想情形导出**快至 ~20x**；不匹配则该段仍走预览文件→再编码。来源：[Adobe FAQ: What is Smart Rendering?](https://community.adobe.com/questions-729/faq-what-is-smart-rendering-1355162)、[Adobe: Smart rendering supported formats](https://helpx.adobe.com/premiere/desktop/render-and-export/render-sequences-for-playback/smart-rendering-supported-formats.html)

**质量争议（并列）**：
- 「无损」派：预览编码=导出编码时 Smart Rendering 无代损；风险仅存在于不匹配场景（[Reddit: Use previews toggle 影响画质吗](https://www.reddit.com/r/premiere/comments/14zkhse/does_having_the_use_previews_toggle_on_affect_the/)）。
- 「代损」派：只要非无损编码就存在代际损失（[Creative COW: Smart rendering in Premiere](https://creativecow.net/forums/thread/smart-rendering-in-premiere/)）；设置不匹配会「双重代损」（[Wipster](https://www.wipster.io/blog/understanding-premiere-pros-confusing-export-settings)）；旧默认 I-Frame Only MPEG 码率低，配合「Match Sequence Settings」导出会明显掉质（[Adobe Community 实例](https://community.adobe.com/questions-729/source-mp4-sequence-settings-i-frame-only-mpeg-export-settings-match-sequence-why-not-mp4-output-1363476)）；有技术评测认为难以保证哪些段走了预览，最终成片风险不可控（[pointsinfocus CS6 评测](https://www.pointsinfocus.com/learning/digital-darkroom/performance-aspects-of-exporting-video-with-premiere-pro-cs6/)）。
- 实务共识：交付 H.264 时许多剪辑师**不勾** Use Previews；要快就用 ProRes 422 LT/422 预览 + 严格匹配。**注意与代理的区别：代理工作流的导出永远取原片，与预览文件代损问题无关。**（[Frame.io 2024](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/)）

---

## 8. 硬件加速解码（H.264 长 GOP 的 GPU 解码）

**支持矩阵**（Puget 实测汇总，2024-11 发、2025-06 更新；Premiere 版本敏感）：
- 硬解历史上仅 **Intel Quick Sync**（随 CPU）；**Premiere Pro 14.5（2020-06）加入 GPU 解码**（NVIDIA/AMD）。
- 独显（GTX 10 / RTX 20/30/40 / AMD 5000+ / Intel Arc）：**H.264 8-bit 4:2:0 全支持**（我们 1080p60 8bit 4:2:0 命中）；H.264 4:2:2/10-bit 支持很晚（2025 年 25.x 才补齐，RTX 50 需 25.3）；MXF 封装 H.264/HEVC 硬解需 24.4+。
- 核显：Quick Sync 10 代起 8-bit 4:2:0/4:2:2 支持。
- 开关：Preferences > Media > Enable hardware accelerated decoding（更改需重启）。
来源：[Puget: What H.264 and H.265 Hardware Decoding is Supported in Premiere Pro?](https://www.pugetsystems.com/labs/articles/what-h-264-and-h-265-hardware-decoding-is-supported-in-premiere-pro-2120/)（含自行验证方法：Alt+Ctrl+F12 Debug Monitor 看 Importer.MPEG 的 SW/HW 帧计数）

**实测收益**（Puget 2020，10900K/3900X + RTX 3080，Premiere 14.3 vs 14.5，4K H.264/HEVC 60–100Mbps）：
- **scrub：软解卡顿 → 硬解（QSync/GPU）顺滑**，差距立现；
- **JKL：软解正向 ≤2x 可用，>2x 与倒放需硬解**；
- **多机位：9+ 路软解明显吃力，硬解改善**；
- H.264 媒体 QSync 略优于 GPU 解码，HEVC 则 GPU 略优。
来源：[Puget: Premiere Pro GPU Decoding for H.264 and HEVC Media — Is it Faster? (2020-10)](https://www.pugetsystems.com/labs/articles/premiere-pro-gpu-decoding-for-h-264-and-hevc-media-is-it-faster-1908/)

---

## 9. 对我方「物化重剪代理」方案的印证与反驳

| # | Premiere 做法 | 对我方方案（保留时间线离线重编码 H.264 CRF14 代理 + 预览播代理 + 暂停真源帧，4x） |
|---|---|---|
| 1 | 长 GOP 素材官方路线=后台物化代理（Ingest→AME），**架构同构**：都按节奏离线重编码、预览播轻量副本 | **印证**：产品范式一致；我方按导出节奏物化 ≈ Ingest 后台 AME |
| 2 | 代理质量：首选 ProRes Proxy ≈ 40.5 GB/h @1080p60；我方 H.264 CRF14 ≈ 2.26 GB/h，**小一个数量级以上** | **印证（存储优势）**，但注意 §3：ProRes 代理同时是**帧内编码**——我方 H.264 代理若长 GOP，seek/scrub 会重蹈长 GOP 痛点。**反驳/改进点：代理应约束 GOP（≤60 帧或 All-I）+ 帧级索引**（我方已有段表+选择性解码，天然适配，但代理文件本身的 GOP 参数要显式设置，不能沿用源片 79%B 帧风格的长 GOP） |
| 3 | 「暂停=真源帧」：Premiere 的 Toggle Proxy 是**播放/暂停全用代理**（导出才回原片） | **我方超出 Premiere**：暂停即真源帧校验，等价于「代理编辑 + 原片审阅」二合一，Premiere 需手动 Toggle 才能做到 |
| 4 | Use Previews/Smart Rendering 的代损争议（§7）：预览文件参与导出有代际损失风险 | **印证我方规避**：我方导出走「真源+段表选择性解码→重编码」，代理永不进成片，无 Use Previews 类代损；代价=成片必须重编码（Premiere 交付 H.264 亦然，非劣势） |
| 5 | 内存模型：Premiere 依赖大 RAM（32–64GB 建议）做帧缓存；我方 Rust 解码服务选择性解码 + mpv | **印证（差异化优势）**：按需解码对内存需求低，且不依赖 Mercury/GPU 合成栈 |
| 6 | 倍速：>2x 依赖硬解、高倍速 best-effort 丢帧（§5/§8） | **印证**：4x 需要轻量代理+硬解；我方 4x 已实测可行；代理存储 2.26GB/h 使全片常驻本地盘无压力 |
| 7 | 预览文件失效规则（§2）：位置敏感、不自动清理、序列设置变更全灭 | **设计借鉴**：我方物化代理若按「保留时间线区间」生成，需定义与 Premiere 等价的失效粒度（区间级失效而非全量重编码），并做旧段垃圾回收——Premiere「不自动删旧预览」是被社区抱怨的缺陷，不应照搬 |
| 8 | 剪辑即时性（§6）：元数据即时 + 渲染惰性（红/黄/绿条） | **设计借鉴**：我方 UI 可对应实现「未就绪区间」指示（等价红条→代理物化完成转绿），把「导出节奏物化」显性化 |

**结论**：Premiere 的官方答案是「代理工作流（播轻量副本）+ 预览文件/Smart Rendering（导出复用）」双轨制；我方「物化重剪代理」在架构上与其同构，且以真源帧暂停、无预览代损、低存储三点亮于 Premiere。**唯一必须吸收的教训**：代理编码必须面向随机访问（短 GOP/All-I + 索引），Premiere 用 ProRes 帧内代理正是为此；长 GOP 代理只解决「实时回放」，不解决「seek/scrub」。

---

## 主要来源清单

- Adobe 官方：[Ingest and proxy workflows](https://helpx.adobe.com/premiere/desktop/organize-media/ingest-proxy-workflow/ingest-and-proxy-workflow.html) | [Create proxies](https://helpx.adobe.com/premiere/desktop/organize-media/ingest-proxy-workflow/create-proxies.html) | [Use preview files when rendering](https://helpx.adobe.com/premiere/desktop/render-and-export/render-sequences-for-playback/use-preview-files-when-rendering.html) | [Smart rendering supported formats](https://helpx.adobe.com/premiere/desktop/render-and-export/render-sequences-for-playback/smart-rendering-supported-formats.html) | [Memory preferences](https://helpx.adobe.com/premiere/desktop/get-started/preferences-and-settings/memory-preferences.html) | [Optimize playback performance for H.264/H.265](https://helpx.adobe.com/premiere/desktop/troubleshooting/playback-issues/optimize-playback-performance-for-h264-h265-media.html) | [Known and fixed issues](https://helpx.adobe.com/premiere/desktop/troubleshooting/limitations-and-known-issues/known-and-fixed-issues.html) | [Hardware-accelerated decoding and encoding](https://helpx.adobe.com/premiere/desktop/get-started/technical-requirements/hardware-accelerated-decoding-and-encoding.html)
- Puget Systems：[H.264 难剪原理 (2019)](https://www.pugetsystems.com/support/guides/why-cant-my-new-workstation-edit-h-264-media-smoothly-1367/) | [GPU 解码实测 (2020)](https://www.pugetsystems.com/labs/articles/premiere-pro-gpu-decoding-for-h-264-and-hevc-media-is-it-faster-1908/) | [硬解支持矩阵 (2024/2025)](https://www.pugetsystems.com/labs/articles/what-h-264-and-h-265-hardware-decoding-is-supported-in-premiere-pro-2120/) | [RAM 容量影响实测](https://www.pugetsystems.com/labs/articles/when-does-ram-capacity-impact-performance/) | [Premiere 硬件建议](https://www.pugetsystems.com/solutions/video-editing-workstations/adobe-premiere-pro/hardware-recommendations/)
- Frame.io：[Complete Guide to Premiere Pro Proxies (2024)](https://blog.frame.io/2024/07/29/updated-guide-premiere-pro-proxies-and-proxy-workflows/) | [Editing Codecs Guide](https://workflow.frame.io/guide/editing-codecs)
- Adobe 社区 FAQ：[I-Frame vs Long GOP](https://community.adobe.com/questions-729/the-difference-between-intraframe-like-prores-and-long-gop-like-h-264-codecs-1406869) | [预览编码 FAQ](https://community.adobe.com/questions-729/faq-how-to-change-the-codec-for-video-preview-files-1393003) | [Smart Rendering FAQ](https://community.adobe.com/questions-729/faq-what-is-smart-rendering-1355162) | [ProRes 默认预览 (23.0)](https://community.adobe.com/questions-734/discuss-use-prores-as-the-default-codec-for-sequence-rendering-313242) | [pek files](https://community.adobe.com/questions-729/pek-files-1425972)
- 其他：[Apple ProRes (Wikipedia)](https://en.wikipedia.org/wiki/Apple_ProRes) | [Digital Rebellion 内存语义](https://www.digitalrebellion.com/blog/posts/premiere_pro_memory_settings_explained) | [Wipster 导出设置](https://www.wipster.io/blog/understanding-premiere-pros-confusing-export-settings) | [Larry Jordan 代理入门](https://larryjordan.com/articles/adobe-premiere-pro-cc-create-an-automatic-proxy-workflow/)
