# 04 — 开源 NLE 预览/seek/倍速 源码级调研（MLT / OpenShot / Olive + 先例检索）

> 调研日期：2026-09-14。调研者：串行子代理。
> 目的：为「物化重剪代理」方案寻找开源 NLE 中的对应实现/先例/反面证据。
> 纪律：数字/结论标来源（issue 链接、源码路径）；冲突并列；查不到写「未找到」+[推断]。

## 目录
- §1 MLT 框架（Kdenlive/Shotcut 引擎）
- §2 OpenShot（libmpv 播放器）
- §3 Olive
- §4 对比分析：连续预览的三条路线
- §5 「按导出节奏预物化时间线」先例检索
- §6 结论与对我方案的启示

---

## §1 MLT 框架（Kdenlive/Shotcut 引擎）

### 1.1 核心回放模型：consumer 拉帧 + producer 每帧推进（源码已核）

架构（src/framework/mlt_consumer.c，mltframework/mlt master）：
- 典型 consumer 启一个"读/渲染线程"循环调 `mlt_consumer_get_frame`：向 producer 链请求 frame → 放入预读队列；默认 `buffer=25` 帧、`real_time=1`（mlt_consumer.c:164-173）。渲染（`mlt_frame_get_image`）在消费侧做，队列满则丢。
- 播放节奏由音频样本数 + 帧时长驱动；`real_time>0` 时按**平均处理耗时 vs 帧时长**决定 `skip_next`：平均超帧时长→下一帧跳过渲染（纯掉帧，不解码跳帧），连续掉帧超过 `drop_max`（≈1s）后强制渲染一帧（mlt_consumer.c:920-1010）。**即 MLT 的"跟不上"策略=掉帧，不是降低解码密度。**

seek（两层）：
1. `mlt_producer_seek`（src/framework/mlt_producer.c:339）= 只写 `_position` 属性 + 递归处理 cut 父子，O(1) 元数据操作，不触发解码。
2. 真正解码 seek 在 `src/modules/avformat/producer_avformat.c` 的 `seek_video()`（:1849-1935）：
   - 属性 `seek_threshold` 默认 **64**：只有 `position < video_expected`（往回）或向前跳 ≥64 帧才真 seek；`position+1==video_expected` 视为 paused，直接复用上一帧图像。
   - 真 seek = `av_seek_frame(context, video_index, timestamp, AVSEEK_FLAG_BACKWARD)`（**只能落在前一关键帧**）+ `avcodec_flush_buffers` + 清空后台 packets 线程的 vpackets 队列 + `skip_loop_filter=AVDISCARD_NONREF`。
   - seek 后由 `producer_get_image` 的解码循环**顺序解码并丢弃**直到 `current_position >= req_position`（时间压缩编码 `must_decode` 恒真，:2858-2875）。B 帧且 0≤speed≤1 时 `preseek` 回退 2 帧以满足 B 帧参考（:2866-2867）。
   - 关键帧成本完全转嫁给 libavcodec + 文件 IO：**长 GOP（如 GOP=250@60fps≈4.2s）下每次真 seek 要多解最多 250 帧才能出目标帧**。无 keyframe-only seek 模式（未找到该属性）。
   - `find_first_pts()` 打开文件时最多扫 500 个包找首个视频关键帧 PTS（:~923 附近逻辑，WebFetch 摘要 + 源码核对）。

倍速（正常向）：
- 速度就是一个 double 属性 `"_speed"`（`mlt_producer_set_speed`，mlt_producer.c:441-455）。播放推进= `mlt_producer_prepare_next()`：`seek(position + speed)`（mlt_producer.c:619-622）。
- **推论（源码级）**：speed=2/4 时 position 步进 2/4，但相邻请求帧距 < `seek_threshold`(64)，avformat **不真 seek、顺序解码每一帧**，只是输出帧隔 2/4 取 1 —— 4x 预览要求解码吞吐 4×实时，长 GOP 素材在 CPU 解码 ~1x 的机器上必然疯狂掉帧（consumer 的 skip 只能省渲染不能省解码）。这与我方「4x 预览需短 GOP 代理」的动机互证。

变速（timewarp producer，src/modules/core/producer_timewarp.c，factory.c:263 注册）：
- 包一层：内部 clip producer `set_speed(0)`（暂停态），timewarp **每输出帧显式 `mlt_producer_seek(clip, position±in)`**（producer_timewarp.c:147-153），再按 `frame_rate_num/|speed|` 缩放假 profile 的 fps 让 consumer 慢放/快放节奏正确（:270-283）；音频走 `rbpitch` filter 保/变调（:170-182）。
- 即 MLT 的变速=**每帧一次随机定位**。慢速 0.25x 时相邻输出帧的源位置仍步进 1（仍是顺序解码）；但任意 speed 变化、反向、scrub 都退化为 seek_video 路径 → 长 GOP 全部踩坑。

