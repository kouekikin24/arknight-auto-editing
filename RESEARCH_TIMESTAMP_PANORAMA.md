# 调研报告 — 时间戳问题四维全景（2026-08-26，本会话完成）

对应 HANDOFF_PREVIEW_ACCURACY.md §5.9 的四维深度调研任务。因当日子代理模型限额，
改由主会话直接调研完成。全部结论附证据出处；**本机网络可达性有限**（见 §0.2），
不可达的源已如实标注，不做无来源断言。

---

## 0. 调研方法与网络约束

### 0.1 方法
直连可达源取证：GitHub API（仓库/issue/源码）、ffmpeg.org 官方文档、mkvtoolnix.download
官方手册、obsproject.com 系、developer.apple.com JSON API、learn.microsoft.com、
mediaarea.net issue 库、forum.doom9.org 搜索。未做任何本地实测（owner 指示：实测后置）。

### 0.2 本机网络可达性（影响结论完整性的硬约束）
| 可达 | 不可达 |
|---|---|
| github.com / api.github.com、ffmpeg.org、mkvtoolnix.download、obsproject.com、developer.apple.com、learn.microsoft.com、mediaarea.net、forum.doom9.org、smpte.org（首页/标准库导航）、vegascreativesoftware.com / grassvalley.com / tech.ebu.ch（仅 JS 壳，无正文） | helpx.adobe.com 内容页（根域可达、内容页挂起）、support.google.com / YouTube、web.archive.org、wikipedia、blackmagicdesign.com、developer.android.com / issuetracker.google.com、bbc.co.uk、amwa.tv |

因此：Adobe/Resolve/Vegas/Edius 官方 VFR 文档、Netflix Partner Portal、Android
录屏 issue 库本轮**无法直接取证**，相关结论只写到"页面存在/被引用"级别。

---

## 1. 维度 1：帧服务器 / 帧索引库

### 1.1 BestSource（本轮最大发现）★
- 出处：github.com/vapoursynth/bestsource（**VapourSynth 官方组织**仓库，MIT，
  146 stars，最后推送 2026-08-25——极度活跃）。
- 定位："wrapper library around FFmpeg that **ensures always sample and frame
  accurate access**"。C++ 库 + VapourSynth/Avisynth+ 插件；支持 FFmpeg 8.0/8.1.x。
- **索引 = 一次全量解码**：索引时逐帧记录 pts、关键帧标志、格式/尺寸与
  **XXH3 内容哈希**（src/videosource.cpp:1107）。索引文件可缓存（cachemode 0-4）。
- **寻址算法**（源码注释 src/videosource.cpp:1183-1191）：
  1. 已有解码器离目标近 → 直接顺解；
  2. 否则 seek 到 ≤ N-preroll 的最近关键帧（按 PTS），seek 后**逐帧解码并比对
     哈希**；**重复哈希（连续相同画面）时匹配最多 10 帧的哈希串**；
  3. 落点无法由哈希唯一确定 → 标记该关键帧为坏点，回退 ≥100 帧重试。
- **无有效时间戳时的兜底**：全部帧无有效 PTS 时合成 `PTS = i*2`（videosource.cpp
  ~1150）——即纯帧号计数，与本仓 PyAV 方案同源。
- 已知局限（README）：mpeg/ts/vob seek 性能差（FFmpeg demuxer 限制）；
  lossy 音频 seek 差；VC-1 seek 后非 bitexact 不可 seek；VFR H264 in AVI seek 慢；
  issue #87 自陈"best_effort_timestamp 的猜测基本是坏的"（故不用它）。
- 周边能力：`timecodes` 参数导出 Matroska v2 时间码文件；`exporttimestamps`
  返回全部帧时间戳数组；`fpsnum/fpsden` 内置 VFR→CFR 转换（早期有 AVI 相关
  bug，#91/#127 已修）。
- **对本仓的适用性判定**：概念上完全覆盖本案（内容哈希定帧对 pts 碰撞免疫，
  比我们的"亮度语境判别"更强的消歧）。但它是 C++/VapourSynth 生态零件，
  引入 = 装 VapourSynth 宿主 + 插件构建，且首次索引同样是全量解码（≈我们的
  75~85s 建表）。**当前自研方案已等价达成其核心机制，替换收益低、集成成本高**。
  列为"未来若重构取帧底层的评估对象"，与 TorchCodec 并列。

