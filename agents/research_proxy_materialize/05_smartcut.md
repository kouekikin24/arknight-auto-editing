# 05 调研报告：无重编码/智能剪切（Smart Cut）生态
调研日期：2026-09-14。调研人：串行调研子代理。
背景：自研 1080p60 H.264 长 GOP（B 帧 79%）游戏录像剪辑工具；预览=自研 Rust 解码服务段表选择性解码+mpv；计划「物化重剪代理」（CRF14 短 GOP，实测 6.8x 实时、2.26GB/h）。本报告调研「无重编码剪辑」家族，评估：a) 预览可否部分借用无重编码思想；b) 最终导出可否用 smart-cut 大幅提速。

## 1. LosslessCut

### 1.1 keyframe cut vs normal (smart) cut 的区别
- **keyframe cut（也叫 normal cut 模式）**：纯 stream copy（ffmpeg -c copy），完全无损、瞬时完成。但受现代编码器技术限制，只能切在关键帧上；若用户指定的切点不在关键帧，ffmpeg 会取最近的关键帧，与用户意图不符。实际行为：切点起点会**回退到前一个关键帧**（段开头提前），文件关键帧稀疏时甚至可能完全切不动。[来源: GitHub mifi/lossless-cut issue #126、discussion #2281、shortlisted.tools 评测]
- **smart cut（实验性功能）**：不受关键帧约束，可精确在任意帧切。代价是**只把「切点 → 下一个关键帧」之间的小段重新编码**，其余帧原样 stream copy，因此"当然不是完全无损，但几乎无损"（HN 讨论）。[来源: discussion #2281、issue #126、HN 33969873、converter.brightcoding.dev 博客]
- 权衡表：keyframe cut=瞬时/完全无损/精度受关键帧限制；smart cut=较慢（局部重编码）/近无损/帧精确。

（待续：实现细节、拼接、预览）
### 1.2 smart cut 实现细节（官方 issue #126 权威描述）
- 初版实现（维护者 mifi）："重编码从切点到下一个关键帧之间的视频部分，以实现 100% 精确切点，同时无损 copy 其余段"。
- **限制清单**（issue #126 Limitations 节，2026-09 现状）：
  - 只对部分 h264 文件成功；h265 只有少数能用；其他编码不确定。
  - 只支持单视频流（其他视频流必须禁用；缩略图流会丢）。
  - **向前扫描最多 60 秒找关键帧**；段内无关键帧则报错 `Cannot find any keyframe after the desired start cut point`（#1190 讨论中提出改为整段重编码）。
  - 自动探测输入流码率，失败则用全文件 size/duration 估算（可能不准）。
  - 尝试把 timebase 从源复制到目标。
- **已知质量问题**（Remaining issues 节）：
  - seek 不精确 → 拼接点 glitch/跳帧（#1216）。
  - 音频 samples 过大 → 切点精度/杂音；workaround=音视频分开切再合，但可能音画不同步（#1834、#2050）。
  - 字幕不支持（#2049）；旋转元数据偶尔被破坏（#2020）。
- 相关生态：VideoRedo 已停运（作者去世，产品终止）；**Smart Media Cutter**（SMC）为接替者，其作者 skeskinen 开源了 smart cut 算法 CLI（github.com/skeskinen/smartcut，MIT），"minimal recoding around the cutpoints"；注意该开源仓 **2026 年 2 月起停止更新**，新修复只进闭源 SMC。

（待续：ffmpeg 命令组织与拼接）
### 1.3 ffmpeg 命令组织与拼接方式（读源码 src/renderer/src/）
- **smart cut 判定**（smartcut.ts `needsSmartCut`）：以切点为中心先扫 ±10s 关键帧，找不到下一关键帧再扩到 60s；若切点恰为关键帧→退化为纯 keyframe cut；否则 `losslessCutFrom=下一关键帧时间`。
- **重编码段**（useFfmpegOperations.ts `cutEncodeSmartPart`）：
  - 命令骨架：`-ss <cutFrom> -i src -ss 0 -t <dur> -c:v <codec> -b:v <bitrate×1.2> -bf <源B帧数> -f <outFormat>`。`-ss` 放 `-i` 前以加速 seek。
  - 编码器映射：h264→libx264、av1→libsvtav1（mapVideoCodec）。**单遍平均码率模式（-b:v），不是 CRF**；码率=探测源码率×1.2（"补偿误差与质量损失"，issue #126 comment 1602266688）；探测失败则用全文件 size/duration 估算。
  - 显式传 `-bf`（源 has_b_frames）与 timebase，保证拼接时流参数一致。
- **每段两件套**：① 重编码件=切点→(下一关键帧-1帧)（减 1 帧防拼接重复）；② 无损件=从下一关键帧（keyframe cut 模式，`-ss` 在 `-i` 前，`-c copy`，不加 avoid_negative_ts）到段尾。
- **拼接：ffmpeg concat demuxer**（`-f concat -safe 0 -protocol_whitelist file,pipe,fd`）。两个层面都用它：段内（编码件+无损件→单段文件）与段间（多段→最终文件，`concatCutSegments`）。concat 后删除中间文件。
- **keyframe cut 实现**（`losslessCutSingle`）：可选 `-ss` 位置（`-i` 前=快但只能关键帧对齐；`-i` 后=慢但更准？实际 keyframeCut=true 即 ssBeforeInput），`-avoid_negative_ts make_zero` 等，`-c copy`。

### 1.4 预览实现（问题 7 相关）
- **播放器 = Chromium 的 HTML5 video（Electron 内嵌），不是 mpv**。[来源: README "LosslessCut uses the Chromium browser's HTML5 video player"]
- 原生仅支持 MP4/MOV/WebM/MKV/OGG/WAV + H264/AV1/VP8/VP9/H265(硬解)等；**遇到不支持的编码，用 ffmpeg 生成"HTML5 友好版"代理文件再播（html5ify）**：hq 档=libx264 CRF17 原尺寸；lq 档=400p ultrafast CRF28；还有 fastest 档=假文件（静音音频占位，只求时间线可拖）。**"实际剪切/导出仍作用于原文件，因此无损"** —— 与我方「预览播代理、导出真源」架构同构。[来源: README Supported formats 节、useFfmpegOperations.ts html5ify]
- seek 精度问题在 HTML5 video 上同样存在（issue #126 列为 remaining issue：seek 不准导致拼接点 glitch）。

### 1.5 对我方可借鉴点（LosslessCut 小结）
- 它的 smart cut=「边界窗口重编码」，窗口=切点→下一个关键帧；对 GOP=120@60fps 的源最坏 ≈2s。
- 它验证了「代理预览 + 原源导出」是成熟模式（html5ify）。
- 它的痛点（seek 不准、音频对齐、拼接点 glitch）正是我方自研解码服务+段表架构可以绕开的（我们直接按帧号操作，不依赖 HTML5 video 的秒级 seek）。

## 2. Avidemux（copy 模式 + smart rendering）
- **copy 模式约束**（官方文档 Cutting）：视频设为 copy 时，[A,B) 删除后残留帧因丢失参考帧而"broken"（文档记作 xxOOOIxxxx）；两个出路：B 标记恰为 I 帧，或 smart copy。[来源: avidemux.sourceforge.net/doc/en/cutting.xml.html]
- **smart copy 机制**："对失去参考图像的那部分用固定量化器重编码"（fixed quantizer，文档建议 QP 4~5），产出 xxRRRIxxxx（R=重编码帧）。**官方文档明示 smart copy 仅适用于 DivX3 与 (MPEG-4) ASP，且 "It DOES NOT work with B frames"**。[来源: 同上]
- 即对带 B 帧的 H.264，Avidemux 的 smart copy 不适用；现代版 Avidemux 在 copy 模式下对 H.264/HEVC 做切点安全检查（changelog: "Multiple issues in cut point checks for H.264 and HEVC streams in copy mode ... were identified and resolved"），非关键帧切点会拒绝或要求处理。[来源: avidemux.sourceforge.net/news.html]
- 论坛补充（topic 13472 Smart-Copy）：copy 模式目前只能切在 I 帧；非 I 帧切点唯一途径是全量重编码（该帖时代）；topic 16372 给出 smart copy 定义"大部分帧不变，重编码失去参考图像的部分，固定量化器"。[来源: avidemux.org/smif]
- **对我方含义**：Avidemux 路线与 B 帧 79% 的源正面冲突——它是"重编码到参考可恢复为止"的更老/更保守实现，覆盖编码格式极窄。反面教材价值：证明 B 帧越多，smart cut 边界窗口越大（必须重编码到下一个 IDR/关键帧，因为 B 帧既参考前又参考后）。

## 3. 商业 smart-cut 工具
### 3.1 VideoReDo（已停运，但原理最经典）
- **停运确认**：owner/创始人 Dan Rosen 2023 年 9 月意外去世，销售/支持/更新全部停止；主要代码作者 Dan203 亦离开；激活服务器一度下线（后短暂恢复）。[来源: hummy.tv forum "VideoReDo future very uncertain"、GRC forums、digitalFAQ、videohelp.com/software/VideoReDo]
- **smart rendering 原理**（多个社区源一致）："只重编码编辑点所在的 GOP"（Reddit r/videoredo: "only re-encode a GOP group at the edit point"）；未修改的 GOP 原样通过（Magix 论坛对 smart render 的通用定义）；TVSuite V4 起支持 H.264："not recode your source video except for a few frames at cut points"。[来源: reddit r/videoredo 1v9fmm1、forums.sagetv.com t_46405、forum.videohelp.com 375841]
- 注意其边界窗口=**整个被触及的 GOP**（比 LosslessCut 的"到下一个关键帧"略保守/同量级）。
- 后继生态：SMC（Smart Media Cutter）、TMPGEnc Smart Renderer、SolveigMM、VideoHelp 论坛社区自研替代品（thread 421059 "a smart-rendering editor (my replacement for VideoReDo)"）。

### 3.2 SolveigMM Video Splitter
- 官方定位：H.265/H.264/AV1 等码流级深度分析，"Frame accuracy" vs "K-Frame accuracy"（关键帧精度=全无损）两档；smart rendering=只处理切点处片段，无全量重编码。[来源: solveigmm.com 产品页、version history]
- 2026-03 博客宣布 AV1 smart rendering 支持（"editing goes fast and without full re-encoding thanks to SolveigMM smart rendering technology"）。[来源: solveigmm.com/blog/en/av1-smart-rendering-...]
- 对比博客自评："LosslessCut 的 smart cut 是实验性的、不是所有文件都行；SolveigMM 可做到无损剪接+最高帧精度"。[来源: solveigmm.com/blog/en/losslesscut-vs-solveigmm-video-splitter/]
- 边界窗口的官方精确数字：**未找到**（官方未公布重编码窗口宽度；社区通用描述=切点周围的 GOP）。搜 "AccuFrame" 与 SolveigMM 无关（该词属建筑行业产品）。

### 3.3 Smart Media Cutter（SMC / smartcut）
- 背景：作者 skeskinen 在 r/videoredo 发帖称"想造一个 VideoReDo 的现代替代品"（2024）；开源 CLI 库 smartcut（MIT，PyPI: smartcut），核心承诺 "cut video files in many different formats and codecs with only minimal recoding around the cutpoints (a.k.a. smart cut)"——几秒内剪完长视频。[来源: github.com/skeskinen/smartcut README、reddit r/videoredo 1f71j0s]
- **开源仓 2026-02 起停止更新**：算法的新性能/正确性修复只进闭源 SMC GUI（含转录编辑、静音自动剪、导出时间线到 Resolve/Premiere、免费档）。CLI 语法示例：`smart-media-cutter.exe input.mp4 output.mp4 --keep 10,20`。[来源: README UPDATE Feb 2026]
- 开源实现质量：测试检查"输出像素值≈不变"（视频）与相关性/绝对差（音频），覆盖多种格式编码。[来源: README Testing 节]
- 算法细节（窗口宽度、B 帧处理）待查其源码/文档（见 3.4）。

### 3.4 SMC/smartcut 开源算法细节（读源码 smart_cut.py / video_cutter.py / media_utils.py）
这是目前唯一可读源码的成熟 smart-cut 实现，参考价值最高：
- **重编码单位 = 整个被部分覆盖的 GOP**（与 VideoReDo 口径一致，比 LosslessCut 的「切点→下一关键帧」更保守）：
  - `make_cut_segments` 按 `gop_start_times` 把保留时间线切成 CutSegment；完全落在保留区间内的 GOP → `require_recode=False`（纯 remux）；部分重叠的 GOP → `require_recode=True`。keyframe 模式则全部 False。
- **recode_segment 实现**：从 GOP 起点（`gop_start_dts`）开始解码整段 GOP（保证参考帧完整），丢弃 `start_time` 前的帧，把 [start,end) 的帧重编码；`frame.pict_type=NONE` 不强制帧型。
- **编码参数**：CRF 档位映射 LOW=23 / NORMAL=18 / HIGH=14 / INDISTINGUISHABLE=8 / NEAR_LOSSLESS=3 / LOSSLESS=0（hevc/av1/vp9 时 +4）。**CRF 而非码率**（与 LosslessCut 相反）。
  - h264：libx264 + `x264-params sps-id=3`——**特意用不冲突的 SPS id，使重编码帧可与原流 SPS 共存拼接**。
  - **B 帧数不手动设置**（注释：测试里设了反而更糟），由编码器自定——即重编码 GOP 的 B 帧结构与原流不同，但码流仍合法。
- **HEVC/AV1 的 hybrid 精细路径**：CRA GOP 带 RASL（跨 GOP 前向参考）且前文有跳切时，只重编码 leading pictures（RASL/RADL），CRA+trailing 原样 remux——比整 GOP 重编码更省。
- **时间戳**：全段重映射到输出时间线（`segment_start_in_output` 累加），修补 DTS 单调性/PTS≥DTS/B 帧延迟；encoder flush 处理。
- **拼接**：PyAV 单输出文件，编码包与 copy 包按时间顺序直接 mux 进同一文件，**不需要 concat demuxer**（比 LosslessCut 的分文件+concat 更干净，无二次封装风险）。
- 音频默认 passthrough（`PassthruAudioCutter`），字幕流直通。
[来源: github.com/skeskinen/smartcut v1.7 源码]

### 3.5 Machete（Machete Lite/Machete）
（调研中，待补）
- Machete（MacheteSoft）：轻量商业工具，定位"lossless editing without re-encoding"=**只在关键帧粒度剪切/拼合**（AVI/FLV/WMV/MKV/WebM/MP4/3GP；Lite 免费版仅 AVI/WMV）。**没有 smart cut（无边界重编码）能力**，属"keyframe cut 家族"而非"smart cut 家族"。对 H.264 内容剪切自动吸附关键帧。[来源: machetesoft.com/about-machete.html、lossless-edit-video-files-avi-wmv-flv.html、videohelp.com 条目]

## 4. EDIUS 的 smart rendering（导出侧家族）
- **机制**（社区与评测一致口径）：导出时若输出编码格式与源素材/时间线格式一致，**未被编辑/未加效果的时间线区段原样通过（不重编码），只有过渡、效果、剪切点处重编码**。Blackmagic 论坛用户（对比 Resolve 时）称 "Edius is smart even for long GOP codecs and will re-encode only just [the edited parts]"——即 EDIUS 对长 GOP 编码（XAVC Long GOP、AVC-Ultra、H.264/HEVC）也支持 smart rendering，这在 NLE 中罕见。[来源: forum.blackmagicdesign.com/viewtopic.php?f=21&t=157600]
- **格式范围**：官方支持列表含 DV/HDV/MPEG-2、Sony XAVC (Intra/Long GOP)/XAVC S、Panasonic AVC-Ultra/AVC-Intra/P2、Canon XF-AVC/XF-HEVC；H.264/AVC、H.265/HEVC 导出（软件或 Intel QSV/NVIDIA 硬编，10-bit 4:2:2）。[来源: edius.net/new_in_edius.html、edius.net/features.html、EDIUS11FormatList PDF]
- **局限**：EDIUSWorld 官方手册未索引到 "smart rendering" 字样（该能力藏在 exporter 对话框与渲染设置中，官方文档口径不透明）；smart rendering 生效条件苛刻=导出编码器/参数必须与源码流匹配，加了滤镜/变速/尺寸变换的片段必然重编。具体到「H.264 通用网络视频（非广播 cam 格式）」的 smart rendering 支持度，社区评价分歧，**未见官方明确声明**（查不到）。
- 对照：TMPGEnc Smart Renderer（PEGASYS）是导出侧 smart rendering 的专职产品，按"未变化帧帧级比较"决定重编码范围，支持 H.264/HEVC 输出（佐证导出侧家族的可行性与商业化成熟度）。[来源: tmpgenc.pegasys-inc.com 版本历史页提及 Smart Rendering]

## 5. 关键评估：对 1080p60 长 GOP（GOP 120，B 帧 79%）源的 smart-cut
### 5.1 边界重编码窗口有多大
- **两派口径，同量级**：
  - LosslessCut：切点 → 下一个关键帧（向前扫描上限 60s，找不到即失败）。[来源: smartcut.ts needsSmartCut]
  - SMC/smartcut 与 VideoReDo：**整个被保留区间部分覆盖的 GOP**（从 GOP 头解码保证参考完整，重编码其与保留区的交集）。[来源: skeskinen/smartcut smart_cut.py make_cut_segments、reddit r/videoredo "only re-encode a GOP group at the edit point"]
- **代入 GOP=120@60fps**：关键帧间隔若=GOP=120 帧，则每处非关键帧切点的重编码窗口 ≤ 120 帧 = **2.0 秒**。LosslessCut issue #126 有用户实测命令 `-ss 2255.48540 -t 2.01427`——正是 ~2s 窗口的实例。[来源: issue #126 评论 JokerQyou 2022-05-21]
- **B 帧 79% 的额外影响**：
  - 窗口长度不变（窗口由关键帧间隔决定，与 B 帧占比无关）。
  - 但 B 帧双向参考使「任意点 stream copy」更不可行：若流为 open GOP，即使切点恰在 I 帧，其后 leading B 帧仍参考前 GOP → 该小撮帧也必须丢弃/重编（SMC 为 HEVC 的 RASL 专门做了 hybrid 路径=只重编 leading pictures；H.264 open GOP 同理）。[来源: video_cutter.py hybrid_recode_cra_segment]
  - 重编码窗口内 B 帧比例高 → 编码器需重建更多双向预测帧，该窗口编码耗时/码率开销略增（定性，未见量化数据——未找到）。
- 重编码占比估算（每段一个起点边界）：设保留段平均长 L 秒，则重编码时长占比 ≈ 2/L。L=30s → ~6.7%；L=10s → ~20%。

### 5.2 视觉接缝是否可见
- **接缝的风险构成**（mifi 自述难点）："重编码部分必须与原流参数完全一致：profile、分辨率、色彩空间、FPS、位深、码率……因 codec 而异"；重编码段是孤立编码（无全序列码控状态），与原流的量化残留不同 → 边界两侧纹理质感可能有差异。[来源: issue #126 评论 mifi 2021-05-03]
- **业界主流缓解**：LosslessCut 用源码率 ×1.2（mifi："slightly increase the smart cut encoded bitrate to counteract the loss of quality when re-encoding"，#126 评论 1602266688）；SMC 用 CRF 18(HIGH=14)/sps-id=3/不强制 B 帧结构；VideoHelp 共识："有损再编码必有代际损失，但足够高码率下不可感知"。[来源: 前述+forum.videohelp.com/threads/396071]
- **评价并列**：HN 有"naive 重编码=double lossy encoding"的批评（news.ycombinator.com 40829494）；但 VideoReDo 以此原理在广播电视去广告场景存活约 20 年直至作者去世，是 smart rendering 实用性的最强行业佐证；SMC 开源测试验证"输出像素值≈不变"（指未触及部分；重编码部分以相关性校验）。[来源: hummy.tv、smartcut README Testing]
- **对游戏录像的推断**（无直接测量，标注意见）：1080p60 游戏内容含大量锐利 UI 边缘与重复纹理，属重编码最敏感内容之一；但 CRF≤14（或码率×1.2）的 2s 边界窗口大概率不可辨；且**切点本身即用户跳切处，两侧内容本来不连续**，接缝感知被跳切天然掩盖。此推断需以我方素材 A/B 实测确认。

### 5.3 业界评价小结
- LosslessCut smart cut：官方标注 experimental，格式覆盖窄（"部分 h264 成功，h265 少数"），仍有 seek 精度/音频对齐遗留问题。[来源: issue #126]
- SMC/smartcut：目前工程完成度最好的开源实现（GOP 级算法+hybrid CRA+像素级测试），但开源版 2026-02 起冻结。[来源: README]
- VideoReDo：原理最成熟、行业金标准，已停运。[来源: issue #126 引 videohelp 讣告]
- SolveigMM/TMPGEnc/EDIUS：商业实现稳定，格式覆盖以广播/摄像机格式为强项。

## 6. 对我方的两个结论性评估
### 6a. 预览侧：无重编码思想能否借用？——**确认：核心路径不可行，仅有边角收益**
- **确认理由**：我方预览物料=「物化重剪代理」必须承载**烤节奏（变速）**。变速=时间重映射=每帧播放时长改变，任何 stream copy（-c copy）都保持原帧率/原时长，数学上不可能表达变速 → 只要任一保留段变速，该段必重编码。这与全行业一致：smart rendering 家族（EDIUS/TMPGEnc/VideoReDo）的通则是「未触及帧才 copy，触及帧必重编码」，而变速触及每帧。[来源: 本报告 §3/§4 机制综述，及 ffmpeg 语义]
- **可借用的边角**（按性价比排序）：
  1. **边界条件改善代理物化成本的机会极小**：即使某段不变速且两端恰对齐关键帧可整段 stream copy，该段也需重新 mux（关键帧边界裁剪），省下的是该段编码时间——但我方实测代理物化已 6.8× 实时（2.26GB/h），编码不是瓶颈，且混入原画质段会造成预观感不一致（代理 CRF14 vs 原画质 copy）。不值得加双路径。
  2. LosslessCut 的「假文件占位」（html5ify dummy=只生成静音音轨的时长占位文件）思想：对我方「未物化段先出占位、后台按导出节奏物化」的调度有借鉴意义，但 mpv 直连解码服务的架构下收益有限。
  3. 我方自研解码服务+mpv 的帧精确 seek，优于 HTML5 video 系工具（见 §7），LosslessCut 的 seek 痛点我方架构天然规避——**预览侧无需借无重编码思想**。
- 结论：**确认**——段内变速（烤节奏）使无重编码在预览主路径不可行；stream copy 仅对「不变速+关键帧对齐」的边角段适用，工程上不值得。

### 6b. 导出侧：smart-cut 能否把导出从全量重编码降到近零代价？
- **前提判定**：导出=按段表拼接保留段。两个子问题：
  1. **不变速导出**：可以。整段保留且边界对齐关键帧→纯 copy；未对齐→每边界 ≤2s 重编码窗口（§5.1）。SMC 证明该路线成熟："cut even long video files in seconds"。但注意我方源 GOP=120 长 GOP：**导出物的 GOP 结构=源的长 GOP**，拼接物的 seek 粒度/平台兼容性=源的特性（这是特性不是缺陷，但与「短 GOP 代理更利于二次剪辑」形成对照）。[来源: smartcut README]
  2. **有滤镜（字幕/水印/变速）导出**：**必然重编码**——滤镜覆盖到的每一帧都是「触及帧」。若水印/字幕烧在全片→退化为全量重编码，smart cut 无收益。行业通例一致（EDIUS smart rendering 对加特效片段也全重编）。[来源: §4]
- **可达的折中形态**（若产品走「两种导出」）：
  - 「快速草稿导出」：不变速+无滤镜时走 smart cut（近零代价，秒级）；用户接受边界 ≤2s 的重编码微瑕（且跳切掩盖，§5.2）。
  - 「成片导出」：烧字幕/水印/变速→全量重编码（可只在保留区间内编码，本身已是大幅节省：跳过的删减区间不用编码）。
  - **即使全量重编码也有个被低估的增益**：导出重编码只需覆盖保留时间线（删减区间直接跳过解码甚至跳过编码），而非整源时长——这与段表驱动架构天然契合。
- **风险清单**（若做 smart cut 导出）：
  - 单视频流假设成立（我方单轨）；音频 passthrough（SMC 默认）或同参局部重编（LosslessCut 音频是遗留痛点，#1834/#2050）；字幕轨我方若有需另行处理（LosslessCut #2049）。
  - 编码参数匹配：h264 重编码段须与源 profile/level/色彩空间一致（LosslessCut 只复制 bf+timebase，SMC 复制 profile/pix_fmt/SAR+sps-id=3——SMC 方案更完备，值得抄）。[来源: §1.3/§3.4]
  - 码控策略选择：LosslessCut 码率×1.2 vs SMC CRF18/14——对质量一致性 CRF 更稳；对大小确定性码率更稳（未找到两者对比实测）。
- **结论**：a) 不变速+无滤镜的导出，smart cut 可把代价从「全量重编码」降到「每切点 ≤2s 重编码」≈近零代价，业界有成熟先例（VideoReDo/SMC），技术可行；b) 烧字幕/水印/变速的成片导出必然重编码，smart cut 无处生效——但「只编码保留区间」仍有接近比例的节省。产品若两种导出并存，smart cut 版本的价值=快速草稿/对画质零损失的存档剪辑。

## 7. 各工具 seek/预览体验对照（问题 7）
| 工具 | 播放/预览技术 | seek 精度 | 备注 |
|---|---|---|---|
| LosslessCut | Electron + **Chromium HTML5 video**（非 mpv） | 非帧精确（HTML5 限制）；编码不支持时先生成代理（hq=CRF17/lq=400p/dummy=假文件） | seek 不准列为 smart cut 遗留问题根因之一（#126、#1216） |
| Avidemux | 自研解码器 + Qt | 帧级导航；copy 模式下切点自动吸附/校验关键帧 | 目标帧类型显式显示（A/B 标记+关键帧跳转键） |
| VideoReDo | 自研解码+渲染 | 帧精确（其卖点 frame accuracy） | 广播 TV 去广告工作流，已停运 |
| SolveigMM | 自研码流分析引擎 | Frame accuracy / K-Frame accuracy 两档可切 | 引擎亦授权为 SDK（Video Editing SDK） |
| SMC | GUI 播放器（未公开细节，未找到） | 转录式编辑（按文本剪）为主打 | 开源 CLI 无 UI |
| 我方 | 自研 Rust 解码服务（段表选择性解码）+ mpv | 帧精确（架构自带） | 优于 HTML5 video 系；与 VideoReDo/SolveigMM 同级 |

## 附：来源清单
- LosslessCut：github.com/mifi/lossless-cut issue #126（正文+评论）、issue #1087、discussion #2281、README（Supported formats）、src/renderer/src/smartcut.ts、src/renderer/src/hooks/useFfmpegOperations.ts（master@2026-09）
- SMC：github.com/skeskinen/smartcut README + smart_cut.py/video_cutter.py/media_utils.py/track_cutters.py（v1.7）、reddit.com/r/videoredo/comments/1f71j0s、reddit.com/r/Python/comments/1f767df
- VideoReDo：videohelp.com/software/VideoReDo、forum.videohelp.com/threads/407142、hummy.tv/forum/threads/videoredo-future-very-uncertain.10762、forums.grc.com/threads/videoredo-video-editing-software.1038、digitalfaq.com/forum/video-capture/12978、reddit r/videoredo 1v9fmm1、forums.sagetv.com showthread_t_46405
- SolveigMM：solveigmm.com（产品页、blog av1-smart-rendering…、blog losslesscut-vs-…、version history）
- Avidemux：avidemux.sourceforge.net/doc/en/cutting.xml.html、avidemux.sourceforge.net/news.html、avidemux.org/smif topic 13472/16372
- Machete：machetesoft.com（about、lossless-edit 页）
- EDIUS：edius.net/new_in_edius.html、edius.net/features.html、ediusworld.com 手册 chapter15、forum.blackmagicdesign.com/viewtopic.php?f=21&t=157600、EDIUS11FormatList PDF
- TMPGEnc：tmpgenc.pegasys-inc.com/en/download/tme3_rireki.html
- 综合讨论：news.ycombinator.com 33969873 / 40829494、forum.videohelp.com 375841 / 396071 / 345394、forum.blackmagicdesign.com 84651、shortlisted.tools/products/losslesscut、converter.brightcoding.dev 博客、superuser.com 612789
- 未找到：SolveigMM 官方边界窗口宽度的精确数字；LosslessCut/SMC 码控策略（码率×1.2 vs CRF）的对比实测；SMC GUI 播放器技术栈；EDIUS 对非广播 H.264 源 smart rendering 的官方明确声明。