修剪/删除的即时性（模型层）：
- MLT 时间线=tractor/playlist/multitrack 纯元数据（Kdenlive 内部模型→序列化 MLT XML）。删/剪只改 playlist 的 in/out 与占位，播放图无需重解码重编码即可用——"即时"来自元数据编辑，代价转嫁给每次 get_frame 的按需解码。
- [推断] 每个 avformat producer 实例独立维护解码状态（video_expected/last_position/vpackets），多片段引用同一素材=多实例，各实例首个 seek 都付 find_first_pts + 关键帧回退的冷启动成本；社区有"同 resource 的 producer 是否缓存"的讨论（mltframework/mlt discussions/1075，无内建共享解码缓存）。

### 1.2 Kdenlive：两条"重物化"流水线（源码已核）

**A. 素材级 proxy clips**（src/jobs/proxytask.cpp + data/encodingprofiles.rc + src/kdenlivesettings.kcfg）：
- 默认代理参数（data/encodingprofiles.rc `[proxy]` x264 项）：`-vf scale=%width:-2 -fps_mode cfr -c:v libx264 **-g 1 -bf 0** -vb 0 -crf 20 -preset veryfast -c:a aac -ab 128k`，容器 mov。**注意：Kdenlive 的素材代理默认就是"全 I 帧 H.264"（g=1 bf=0）**，专为逐帧 seek 服务；`%width` 来自 `proxyscale`，kcfg 默认 **640**（素材最小边 `proxyminsize`≥1000 才生成，kdenlivesettings.kcfg:251-267）。
- 触发为后台任务（jobs/proxytask.cpp），生成后 bin 内 clip 的 producer resource 换为 proxy 文件（`enableproxy`/`proxypreview=true` 等文档属性，kdenlivesettings.kcfg:390-393）。

**B. 时间线级"物化重剪代理"——Timeline Preview（src/timeline2/view/previewmanager.cpp，977 行，已核）**：
> 这是我方"物化重剪代理"在开源 NLE 中的**最直接同类物**。
- 结构：在时间线 tractor 顶上插入一条专用 **preview track**（`kdenlive:playlistid=timeline_preview`，hide=2 关其音频，previewmanager.cpp:216-236）。已渲染的时间线片段以**独立小文件**（默认扩展名按 profile，如 DNxHD→mov）作为 playlist clip 填进该轨道；未渲染处为 blank，播放落回真实时间线图。
- 分块：chunk 粒度 = `timelinechunks` 帧数，kcfg 默认 **25 帧**（kdenlivesettings.kcfg:378-381）。渲染块 = `startChunk=zone/chunkSize` 的整块序列（addPreviewRange, :521-545）。
- 渲染：独立进程 `kdenlive_render preview-chunks ...`（QProcess，doPreviewRender :650-688）按 MLT 参数（`previewparameters`/`previewextension` 文档属性）渲染各 chunk；完成后 `gotPreviewRender` 把文件插回 preview track（:619-643）。
- 失效与重建：**chunk 级 invalidate**。任何编辑→`invalidatePreviews()`；受影响 chunk 从 `m_renderedChunks` 移入 `m_dirtyChunks`，对应 playlist 位置 `replace_with_blank`（:317-400）。状态以序列属性 `previewchunks`/`dirtypreviewchunks` 随工程持久化（projectmanager.cpp:354-355,999-1001）。undo/redo 深度集成：每个 undo 步归档 dirty 集合（:329-351）。`autopreview`（kcfg 默认 **false**，:382-385）控制是否自动重渲染脏块，否则由用户手动"Render Preview"。
- 默认预览编码档（encodingprofiles.rc `[timelinepreview]`）：**DNxHD 1080p 36/45/75/90Mbps 全 I、ProRes `g=1 bf=0 qscale=4 s=800x450`、MJPEG、或硬编 h264 `g=1 bf=0`** —— 与素材 proxy 同理：**预物化文件一律帧内/超短 GOP，保证 mpv/解码端任意 seek 零开销**。分辨率还低于工程（如 800x450）。
- 对比我方：Kdenlive=按 25 帧小 chunk 物化、chunk 失效即重渲、预览轨道与真轨道运行时切换；我方=按导出节奏整段物化+暂停帧真源。差异点是失效粒度（25 帧重渲 vs 物化段失效）。