### 1.2 FFMS2（FFmpegSource）
- 出处：github.com/FFMS/ffms2；API 文档 doc/ffms2-api.md。
- 机制：demux 级索引（关键帧位置 + timecodes），`FFMS_GetFrame(帧号)` /
  `FFMS_GetFrameByTime(最近时间戳)`；SeekMode = LINEAR / NORMAL（按 lavf 关键帧）
  / UNSAFE / AGGRESSIVE。
- **对本案的关键证据（负面）**：issue #77 "Timecodes are out of order or too
  close together"——带重复/相同时间戳的文件（HorribleSubs flv→mkv dump 双零
  时间码）**索引阶段直接报错拒绝打开**，当时的解法是"remux 修正时间码"。
  → 对 2.mp4（14644 处重复 pts）FFMS2 大概率拒载。
- 时间戳不连续处理：PR #295（2017，已合并，changelog "Discontinuous Timestamp
  Support"）——按不连续点切段、段内按 PTS 重排，自述"probably breaks on insane
  VFR content"，且明说彻底解法是全解码但"对很多用例不可接受"。
- 结论：老牌（压制/字幕组社区事实标准之一，Doom9 官方支持帖 2996 回复仍在活跃），
  但**对重复时间戳是硬拒绝**，不适用于本案源文件。

### 1.3 L-SMASH Works
- 出处：github.com/HomeOfAviSynthPlusEvolution/L-SMASH-Works（活跃 fork，
  2026-08-23 推送；原作 VFR-maniac，作者名即"VFR 狂人"）。
- `LWLibavVideoSource`（libavformat 路线）：文档原话 "**Parsing all frames is very
  important for frame accurate seek**"——.lwi 索引 = 全帧解析缓存；
  seek_threshold 机制 = 目标与当前位置差 ≤T 帧则顺解，否则退最近 RAP（随机访问点）
  再顺解——**与 MLT/我们的计数式同构**。另有 `rap_verification` 选项：索引期
  实际解码验证 RAP 有效性。
- `LibavSMASHSource`（L-SMASH 原生 MP4 解析）：直接读 ISO BMFF box（不依赖
  lavf），索引快，但**完全信任容器时间戳**——对 2.mp4 这类坏时间戳容器会原样
  继承碰撞歧义。
- 结论：机制与我们同构（关键帧+顺解计数），但无内容级消歧（哈希/亮度），
  碰撞位同样歧义；且是 AviSynth/VapourSynth 插件，集成成本同 1.1。

### 1.4 小结（维度 1）
| 方案 | 定帧机制 | 对重复 pts | 对本仓 |
|---|---|---|---|
| BestSource | 全解码索引+帧哈希校验+坏点回退 | **免疫**（哈希串匹配） | 概念覆盖本案，集成成本高，列为未来评估对象 |
| FFMS2 | demux 索引+关键帧 seek | **拒载**（#77） | 不适用 |
| L-SMASH Works | 全帧解析索引+RAP+顺解 | 继承容器歧义 | 机制同构无增益 |
| 本仓现状（PyAV 计数+亮度消歧 / CLI 两段式） | 关键帧索引+纯计数+语境消歧 | 免疫（已验证 27/27） | **已在产** |

**社区生态旁证**：Doom9（压制/帧精确工作流重度社区）搜索 "variable frame rate"
493 条、"duplicate timestamps" 495 条——问题广为人知，但**没有任何帖子指向
"修好重复 pts 的成熟工具"，主流做法仍是转 CFR 或换索引方式**，与 §5.8 既有结论一致。

---

## 2. 维度 2：容器级时间戳修复全景

### 2.1 GPAC / MP4Box — patch_dts（源码级结论：只补 DTS，不碰 pts 碰撞）
- 出处：gpac/gpac src/filters/mux_isom.c（MP4 mux 滤镜）：
  - L160 `u64 dts_patch;`、L326 `Bool ... patch_dts;`
  - L4959-4972：样本 DTS < 前一样本 DTS 时告警 "[MP4Mux] ... Sample %d with DTS
    less than previous sample DTS, patching DTS"，累加 `dts_patch` 平移并 +1
    避免零时长样本，可选同步调整前一样本时长（`gf_isom_patch_last_sample_duration`）。
  - L8873：mux 参数定义 `patch_dts`——"patch previous samples duration when dts
    do not increase monotonically"，**默认 false，expert 级**。
  - L5209 附近的 `clamp_ts_plus_one` 只作用于 SKIP_PRES（跳过呈现）包，
    **不是通用 pts 去重**。
- **判定**：该机制只处理存储序 DTS 回退；本案的 14644 处重复 pts 是
  "PTS=DTS+ctts 偏移"层面的碰撞，DTS 本身可以单调而 pts 仍重复——
  **GPAC 重封装后碰撞依旧**。§5.9 的悬置问题（"patchdts 对呈现序 pts 重复是否
  有效"）至此关闭：**无效，源码为证**。
- 附注：MP4Box 主程序源码未直接暴露该开关，需经 GPAC 滤镜参数机制调用
  （具体命令行语法未在本轮验证，本地实测时再查）。
- Windows 二进制：gpac.io 提供官方安装包（未实际下载验证）。

### 2.2 bento4 — 无时间戳重写能力
- 出处：axiomatic-systems/Bento4 README：工具集 = mp4info / mp4dump / mp4edit /
  mp4extract / mp4mux 等；mp4edit 的能力是 "add/insert/remove/replace atom/box
  items"——**box 级编辑，无逐样本时间戳重写**。结论：不覆盖本案。

### 2.3 mkvmerge 外部时间戳注入 ★（半无损修复的第一候选）
- 出处：mkvtoolnix.download/doc/mkvmerge.html（官方手册，本机已取全文）：
  - `--timestamps TID:file`："timestamps **forcefully override** the timestamps
    that mkvmerge normally calculates"；
  - `--default-duration TID:x`（如 `0:60fps`）："**modifies the track's
    timestamps to match the default duration**"——强制 CFR 时间轴；
  - `--fix-bitstream-timing-information`：把容器时间轴同步回 H.264 码流 VUI
    （仅 AVC 实现）；
  - 外部时间戳文件格式 v1（分段 fps）/ **v2（逐帧毫秒，必须有序）** /
    v3（逐帧时长）/ v4（=v2 无序）。
- **为什么这条路与 MP4 路线本质不同**：Matroska 块只携带呈现时间戳，
  **没有 MP4 那种独立的 DTS + ctts 双结构**——呈现序时间轴就是容器时间轴本身。
  把认证表（已验证为理想化呈现序）写成 v2 文件，`-c copy` 重封装，
  B 帧解码由码流自身保证，理论上可无损消除全部碰撞。
- **判定**：机制成立、工具成熟（MKVToolNix v100，作者 Mosu 在 Doom9 持续活跃）、
  半无损（输出为 MKV，管线需接受 mkv 容器）。**未实测（按 owner 指示后置）**。

### 2.4 setts BSF — 变量全集已核，表达力不足（理论关闭）
- 出处：ffmpeg.org/ffmpeg-bitstream-filters.html §2.31（本机已取全文）；
  git 史：2021-02 加入，2026-01 仍活跃（新增 `prescale`、TB 变更时重缩放）。
- 可用变量：N（输入包计数）、TS/PTS/DTS/DURATION、POS、STARTDTS/STARTPTS、
  PREV_INDTS/INPTS、PREV_OUTDTS/OUTPTS、NEXT_DTS/NEXT_PTS/NEXT_DURATION、
  TB/TB_OUT/SR/NOPTS。文档自带警告："to set PTS equal to DTS (**not
  recommended if B-frames are involved**)"。
- **判定**：变量都是"解码序域"的量；要在写出时生成无碰撞的**呈现序**时间轴，
  需要每帧的呈现排名，而该信息无法由这些变量表达（N 是解码序计数，
  `pts=N*256+PTS-DTS` 这类保偏移公式必然继承原碰撞——本地 §5.8 实测已证）。
  setts 路线**理论关闭**，与实测一致。