### 1.3 长 GOP 素材的已知表现（issue/论坛级证据）
- MLT FAQ（mltframework.org/faq）：avformat producer 依赖 libavcodec，长 GOP 随机访问需从关键帧重解；FAQ 提到硬件加速仅在"CPU 不擅 H.264"时有收益。
- Shotcut 论坛（MLT 之上的官方论坛）：
  - "GOP, B Frames and Codec Threads"（forum.shotcut.org/t/1973）："GOP size is very important to a frame-precision seeking tool such as Shotcut and nearly all other video editors"。
  - "Lag when skipping individual frames"（/t/29633）：逐帧步进在长 GOP 上卡顿是典型报告。
  - "Format (codec) for editing"（/t/36127）：官方建议编辑用小 GOP/全 I 中间格式。
  - /t/22307：跳帧 bug 报告，解法=转 DNxHR/短 GOP 中间格式。
- MLT 侧无针对长 GOP 的结构性方案（无关键帧级 seek 模式、无跨实例解码共享缓存，discussions/1075 亦确认无缓存）；**开源生态给的答案就是"先物化（proxy/preview）再编辑"**。

## §2 OpenShot

### 2.0 先修正任务前提：「libmpv 播放器」不成立
- openshot-qt master 实测源码：`src/windows/preview_thread.py:136` 用的是 libopenshot 的 **`openshot.QtPlayer()`**（不是 libmpv）；`src/windows/video_widget.py` 是普通 QWidget+paintEvent 接收 QImage（`present()`，:428）。
- libopenshot 播放器沿革：2.x 有 SDL1Player/SDL2Player，3.x 默认 QtPlayer（JUCE 音频输出 + Qt 渲染）。**未找到**任何已合并的 libmpv 播放器 PR。
- OpenShot 3.0 官方发布文的播放改进措辞（openshot.org/blog/2022/12/10/new_openshot_release_300/，已核对原文）：解码器重架构、**"caching background thread … improved to operate at different speeds (1X, 2X, 4X) and in both forward and backwards directions"**、pre-roll 缓冲、Preferences 新增缓存设置——全程未提 mpv。

### 2.1 预览架构：timeline 是"逐帧实时服务"，无预物化（源码已核）
链路：`QtPlayer` → `PlayerPrivate::run()` 播放线程（src/Qt/PlayerPrivate.cpp:82-150）：
- 每次迭代 `video_position += speed`（float 倍速步进，GetNextFrame :155-163），然后 **`reader->GetFrame(video_position)`= Timeline::GetFrame —— 对整条时间线图从头合成该帧**（时间线=FrameMapper+Clip+Effect 图，逐帧现算）。
- 节奏=sleep 补偿渲染耗时 + 音频帧差校正（:105-148）；不与音频对齐时 seek 音频（speed≠1 每帧 `audioPlayback->Seek(video_position)`，:108-110）——变倍速的音频每帧重定位+重采样，代价高。
- 时间线级缓存仅 `CacheMemory`（RAM，Timeline.cpp:216；逐帧缓存合成结果，按字节上限淘汰，CacheBase.h `max_bytes`，0=无限）。编辑/属性改动即 `final_cache->Clear()`（Timeline.cpp:753-755）——**预览缓存极脆弱，任何编辑全失效**。
- 另有 `VideoCacheThread` 预取线程：向播放位置前方预取 `max_concurrent_frames = OPEN_MP_NUM_PROCESSORS*4` 帧（VideoCacheThread.cpp:42-118），支持按 speed 预取（3.0 改进点）；缓存漂移过远就重置。

底层素材 seek（src/FFmpegReader.cpp）：
- `GetFrame(requested_frame)` 发现请求不连续 → `Seek()` → `av_seek_frame(pFormatCtx, video_stream, seek_target, AVSEEK_FLAG_BACKWARD)`（:1757）+ 顺序解码前进补帧 + `working_cache`/`missing_frames` 辅助（:1671-1700, 2026-2108）。**与 MLT 同构：长 GOP 每次 seek 从前一关键帧重解。**
- 有 `CheckSeek` 的"越回退越远"试探逻辑（`Seek(seeking_frame - (10*seek_count*seek_count))`，:1188）用于处理 pts 异常素材。

倍速：
- API 层 `QtPlayer::Speed(float)`（QtPlayer.cpp:218-228）= 设 video 步进倍数 + 预取线程 speed + 音频 setSpeed；**openshot-qt UI 未暴露预览倍速按钮**（UI 只有 clip speed 属性）[基于 master 源码未见调用；推断]。
- 结构性结论：**OpenShot 的 2x/4x 预览同样要求解码+全图合成吞吐 ×2/×4**，只是 3.0 让预取线程能按倍速预取，缓解但不消除。