- 澄清：setts 是 **BSF**（`-bsf:v setts`），master 版视频滤镜文档中无同名滤镜。

### 2.5 平台摄入规范（可达性受限）
- **Netflix / 流媒体母版**：交付格式 = IMF（SMPTE ST 2067 系列）。
  SMPTE 官方公开仓库 github.com/SMPTE/st2067-21（IMF Application #2E，
  最新正式版 DOI: 10.5594/SMPTE.ST2067-21.2023）——IMF Composition 以固定
  EditRate（有理数）描述轨道，**结构上不存在 VFR 母版**。规范正文在 DOI 门后，
  此结论为结构事实+可查仓库，非规范原文引用。
- **YouTube**：推荐上传设置页 support.google.com/youtube/answer/1722171
  本机不可达（Google 域被阻），本轮无法引用原文。
- **GitHub 上的"平台交付规范镜像"仓库**（GabbyYoboho/Platform-delivery-specs，
  描述含 Netflix/Prime/TV/YouTube）**是空仓库**，无内容。

---

## 3. 维度 3：NLE 与广播专业流程（本轮网络受限最重的维度）

### 3.1 可直接确认的
- **SMPTE 标准库**（smpte.org/standards，本机已取）：Time Code 标准在册；
  IMF = ST 2067 系列（见 2.5）。标准全文在 IEEE/SMPTE 付费门后。
- **Doom9 社区生态快照**：FFMS2 官方帖 2,996 回复（最后回复 2026-08-18）、
  MKVToolNix v100 发布帖（Mosu 活跃）、VirtualDub2 置顶帖——帧精确工作流的
  社区中枢仍是 AviSynth/VapourSynth + 帧服务器生态（呼应维度 1）。
- **EDL/AAF 结构性事实**（背景知识，本轮无源可引）：CMX EDL 以固定帧率 +
  timecode 表达，结构上假设 CFR；广播交付链条（MXF 等）以 EditRate 为核心。
  标注为"未在本轮取得一手来源"。

### 3.2 存在但本轮不可达（留待网络恢复后补证）
- Adobe Premiere VFR 官方页：helpx.adobe.com/premiere-pro/using/variable-frame-rate.html
  （前一会话已确认存在；本轮内容页挂起）。
- DaVinci Resolve 手册（blackmagicdesign.com 不可达）。
- Vegas（vegascreativesoftware.com 可达但页面为 JS 壳）、Edius（grassvalley.com 同）。
- BBC/EBU 制作规范（bbc.co.uk 不可达；tech.ebu.ch 页面为 JS 壳）。
- Netflix Partner Portal（需账号）。

### 3.3 间接但硬核的 NLE 兼容性证据（来自 OBS issue，见 4.1）
OBS #13396 的报案内容直接记录了 NLE 对 VFR 时间戳的实际失败模式：
**FCPX 按时间戳测出 ~30.30fps 拒绝匹配 30.000fps 预设、relink 失败；
Premiere 高速播放卡顿（OBS #12224）**——这是厂商行为的一手旁证
（虽出自 OBS 帖而非厂商文档）。

---

## 4. 维度 4：录制端预防 + 大规模管线

### 4.1 OBS — 本案成因机制的最佳公开证据 ★
- **OBS issue #13396**（obsproject/obs-studio，已关闭=重复帖，指向 #7496 长期
  已知问题）：
  - 现象：NVENC/AMF/QSV 录制 + 自动 remux MP4 后，MediaInfo 报
    "Frame rate mode: Variable"（标称 30，实测 29.412~30.303）——
    **即便设置了固定帧率 + CBR**（"CBR 管的是码率不是帧间隔"）。
  - 根因代码分析（报案者逐文件追踪）：`obs-ffmpeg-mux.c` 原样转发编码器
    时间戳；`ffmpeg-mux.c` 的 `rescale_ts()` 只做 timebase 换算、保留原始时刻；
    NVENC 封装仅有 B 帧 dts_offset 补偿——**OBS 管线没有任何 CFR 规范化步骤**。
  - **软件编码器（x264/x265）不受影响——输出真 CFR**。
  - 提案修复 = remux 时 `-fps_mode cfr`；给出的 workaround 就是
    `ffmpeg -i in.mp4 -c:v libx264 -fps_mode cfr out_cfr.mp4`
    ——**与本仓 §5.8 实测有效的 CFR conform 完全一致**（独立印证）。
- **OBS PR #12431**（open）：异步视频时间戳语义修正，自述现行实现会制造
  "序列开头的假重复帧"（false duplicate）——采集/缓冲层确实存在制造重复
  时间戳的已知缺陷模式。
- **对本案的意义**：2.mp4 的录制工具虽仍不明（用户已纠正不要臆断），但
  "硬件编码/采集管线在画面冻结或调度抖动时产出重复/漂移时间戳"是**被公开
  证实的普遍机制类别**，不是孤例。预防建议（面向未来录制）：能选的话用
  软件编码 x264 + 固定帧率；摄入前用解封装级统计（见 4.4）验收。

### 4.2 Windows 录屏（Game Bar 的底层 = Windows.Graphics.Capture）
- 出处：learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture
  （本机已取）：帧经 Direct3D11CaptureFramePool 逐个取出，每帧带
  `SystemRelativeTime`（QPC 时钟）——**帧到达间隔取决于显示输出，文档无任何
  CFR 承诺**。时间戳质量完全取决于录制方如何把 QPC 写进容器。

### 4.3 iOS ReplayKit
- 出处：developer.apple.com/documentation/replaykit（JSON API 已取）：
  官方描述仅到"录制屏幕视频与应用/麦克风音频"，**文档层没有帧率/时间戳保证
  条款**。时间戳行为无公开承诺。

### 4.4 大规模管线与 QC
- **数据集管线**：video2dataset 原仓库已消失，现存为若干 fork
  （lafauxbolex/video2dataset 等，2025 活跃）；NVIDIA DALI（14.5KB README 已取）
  定位是深度学习数据加载/预处理库，视频路径按**帧序列/解码序**消费。
  与 §5.8 既有结论一致：工业管线默认无视容器时间戳，顺序解码+采样。
- **MediaInfo 作为 QC 手段的边界**（mediaarea.net / MediaArea/MediaInfo issues）：
  - 能报 `FrameRate_Mode: Constant/Variable`（OBS #13396 即用它验收）；
  - **但 VFR 判定是启发式的且有已知误报**：#576（实际恒定却报 Variable，open）、
    #293（帧率模式报错误，open）；
  - **没有重复 pts 检测能力**（"duplicate frame/timestamp" 检索 0 命中）。
  - 结论：MediaInfo 只能当**筛查级** QC；重复时间戳的验收必须走解封装级
    统计（本仓已有：PyAV demux 全量统计脚本，即 §5.7 数据来源）。
- **MAM/DAM ingest 规范化步骤**：相关厂商站不可达，本轮无结论。

---

## 5. 全景总表