### 2.2 seek/scrub 的知名度问题与缓存策略
- 高热 issue（均为 openshot-qt）：
  - #993 "OpenShot is still not useable.. stuttering video preview playback"：720p60 H.264 预览 CPU 100% 卡顿（导出却正常）。
  - #3652 4K60 H.264 开硬解仍每 1-2 秒卡顿；#4375 2.6.0 慢到不可用（CPU 利用率低——瓶颈在串行合成而非解码）。
  - #960/#965：官方解法=降预览分辨率/调 Preferences→Cache。
- 缓存三处：时间线 `CacheMemory`（RAM）、per-clip FFmpegReader `final_cache`（RAM）、素材箱缩略图/时间线 filmstrip（磁盘，src/classes/thumbnail.py 经 libopenshot 出 PNG）。libopenshot 另有 `CacheDisk`（磁盘帧缓存，CacheDisk.cpp）但主预览路径未用磁盘缓存做 scrub 加速——**无任何"预渲染时间线到文件"机制**。
- 修剪/删除后：重建 timeline 模型 + 清 RAM 缓存，无需重编文件；代价是再次全图逐帧现算。

## §3 Olive

### 3.0 项目现状（gh api + 官网，2026-09-14 核）
- olive-editor/olive master：9,134 stars、159 open issues、未归档；**最后推送 2024-12-05**（gh api repos/olive-editor/olive → pushed_at）→ 截至本调研日已 ~21 个月无提交，实质停滞。
- 发布史（gh api releases）：最后一个"正式版"= **0.1.2（2019-11-11）**；仅剩 prerelease `0.2.0-nightly`（2024-12-05，与最后提交同日，即 nightly≈master）。**1.0 未发布**。官网 olivevideoeditor.org 首页现挂 **"Olive will return..."**（WebSearch 摘录），README 仍自称 "Olive is alpha software and is considered highly unstable"。
- 结论：作为"可借用的生产实现"权重低，但 0.2 系的缓存架构设计本身仍是开源 NLE 中**唯一成体系的"时间线→磁盘逐帧预物化"基础设施**，架构上有参考价值。

### 3.1 回放架构：节点图单帧渲染服务 + 一层"优先吃磁盘缓存"的播放路径（源码已核）
- master（0.2 系）时间线=节点图（Track/Block → ViewerOutput 汇聚），**没有 MLT 式 consumer/producer 运行时**；预览=对节点图逐帧请求渲染（ViewerWidget::GetSingleFrame → RenderManager 后台线程，OpenGL 输出），音频独立走 AudioManager+PreviewAudioDevice。
- 播放循环（app/widget/viewer/viewer.cpp `ViewerWidget::PlayInternal` :916-990）：Play 时先**预排队**若干帧（循环 `RequestNextFrameForQueue` 填 playback queue）再起播；节奏由音频输出回调驱动 + backup QTimer（interval=1 帧时长，:1196-1197）兜底；`playback_speed_` 为整型且**可为负（支持倒放）**，倍速变化时音频处理器按 `|speed|` 重开（:519）。
- **关键机制**：`ViewerWidget::GetFrame(t)`（:1160-1175）先查 `video_frame_cache()->GetValidCacheFilename(t)`——磁盘上有已物化帧文件就直接 QtConcurrent 读盘（`DecodeCachedImage`，:444-459），没有才 `GetSingleFrame` 现算。**即 Olive 的播放路径设计为"缓存优先、实时渲染兜底"，与我方"预览播代理、缺段真源"同构**（差异：Olive 缺段兜底=现算单帧，我方=真源解码）。

### 3.2 PreviewAutoCacher + FrameHashCache：时间线预物化基础设施（存在但被 stub 关死）
- **PreviewAutoCacher**（app/render/previewautocacher.cpp，754 行；类注释："Manager for dynamically caching a sequence in the background ... based on the playhead"）：
  - 缓存窗口=playhead − `DiskCacheBehind`（config 默认 **0s**）～ + `DiskCacheAhead`（默认 **60s**）（config.cpp:150-151，SetPlayhead :~200）；另支持 `ForceCacheRange`（h:71）强制缓存任意区间。
  - 后台渲染经 **ProjectCopier**：复制节点图到渲染线程、图更新时排队增量同步（TryRender :427 附近 `copier_->HasUpdatesInQueue/ProcessUpdateQueue`），避免与 UI 竞争；并发渲染任务上限 `max_tasks=4`（源注释 "Completely arbitrary number"）。
  - **UI 入口**：Sequence 菜单 "Cache Sequence"（整条序列，viewer.cpp:398）/"Cache Sequence In/Out"（workarea，:401-404）→ ForceCacheRange——"手动预物化一段"的现成先例。
- **FrameHashCache**（app/render/framehashcache.cpp）：**每帧一个文件**，文件名=时间线时间戳 int64（CachePathName :310-335、ValidateTimestamp :56），目录=项目缓存目录/UUID（DiskManager 管理容量/清理）；8bit→PNG、浮点→EXR(DWAA)（SaveCacheFrame :337-395）。
- **失效与重建**：PlaybackCache 维护 validated/invalidated 时间区间并随工程持久化（playbackcache.h:56-74；framehashcache.cpp:251,267 SaveStateEvent/LoadStateEvent）→ **重开工程可复用已物化帧**。节点图任何变化 → `ViewerOutput::InvalidateCache(range)` → autocacher 收 VideoInvalidatedFromCache → `cache->Invalidate(range)`（previewautocacher.cpp:206）区间级失效、按需重渲。**失效粒度=时间区间（帧文件级），不是全清**——比 OpenShot 的 `final_cache->Clear()` 强一个量级。
- **但是（重要）**：master 中序列视频自动缓存被 stub：`ViewerOutput::IsVideoAutoCacheEnabled() { qDebug() << "sequence ac is a stub"; return false; }`（viewer.h:186-187），`autocache_input_video_` 默认 false（viewer.cpp:43）。设置对话框里 AutoCache 勾选项还在（sequence.cpp / sequencedialogparametertab.cpp 引用 IsVideoAutoCacheEnabled），issue #1758 "[CACHE] Sequence -> cache sequence in/out doesn't work if AutoCache is disabled [closed]" 印证该开关影响实际行为。[推断] 系 2022-2023 渲染器重构期间临时关停（viewer.h 最近一次大改 2023-02），此后开发停滞未再启用。
- Olive **无素材级 proxy 功能**（全树 grep "proxy" 0 命中）；多机位、无编辑友好中间码制导。

### 3.3 素材级解码 seek（app/codec/ffmpeg/ffmpegdecoder.cpp）
- 解码实例仅 ~2 帧 RAM 缓存（`MaximumQueueSize()` 返回 2，:1011-1017，注释自称"可能可调"）；请求帧超出缓存范围 → `ClearFrameCache()` + `av_seek_frame(fmt_ctx_, stream, ts, AVSEEK_FLAG_BACKWARD)`（Instance::Seek :1189-1193，只能落前一关键帧）+ 顺序解码前进（GetFrame 主循环 :836-875 边解边筛）。
- 无帧索引、无 keyframe-only 模式 → **长 GOP 随机访问成本与 MLT/OpenShot 同构：每次 seek 从前一关键帧重解**。

### 3.4 scrub/seek 行为
- playhead 变动 → `UpdateTextureFromNode`（:878）单帧渲染（走 GetSingleFrame 完整节点图合成）；scrub 停止时 PreviewAutoCacher::SetPlayhead 平移缓存窗口（若启用）。堆积防护=CancelVideoTasks（单帧任务可取消）。
- 未找到 Olive 有预览质量降级/分辨率切换机制（无 proxy、无 preview scale）[基于 master 全树；未找到]。

### 3.5 H.264/长 GOP 的已知表现（issue 级证据）
- **#920 "[Bug?] H264 backwards scrolling problems"（closed）**：任意 H.264 素材反向拖动时帧前跳/重复、错乱；用户自诊为 ffmpeg 关键帧 seek 问题；慢速逐键操作则正常——与"每 seek 回退到关键帧+顺序重解"的实现互证。
- **#1706 "[CACHE] Some frames do not get cached"（closed，commit bdbeb111/Win11）**：逐帧磁盘缓存出现散在缺帧（"Missing frames 32 124 481 483 ..."），缺帧处"about a second of lag trying to cache a frame"，删缓存+整条重渲也不能补全——两点启示：(a) 缓存缺段的兜底成本=单帧全图现算（秒级），(b) 逐帧小文件缓存的完备性/一致性需要额外工程保障。
- 倍速/倒放：`playback_speed_` 整型可负（倒放依赖解码器把帧缓存住倒序放，RAM 缓存仅 2 帧的实现对长 GOP 倒放成本极高）[源码级推断]。

## §4 对比分析：三个引擎如何做"剪好后的连续预览"

### 4.1 路线光谱（源码级）