| # | 方案 | 机制 | 成熟度/证据 | 对本仓判定 | 风险 |
|---|---|---|---|---|---|
| 1 | 索引定帧绕过（现状：认证表+PyAV计数+亮度消歧/CLI两段式） | 关键帧索引+纯计数+语境消歧 | 本项目实测 27/27、12/12、34 点扫描 | **在产，零损** | 无（表失效判据见交接 §5.5） |
| 2 | CFR 重编码 conform（-fps_mode cfr） | 时间轴重建+重采样 | 本仓实测 6600 帧精确；OBS #13396 workaround 独立印证；Shotcut 官方背书 | **可用，有损一代** | 重编码损失 + 20~40min |
| 3 | **mkvmerge --timestamps/--default-duration** | 流复制+外部呈现序时间轴注入（MKV 无双时间结构） | 官方手册明文"forcefully override"；工具活跃（v100） | **待实测第一候选（半无损）** | 输出 MKV；B 帧+新时间轴的组合需验证 |
| 3b | **自研 MP4 双表重写**（stts+ctts 同步重排，无现成轮子） | 保持码流不动，直接改写样本表：stts 全部等距 → DTS 严格单调；ctts(v1 带符号) 写入"呈现序排名-解码序"偏移 → pts 无碰撞且 B 帧重排保留 | 本仓已持有权威呈现序（认证表）；ctts v1 负偏移是标准特性；盒子读写有现成积木（bento4/GPAC API、python mp4 解析库） | **调研收尾时新识别的拼装路线**：非成熟项目，但工程路径清晰；setts 失败正因表达式引擎拿不到"全局呈现排名"这一信息，而自研脚本拿得到 | 需开发（中等）；elst/音轨同步/规范细节需处理 |
| 3c | CFR + 高质量中间编码（ProRes/DNxHR conform） | CFR 重编码的广播变体：转专业中间编码再剪 | 广播界剪辑 H.264 源的惯例做法（行业常识，本轮无一手来源） | CFR 路线的低损变体 | 文件巨大（10~30×）；仍是重编码 |
| 4 | BestSource | 全解码索引+帧哈希校验+坏点回退 | 官方组织、极活跃、源码级证据 | 未来取帧底层评估对象（与 TorchCodec 并列） | 集成成本高（C++/VS 生态） |
| 5 | GPAC patch_dts | mux 时 DTS 单调化补丁 | 源码级（默认关、expert） | **不覆盖 pts 碰撞，判定无效** | — |
| 6 | setts BSF | 解码序表达式重写 ts | 官方文档变量全集 | **理论关闭**（呈现序不可表达） | — |
| 7 | bento4 | box 级编辑 | 官方工具集 | 无时间戳重写能力 | — |
| 8 | FFMS2 | demux 索引+关键帧 seek | 老牌、社区标准 | **重复时间戳直接拒载**（#77） | — |
| 9 | L-SMASH Works | 全帧解析索引+RAP+顺解 | 活跃 fork | 机制同构无增益 | — |
| 10 | TorchCodec | 帧号一等 API（解码） | PyTorch 官方（前轮调研） | 未来评估对象 | 未实测 |
| 11 | 录制端预防 | 软件编码 x264 CFR；硬件编码已知产 VFR | OBS #13396/#12431 | **未来录制的摄入规范** | 本案源工具不明 |
| 12 | QC 验收 | 解封装级重复统计（非 MediaInfo） | MediaInfo 误报案例（#576/#293） | 沿用本仓 PyAV 统计 | — |

## 6. 对既有结论的修正/加强

1. §5.8 "没有专门修重复 pts 的成熟项目" —— **维持**，且加强了：连最接近的
   帧服务器生态（FFMS2/BestSource/L-SMASH）也只是"绕开"（索引/哈希/拒绝），
   没有任何一个在容器层修复。
2. §5.8 "GPAC 只补 DTS" —— 从"文档线索"升级为**源码级定论**（含参数名
   `patch_dts`、默认关、行为细节），§5.9 的悬置问题关闭。
3. §5.9 "mkvmerge 外部时间戳注入" —— 从"设想"升级为**官方手册明文机制 +
   MKV 单时间结构的理论根据**，列为待实测第一候选。
4. CFR conform 路线获得 OBS 社区的独立印证（#13396 workaround 原样同款命令）。
5. 我们的 PyAV 方案与 BestSource 哈希校验方案**思路同源**（计数免疫时间戳 +
   内容校验消歧），且是针对"碰撞帧"做了显式消歧的——业界同类零件里
   BestSource 用哈希串、我们用亮度语境，各有取舍。

## 7. 待 owner 拍板的实测清单（调研已回归，按指示等拍板）

1. **mkvmerge 时间轴注入实测**（半无损候选）：认证表 → v2 时间码文件 →
   `mkvmerge -c copy --timestamps 0:tc.txt 2.mp4 -o 2.mkv` → 重复/倒退/内容
   对齐三项验收（复用 .cache/norm_test 的检查脚本）。
2. **自研 MP4 双表重写**（若 mkvmerge 实测通过但 MKV 容器不可接受，或直接
   要 MP4 形态的无损修复时启动）：PyAV 解封装取解码序样本表 + 认证表呈现序
   排名 → 重写 stts（等距）与 ctts v1（带符号偏移）→ 三项验收同上。
   这是调研确认的"现成轮子空白点"的自建版，工程量中等。
3. BestSource / TorchCodec 作为未来取帧底层零件的评估（非紧急）。
4. GPAC patch_dts 命令行语法验证（预期收益低，可不做）。