| 维度 | MLT（Kdenlive/Shotcut） | OpenShot (libopenshot) | Olive (0.2 master) |
|---|---|---|---|
| 连续预览主路线 | consumer 拉帧：时间线图逐帧现解现渲（§1.1） | 播放线程逐帧 `Timeline::GetFrame` 全图现算（§2.1） | 节点图逐帧单帧渲染服务；**设计上缓存优先**（GetFrame 先查磁盘帧文件，§3.1） |
| 预物化到文件 | 无（框架层）；Kdenlive 在上层补 Timeline Preview chunk 轨道（§1.2B） | **无任何机制**（§2.2） | **有完整基础设施**（PreviewAutoCacher+FrameHashCache，逐帧 EXR/PNG），但序列视频 autocache 被 stub 关死（§3.2） |
| 倍速 | speed 步进、相邻帧距<64 不真 seek→长 GOP 倍速=解码吞吐×N | float 步进+预取线程按 speed 预取 | 整型 speed 可倒放；音频按 \|speed\| 重采样 |
| "跟不上"策略 | 掉帧（skip），不解码跳帧 | 无管理（stutter 即用户可感知卡顿） | 预排队+音频驱动+backup timer；缓存缺段=单帧现算兜底 |
| 删剪后失效 | 元数据编辑，重放时按新图现算；Kdenlive：chunk 级 invalidate→25 帧 chunk 重渲 | `final_cache->Clear()` 全清 RAM，重建=再次全图现算 | **区间级 invalidate**（只废受影响帧文件）+ 按需重渲 + 跨会话持久化 |
| 素材级 seek | avformat 关键帧回退+顺序重解，seek_threshold=64 | FFmpegReader 同构（AVSEEK_FLAG_BACKWARD+补帧） | 同构（§3.3），RAM 缓存仅 2 帧 |
| 素材 proxy | Kdenlive 有：**g=1 bf=0 全 I 帧**（§1.2A） | 无 | 无（全树 0 命中） |

### 4.2 三条路线的深层分野
- **纯实时服务（MLT/OpenShot）**：编辑"零延迟生效"（元数据/图改动即可播），但把全部成本推给每一次播放帧——对长 GOP 高帧率素材，成本=关键帧回退×seek 次数 + 全图合成×帧率。掉帧/卡顿是结构性结局（issue 证据：§1.3、§2.2）。它们对"剪好后的连续预览"的回答是**"每次都现算，接受降质"**。
- **实时服务+磁盘帧缓存（Olive 设计意图）**：同样的实时服务，但把算过的帧落盘复用，编辑只失效区间——这是**"事后缓存"（reactive cache）**：播到哪缓存到哪，首次经过仍卡。且 master 实际处于半弃用状态。
- **预物化轨道（Kdenlive Timeline Preview）**：唯一真正"先渲染后观看"的路线，但实现为 DNxHD/全 I 低分辨率 chunk + 25 帧小粒度失效重渲——优化目标是"反复观看/精修"场景，牺牲磁盘与重渲开销换取任意 seek 零成本。
- **共同点（对我方最有用）**：凡做了物化者（Kdenlive timeline preview、Olive FrameHashCache、Kdenlive 素材 proxy），**文件格式一律全 I 帧/超短 GOP**——这是开源生态对"物化产物该长什么样"的一致答案（§1.2、§3.2）。
- 我方路线（按导出节奏预物化到**短 GOP H.264 CRF14 代理**、预览播代理+暂停真源帧）在开源三家中无完全对应物：介于 Kdenlive（物化但 chunk 小粒度、I 帧）与纯实时服务之间；**"短 GOP 可视+暂停回真源"的混合取帧是自有设计**。

### 4.3 1080p60 H.264 长 GOP 的已知表现（跨项目证据汇总）
- OpenShot #993：720p60 H.264 预览 CPU 100% 卡顿（§2.2）；#3652 4K60 每秒卡顿。
- MLT/Shotcut 论坛：GOP 对帧精度 seek 的决定性、逐帧步进卡顿、官方建议短 GOP 中间格式（§1.3）。
- Olive #920：H.264 反向 scrub 错乱（§3.5）。
- Kdenlive 的回答最诚实：直接把"默认全 I 帧素材 proxy + 低分辨率时间线预览"做成功能（§1.2）。
- 结论：**没有任何一家在 1080p60 长 GOP 素材上靠"实时服务裸跑"给出流畅预览**；生态答案=降分辨率/转码/物化三选一或组合。

## §5 「按导出节奏预物化时间线到文件」先例检索

> 已知并排除（任务书指定）：LosslessCut smart cut、DaVinci Resolve render cache（+optimized media/proxy）、Premiere preview files。以下为三者之外找到的。

### 5.1 开源阵营
- **Kdenlive Timeline Preview（§1.2 已源码级覆盖）**——开源唯一成熟实现（WebSearch 旁证：docs.kdenlive.org "Timeline Preview Rendering"，社区称 "bake timeline"/"preview render"，红条/绿条状态）。
- **Olive PreviewAutoCacher + FrameHashCache（§3.2）**——基础设施级（逐帧 EXR/PNG 落盘、区间失效、跨会话持久化），但序列视频 autocache 被 stub 关死且项目停滞（§3.0）。设计文档价值 > 可运行价值。
- **Cinelerra-GG "Background Rendering"**——**开源中最接近"走在播放前头"的先例**：在 Preferences→Performance 启用后，后台按 playhead 前进持续把时间线渲染到文件，时间线顶部红条=已渲染区间，播放可用已渲染文件（Secrets of Cinelerra, heroinewarrior.com/cinelerra/cinelerra.html；CinelerraGG_Manual, download.cinelerra-gg.org）；参数 "frames to preroll background rendering"="每个后台渲染任务向前多渲的帧数"；渲染文件格式可选（含 EXR 序列=逐帧文件，2021-08 列表邮件）。触发=playhead 驱动+手动 Toggle，非导出节奏。
- **Blender VSE**：素材级 strip proxy（低分辨率替代，Strip→Rebuild Proxies，docs.blender.org proxy 页）+ 4.x 时代 strip 磁盘缓存（Preferences→System "Disk Cache: Writes cached strips to disk"）；新缓存重构 PR #137926 已**移除磁盘缓存**（"Disk cache is gone (primary reason: proxies are kinda the same thing)"）——生态内一次"磁盘缓存 vs proxy 二选一"的真实取舍记录。
- **未找到**：Flowblade/Pitivi/LiVES/Shotcut 等无时间线级预物化（Shotcut 只有 preview scaling；Pitivi=素材 proxy）[关键词检索未见；未找到]。

### 5.2 商业阵营（印证"工业界普遍做预物化"）
- **Final Cut Pro**：后台渲染——默认**停止操作 0.3s 后自动开始**渲染需要渲染的时间线段为 render files（support.apple.com/guide/final-cut-pro/background-rendering-ver717f3ca3/mac；Manage render files ver68a8c250）；旧版手册还有 **"Render Ahead" preference**（播放前按指定时长渲染 playhead 前方，Final Cut Pro User's Manual PDF）——"按播放推进节奏预物化"的完整商业先例。
- **Nuke Studio / Hiero "Timeline Disk Cache"**：GPU 渲染帧落盘换可靠回放；手动 Cache→Disk Cache→Cache Sequence Range（learn.foundry.com hiero 12.2 caching_sequences）；**Nuke 15.0/14.1 新增 Automatic Timeline Disk Cache：检测到回放低于实时自动启用**（Foundry 官方视频 "Automatic Timeline Disk Cache"）——"跟不上就自动物化"的现成商业语义，与 MLT 掉帧策略形成对照。
- **Avid Media Composer "Background Render"**：v8.2 引入的独立后台服务，编辑同时把效果渲为 precompute 媒体（resources.avid.com WhatsNew v8.2 PDF；v2025.x Editing Guide：Background Transcode/Render/Dynamic Media Folder 服务默认关闭）。
- **Filmora "Preview Render"**：消费级产品的后台时间线预渲染（wondershare 教程页）。
- （RAM 路线对照：Vegas Dynamic RAM Preview 不落盘；After Effects 为帧级持久磁盘缓存。）

### 5.3 导出侧 "smart rendering" 家族（与我方共享"按需重编/直通"思想，但服务导出）
- EDIUS smart rendering（未改动段直通）；**SolveigMM Video Editing SDK**（"Smart Rendering Technology"：切点邻域重编+其余直通实现无损剪辑，solveigmm.com）；GStreamer GES 未改动段 passthrough。LosslessCut smart cut 为同家族（已知）。

### 5.4 负结果（诚实登记）
- "baked timeline"/"preemptive encode"/"bake preview" 在视频剪辑域**无既有术语对应物**（检索命中的是 3D lightmap baking 与调色圈"bake timeline"工作流讨论）。
- "**按导出节奏**（而非 playhead/空闲触发）把时间线物化为代理文件"：**未找到**任何开源或公开描述的商业实现。最接近的语义邻居=①Cinelerra-GG（playhead 驱动 ahead 渲染）、②Nuke 自动 timeline disk cache（低于实时自动物化）、③FCP Render Ahead（按播放提前量）。三者的物化产物都是缓存/中间帧或序列帧，**没有一个把"导出所用的编码产物"直接复用为预览**——我方"短 GOP 导出级代理兼作预览"在检索范围内是未见的组合。

## §6 结论：对我方「物化重剪代理」方案的印证/反驳清单

### 6.1 印证（证据→我方设计点）
1. **长 GOP 随机访问三家同构地烂**：MLT 关键帧回退+顺序重解（§1.1）、OpenShot 同构（§2.1）、Olive 同构且 RAM 缓存仅 2 帧（§3.3）；issue 证据链完整（§1.3、§2.2、§3.5）→ 预览播短 GOP 代理、seek 走索引，是对症的（唯一对症）解法。
2. **倍速=解码吞吐×N**：MLT/OpenShot 的倍速不省解码，长 GOP CPU 解码 ~1x 必然掉帧（§1.1 推论、§2.1）→ "4x 预览需代理"动机成立。
3. **物化产物全 I/超短 GOP 是生态共识**：Kdenlive 素材 proxy `g=1 bf=0`、timeline preview 全 I 档、Olive 逐帧 PNG/EXR（§1.2、§3.2、§4.2）→ 我方 CRF14 短 GOP 落在共识谱系内；短 GOP 比全 I 物化省码率且能直接复用为导出素材，属合理外推（代价：seek 非零成本，需 mpv 索引+每 GOP 一次回退 [待实测]）。
4. **预物化时间线是工业界常规武器**：FCP（空闲 0.3s 触发+Render Ahead）、Avid Background Render、Nuke/Hiero Timeline Disk Cache、Filmora、Cinelerra-GG（§5.2）→ 方向无异；且 Nuke 15"低于实时自动物化"证明"检测跟不上→启动物化"是成熟语义。
5. **"缓存优先、缺段兜底"与 Olive 同构**：GetFrame 先磁盘后现算（§3.1）；我方"预览播代理、暂停真源帧"是该模式的暂停特化——暂停时真源解码无实时压力，比 Olive 缺段单帧现算（秒级卡，#1706）更稳。
6. **6.8× 实时 vs 1× 消费**：MLT 跟不上时只能掉帧（§1.1），而我方物化速度为播放 6.8 倍，后台物化理论上可永远领先 playhead——顺带解释为何商业产品"跟不上才物化"即可，我方更宽裕。
7. **跨会话持久化有先例**：Kdenlive previewchunks 属性化（§1.2B）、Olive SaveStateEvent（§3.2）→ 物化状态随工程存续是标准做法，不是过度设计。
8. **开源无可直接抄的完整实现**（MLT 无物化层、OpenShot 缓存全清、Olive 半弃用+stub，§4.2/§3.0）→ 支持自研决策。

### 6.2 反驳/风险（证据指向的我方薄弱处）
1. **触发语义无先例**：所有先例的物化触发=playhead/空闲/低于实时，**无"按导出节奏"驱动者**（§5.4）。导出时间线 ≠ 用户即将观看的时间线：用户可能反复精修头部 10% 而导出在尾部——按导出节奏预物化可能算力投错方向。[反驳成立度高] 建议：导出节奏+playhead 预读双驱动（Kdenlive DiskCacheAhead=60s 窗口与 FCP Render Ahead 都是现成参照）。
2. **失效粒度 vs 重编成本**：Kdenlive 用低分辨率+25 帧 chunk 把重渲单位做小（§1.2B），Olive 按帧区间（§3.2）；我方 1080p60 全质量段级物化，一旦在已物化区中段删 1 帧，段级失效→整段 6.8x 重编，反噬明显。[反驳成立度高] 缓解方向：追加式物化（只编到编辑点之后的已物化内容仍有效）+失效只影响后缀，或编辑热区降级小段。
3. **分段文件完备性**：Olive #1706 显示散在缺段/缺帧会造成秒级卡顿且难以自愈（§3.5）→ 我方段表需校验和/索引自愈；2.26GB/h 的磁盘占用需容量管理（Kdenlive cache 目录、Olive DiskManager 均为先例）。
4. **反向 scrub**：Olive 负 speed 支持但成本极高（§3.5 推断）；短 GOP 代理反向 seek 成本≈正向×2（回退一个 GOP）[推断，待实测 mpv 行为]，方案本身不吃亏，但测试集须含反向。
5. **音频路径别走实时处理**：OpenShot 倍速每帧重定位音频代价大（§2.1）、Olive 音频独立 conform（§3.2 头）→ 代理物化时音频应一并物化（PCM/AAC），播放端零变换。
6. **MLT skip 策略提醒**：掉帧省的是渲染不是解码（§1.1）——我方代理解码 ~1x 播放 60fps 无压力，但若代理参数错配（如高 profile 硬解不可用）退化为软解瓶颈时，勿指望"跳帧救场"，需在物化时保证播放端解码兼容性（x264 baseline/main+常见色域）[工程注意项]。

### 6.3 总评
证据压倒性指向：**长 GOP 素材的流畅预览在开源生态没有"实时服务"解，只有"物化"解**；我方方案是物化家族中一个新组合（导出节奏触发+短 GOP 导出级质量+暂停真源混合取帧），主要风险不在"该不该物化"，而在**触发时机（6.2.1）与失效粒度（6.2.2）**——这两点皆有先例可借（playhead 双驱动 / 追加式物化）。

<!-- APPEND_MARKER -->
