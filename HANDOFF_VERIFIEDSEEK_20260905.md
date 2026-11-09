# HANDOFF_VERIFIEDSEEK_20260905 — C+提速与选项2闭环定位会话交接

> **§0：新会话先读 ASSET_INVENTORY.md**（资产/能力/铁律/决策唯一权威），
> 再读 checklist.md（当前工单，本地不提交），本文件为冷档交接棒。
> 前棒：HANDOFF_NATIVESTEP_20260905.md（①业务加速+②G选项1原生步进，已提交 f53d6dc/f825983）。

## 1. 本会话成果（全部在工作区，未提交，等「验收」）

| 项 | 内容 | 验证 |
|---|---|---|
| **C+（C4 挤水分）** | `_still_render_mpv`：loadfile 改 `prepare_and_wait_for_event`（5s超时回退path轮询）、固定 sleep 0.25s→60ms 渲染拍、`screenshot-to-file`+文件轮询+PIL（81.6ms）→ **`screenshot-raw` 内存直返**（3.7ms，bgr0→numpy→PIL，尺寸/stride 校验，失败回退老路径） | 点轴 0.82s→0.45-0.50s；mad 0.18-0.21 维持；still_via_mpv=true |
| **选项2（闭环验证定位）** | 引擎 `seek_source_verified`：无修正 exact seek → **playback-restart 相位门**（瞬态期 tp 报目标值假象）→ tp 与账本 float 精确匹配（±4窗/1e-7容差，不复用 `_frame_for_position_locked` 的 Fraction(str()) 路径）→ delta∈[1,4] frame-step 前进补齐 → 复核 → `seek-verified` 事件（ok/reason/frame/steps/ms）。失败类 timeout(1.2s)/overshoot/budget/step_error。拦截：EDL=False、重复段内=False（段首放行）、未认证抛 CERTIFICATION_REQUIRED。取消语义：seek_source/seek_edl/play/play_edl/step_frame/换载静默作废。预览器：`_seek_frame` 暂停+原生+非拖动→verified 分支（`_verify_pending_frame`+看门狗1.6s）；渲染循环消费事件（ok→撤贴层+`_still_frame_idx`收敛+OSD；fail→`_show_paused_still` 回退C4）；追帧飞行中抑制；`_native_step` 成功清 pending。`_command_seek_locked` 验证飞行中跳过 +5/120 修正（旧路径保留） | pytest **467+90 绿**（选项2 新增 22 测试：引擎13+预览9）；端到端验收 **4/4 样本 PASS**（63/63 唯一PTS exact 零补步，墙钟 21-90ms；段首8/8放行、段内8/8拒收）；C4 回归不变 |

## 2. 阶段0 证据链（全部 T0，已登记 ASSET_INVENTORY §六）

- **probe_seek_sweep**（2341点全片扫描）：无修正 seek 偏差∈{0,1,2} 零过冲；唯一PTS目标 ~1800 点**零偏差全落准**（「停早」全部是重复段 tp 映射段首的表象）；restart 事件 100%；瞬态早读假阳性 0。
- **probe_sweep_tier2**（40点像素交叉）：39/40 tp 说实话，唯一「反例」是两帧与截图同时位级一致（mad 0.0/0.0 平局，oracle 不可判，非 tp 说谎）；**重复段 34/34 必落段首**。
- **微探针**（瞬态）：seek 后 0~50ms tp 报**寻址目标值**（带下发字符串 9 位截断指纹），playback-restart 落定后报实际帧——老调研「tp报目标值」与探针E「tp报实际帧」都对，差在读的时机。
- **普查**：2.mp4 重复PTS卷入 26.68% 帧（24576段，几乎全是2帧段）；GOP=120 固定；VFR突变 17016 点。
- **认证表硬约束**：PTS 严格递增+duration>0（media_info 验证器）→ 真实重复被单调化(+1 tick)；引擎 `_dup_run` 按**间隙<2ms**识别（`_DUP_GAP_EPS_S=0.002`），段首放行/段内拒收。

## 3. 当前状态（09-05 压缩时刻；**历史快照——最新状态一律以 §6 为准**）

- **Git**：`fix/preview-pacing-metrics` @ f825983；工作区 5 文件未提交（mpv_engine.py +204 / preview_player.py +139 / tests×2 +399 / ASSET_INVENTORY.md +12），即 C+ 与选项2 全部改动；本棒未跟踪。
- **GUI**：pid 21820 运行中（`ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`，全部新码），**待 owner 手检**。
- **验收探针产物**（.cache/v3ass/，gitignore）：probe_seek_sweep.py+results、probe_sweep_tier2.py+results、probe_seek_timepos.py、probe_static_identity.py、verify_option2_prod.py（argv[1]=视频路径；结果文件 2.mp4=verify_option2_results.json【泛化前跑的，无 stem 后缀】、其余=verify_option2_results_{stem}.json）+ 四份 results、smoke_engine_step.py、probe_frame_step.py（探针D）。
- **杂项**：%TEMP% 有 4 个 opt2_acc_* 目录（各含 ~15-20MB 认证表 JSON，可删）；`.cache/media_info/frame_pts/` 下条目是**目录**（内含 v2-*.json），直接当文件读会 Permission denied——GUI 运行时勿动。
- **测试基线**：467 passed + 90 subtests（全套 ~26s；选项2 新增 22 测试：引擎13+预览9）。

## 4. 未决事项（新会话按此接续）

0. **【2026-09-07 追加，同日两轮修订】业务变速掉帧修复待 owner 定方案**：owner 报「补齐业务速度后 UI 1x 播放右上角 FPS 偶发 35-45」并质疑第一轮结论；第二轮重析**定案**：真机制=**EDL 拼接边界风暴**——GUI `skip_trimmed` 默认 True → 日常播放走 EDL（"# mpv EDL v0" 拼接文件；真实项目保留区≈73 个短段、中位 11 帧/0.18s，3.mp4 states 还原），业务段几乎全 0.2X 型 + speedup_02 默认 True + factor=10 → **speed=10 下每 ~30-60ms 跨一个拼接边界 = mpv EDL demuxer 源内 seek + 关键帧回填 → 真实呈现停顿**；OSD 读数诚实（非测量假象）。复现：probe_biz_osd_edl F1（EDL+factor10）OSD **31/33 窗<50、min 14.4、35-45 带全中**（stale/fresh 双变体同跌）；F0（EDL+业务关=旧行为）大体 54-60+密集尾区偶发 46.5（EDL 边界基线成本，先前已存在）；F2（EDL+factor3）**轮内稳态全 60**、仅重播启动瞬态 1-1.5s 跌 28-50。第一轮已证伪/修正项：①「snapshot 新鲜度失配」假说死（staleness≤133ms，双变体同跌）；②「~46%丢帧」→ 实为 ~66-73%（持续10x）；③ source 模式（E0-E3+raw S0-S4）OSD 全场景≥57.5=**第一轮测错了模式**。根因仍是 f53d6dc 移植 speed_step 漏了上游 `_preview_step_plan` 的 cap=3 硬帽（R1 parity 缺口，上游注释自证"stutter storms"史）。修复候选：**A=cap parity（F2 实证 EDL 稳态有效，推荐；parity 细节：上游 cap 作用于 preview_step×biz 乘积，UI≥2x 时现行引擎 base×step 无帽也是缺口，需 parity 表先行）**；A+=EDL 模式业务加速策略再议（裁剪本身已是快进，10x 扫保留区意义存疑——产品决策）；C=EDL 事件线程减负（`source_frame_for_virtual_time` Fraction 线性扫段 210µs/call@450ev/s）；B=OSD 业务感知降为可选（读数本就诚实）。零代码缓解=取消勾选「0.2x 区域加速」或 factor 降到 3。副产物：frame-drop-count **seek 时重置**（跨 seek 差分可为负，E3/F2 实测）。三探针+数据登记 ASSET §四/§六/§八/§九(v2 含"假说空间必须先枚举运行模式"教训)。修不修、怎么修、是否与 C+/选项2 同批验收——**全待 owner 口令**。
0b. **【同日再追加】真 10x 提案 P1（owner 否决 cap=3 为「牺牲效果」后调研+实测定案）**：①运行时 trick-play 判死——`vd-lavc-skipframe=nonkey`（HLS I-frame 流原理）在保留段中位 11 帧 << GOP 120 的几何下段内无关键帧可解：probe_biz_edl_skipframe G2（建引擎即开）rt=0.0、>100ms 停顿 85 次、EOF 空转 12 轮；G3（运行中动态切）选项设置成功但恶化（rt 4.8→2.3、停顿 4→45、tp 倒退废掉 OSD 窗）；G1 复跑基线=F1(min 14.8)。②**连续文件 speed=10 本机干净**（E2：rt 9.978、OSD 恒 60）→ 真 10x=消灭拼接边界=物化。③调研（主会话串行，gh api+WebFetch；auto-editor=export-first 无拼接预览 / LosslessCut=源文件 seek 定位+全文件低质代理先例+smart cut 只重编码切点 / HLS RFC8216 trick-play=预物化 I-frame-only 子流 / mpv EDL 官方文档=切段必回源 seek 无预缓冲（格式固有）/ NLE render cache=物化+段级增量（T1，Adobe·Wikipedia 本轮网络超时未拉到原文））——**成熟项目无一做实时拼接段高速播放，答案一致=物化**。④**P1=span 级渲染缓存代理**：`intervals_for_plan` 精确段 → ffmpeg trim 帧精确 GPU 重编码（`_pick_gpu_encoder` nvenc/qsv/amf 实测探测现成，可选 480p 提速）→ concat 连续代理 mp4；帧精确+同 fps ⇒ **虚拟时间轴≡EDL ⇒ 映射/引擎代码零改动**，`play_edl` 播代理即连续 10x；缓存键同 `_edl_cache_key` 款（源sha+plan指纹+代数）+段级增量重建（Resolve smart cache 式）+`task_manager` 异步防抖；代理未就绪回退现行 EDL（P0 cap 地板生效）。验证：代理帧数==`CertifiedEdl.expected_frames`+认证账本像素抽查。风险：编码映射漂移（帧精确管线+计数验证兜底）、构建时延（3.mp4 保留 21.3s 估 ~10s 级，2.mp4 取决于保留占比；异步+增量缓解）、暂停检查语义不变（C4/verified seek 仍从源渲染）。**P0 地板=cap parity 独立先修**（回退路径+R1 缺口+UI≥2x 乘积无帽缺口）。P0/P1/P0+P1 全待 owner 定夺+「开工」。登记：ASSET §六 2 行（skipframe 实证/调研）、§八 P1 提案行、checklist 同步。
0c. **【同日第三追加】P1 编辑感知修订+端到端实证（owner：段表=活编辑文档，随时删段/拖两端改长度，本质改视频长度）**：物化方案改为**双层预览**——Tier1=live EDL+cap3（P0 地板，编辑后零延迟可播）；Tier2=**区段级代理缓存**（`_export_pts_video_batched` 分批文件=缓存单元，认证 tick 键控）。probe_proxy_skim（T0，3.mp4 真实 73 段/1276 帧，全生产代码复用，NVENC 实探可用）：首建 9.2s(分批)/15.0s(单命令)→1080p 32MB；帧精确 written=1276✓、时长 21.267s≡EDL 虚拟时间轴精确一致✓（PyAV 计 1277=已知末端哨兵保护帧，验收闸门用 written+duration，可复用 `_verify_pts_export_container`）；**代理上真 10x：OSD 36/36 窗恒 60.0、tp 停顿>100ms 零**（对照 EDL 直播 10x：min14.4、94%窗<50）；**编辑增量实测：删段=concat copy 重拼 297ms 零重编码帧数精确；拖边界=单区段(8段)重编码 2.07s**。拖动即时反馈不受影响（暂停定位走源路径 21-90ms）；代理=导出管线副产品（同 schedule 同 tick）→预览与导出帧级一致。开工设计点：2.mp4 规模**自适应分批**（间隙<回填成本合批，防全片解码——3.mp4 单命令 15s>分批 9.2s 已示警）、GPU 解码加速候选、480p 降档候选、磁盘 1080p≈1.5MB/保留秒、代数/revision 新鲜度守卫（_edl_cache_key 同款）。登记：ASSET §六第3行、§八 P1 修订行、checklist 同步。仍全待 owner 定夺+「开工」。
0d. **【同日第四追加】owner 反感提前写硬盘 → P1-RAM 纯内存掠过实证成功（真 10x，零磁盘写入）**：串行调研（owner 授权）+三轮探针。①**seek 率墙定理**（实测数推定）：实时读源方案封顶 ~5-6x——每遍掠过需 73 次源内 seek×30-80ms（mpv EDL 与上游 OpenCV cap.set 两引擎同墙），trick-play/demuxer缓存/hwdec 各路已证伪或不足 → 真 10x 必须段内容**预驻留**（RAM 或磁盘）。②先例：After Effects RAM Preview（帧渲染进内存实时播、编辑失效、RAM 预算封顶——T1 公认，本轮 adobe/wikipedia 网络不可达未拉原文）；LosslessCut 低质代理；HLS 物化 I-frame 流；auto-editor export-first。③实现路径实证：libmpv 官方 `mpv_stream_cb_add_ro`（mpv/stream_cb.h，ctypes 绑定，**python-mpv 构造后注册 rc=0 可行**）自定义 `ram://` 协议直接吃 bytearray；媒体=生产 filter 图（-ss/-copyts/select/setpts 认证 tick 精确）**单命令全局 PTS 轴** GOP2 无B帧 MPEG-TS→stdout→RAM（NVENC 本驱动拒 GOP=1：严格校验 GOP>B+1；3.mp4 73段/1276帧：**构建 6.2-6.6s、89.2MB=4.2MB/保留秒**；480p 变体 38.5MB）。④播放实测：speed=10 整遍 EOF 2.33s=**有效 9.11x**、OSD **8/8 恒 60.0**、**tp_end=21.267s≡EDL 虚拟全长精确一致（PTS 保真→映射零改动成立）**、重播同净；唯一疵=每 load **0.19-0.23s 处一次性 ~0.2s 启动探测停顿**（流中段零停顿；预热=预 loadfile+暂停待命可规避）。⑤流内 seek 落点不准（TS 无索引：seek2.0→落7.78/611ms）→ 精确停顿/定位走源路径（双层设计本就如此；若需流内精确 seek→RAM 内 MP4 faststart 变体=设计点）。⑥**证伪三项**：命名管道喂 libmpv（不连接 \\.\pipe\，file-loaded 25s 超时×3）；TS 批块字节级拼接（批间 PTS 不连续→时间轴塌缩 6.9s+350ms 周期停顿；增量重建须 `-output_ts_offset` 或 remux）；小探测参数 demuxer-lavf-o probesize/analyzeduration（此构建 load 超时，不用）。⑦编辑代价：删段=弃 RAM 块 ~0ms；拖边界=重取单段 ~1-2s；重分析=全量预取 6.6s（3.mp4；2.mp4 需自适应分批设计——单命令 6.6s<分批 9.2s 的规模反转点待测）；**会话重启=重新预取（易失性=零磁盘的代价；期间 Tier1=live EDL+cap3 兜底）**。RAM 足迹 2.mp4 规模估 ~630MB(1080p GOP2)/~270MB(480p)；大 GOP/降档=开工调优点。P0 cap 地板不变（预取未就绪回退+R1 parity）。探针：probe_skim_ram_pipe（管道证伪）+probe_skim_ram_streamcb（成功路径，结果 json 留盘）。登记：ASSET §六第4行、§八 P1-RAM 行、checklist 同步。**P1-RAM / P1-磁盘 / P0 单独——全待 owner 定夺+「开工」**。
0e. **【09-08 追加】P1-RAM 足迹/天花板/规模三问实测**（probe_ram_footprint,T0）：①足迹（MB/保留秒）：1080p GOP2=4.20/GOP30=2.42/480p GOP2=1.88/**480p GOP30=0.68**（构建均 ~6.5s 与档位无关；2.mp4 内容 GOP30=1.21，内容依赖±2x）；②天花板（1080p GOP30+`cache=no`）：10x 干净 OSD 恒 60，16x/24x **饱和 14.3x=软解墙 OSD 塌**→factor≤12 或 480p 提顶（估>30x 未测）；③**暂停态流内 seek 精确**（5.0→5.0/810ms）、运动中不可靠→生产=先暂停再定位；④`cache=no` 必须（否则 demuxer 缓存复制≤150MB）；⑤**2.mp4 规模病理=开工首要诊断**：逐段独立 -ss median 42s/max 82s（64段 2684s 不可接受），嫌疑=探针合成段表单调化 tick vs -copyts 原始流时间在重复PTS源漂移；生产走 intervals_for_plan+自适应分批；hwaccel cuda 亦超时未定因；首建估计单命令 ~3-6min 一次性(T2)。owner 三问报告已交付（UX 时间线/占用公式表/诚实缺口清单：冷窗口 Tier1=cap3、>14x 解码墙、播放画质=重编码 cq22（暂停即回源级静帧）、易失重开重预取、RAM 预算管理）。ASSET §六新行+checklist 同步。后台进程核查：30976/12044/20324=owner 自有程序非遗留。
0f. **【09-08 再追加】「零预存真10x」调研+实测定案=物理不可达**（owner：不依赖内存不依赖硬盘、不考虑成熟度只看效果）：唯一非物化路线=**并行预取管线**（N 解码实例分摊段回填；先例=VapourSynth 帧级并行帧服务/DASH 并行分段拉取/游戏资产流送池——均受同一解码账约束；mpv 社区无密集段 EDL 先例；NLE/HLS/YouTube 高速掠过全走物化）。probe_parallel_refill 在 2.mp4 真源实测（生产语义最优 seek）：**回填税**=seek 落前关键帧→段首 avg 81/max 121 帧（GOP=120）、单 worker 159ms/段——稀疏访问把解码账单乘 (回填+段)/段≈5-6 倍；10x 需 34.3 段/s≈**3300 帧/s** 1080p 高码率解码+34 seek/s，机器软解总吞吐 ~500-1000 帧/s → **sw K=1/4/8=5.0/14.0/16.5 段/s=掠过上限 1.5/4.1/4.8x**（K=8 refill 159→279ms=全核饱和，GUI 同跑）；**GPU nvdec 不扩展**：ffmpeg 单进程多输入 N=1/4/8=2.4/3.4/3.8 段/s（上限 1.1x）、每段进程模式被 CUDA init 300-450ms 淹没、但 8 并发**无会话墙**（16/16 ok）。对照锚点：连续文件 10x 可行（E2 rt9.978=顺序解码 600 帧/s）→ 墙的本质=稀疏性×回填税，非成熟度。**反讽**：8 worker 活管线瞬态 RAM ~0.8-2.4GB（解码工作集）> P1-RAM blob 14.5-100MB——「不依赖内存」的活方案实际更耗内存，只是不预存内容。残余杠杆（T3 未测）：`skip_frame nonref` 回填省 30-50%→估 ~7-9x；nvdec 持久多会话扩展性存疑——全拉满至多边缘 10x 且 CPU/GPU 满载、复杂度巨大，效果仍逊 P1-RAM（已证真 10x+掠过仅 ~1 核）。**效果排序定案：P1-RAM ★5 > P1-磁盘 ★5(跨会话,但写盘) > 活并行管线 ★2(4.8x 上限+全机满载) > P0 地板(3x)**。probe_parallel_refill(+p34) 留盘；ASSET §六新行+checklist 同步。仍待 owner 定夺+「开工」。
0g. **【09-08 压缩后「开工」实施：P1-RAM + P0 地板（Tier1）已落地，全绿+真机 E2E 8/8】**：
   - **B0 病理改判（probe_b0_2mp4_build + probe_b0_tlimit，T0）**：0e 所记「2.mp4 逐段 -ss 病理」= **旧探针设计缺陷，非生产路径问题**。机理：`tpad=stop_mode=clone` 尾克隆要等**输入 EOF** 才发出，而 select 只在窗口内传帧 → 带 clone_guard 的命令窗口结束后仍解码到文件尾（耗时∝段到 EOF 距离/≈2200fps：首段 82s/中段 42s/末段 1.5s 线性吻合；旧探针每段都 last=True）。尸检补了旧探针盲区：输出帧数全部正确（晚段 11/11、段0 13/13）——窗口一直匹配。**Δ=0**：4 位置 showinfo 地面真值 ↔ 账本 tick 完全一致（T=0 锚点无对齐歧义；head 异常=已裁决的帧1-5），「单调化 tick 漂移」嫌疑证伪。生产语义健康：批块（克隆只挂最后批+`-frames:v` 提前停）8 段 11.0s；**单命令全量 85.3s/9.6MB/1266 帧/21.1s≡虚拟**（帧数时长双精确）→ 2.mp4 规模首建定案 ~85s（原 T2 估 3-6min 过于悲观）。**-t 输入截断**实证消掉尾克隆 EOF 税（同窗口 11.5s vs ~80s）→ **生产命令形态定案**：`-ss(首段-1s) -t(尾段+1.5s余量) -copyts -i … select/setpts/tpad+scale … -frames:v kept+1 + setts哨兵 -f mpegts pipe:1`。
   - **B1 核心**：新 `skim_cache.py`（420行）：`build_command` 纯函数（测试可断言命令形态）+ `build_skim_blob`（communicate+看门狗线程：cancel 0.3s 粒度杀 ffmpeg；验证门=TS同步字节+帧数==kept+1+时长≡压缩轴±2%+预算）+ `build_skim_blob_auto`（BUDGET_* 降档阶梯）。`mpv_engine.py`（+289）：stream_cb ctypes 迁移（`_SKIM_ENGINES` id→engine 强引用注册表；open 回调快照 blob 引用，回调内禁 mpv API/禁引擎锁；keepalive 列表保活）；**`canonical_load_target`**：带 scheme URL 旁路 `Path.resolve`（否则 ram:// 被揉碎 → file-loaded 路径校验永失配 → pending seek/resume 卡死，实装前发现的暗雷）；`_ensure_loaded_locked(file_options=)` per-file `cache=no`（只作用 ram:// 装载，源/EDL 行为零扰动）；`_skim_id` 自增代号入 URL（blob 换代强制重载）；两级切换=play_edl 内 `_skim_eligible_locked`（键前4元=源sha×plan指纹×gen×rev，同 EDL cache_key 构型）→ Tier2 `ram://skim<id>` mode="skim"，否则 Tier1 EDL；**cap parity**：`_biz_speed_cap`（Tier1=request.preview_step_cap（GUI 一直在发 3，f53d6dc 引擎漏接）作用于 base×biz 乘积+UI 速率；Tier2=_SKIM_SPEED_CAP=12.0）经 `_capped_speed_locked` 进 play/play_edl/_apply_biz_speed 三点；skim 模式对齐 `_frame_for_position`（虚拟时间映射≡EDL，blob PTS 保真已证）/`seek_source_verified`（拒→回源路径）/`snapshot_perf` bias 三字段；close() 释放 blob+摘注册表。`preview_player.py`（+176）：`_run_skim_build_task` 工作函数 + `_maybe_schedule_skim_build`（守卫链全 getattr 防御 legacy 桩：引擎 skim_ready/账本认证/skip_trimmed/设置开关/有删除段/键匹配/在飞同代去重；submit kind="player.skim_build" scope 绑定 replace=True；on_success 装 blob 带源当前性守卫）+ 触发点=_send_play（兜底）+_finish_analysis（预热）。
   - **B2 编辑感知**：`_publish_project_snapshot` 是全部段表变更唯一漏斗（编辑命令/分析/加载恢复）→ 尾部 `_arm_skim_rebuild()` debounce **1.2s**（Tk after，重复 arm 顺延）→ due 时 `_maybe_schedule_skim_build`；编辑播放中=现行 `_send_play` 重启语义不变，旧 blob 键失配自动 Tier1，重建完成下一次播放回 Tier2（流中换代安全：open 快照引用）。关闭清理：invalidate(_SKIM_TASK)+after_cancel 列表+_skim_build_head 置空。
   - **B3 预算/降档/开关**：`SKIM_SPEC_LADDER`=480p→360p→240p（GOP30/q6 不动，分辨率对足迹杠杆最大；仅 BUDGET_EXCEEDED/BUDGET_ESTIMATE_EXCEEDED 降档，取消/其他错直通）；预算预筛=实测 MB/保留秒 表×1.5-2 余量（480p30:1.5 / 1080p30:4.5），max_blob_mb=400 默认；设置面板「基本」页新勾选**「快速掠过代理（内存版真倍速）」**默认开（get_params['skim_preview']）。
   - **验证**：pytest **495 passed + 90 subtests**（基线 467+90 → 新 28：test_skim_cache 12 + test_preview_skim_rebuild 9 + test_mpv_engine 新3改1——原「factor5→speed5.0」断言按 parity 更新为 3.0 + cap=10 请求反向断言 5.0）。**probe_skim_e2e（真 libmpv+真 NVENC 构建+真 MpvEngine 生产代码，headless vo=null）8/8 判据过且复测一致**：R1 注册 rc=0；构建 8.3s/14.5MB/1277帧(1276+1克隆)；Tier2 mode="skim" **speed 实达 10.0**、tp_end=21.267≡虚拟、EOF 1.9s、**drop 0.0%**、重放同净；Tier1 清 blob 后 mode="edl" **speed 恰封顶 3.0**。per-file cache=no 被此构建接受。
   - **遗留/后续候选**（非阻塞）：增量重建（remux/-output_ts_offset）未做——全量重建 8.3s@3.mp4、85s@2.mp4 规模，debounce 已把连续编辑收敛为一次，收益待 owner 实测体感再定；预热 loadfile（消 0.2s 启动探测停顿）未做——E2E 未见可观测停顿；设置面板 factor spinbox 上限仍 20（引擎 12 守卫兜底，UI 层钳制可加）；OSD 角标未显示 Tier 状态（可加「掠过」标记）。**GUI 已重启载新码**，待 owner 手检：旧两项（§4.1）+ skim 三场景（删段/拖端点/重分析后高速播放：控制台 [skim] 日志见重建，播放应自动 Tier1→Tier2）。
   - **自查 review 轮（同日，owner「Review一下有没有引入新的问题」）——修出 5+2 项，最严重一枚是本轮实施自己引入的**：**F1** `seek_edl` 在 skim 播放中会强切回 EDL 文件（GUI 时间轴拖动/箭头步进共用路径）：丢 Tier2 之外更糟的是 **Tier1 活流继承 skim 的 12x 上限（cap 只在 play 请求时钉）→ 业务 10x 拼接边界风暴原样复活**。修=skim 模式留在 ram:// 流内寻址（播放中先落暂停→精确 seek→恢复；暂停态落点精确 T0 背书 5.0→5.0）；blob 被清的防御回落分支把 cap 复位 `_TIER1_DEFAULT_CAP=3.0`。**F1b** `_command_seek_locked` 的 +5/120 暂停修正对 skim 跳过——该修正是源/EDL PTS 边界"陈旧帧"病的实测补偿，blob 均匀时间轴带上反而过冲 ~2.5 帧（41.7ms=2.5×16.7ms）。**F2** 源模式撤 cap：原实现把源模式也封 3=**回归 f53d6dc 既有能力**（UI 4x、连续文件业务 10x 均 T0 干净：E2 rt9.978/probe_biz_osd_engine 源模式 OSD≥57.5；风暴只发生在拼接流）→ cap parity 收窄至 EDL Tier1；测试断言恢复 5.0+新增 EDL Tier1 cap=3.0 用例。**F3** 构建前源新鲜度守卫（size+mtime stat 比对，引擎 bind 同语义；认证后源被换→SOURCE_CHANGED 拒建，防静默错位产物；刻意不用 source_is_current 全量 sha 重哈希=多 GB 源卡数秒）。**F4** 预算预筛改账本 tick 精确保留秒（原 kept/60 假设 60fps 源，30fps 源低估 2 倍）。**F5** `seek_source` 清 cap 残留——模式不变量定案：**source=None / edl≤Tier1默认3 / skim=12**，6 处赋值点 grep 审计一致。**F6** `_schedule_for` 裸异常包装 SkimBuildError(EMPTY_SCHEDULE/PLAN_MISMATCH)。**审出无问题项**：流畅度分档与角标 FPS OSD 均基于显示节奏（rt=墙钟/呈现帧，skim 10x 不误报）；GUI 无 engine-mode 字符串依赖；step_frame 在 blob 上=步进保留帧（语义正确，与 EDL 步进一致）；引擎 per-source 重建→blob 无跨源泄漏；keepalive loadfile 级增长+close 清理；E2E 新增 seek-mid 阶段实证 F1（mode 留 skim/恢复播放/落点窗内/EOF 净）。**已知限界（登记不修）**：_calib_expected_rate 校准工具硬编码 cap3（Tk 诊断标签，Tier2 下期望值误报，不影响播放/角标 OSD）；播放点击触发的构建与 Tier1 播放有 ~8s CPU 竞争窗（checklist 已注"等就绪再评判"）；communicate 双缓冲峰值=2×blob（预算 400MB 最坏 800MB 瞬态，典型几十 MB）；UI 4x×业务 factor10=源模式 40x 解码墙受限（f53d6dc 既有行为，非本轮引入）。**测试 495→498+90 全绿（新3：EDL Tier1 cap/seek_edl 保 skim/SOURCE_CHANGED）；probe_skim_e2e 扩为 10/10 判据两轮一致；GUI 已再次重启载修复后代码（此前实例跑的是 review 前代码——手检必须用当前实例）**。
0h. **【09-08 owner 实测反馈轮：「还是掉帧 35-45」+「怎么判断 0.2 加速 10 倍生效」→ GUI 日志现场取证，修出 G1-G4】**：owner 会话加载的是 **4.mp4（2 小时级源，L=424176 帧，pause_boundary_records=2210=千段级几何）**，GUI 控制台抓到构建**秒崩**：`AttributeError: 'TaskContext' object has no attribute 'cancel_event'`（×2 次）→ 全程 Tier1，owner 看到的 35-45 是 Tier1 在超密几何下 3x 的边界成本（真实但预期的回落行为，非风暴复发）。**G1**：TaskContext 只暴露 `cancelled` 属性/`checkpoint()`，不给 Event 本体——worker 改传 `lambda: context.cancelled`，skim_cache cancel 参数归一化兼容 Event/callable（新测试）。**G2（若无 G1 也会第二个炸）**：Windows CreateProcess 命令行上限 32767 字符，千段级 select/setpts 表达式内联必超——probe_skim_scale1000 实证 1000 段内联 ≈**57099 字符**；改 `-filter_complex_script` 临时脚本文件（生产分批导出同款；几 KB 文本非媒体写盘，用完即删），1000 段真实构建 **21.7s/25.8MB/3001 帧全对**。**G3**：预算估算表缺 360/240p 档→落入 4.5 默认值=比 480p 还严，降档阶梯被预筛闸卡死形同虚设——补 (360,30):0.9/(240,30):0.5 等。**G4（回答 owner 第二问）**：角标 OSD 加档位+实时速率标签——`掠过10.0x | 60.0 FPS`（Tier2，数字随 0.2x 段进出在 10.0x↔1.0x 切换=业务加速生效的直接可视证据）/`保底3.0x |…`（Tier1 封顶）/`源…`；引擎 snapshot_perf 新增 speed 字段。验证：**pytest 500+90 全绿**（新增 graph_file 形态+callable cancel 用例）；probe_skim_e2e **10/10 三轮一致**（含 script 文件路径）；probe_skim_scale1000 留盘。GUI 已三度重启载 G 修复。**4.mp4 规模预期（已告知 owner）**：首建=保留段跨度全解码，2 小时级源估 **3-6 分钟一次性**（T2 推断，待 owner 现场出真数）；构建期间照常可播（保底 3x）；等控制台 `[skim] 掠过代理就绪` 后再看角标应显「掠过」。保留量大时可能触发预算降档（控制台可见），480p 每保留分钟约 27-41MB。**新疑点登记**：4.mp4 千段级几何下 Tier1 3x 仍偶见 35-45（F2 的 cap3 稳态结论来自 3.mp4 的 73 段几何，段密度↑→边界率↑→3x 也可能偶跌）——Tier2 就绪后应消失，若就绪后仍跌=新问题需再取证。
   - **第二轮 review（同日，owner 再「Review一下有没有引入新的问题」）→ 再修出 2 项**：**G5** 设置开关「快速掠过代理」只挡新构建、**不清已装 blob**——取消勾选后播放仍按代数命中旧代理走 Tier2（手检 e 项「关开关应永远 3x」必挂）→ `_send_play` 参数读取后加门：开关关=`engine.clear_skim_blob()`（幂等、防御式）。**G6** 预算覆盖风险：400MB 默认 + 预估 2-3x 余量 → 480p 只覆盖 ~4.5 分钟保留内容，owner 两小时级项目保留量稍大即**三档全拒、skim 永不就绪**（控制台只会见 BUDGET_ESTIMATE_EXCEEDED）→ `max_blob_mb` 默认 **800**（覆盖 480p≈8.9min/360p≈14.8min/240p≈26.6min；communicate 双缓冲瞬态峰值≈2x 实际 blob，已登记文档字符串）。**审出无问题项（二轮）**：build_skim_blob try/finally 变量作用域正确（成功路径 blob/kept 在 try 外可用、异常路径 unlink 先行）；watchdog kill 与 communicate 自身 timeout 双路径 TIMEOUT/CANCELLED 判定互不吞没；`_apply_native_perf` 的 perf 即 `snapshot_perf()`（mode+speed 齐）；cv 引擎 perf 无 mode → 无标签向后兼容；snapshot 内 speed 属性读与既有 frame-drop-count 读同类（锁内属性读有先例，libmpv 核心线程处理 get 不被事件回调阻塞）；OSD 内容帧率口径在 UI4x×skim 下钳 240=既有公式语义非新引入；temp 脚本文件 unlink 在进程退出后（Windows 无删除竞争）。**pytest 504+90 全绿**（新增：角标标签 2 + 开关清库门 2）；GUI 第四次重启载本轮修复。
   - **owner 现场第二轮取证+修复（G7）**：owner 导入 4.mp4 完成 A_PT 分析后问「预览多久能从保底 3x 变 10x」——读 GUI 控制台日志取证：构建**被预算预筛拒绝**（`estimated skim blob 895MB exceeds budget 800MB before build`，×6 次），非 AttributeError。反推 owner 项目保留量 ≈ **597 秒 ≈ 9.9 分钟**（895÷1.5 预估费率）；预估余量 2-3x 意味着实际 blob 大概率 ~300MB@480p 本可入库。**修复**：`max_blob_mb` 800→**1200**（480p 覆盖≈13.3min/360p≈22.2min/240p≈40min；communicate 瞬态峰值≈2x 实际 blob，极端大项目最坏 ~2.4GB 瞬态后释放）。pytest 504+90 复跑绿。**owner 现场预期（第五次重启后）**：载入 4.mp4 → 快照发布 1.2s debounce 后自动开拼（或首次点播放触发）→ 4.mp4 首建估 3-6min（T2，跨度全解码）→ 控制台 `[skim] 掠过代理就绪` + 角标变「掠过10.0x」= 升级完成；期间角标「保底3.0x」。**证明链**：owner 的「还是掉帧」= 全程 Tier1（构建从未成功），角标标签+日志是双证据通道。
   - **上游对比总表·修订版（owner 纠正基准：对比对象=带 A_PT 的上游克隆①，非旧基线）**。**本地上游资产盘点（owner「先找出来我来决定」→ 已定夺）**：克隆①=`D:\ArknightsPathFinding\arknight-auto-editing-upstream`（origin=liemark，HEAD ce10360 2026-07-23「Merge PR #9 from **kouekikin24**/feat/optional-ffmpeg-apt-boundary-context」，A_PT 标记 34 处）；克隆②=`D:\1bt\arknight-auto-editing-main - 副本`（remote 名 upstream=liemark，HEAD e91ef26 2026-05-22 Merge PR #2 音频，A_PT=0，工作区含未跟踪实验残留）；另 1bt(1)=07-08 无 remote 快照仓、1edge=纯拷贝、v26.7.15/23-win=exe 发行版（非源码基准）。**身份关键**：主仓 remote origin=kouekikin24(fork)+upstream=liemark——**A_PT+跳二遍=本工作区产出、以 PR #9 合入上游**（我方 dc6259a~52b7aef ↔ 上游 e9f4de1~b58bce0 消息逐条对应）；但**上游默认后端仍= "OpenCV（默认）"**（settings_panel:70 实查），A_PT 要手动选；我方 fork 默认= "FFmpeg软件 A_PT（默认）" 且 8 月 PyAV 整合后再迁 PyAV（OpenCV 后端冻结）。**修订后对比（4.mp4=1080p/60fps/424k帧/117.8min）**：①导入 cv2 0.07s(实) 两边同；②认证 上游无 vs 本 fork 首建~3-5min(T3)/缓存秒级(4.mp4 已缓存 08-17)；③分析 **能力同源不同默认**：上游开箱=OpenCV 两遍≈62min(T2 折算, pass1 129fps 实测+pass2 grab 1884fps 实测)，手动切 A_PT≈7.6min=**与本 fork 同量级**（本 fork 默认即 A_PT/PyAV，1835fps 实测，锚=样本3 32s/8min）；④可预览 上游分析完即播但 OpenCV 顺序喂帧名义 3x 撑不住("stutter storms"注释出身) vs 本 fork 立即播保底 3x 稳 60(F2/E2E 实)；⑤真10x 上游**无此能力**(任何版本) vs 拼装 3-6min(T2)+验证 12-35s(1418fps 线性)一次性；⑥定位 cap.set 30-80ms VFR 漂移 vs verified 21-90ms 帧精确(T0)；⑦播放中拖轴 回源跳变 vs 流内 0.15-0.8s 无缝(实)；⑧编辑后 即播(无物化) vs debounce 1.2s+全量重拼(4.mp4 级分钟级)；⑨冷启动到分析完 上游默认 ~62min/选 A_PT ~7.6min vs 本 fork ~7.6min 默认直达；到真 10x 上游不存在 vs ~12-15min。**结论修正**：对克隆①，分析阶段优势=「默认即最快路径+后续 PyAV 整合」而非 8x 代码差；预览侧全部差异（认证/EDL/skim/verified seek）对任何上游版本成立。probe_upstream_decode_bench 留盘。
   - **分阶段计时补测（probe_skim_phase_timing，owner 问「剪完存入内存要时间吗」）**：1000 段/3000 保留帧/50s/25.8MB 实测——段表推导 **0.1s** / ffmpeg 拼装 **16.6s** / PyAV 逐帧验证 **2.1s**（~1418 帧/s@480p，随保留帧数线性）/ **入库=引用交接 2µs（零成本）**。产物由 ffmpeg 直接流进内存，无独立"拷贝存入"步骤。**新优化候选**：验证门改包计数（demux 不 decode，`-c copy` 语义/PyAV packets）可把验证段再压 ~10x——4.mp4 级保留 17k-50k 帧时验证 12-35s 值得做，登记遗留候选。4.mp4 全链参考：导入(认证已缓存,秒级)→分析(owner 已跑过,时长其自测)→分析落定**立即**自动开拼→拼装 3-6min(T2)+验证 12-35s→「掠过」就绪；期间任何时刻可播（保底 3x 零等待）；编辑=1.2s debounce+全量重拼（4.mp4 级又是分钟级——增量 remux 候选的权重上调）。
0i. **【09-12 破墙调研定案：解码直通(packet-skip)路线 E2E 实弹通过——Tier2 改判依据】**：
   - **触发链**：owner 现场复测成功半程——分析干净（424176 帧/`skip_second_scan=True`/**Errno 22 未复现**）+ skim 首建成（**294.1MB/107433 帧/386.3s**）→ **owner 否决：240p 画面糊不能接受**。糊的根因拆解：估算表悲观 3x（4.mp4 实测压缩率远好于静态表）+ constqp qp6 接近无损级浪费（**vbr-cq26 同分辨率体积 1/3**——480p 实际 ~1.2GB 本可入预算）。G7 反推修正：895MB 被拒估算=**240p 档估算**（1790s×0.5），保留量实为 **29.8min** 而非当时推断的 9.9min。更好方案调研（串行子代理**首轮被套餐并发额度杀×2** → 主会话亲查 T0 实测）→ owner「重点是别卡不是清晰，溯源」→ 溯源六方案族谱（ seek 墙只约束 seek 系）→ owner「打破物理规律，允许串行子代理，考虑换语言，flclash 代理」→ **新假设：顺序解码+输出过滤（零 seek）绕开 seek 墙，天花板=解码吞吐**。
   - **探针链（全 T0 本机）**：①lavfi-complex 分支：机制通（quoted 图/虚拟轴重映射），三坑=setpts 无 `t` 变量（sgn 函数也不存在，改 PTS 时基式）/O(K) 表达式单线程 eval 毒药（64 项 min/max ≈3.4ms/帧 → 2.0x；**N 计数 `setpts=N/(60*TB)` O(1) 解**）/双钟失配（音频源钟 vs 视频虚拟钟 → 13s 停摆 → 预览静音即解）。②生产 PoC（真窗 vo=gpu+64 段图）：虚拟 EOF 精确（300.78/300.8）、起播 373-398ms。③**最终架构转向：PyAV 包级跳过解码线程 → stream_cb rawvideo（CFR fps 参数=虚拟轴天然成立）→ mpv 渲染**——比 lavfi 分支干净：包过滤/虚拟轴在循环内做（bisect O(logK)），无图无 seek 毒药无双钟。④正确性：**包级跳过+锚点关键帧恢复 vs 干净解码 max-mad=0.0 位级一致**（间隙包根本不喂解码器，锚点=关键帧=干净重启）；旁证：PyAV `skip_frame='NONKEY'` 不生效（mad=0 平凡真，弃用）；mpv `vd-lavc-skipframe` 运行时切 default↔nonkey 冒烟无崩（选项值只认 `default/nonkey`，`no`/大写拒绝）。⑤吞吐：冲刺上限 **2160fps（36x 源，7.3 核）**；纯 Python 循环 281-447fps（逐包开销）→ E2E **3.8x 虚拟 @1.39 核**；**10x 虚拟需求=混合负载 ~750-825fps → 编译循环余量 2.4 倍**。⑥文献（串行子代理成功一役，738k tokens）：lavfi 语义 T0（per-file loadfile 第 4 参自动恢复/framedrop=vo=官方明文「still decodes and filters all frames」/seek+图重建毒药 #16333 open@0.40/hwdec 需 vo=gpu）；**NVDEC Ada 官方 903fps < 本机 CPU 软解 1835-2160（硬解无优势反转，vo=null 下 nvdec 根本不启动）**；业界零先例（T1 负结果：mpv 社区全走 seek 类脚本/NLE 无 decode-through/HTML5 playbackRate 16x=大规模先例）；Rust 兜底=ffplay 骨架+SDL3。⑦三个我方工事坑修复录：stream_cb open 回调签名漏 uri 参（`user_data,uri,info` 三参）/bytearray 头删 O(N) 拖死管线（改分块队列）/漏设 speed。rawvideo 语义：I420 fourcc 可用、size=-1 非 seekable、EOF=read 返 0 须显式 done。
   - **定稿（owner「按推荐执行」+「哪个阶段 rust」修正后）**：**Tier2 改判=解码直通；一期 1a=Rust 解码服务**（cargo/rustc 1.97.1 已装；ffmpeg dev 库走 flclash 127.0.0.1:7890 下载 shared dev 包）**+1b 引擎三级模式（≤3x 源/EDL 原画质，>3x 直通）+1c GUI 分档接管+1d 测试门**；skim RAM blob 退役休眠；物化=重剪素材备胎；PyAV 原型留作接线自测对照。E2E 记录：4.mp4 合成段表（64 段/1200s 窗/保留率 25%≈真实统计）298.8/300.8 完整/237ms 起播/停顿 752ms/I420/零存储零构建；真实段表接线=1b/1c 工作项。**参考实现=probe_dthrough_e2e.py 可直接移植**（分块队列/三参 open 回调/rawvideo 选项/EOF 信号全在）；格式 **I420 已证、NV12 fourcc 未测**（1b 落地时验证或径用 I420）；推流端节流**省带宽不省解码**（10x 段仍须全帧解码供参考链，730-750fps 需求不变）。探针（.cache/v3ass/）：probe_proxy_spec/probe_shader_embed(+anime4k/)/probe_decode_through/diag_g64/diag_rawpush/probe_dthrough_prod/probe_pskip_ab/probe_skipframe_hybrid/probe_dthrough_e2e。
   - **造轮子检查（owner 令，09-12 深夜，子代理再遭额度墙×2→亲查，中期判定）**：自家库存盘点（stream_cb 绑定/关键帧 npz[4.mp4 表现成]/包级迭代先例[analyzer]/取消与防抖框架/内存流管线——全现成，真正新造=Rust 循环本体+引擎/GUI 胶水）；**绑定选型 T0：rsmpeg 0.18.0+ffmpeg.8.0（2025-08 活跃，larksuite，178k 下载，有 `ffmpeg7_1` feature 直接对口我们 7.1.0，system/vcpkg 链接）**；备选 ffmpeg-the-third 6.0.0+ffmpeg-9.0（2026-08 活跃，113k 下载，版本偏新需验 7.1 兼容）；**GES/MLT 整体替换=避免**——GES nlecomposition 跨段=激活/停用 nlesource+源内媒体时 seek（search T2；源码直证 pending nlesource.c），MLT 按位拉帧同构 → 撞 EDL 同款 seek 墙；**包级跳过业界零先例结论再获佐证**（框架全走 seek 系）→ 自造部分确属无人造过；逐段倍速先例=自家 _biz_speed（f53d6dc）+MLT timewarp 佐证常规性。待补：nlesource.c T0 直证/ac-ffmpeg/NVR 智能回放先例（SA-B 批，额度恢复后子代理或继续亲查）。
0j. **【09-12 造轮子审计完成态（上一条 0i-末并行条目的收口；owner 令串行子代理不限量+本地 T0 并行）】**：
   - **本地 T0 两发（probe_1a_wheels）**：cv2 grab/retrieve 路线推流仅 **181fps**（10x 需 600）✗；PyAV 优化循环（int tick+平面 bytes 直取）demux 天花板 **1457pkt/s < 10x 需 2400**、解码 440fps、**虚拟 6.2x @0.98 核**（CPU 闲置=逐包胶水结构性开销非算力）→ **纯 Python 判死，Rust 必要性=实测结论非偏好**。
   - **Agent1（Rust libav 绑定生态，169 万 tokens）**：ffmpeg-the-third 3.0.2+ffmpeg-7.1（版本锚定）/ffmpeg-next 9.0 复活（700 万下载）；Windows 三件套 preflight=**link.exe**（msvc-host 无 VS Build Tools 一切 build 即败）+**libclang**（bindgen 必需、社区未文档化坑 slint#4621）+**FFMPEG_DIR**（gyan `full_build-shared`/BtbN gpl-shared=官方姿势，含 include+lib+dll）；**PyAV 自带 DLL 不可链接**（无 .lib、hash 文件名，libloading 手写=随升级碎裂不值）；**独立进程>cdylib 定案**（DLL 撞名风险+崩溃隔离）；推帧直写 mpv stream_cb（已有）、控制面=interprocess 命名管道（KB 级信令，shared_memory 休眠 4.5 年不用）；**段表选择性解码 T1 零先例**（多轮关键词+avtensor 单区间/ff-decode 单点 seek 均不合）→ **自造面五件套**=段表匹配器/包跳过状态机+关键帧回退/段切换 flush 策略/帧写管道循环/控制协议；ffmpeg CLI 三形态源码级证伪（select=逐帧 O(K) 表达式解释执行 f_select.c/trim+concat=K 套解码器实例/streamselect 同）→ CLI 管道路线也死。
   - **Agent2（先例补捞四领域，142 万 tokens）**：GStreamer——GstSegment **无 merge 函数**（全文抓取推翻搜索摘要）、GES/NLE/concat 全为 seek 切源、整机引入判否；libmpv2 6.0/rsmpv 0.2.1 的 stream_cb 封装可抄；**安防 NVR 全域 seek 跳段**（Milestone/Hikvision/Blue Iris；近亲 ZoneMinder=JPEG 帧库直读、Frigate=HLS 段拼接，粒度粗于包级）；ffmpeg 8.0 segment 滤镜=拆分非保留；2025-26 新引擎零命中 → **首创维持、架构零改动**。**上一条目待办的「NVR 智能回放先例」由此闭环**。
   - **Agent3（rawvideo CFR 边缘语义+浏览器倍速先例，360 万 tokens）**：speed=N=消费 N×60 帧/s、demuxer cache 字节制（默认 150MiB≈50 帧）、超限=纯背压不丢包；**speed 运行时切换有官方在途 bug 族 #13694/#13513（位置跳变，0.41 未决）→ Phase2 机制改判=推流端节流**（业务速率编进推帧节奏、mpv 恒 speed=1、带宽恒 186MB/s vs speed=10 的 1.86GB/s、60Hz 下视觉等价；浏览器旁证：Chromium 40190553 desync/Jump Cutter 需前瞻缓冲/SponsorBlock 干脆纯 seek——「客户端切 rate 非稳态」）；无音频=视频即主时钟、**display-resample 禁用**（wid resize 跳帧+多屏 #10489）、framedrop=vo 保持（decoder 档在 audio=no 下死代码）；**stream_cb 无 URL 去重→重载安全**（open 必再触发、cache 不跨文件）；EOF=read 返 0→end-file(EOF)→idle（废弃 IDLE 事件勿用）；**rawvideo seek 帧精确**（字节=帧号×帧大小、seek_fn(0) 探测≥0 才置 seekable）→ **跳转升级=流内帧级 seek**（推流端按需解码供帧），仅目标不可得才回退重载。
   - **判定总表**：用轮子=FFI 绑定/demux 解码/swscale/控制面管道/渲染 mpv/rawvideo demuxer/关键帧表；抄模式=stream_cb 契约（libmpv2/rsmpv）/推流端节流（直播服务端控速同构）/「不进播放序列即跳过」（ZoneMinder/Frigate）；**真自造=段表匹配器/包跳过状态机/锚点恢复/段切换 flush/推帧节奏映射/控制协议（与 Agent1 最小自写面一致）**。
   - **施工单修订**：1b 增补（**恒 speed=1**/禁 display-resample+interpolation/实现 seek_fn+size_fn 帧级 seek/EOF 事件规范 end-file+idle-active）；**Phase2「逐段业务倍速」并入一期 1a/1b**（段表→推帧节奏映射，消灭 speed 切换风险面）；二期余=极端碎段优化/Errno22/验收。**绑定选型双候选调和**：0i-末并行条目推 rsmpeg 0.18.0（`ffmpeg7_1` feature 直对口 7.1.0）vs 本轮 Agent1 推 ffmpeg-the-third 3.0.2+ffmpeg-7.1——两者均为活跃正当候选，**1a 首日做链接预筛 bake-off**（同一下载的 dev 库分别 link hello-decoder，半小时定胜负），不预判。报告原文：agents 目录 agent_38c56ab9（Rust 生态）/agent_4740788f（先例补捞）/agent_a9e484bf（CFR 语义）三份 output.txt。
   - **工具线登记**：ZCode 子代理思考级别=模型 reasoning 档（low/max/high，默认 max），Agent 调用**无档位参数不可选**；官方文档：自定义代理 frontmatter `thoughtLevel` 须绑定显式模型才生效、inherit=跟随主会话档位、键名错静默忽略（非 reasoningEffort）；子代理并发额度=套餐级（撞限×2）；flclash=127.0.0.1:7890（本机实测 LISTENING）。文献役报告原文留档：`C:\Users\AAA\.zcode\cli\agents\sess_058dd90d-0f14-41f2-91a6-9b3386736d73\agent_6e812ff1-3724-4a42-92fe-b2b2063e9bb8\output.txt`（Q1-Q4+风险 Top5+mpv vs 自写对比）。
0k. **【09-12 深夜+ 上游基线调研+放弃 MPV 裁决（owner 新令：基于上游方案找更直接更优的预览流畅方案，允许大量串行子代理、甚至放弃本地 MPV；flclash 代理；两份上游克隆在盘）】**：
   - **本地盘点（主会话 T0）**：owner 所说两份克隆**实为 exe 发行包**——`D:\ArknightsPathFinding\arknight-auto-editing-v26.7.15-win-fix`（剪暂停.exe）与 `...-v26.7.23-win`（剪暂停260723.exe+`.exe_extracted` 解包目录）；真源码基准=已有 git 克隆 `arknight-auto-editing-upstream`（origin=liemark，HEAD=ce10360=PR #9 合并点，GitHub API 直证 7-23 后零提交）。
   - **子代理 1 考古（agent_60b5a30f，T0 源码直证）**：上游预览管线=cv2.VideoCapture 单 cap+IO 线程**顺序解码+grab 丢帧**（六个 parity 功能全=步幅丢帧）+`CAP_PROP_POS_FRAMES` 硬 seek（>30 帧阈值）+Tk PhotoImage 每帧重建+after(16) 轮询；**无拼接预览（跳裁剪区=逐帧吃或 seek）/无音频（PR #2 ffplay 子进程且起播不 seek=音画不同步缺陷未修）/无预览物化**；预览被注释钉死 OpenCV（settings_panel.py:68）。发行包核对：v26.7.23 exe≡ce10360、v26.7.15≈8f2dc2e。「上游有而我们没有」=8 条 UI 管道技巧，**3 条值得吸收进 1c：seek 命令合并去重（video_io.py:264-277）/150ms 节流刷图（preview_player.py:312-328）/有界丢帧队列 maxsize=2 满丢旧（video_io.py:123-128）**——与我们 stream_cb/恒速方案正交可叠加。
   - **子代理 2 GitHub 全景（agent_314dd539，T0）**：唯一分支 main=ce10360、8 tags 至 v26.7.23、7-23 后零提交；4 issue 无一涉预览卡顿（#1 音频/#3 AMD 编码器/#7 导出卡死+#无声/#8 绿色特效 open）；PR 5 条（唯一未合并 #5=macOS CI）；5 fork 除我方（kouekikin24，远端已有 fix/preview-pacing-metrics 分支 658a691）外全落后无预览工作；liemark 其余仓库全为棋类 AI。**结论：上游生态无隐藏路线、无未合并好想法**。
   - **子代理 3 后端文献（agent_8c811a24）**：libVLC（callbacks+rawvid 喂 I420、DEMUX_SET_TIME 帧精确 T0；但 rate>1=全解码+迟帧丢弃 video_output.c T0；Tk 嵌窗雷更多）；Qt Multimedia（playbackRate=时钟加速+丢帧不省 CPU，TimeController 源码 T0；QVideoSink 外供帧形态可做但须整套 Qt+双事件循环）；GStreamer（appsrc 外供帧正统先例+TRICKMODE 对裸流无意义；**2026-02 起 Centricular 官方 pip wheels** 打包大改善，bundle 200-400MB）；**WebCodecs/WebView2=唯一解锁 GPU 硬解+AV1 硬解+音频同步的路线，但 pywebview 不可嵌 Tk（#405）=UI 整体 HTML 化才可行**；SDL2（去 mpv 最小兜底：UpdateYUVTexture+CreateWindowFrom，渲染环 200-400 行，音频自建）；pyglet/OpenCV 判死。**NVR 智能回放=设备端出流+客户端全解码跟拍+离散档位（海康 PlayM4 SetPlaySpeed 等）——旁证我方客户端侧方案是同问题更激进解**；**GES nlesource.c 直证跨段=对源元素 ACCURATE|FLUSH 源内 seek（0j 待补三项收口其二→T0；ac-ffmpeg 仍未查，低优先：绑定双候选已覆盖）**。**总裁决：「外供帧渲染+平滑无级变速」无任何成品后端——我方恒速推帧+包级跳过+流内 seek 恰是该组合的正确实现；换后端零机制性收益**（三大 mpv 毒药已架构规避，换=作废全部已验证数字）；**SDL2 存档应急预案；WebCodecs=条件性重估（CPU 预算≤2 核或强需 AV1 时优先重估）**。
   - **登记**：ASSET §六新两行+§八 09-12 第三行；checklist Resume 同步。报告原文：agents 目录 agent_60b5a30f/agent_314dd539/agent_8c811a24 三份 output.txt。**W 路线预案（owner 追问「具体是哪个项目」后落成文）**：pywebview+WebView2+WebCodecs 三层组合，现成开源参考=hughfenghen/WebAV；三段式 W0 探针日（5 发）/W1 PoC（3-5 天）/W2 UI 迁移（1-2 周级 T3）+四条触发条件+DoD（CPU≤2 核）——**预案全文登记 checklist「W 路线预案」节；由此给一期 1a 新增架构约束：Rust 服务帧输出/包输出分离**（demux 包流供 W 路线复用，设计期零成本）。
0l. **【09-12 深夜++ 一期施工实录（owner「开工」）：Step0+1a+1b 全过，进行至 1c】**：
   - **Step0 三雷全通**：cargo 1.97.1 msvc+hello world（link.exe=VS2022 BuildTools 已装）；libclang=**pip wheel `libclang` 免提权路线**（winget LLVM 因 UAC 静默取消失败 0x800704c7；`site-packages/clang/native/libclang.dll`）；ffmpeg 7.1.1 shared dev=**gyan GitHub 镜像 GyanD/codexffmpeg tag 直链**（BtbN latest 已无 7.1；gyan.dev packages 页不可达）。**bake-off=ffmpeg-the-third 3.0.2 胜出**（sys 绑定对 7.1.1 编译链接全通；rsmpeg 0.18 实为 `0.18.0+ffmpeg.8.0` 专属、7.1 头文件上 rusty_ffmpeg AV_CH 宏代码生成崩——**0j 的「rsmpeg 有 ffmpeg7_1 feature」表述修正**）。env 注意：ffmpeg-sys-the-third 认 `FFMPEG_DIR`（根目录）、rusty_ffmpeg 认 `FFMPEG_LIBS_DIR`+`FFMPEG_INCLUDE_DIR`；Bash env 不跨调用保留，编译命令须自带全量 env。
   - **1a dsvc.exe（Rust 解码直通服务，.cache/v3ass/dsvc/，ffmpeg-the-third 3.0.2+serde）**：协议 stdin=JSONL（load/seek/stop/quit）、stdout=裸 I420 帧流（w*h*1.5 定长）、stderr=JSONL 事件（gen/loaded/span/seek_ok/eof/per_span）；段表 `[start,end,rate]`（rate=业务倍速抽帧、相位锚定段首）；**四 bug 修复**：①send_packet EAGAIN 被吞=丢包（帧级多线程解码器收发互斥，泵=收帧优先/EAGAIN 才发包）②段尾流水线压 ~线程数(16) 帧——**段切换 flush 策略**=空包进 drain 吐尽余帧再 flush_buffers（复位无害：下一段锚点关键帧重启）③**B 帧重排：包按 dts 序到达 pts 乱序，按 pts 推段会误判间隙丢帧（-50 帧根因）→ 段推进必须按 dts**（dts≥段尾⇒不可能再有更早 pts 包）+「未来包」扣留池待段推进补喂 ④drain 哨兵被 EAGAIN 吞须重试。**验收（probe_dsvc_smoke v3）**：4.mp4 64 段 pushed=18061/eof 19.9s(devnull)/seek 虚拟帧 9000→27.6s 完成/**span0+span2 起始各 3 帧 vs PyAV 干净解码 mad=0.0 字节全等**/per_span ±1=源 VFR 抖动；bench（无管道）decoded 2029fps/kept 1606fps=**10x 需 750 的 2.7 倍余量**。
   - **1b 引擎接入**：`dthrough_service.py`（新产品码：spawn/事件线程/有界队列背压 160MB/_stream_done 事件驱动 EOF）+ mpv_engine 六处（rawpush 注册/_dthrough_open_callback[read 阻塞式·cancel→-1·seek_fn 帧级字节寻址→dsvc]/play_dthrough[per-file 五件套]/seek_dthrough_frame/close 摘注册+关子进程/_ensure_loaded 5 参化）。**新坑六枚（全 T0，详见 checklist Current）**：loadfile 5 参签名（4 参字符串=options 落 index 槽静默吞——**skim cache=no 暗雷假绿修正**，2 处测试断言更新）/注册名纯 scheme（`rawpush` 非 `rawpush://`）/**framedrop=vo×keep_open=yes 组合在 rawvideo CFR 流致死**（diag H/I 二分）→ per-file framedrop=no/cancel 后 read 返 -1 非零/seek_fn 漏 contents 解引用=NameError→demuxer wedged（17 帧冻结）/mpv log-file 原生日志=排障利器。**E2E PASS（probe_dthrough_engine）**：R1 ready/R2 完整 EOF（mode=dthrough/speed 恒 1.0/tp_end×60=411≈dsvc_kept 413 自洽）/R3 播中 seek delta=0.033s/R4 零孤儿。**504+90 绿**。**VFR 教训**：3.mp4 名义 520 vs 实测 413——**1c 台账必须用 per_span 实测数做分段线性映射**。
   - **下一步=1c GUI 分档接管**（开关复用/角标「直通」/per_span 台账映射+上游 3 条 UI 技巧）→ 1d 测试门（新增单测+4.mp4 真实段表 10x DoD）。
   - **1c/1d 施工单（09-12 深夜++ 定案宣贯，owner 压缩前未动工——压缩后照此执行，无需新口令，「开工」已覆盖 1c/1d）**：
     - **1c-①台账 `dthrough_ledger.py`（新产品码，纯 Python）**：直通虚拟轴=帧计数轴（第 N 帧=N/60 秒）≠EDL 认证虚拟秒；输入=plan 保留段帧区间+rate+dsvc 实测 per_span；输出双向映射 `virtual_frame_for_source_frame(f)`（段内线性，rate 缩放）/`source_frame_for_virtual_frame(vf)`/`tp→EDL 虚拟秒`（时间轴指针）；**禁用名义值**（3.mp4 实证名义 520 vs 实测 413，VFR 时间窗帧少）。
     - **1c-②preview_player 分档判定**（接 `_send_play`@2171 请求构建 2219-2225 处）：「快速掠过」开关+`engine._dthrough_ready`+生效速率>Tier1 cap(3x)→`engine.play_dthrough(request, spans, fps=60, start_virtual_frame=台账换算)`；否则 Tier1 活 EDL 原路径分毫不变。spans 派生：认证 plan 保留段→`[[源秒起,源秒止,rate]]`，rate=业务(0.2x 段=factor_02、1x 段=speedup_1x?2:1)×UI 倍速，**mpv 恒 speed=1**；段表热更新=编辑后 debounce 重发 load（零物化，天然廉价）。中途 UI 变速 v1=以当前虚拟帧重发 play_dthrough（debounce）。
     - **1c-③角标**：snapshot mode="dthrough"→OSD 角标「直通{生效速率}x·60 FPS」（对齐既有「掠过10.0x/保底3.0x/源」标签族）。
     - **1c-④上游 3 条 UI 技巧吸收**（侦察锚点：preview_player `_maybe_schedule_skim_build`@965 守卫链样式）：seek 指令合并去重（积压同型只留最新）/150ms 节流刷图/有界丢帧队列。
     - **1d-①新增单测**：test_dthrough_ledger（双向映射+VFR per_span 失配+rate 缩放）/test_dthrough_service（skipif dsvc.exe 缺失：load/pop/seek/close+gen 作废）/引擎 play_dthrough 契约（假绑定：mode/URL/5 参 loadfile 参数/恒速断言）。
     - **1d-②DoD 真机验收**：4.mp4 **真实段表**（GUI 项目态/认证 plan）10x 全程/停顿<300ms（调 readahead 与队列 CAP）/CPU≤4 核/零磁盘写/位级已证（1a mad=0）。
     - **1d-③收尾**：新手检清单（合并旧五项+直通三场景：10x 掠过角标「直通」/拖轴点轴精确落位/删段拖端点后零重建续播）→ owner 手检 →「验收」合并单笔提交（工作区含 1b 产品码 dthrough_service.py+mpv_engine 改动）→「推送」。
1. **owner 手检** GUI：点轴瞬时（~30-70ms）/ 拖动松手 / ←→步进 / 播放暂停 / 跳过裁剪 EDL 步进；偶发 ~0.45s 慢定位=重复段内目标走 C4，属预期。**重点两项**：a) 点轴定位后按空格**恢复播放**——验证环跳过 +5/120 修正后应恰好从目标帧续播（旧行为是 ≈N，新行为应精确 N）；b) 原生静帧（无贴层）时**帧号 OSD 可见且正确**（撤贴层后帧号靠 mpv OSD/角标，不再有 ASS 烧字）。**09-08 新增 skim 三场景**（GUI 已载新码）：c) 分析完成或播放一次后等控制台 `[skim] 掠过代理就绪`（3.mp4 规模 ~8s），随后 UI 1x+业务加速播放 → 右上角 FPS 应**恒稳 60**（旧行为 35-45）且控制台/快照 mode="skim"；d) **删段/拖端点** → 立即播放应照常（Tier1 封顶 3x 不掉帧），~1.2s+构建时长后自动回真 10x；e) 设置面板取消「快速掠过代理」→ 高速播放永远封顶 3x（Tier1 地板）。
2. 手检过 →「**验收**」→ 提交。**09-08 更新（压缩点复核数字）**：skim 实施（B0-B3+review F1-F6+现场 G1-G7）与 C+/选项2 叠加同批文件（mpv_engine/preview_player/tests），preview_player.py 现含三代改动，本环境无交互式 `git add -p` → **建议合并单笔提交**（同属「预览提速」域，先例 addc950）；提交含：6 个修改文件（**+1655/−38**，以提交时 numstat 为准）+ 新增 skim_cache.py、tests/test_skim_cache.py、tests/test_preview_skim_rebuild.py + 本棒；checklist 不入库。
3. 「**推送**」→ 推远端（origin=kouekikin24 fork；本地已积压 20+ 提交；**tag `archive/pace-ab-20260901` 也一并推**（本地 tag，老快照备案事项））。
4. 候选二期（各自需「开工」，证据已备齐）：a) 重复段内原生化（seek段首+计数step，34/34 T0 背书，本源可再覆盖 ~26% 点击）；b) 暂停下降沿免贴层（restart 后 tp 即稳态，直接反查帧号采纳原生帧）；c) **EDL 验证寻址**（一期显式拦截 EDL=False；虚拟时间轴上闭环未验证，需同款扫描）；d) mpv 社区贡献：#9206/#9169 复现数据整理（半小时级，纯赚）。
5. 明确不做：改 mpv 源码（调参无效实测/关键帧+数步被 GOP=120 判死：平均 60 步≈0.6-1.2s 慢于 C4/源码补丁=自建编译链+永久维护）；双窗原生（被选项1+2 取代）；render.h（仅远期架构统一时考虑，python-mpv 无封装需 ctypes，SW 路径官方自评不可用）；**运行时 trick-play 关键帧-only 解码**（skipframe=nonkey 被 G2/G3 判死：保留段中位 11 帧<<GOP 120 段内无关键帧可解，rt=0/85 次停顿/EOF 空转——仅当段长>>GOP 的素材才适用）；**零预存并行预取管线**（probe_parallel_refill 判死：回填税=每看 1 帧精华先解 5-6 帧废片，10x 需 3300 帧/s 解码 vs 机器 500-1000；sw 8 路全核饱和仅 4.8x 上限、GPU nvdec 在 ffmpeg 模式不扩展仅 1.1x、瞬态 RAM 0.8-2.4GB 反大于物化 blob——真 10x 物理不可达，详见 §4.0f+ASSET §六）。

## 5. 会话口令史（授权链）

「先做 C+」→ C+ 实现+验证 →「分析选项2风险/为何反转」→ 微探针+评估 →「可以先进行验收」→ f53d6dc/f825983 提交 →「选项2 具体方案」→ 方案交付 →「开工」→ 阶段0 扫描+Tier2 → 阶段1 实现（466+90 绿）→ 验收探针排障（duration>0/单调化/dup间隙判据）→ 2.mp4 PASS →「样本1到4」→ 3/1/4.mp4 全 PASS（3.mp4 首跑 FAIL 为探针 edge 选点撞重复段，修探针后过）→「重新打开GUI」(pid 21820) → 本棒。

**09-07~08 分析链（全程无「开工/验收/推送」口令=零代码改动，纯分析+探针+登记）**：「列出最近修改了什么」→「业务速度补齐后 1x 播放掉帧，深入分析」→ v1 归因（10x风暴+snapshot新鲜度失配）→「我说的掉帧是右上角帧率 35-45」→「这个结论是怎么得出来的？有必要重新分析」→ **v2 定案：EDL 拼接边界风暴**（skip_trimmed 默认 True；F1 复现 35-45 带全中；v1 两处证伪：新鲜度失配死/~46%→66-73%）→「太慢了…要真正 10 倍速…调研成熟项目，不并发子代理可串发」→ trick-play 判死（段11帧<<GOP120）+调研（物化一致答案）+P1 磁盘代理提案 →「切小段是为了随时删/拖两端，本质改视频长度，深入分析」→ **P1 编辑感知修订**（probe_proxy_skim 实证：删段297ms/拖边2.07s/真10x恒60）→「本质还是要占用内存吗？」→ 磁盘vsRAM 答复 →「串行调研，存在更优方案吗？反感提前写入硬盘」→ **P1-RAM 实证成功**（stream_cb ram://，管道/TS批拼接证伪）→「内存版具体效果/占用多少/能完全解决吗，深入分析」→ probe_ram_footprint（足迹表 0.68-4.2MB/保留秒、天花板14.3x、暂停seek精确、2.mp4逐段-ss病理=开工首要诊断）+三问报告 →「通俗易懂的解释」→「串行调研不依赖内存不依赖硬盘的方案，不考虑成熟度考虑效果，深入分析」→ probe_parallel_refill → **零预存判死**（回填税 5-6x；sw 8路=4.8x 上限；GPU 不扩展；瞬态 RAM 反而更大）→「下一步我准备压缩上下文了」→ §6 快照+本段。

**09-08 压缩后实施链（「开工」已下，验收/推送未下）**：「下一步该做什么，给出具体方案」→ 两阶段方案（A 收尾旧账：手检→验收→推送；B 开工 P1-RAM+P0 地板并入 Tier1）→「通俗易懂的解释」→ 白话版交付 → **「开工」**（owner 未先走 A——验收仍挂，实施叠加在未提交树上，棒内已注明合并单笔建议）→ B0 病理诊断（probe_b0_2mp4_build/probe_b0_tlimit：**改判=旧探针缺陷 tpad等EOF，Δ=0，单命令 85.3s 帧精确，-t 截断定案生产形态**）→ B1（skim_cache.py + 引擎 stream_cb/两级切换/cap parity + GUI 调度接线；E2E 8/8）→ B2（_publish_project_snapshot 漏斗 debounce 1.2s）→ B3（480→360→240 降档阶梯 + 设置开关）→ **pytest 495+90 全绿** + probe_skim_e2e 复测一致 → GUI 重启载新码（手检两项旧重点 + skim 三场景待 owner）→ 本棒 §4.0g + §6。

**09-08 续：两轮自查 review + 两轮现场修复 + 上游考古 + Errno 22 取证**：「Review一下有没有引入新的问题」→ **review 轮 F1-F6**（最严重 F1=seek_edl 在 skim 播放中强切回 EDL 且 Tier1 继承 12x cap→风暴复活；修=流内暂停定位+cap 复位 3.0；F1b skim 免 +5/120 修正；F2 源模式撤 cap 恢复 f53d6dc 能力；F3 stat 新鲜度守卫；F4 tick 预算；F5 cap 残留清理；F6 异常包装；E2E 扩 10/10）→「还是掉帧 35-45」+「怎么判断 0.2 加速 10 倍生效」→ **GUI 日志现场取证：构建秒崩 TaskContext.cancel_event AttributeError**（全程 Tier1 真凶）→ **G1-G4**（cancel callable 化/千段级内联 57099 字符超 Win32K→滤镜 script 文件/降档估算表补 360-240p/角标 OSD 档位标签「掠过10.0x·保底3.0x·源」）→「Review一下」第二轮 → **G5-G6**（开关关时清已装 blob/预算 400→800）→「内存预算按原片还是剪后算」→ 剪后算+原片长影响拼装跨度 →「剪完存入内存要时间吗+4.mp4 各阶段用时」→ **probe_skim_phase_timing**（推导 0.1s/ffmpeg 16.6s/验证 2.1s@1418fps/入库 2µs）→「对比上游」→「上游找错了，该比带 A_PT 的，本地有两个克隆先找出来」→ **上游考古**：克隆①=arknight-auto-editing-upstream（liemark，07-23 Merge **PR #9 from kouekikin24**=A_PT 本就是我们产出合入上游）+克隆②=1bt 副本（05-22 前 A_PT）；**克隆①默认后端仍 OpenCV**→「以克隆①为基准重出总表」→ 修订版（分析同量级 7.6min，差在默认值；预览侧差异对任何上游版本成立）→「汇总成总表」→ 修订版总表登记 →「4.mp4 已 A_PT 分析完，预览多久 3x→10x，能直接读我的工具吗」→ **读 GUI 日志：G7 现场证据**（构建被预算拒 6 次：895MB 估>800，保留量反推≈9.9min）→ **预算 800→1200**+重启 → owner 贴截图「Analysis failed: FFmpeg A_PT OSError [Errno 22] Invalid argument」→ **取证轮**：对话框文本仅我仓有/我两实例日志干净（4 号分析成功、5 号从未分析）/无头复现 30s 全程健康/probe_pythonw_spawn 证伪 pythonw 假说/**[analyze] 行缺 flush=日志空白的缓冲假象**→ 埋取证（traceback 落控制台+.cache/analysis_error.log+worker start 锚点+flush 修复）→ GUI 第六次重启（pid 26868）→ 本压缩点。

## 6. 状态快照（2026-09-12 凌晨·二次压缩点，取代此前所有快照）

- **Git**：fix/preview-pacing-metrics @ f825983；工作区 **6 文件修改 +1655/−38**（ASSET +73/−9 / mpv_engine +540/−14 / preview_player +372/−14 / settings_panel +13/−0 / test_mpv_engine +502/−1 / test_preview_fps_osd +155/−0）+ **4 个未跟踪**：`skim_cache.py`(513行) `tests/test_skim_cache.py`(289行) `tests/test_preview_skim_rebuild.py`(273行) 本棒(78行)。内容=三代叠加：C+/选项2 旧账(+773/−16 批) + skim 实施 B0-B3 + review F1-F6 + 现场 G1-G7。**「验收」「推送」仍未下**——验收建议合并单笔（§4.2，preview_player 含三代改动无法拆）。
- **测试闸门（压缩点复跑 09-12，覆盖含取证埋点的最终代码）**：**504 passed + 90 subtests** 全绿。真机 probe_skim_e2e **10/10 三轮一致**（Tier2 speed 实达 10.0/drop 0.0%/tp_end≡虚拟/播放中 seek 留 skim/Tier1 封顶恰 3.0）。
- **GUI：已退出（跨天：实施在 09-08，当前 09-12 凌晨，期间机器重启）**。实例 6（pid 26868，载全部 G1-G7+取证）随关机消失；其日志证实 **owner 复测从未发生**（无 [analyze]/[skim] 行、analysis_error.log 不存在）。**复测前需重启**：`ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`（工作区代码即全部修复）。
- **未决案 #1：Errno 22（A_PT 分析失败，owner 截图）**。已证：代码路径健康（无头复现 30s 全绿）、pythonw 假说死（probe_pythonw_spawn POOL_OK）、对话框文本仅我仓 preview_player.py:3841 可产生、我两实例日志干净→**报错窗口非我启动的实例**（owner 启动方式待答）。已埋取证：失败自动落 `.cache/analysis_error.log`（现不存在=埋点后无新失败）+控制台 traceback+`[analyze] worker start` 锚点+[analyze] 行 flush 修复（旧版缺 flush=块缓冲假象，5 号日志空白的解释）。**下次 owner 报失败：先读 analysis_error.log**。
- **未决案 #2：owner 的 4.mp4 skim 首建真数**（估 3-6min T2+验证 12-35s）与五项手检结果——验收前置。
- **G7 预算史**：400→800→**1200MB**（owner 现场 895MB 预估被拒实测驱动；保留量反推≈9.9min@480p 费率 1.5）。降档阶梯 480→360→240 完好。
- **上游资产档案（棒 §4.0h 修订版有全档）**：克隆① `D:\ArknightsPathFinding\arknight-auto-editing-upstream`（liemark，07-23 HEAD=**我们 PR #9 合入点**，A_PT 同源，**默认后端仍 OpenCV**）；克隆② `D:\1bt\arknight-auto-editing-main - 副本`（05-22 前 A_PT）；1bt(1)/1edge=无 A_PT 拷贝；v26.7.15/23-win=exe 发行版。对比结论：分析同量级（差在默认值+PyAV 整合），预览侧全部差异对任何上游成立；probe_upstream_decode_bench=129 vs 1835fps T0。
- **本会话探针**（.cache/v3ass/，结果 json 同名留盘）：probe_b0_2mp4_build(+span0.stderr)/probe_b0_tlimit/probe_skim_e2e/probe_skim_scale1000/probe_skim_phase_timing/probe_upstream_decode_bench/probe_repro_apt_errno22/probe_pythonw_spawn(+result.txt)。垃圾可清：.cache/v3ass/b0_batches/、%TEMP%\skim_e2e_*、skim-filter-*.txt（正常自清）。
- **owner 关注点备忘（更新）**：反感写盘（skim 纯内存正中）；要真 10x（已交付，角标「掠过10.0x」可视验证）；段表=活文档（debounce 重建）；常要通俗解释（每轮已配）；**会亲自读角标/控制台日志回报现场**（G1/G7 两个真凶都是日志抓的）；在意上游血缘（A_PT=我们 PR #9 这条其很在意）；本地多副本要分得清（克隆档案见上）。
- **恢复路径**：AGENTS.md → checklist.md → ASSET_INVENTORY.md → 本棒（§4.0g-0h 决策与实施链 + §5 三段口令史 + 本节）。等 owner：重跑分析结果 / 报错窗口启动方式 / 手检结果 /「验收」/「推送」。

## 7. 状态快照·续（2026-09-12 晚·skim 现场验证+糊否决+解码直通破墙，取代 §6 中已被更新的事实项）

- **skim 现场验证（owner 真机，GUI pid 36040）**：4.mp4 分析**成功且干净**（424176 帧，A_PT 单遍，Errno 22 未复现）→ skim 自动首建成功：**294.1MB / 107433 帧 / 386.3s**（≈29.8min 保留，G7 反推修正：895MB=240p 档估算非 480p）。**owner 否决：播放糊（240p 直通显示）不能接受**——验收门从「性能」转向「画质不可妥协」。skipline：分离档接管（≤3x 永远源画质）已是共识方向，但被下行路线取代（见 §4.0i）。
- **破墙定案**：Tier2 改判=**解码直通（包级跳过）**，全链 T0 实证（正确性位级一致/虚拟轴精确走完/起播 237ms/冲刺 2160fps→10x 余量 2.4 倍/业界零先例）；一期=1a Rust 解码服务（工具链已装）→1b 引擎三级→1c GUI→1d 测试门；skim 休眠；物化=重剪素材备胎。详见 §4.0i。
- **Git：未动**——解码直通全程探针/文档阶段，产品代码零改动（工作区仍=§6 所记 6 改+1655/−38+4 未跟踪，504+90 绿维持）。
- **GUI：已退出**（pid 36040=本会话「重启 GUI」所启，糊反馈后某时点关闭，09-12 晚复核进程表无此进程；重启命令照旧 `ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`）。
- **未决**：①「开工」一期（Rust 解码服务 ffmpeg dev 库下载走 flclash 127.0.0.1:7890）；②Errno 22 报错窗口启动方式（owner 未答；本轮分析干净未复现，取证埋点在岗）；③五项手检（直通上线后合并为新清单）；④「验收」「推送」口令（直通做完一并）。
- **恢复路径（新）**：AGENTS → checklist → ASSET → 本棒 **§4.0i + 本节**（§6 保留作 09-12 凌晨快照，其中「保留量 9.9min」推断已被本节修正为 29.8min）。
- **压缩点更新（09-12 深夜·四次压缩点）**：造轮子审计**闭环**——本地 T0×2（cv2 推流 181fps✗/PyAV demux 1457pkt/s<需 2400→**Python 判死**）+ 串行子代理×3（Agent1 Rust 绑定生态：ffmpeg-the-third 3.0.2 锚定 7.1+Windows 三件套 preflight+独立进程定案；Agent2 先例补捞四领域全空白→**首创三度确认**；Agent3 CFR 语义：**speed 切换 bug 族→逐段倍速机制改判=推流端节流（mpv 恒 speed=1）**、rawvideo seek 帧精确→跳转=流内 seek、EOF/重载/背压语义全数 T0）。判定总表+自造面六件套+施工单修订全在 **§4.0j**；ASSET §六新行+§八 09-12 第二行同步。**待 owner**：①「开工」一期（Step 0 preflight→1a bake-off[rsmpeg vs ffmpeg-the-third]→1b→1c→1d）；②**并行会话确认**（棒 55 行非本会话条目，已共存合并，若仍须停）；③Errno 22 窗口方式（未答）。**恢复路径（本压缩点）**：AGENTS → checklist → ASSET → 本棒 **§4.0i + §4.0j + §7**；审计报告原文=agents 目录 agent_38c56ab9/4740788f/a9e484bf 三份 output.txt；产品码零改动（504+90 绿维持）。
- **压缩点更新（09-12 深夜+·五次压缩点：上游基线调研+放弃 MPV 裁决）**：owner 新令「基于上游方案展开调研，探索更直接更优预览方案，允许大量串行子代理，甚至放弃 MPV」。串行子代理×3 全成（首轮额度墙一次重试即过）：①**上游考古 T0**（预览=cv2 顺序+grab 丢帧+POS_FRAMES seek+Tk 轮询；无拼接/无音频/无物化；可取仅 3 条 UI 技巧进 1c）②**GitHub 全景 T0**（7-23 后零提交，生态零预览工作，fork 仅我方有工作）③**后端文献**（VLC/Qt/GStreamer 零机制性收益；SDL2=应急预案存档；WebCodecs=条件性重估；NVR+GES nlesource.c 直证双旁证验证现架构）。**总裁决：解码直通+mpv wid 维持冠军，「基于上游/放弃 MPV」双关闭；一期施工单不变**。详证全在 **§4.0k**；ASSET §六新两行+§八 09-12 第三行同步。报告原文=agents 目录 agent_60b5a30f/agent_314dd539/agent_8c811a24。**恢复路径（本压缩点）**：AGENTS → checklist → ASSET → 本棒 **§4.0i + §4.0j + §4.0k + §7**。
- **1c 施工完成（09-12 深夜++，owner「开工」续期覆盖 1c/1d）**：**双轴台账定案**——名义轴（dsvc locate 算式逐位镜像 `ceil(floor((b-a)*fps)/rate)`）编码 seek/start= 命令、实测轴（dsvc span/eof per_span）解码 time-pos 读回（禁名义值，3.mp4 520 vs 413 的修正落地为分段线性+seek `rebase()` 重定基：实测轴原点=落点 tick、截断首段按剩余槽估计、名义轴永远全表）。产品码：`dthrough_ledger.py`（新）+`dthrough_service.py`（**补 gen 门：旧代尾 eof 竞态会把新流误标 EOF=1b 隐患修复**；drain_actuals 增量接口）+mpv_engine（play_dthrough 起点源帧入口+台账建立、seek_dthrough_source_frame 源帧 seek 族、`_frame_for_position_locked` dthrough 分支、perf speed 直通档报台账档位速率、**+5/120 暂停修正豁免扩 dthrough**——漏了暂停寻址整体偏 2.5 tick）+preview_player（`_send_play` 分档门=原生+UI≥1x+跳过裁剪区+有删段+开关+生效速率>3x、失败回落 Tier1、直通时跳过 skim 补建；`_seek_frame` 直通拦截先于 EDL/verified/源寻址；角标「直通 Nx·{fps} FPS」）。**1c-④ 裁决**：150ms 节流刷图与有界丢帧队列架构内已有（`_KEY_PREVIEW_MS`/engine `_events` maxsize/dsvc 160MB 背压）；seek 合并去重=dsvc 世代抢占+mpv seek 队列天然承担——登记「架构性吸收」不另造轮子。**测试 542+90 全绿**（新增 38=ledger 18/service 事件层 7/引擎契约 5/GUI 分档门 8）；probe_dthrough_engine 复验 PASS（R1-R4：恒速 1.0/tp_end×60=411≈kept 413/seek delta 0.0/零孤儿）。**剩 1d**：GUI 重启载新码 → owner 手检（新清单=checklist Current：Tier1 回归 a/b+直通三场景 c/d/e+回落 f）→ DoD 真机（4.mp4 真实段表 10x 全程/停顿<300ms/CPU≤4 核/零磁盘写）→「验收」合并单笔→「推送」。**恢复路径（本压缩点）**：AGENTS → checklist → ASSET → 本棒 **§4.0l + 本条**。
- **真机首测掉帧定案+解码预算限速（09-13 凌晨，owner 手检 c 项报 27.9/32.9 FPS）**：根因=0.2x 段 ×factor10 ×UI10 = rate100 → 直通解码账单 rate×60=6000fps > dsvc 实测 2029fps → mpv 缓冲耗尽**间歇冻结**（探针 T0：饿死形态=冻结而非均匀降速，A 相 rate100 wall−虚拟=5.8s 冻结；OSD 低读数=采样窗撞冻结）。修复=`derive_spans(rate_cap=20)` 解码预算限速（0.2x 区域预览 20x 通过、台账实测轴保指针精确，Tier1 cap 3x 同哲学）；B 相 wall 28.25s≈虚拟 28.07s=总停顿 0.18s 逐段 60fps（GUI 同跑负载）=**CAP_FIX_VERIFIED**。探针=probe_dthrough_mixedrate.py（A/B 双相+停顿账口径）；回归 543+90 绿；GUI 已重启载修复（pid 31824）。教训入档：**直通档每段解码账单=rate×60 帧/秒，与 rate 成正比——业务加速叠 UI 倍速必须过解码预算**；E2E 合成段表全 rate=1 探不出此雷（真实段表手检不可省）。
- **掉帧诊断二次修正+碎段守卫门（09-13 凌晨 3 点档）**：owner 指出滑条上限 4x、截图实为 UI 1x → **首轮「rate100 解码账」诊断对其实际场景证伪**（1x 账单仅 600fps；rate_cap=20 保留但非 owner 场景的 fix）。**真实根因=碎段换段税**：保留段中位 ~11 帧，0.2x 段 rate10 每 ~18ms 换段 × 每段固定成本 ~30-50ms（dsvc 重定位+锚点回填+流内 seek）→ ~30fps；合成段表（长段）探不出。**修正门（双条件）**：UI>3x 且任一 span 呈现帧 ≥60（`_DTHROUGH_MIN_SPAN_KEPT`）→ 否则 Tier1；544+90 绿；GUI pid 36148。**架构定论**：碎段密项目 >3x 真掠过被换段税挡死（新旧架构同罪），解锁=dsvc「小间隙连续解码」（间隙小不 seek、解码穿过、段内各自抽帧；解码账 ~600-900fps 可行，Rust 状态机改造+重验，**待 owner 立项**）；UI 1x 真导出节奏（0.2x 10×）需抬 Tier1 3× 业务帽（mpv 顺序解码 600fps=天花板，边际，需实测，**待 owner 拍板**）。
- **§4.0m 立项施工单：dsvc 小间隙连续解码（09-13 凌晨 owner 已批，压缩前未动工——压缩后收「开工」照此执行）**。**owner 目标（定案）**：4x 预览=导出×4 且流畅（碎段项目）。**病根精确打点**：dsvc 每段尾 drain 冲洗+下段锚点关键帧重启（1a 位级正确设计）→ 每段固定成本=锚点回填解码（≤GOP 120 帧）+流水线排空/回填（~16 帧）；碎段（中位 11 帧）成本占段长 5-10 倍。**核心洞察**：小间隙边界不解码器、参考链连续滑过（=真实源序列，比锚点重启还干净），段外帧由既有 emit 过滤器丢弃——位级 mad=0 保持且零回填零冲洗。**六步**：①dsvc 自适应边界——`连续 iff 间隙帧数 ≤ 该段锚点回填帧数+16（流水线深）`（运行时由 kfs 可算，逐边界自优化）；连续边界三处配套：段尾不 drain_span／该段锚点跳过置空（间隙帧照喂）／pending 池不丢间隙包（按 dts 序全喂，emit 过滤器丢弃段外帧）；B 帧池（1a 语义③）其余不动；span 事件加 `mode:cont/restart`（向后兼容）②位级重验：probe_dsvc_smoke mad=0 + **新增相邻小间隙段用例（B 帧跨连续边界=全工程最险一刀）** + restart 路径复验③新增碎段探针：中位 11 帧岛×30 帧间隙×0.2x/1x 混合倍率 A/B 对照，量呈现 fps+停顿账+cont/restart 比例（顺带摸真实间隙分布）④Python 门放宽：`_DTHROUGH_MIN_SPAN_KEPT` 60→2 + **解码预算帽校准 20→32**（32×60=1920fps，留 5% 余量；20 是误诊期保守值）⑤全量回归 pytest+probe_dthrough_engine+probe_dthrough_mixedrate 复跑⑥GUI 重启→owner 真实项目 4x 手检（流畅+角标真实倍率+无 >300ms 冻结）→DoD→「验收」**与 1c 分两笔提交**。改造需 `cargo build --release` 重建 dsvc.exe（env 速查见 §4.0l）。**UI 1x 节奏问题（抬 Tier1 3× 帽）不阻塞本立项**——改造后 4x 走直通自带真节奏；1x 是否也切直通/抬帽，届时有数据再定。**兑现表**：流畅/普通段节奏(8×)/指针=100%；慢动作段封顶 ~32×（理想 40×=2400fps>2029）=该段 80%、仍为导出该段 3.2 倍。**风险与退路**：B 帧跨连续边界参考链（mad≠0 则阈值收紧至零间隙，仍吃掉大部分税）；大间隙多的项目提升有限（cont/restart 比例数据说话）。**挂起项（owner 令）**：机器上限提升——GPU NVDEC 探针（天花板或翻倍，需回传内存 ~6GB/s PCIe 可扛）与 skip_loop_filter（+20-40%，破坏 mad=0 需拍板）随时可捡。**⚠推送前决策**：dsvc 源码+exe 在 .cache/v3ass/dsvc/（gitignore 域）——产品功能源码应入库（建议迁 tools/dsvc/）。工期约半天。

- **§4.0m 施工完成实录（09-13 凌晨++，owner 睡前批「开工」跳过预手检；1c 快照先行=.cache/v3ass/1c_snapshot_20260913/ 补丁+9 文件=两笔提交拆分依据）**：**dsvc 重构三件**——①帧归属改按帧 pts 二分查段表（`span_of`）：连续边界流水线内跨段余帧按帧自身 pts 落段，推流顺序仍=段序（解码器 pts 序出帧），pushed/span 事件/账目语义 v1 兼容；②**扣留池整体移除、全包 dts 序直喂**：v1 按 pts≥段尾扣「未来包」，但 B 帧的未来参考 P 包恰是 pts≥段尾∧dts<段尾（mp4 ctts≥0 下 dts≤pts 恒成立、推段不变量本身严密，坏的是池），扣留使其 pts<段尾 的 B 依赖先喂=参考链断裂=**段尾 ~3 帧微损坏=v1 潜伏缺陷（合成源实测 units 57-59；此前所有测试只采样段首未覆盖段尾）**，直喂后由 pts 归属+IDR 复位兜住；③dts 推段逐边界自适应：连续 iff 间隙帧数 ≤ 下段锚点回填帧数+cap（**恒等式 restart ⟺ anchor>b+cap ⟹ 锚点包未流过=干净重建自动成立**——v1 大间隙重启的正确性此前靠这个未言明的恒等式）；restart 链保留锚点跳过+[anchor,a) 回填喂养+**段尾参考窗 16 帧**（段尾 B 帧未来参考+末段之后窗内包，垃圾解码无害、IDR 自动复位）；span 事件加 `mode:cont/restart`（服务层 `span_modes()` 捕获）；**env `DSVC_CONT_GAP_FRAMES` 钳位 [0,1000]，负值语义取消**（强制非法 restart=锚点已在 demux 之后的边界=参考链缺失=段坏，相 B 实测 per_span [60,30,60,12]；cap0=零间隙连续=棒预案退路）；顺手硬化：stdin EOF 后通道塞 Quit 防 decode_loop join 死锁（1b 潜伏，生产显式 quit 未触发过）。**位级验收（probe_dsvc_cont 新探针**，合成 640×360@60 x264 bframes=3:b_adapt=0 强制 kf@2/4/6、PyAV 全帧对照）：A 相 240 帧逐字节全等（表 [0,1][1.5,2.5][4,5][5.2,6.2]→模式 [restart,cont,restart,cont]、per_span [60]×4 精确=**跨间隙 B 帧参考链位级正确**）+ seek 相对账 150 ✓ + B 相全 restart 合法表 180 帧全等（restart 段尾损坏一并消除）；v3 smoke 大表复跑 mad=0（per-span ±1 不变=VFR 固有）。**门放宽**：`_DTHROUGH_MIN_SPAN_KEPT` 60→2（连续边界换段税归零）+ `_DTHROUGH_RATE_CAP` 20→32（1920fps 对 2029 留 5% 余量）。**性能验收（probe_dthrough_frag 新探针**，200 岛×11 帧×间隙 30 帧×混合 rate4/32=owner 碎段形态 UI4x 直写段表）：**A 相（连续）纯播放窗停顿 0.02s/虚拟 6.17s=FRAG_SMOOTH_VERIFIED**；B 相（cap0 退路）0.03s——本几何下 restart 税被缓冲吸收（Δ17 restart≈1ms/次；owner 真实项目间隙分布未采样以手检为准；cont/restart 比例可观测 A=137/20）。**回归**：pytest **546+90 绿**（tier 门小岛用例语义反转+新增退化段用例+service span_modes 用例）+ E2E PASS（**EOF 采样容差 3→5 帧**：tp_end 0.02s 粒度采样错过 EOF 前数帧、516 vs 519 压线误 FAIL=探针口径修正非产品改动）+ mixedrate CAP_FIX_VERIFIED。**GUI 已重启载 1d 码 pid 44148**。**剩**：owner 合并手检 1c+1d（清单 c 项碎段直通应已解锁）→「验收」两笔提交（1c 快照笔+1d 增量笔，拆分法=checklist 压缩接力）→「推送」前 dsvc 入库决策（⚠迁 tools/dsvc/）。**挂起项不变**：机器上限提升（GPU NVDEC/skip_loop_filter）；dsvc 换代施工注意 `taskkill //IM dsvc.exe //F`（旧 exe 被锁链接失败）。**本压缩点恢复路径**：AGENTS → checklist → ASSET → 本棒 §4.0l + §7（含本条）。
- **守卫整条移除（09-13 晨，owner 真机首检 T0——探针盲区现形）**：owner 导入 4.mp4 报「画面糊+0.2x 段 12x 掠过（期望 40x）」=Tier1+skim 完整特征（12=UI4×业务帽3；糊=skim 降档 blob 294MB，GUI 日志 `[skim] 掠过代理就绪/构建 302.2s` 实证）。**根因=碎段守卫口径「任一 span 呈现帧数≥2」被 0.2x 岛触发**（11 源帧岛 cap32 下呈现 0.34 帧）→ 整单打回 Tier1+skim 兜底；**探针盲区=probe 直调 play_dthrough 绕过 GUI 门，546+90 全绿仍漏**——合成测试与真实项目门路径的差异再证手检不可省。修复=守卫整条移除（立论已不成立：连续边界零成本/restart 税缓冲吸收实测 Δ17≈1ms/次/ceil(n_src/rate)≥1）+直通接管 clear_skim_blob 清场；tier 门测试反转+新增 blob 清场断言，546+90 复绿；GUI 重启载修正码。教训：**「探针过=功能通」只覆盖被探针走的路径；GUI 门的布尔组合（ready/开关/speed 解析/守卫）必须有走真门的集成测试或真机手检**。
- **skim 触发惯性掐除（09-13 晨，owner「现在顺手掐掉」）**：`_send_play` 的 `_maybe_schedule_skim_build` 加 `speed > 3.0` 门——≤3x Tier1 原生够用不建代理（旧逻辑任何直通失格的播放都触发，1x 手检也白烧 5 分钟+294MB）；>3x 且直通失格仍保留代理兜底。tests 正反两用例 548+90 绿；GUI 第三次重启。
- **路线 1 施工完成：非参考 B 帧按需丢弃（09-13 深夜，owner「开工」，T0）**：owner 问「流畅之路到头了吗/放开限制最容易的路线」→ 评估定案=CPU 物理墙（裸机 2029/实机 1380）下的三条路线中，路线 1（丢弃不显示的非参考 B 帧）唯一保位级。实现=dsvc 喂包前 h264 首 slice `nal_ref_idc` 判定（AnnexB+AVCC 双封装自适应；4.mp4 实测=AVCC+SEI 打头；判不出按参考处理）→ 不显示且非参考 → `skip_frame=AVDISCARD_NONREF`（帧级线程在包提交时快照该字段，逐包切换无竞态；非参考帧不占 DPB=DPB 一致性由解码器维护）→ **显示帧位级 mad=0 不变**。**实测**：4.mp4 60s 窗 rate20=喂入 3720 包/丢弃 2801（75%，B 帧占比 79%）；解码吞吐 +25%（隔离解码 1650→2070fps；PyAV 单线程对照 532→1823=3.4×，多线程下被管线/内存墙摊薄）；probe_dsvc_cont 新增相 C（合成源 rate-10 重丢弃=全帧字节全等）+相 D（真源 A/B 计数器 2801 vs 0）+smoke mad=0+E2E PASS；帽 20→24（1440fps 需求，留 17% 余量；0.2x 段=导出 2.4 倍速）。`DSVC_DISCARD_NONREF=0`=退路开关。549+90 绿；GUI 重启。**探针坑**：ffprobe -show_frames 解码全片（read_intervals 采样替代）；探针 run_dsvc 模块级 env 快照吞掉动态开关（须合并当前 os.environ）。**路线图存档**：路线 2=skip_loop_filter（+20-40%，破 mad=0 需拍板，可与路线 1 叠加）；路线 3=GPU（NVDEC 1080p h264 实测未必赢 16 线程软解，价值在释放 CPU/AV1/音频=W 路线触发条件）；路线 4=帽 20~24 现状即流畅。**40×（2400fps）超本机 CPU 物理墙，单解码引擎无解，换机/多引擎除外。**
- **真机 bug 五连修 + 行业调研（09-16 深夜，owner 手检反馈驱动，全 T0）**：**①P0-A 末尾空流假播放**——播完后 current_frame_idx 停在末段最后呈现帧，再点播放从「无可推之帧」起播=引擎挂牌「播放中」但 tp/源帧 3s 零推进（probe_eof_replay 实证），红条冻末尾+时间戳错乱；修=`_send_play` 片尾回绕（≥末保留段最后呈现帧→从 0）。**②P0-B seek 后冻结**——根因=dsvc seek 锚点取「段首关键帧」→长段深定位从段首回填（600s 段冻结 2.4s+，严格口径 5 次 seek 3 次冻结）；修=目标段锚点前移「目标帧前最近关键帧」+目标段强制 restart；复验 5/5 正常、首次推进 0.06~0.22s；位级四项判定仍全 TRUE。**测量口径错误自纠**：此前报「0.12s 落位」是台账重定基时刻，非播放恢复时刻。**③P1-C 点不到**——暂停段带 15~h-25 覆盖整轴且先于 seek 判定→修=裸单击=跳转/Ctrl+单击=选段。**④P1-E 空格不生效**——焦点被 mpv 子窗持有，两拍才拉回→空格处理即时抢焦点。**⑤P1-F 卡顿**——帽 24（1440fps）超实测吞吐 1380fps→回撤 20。**回归**：553+90 绿 + 五探针全 PASS（cont 位级/engine E2E/frag/mixedrate/smoke mad=0）；GUI pid 36172。**行业调研（owner 令，串行子代理×6+接力×2，报告=agents/research_proxy_materialize/00~07）**：Resolve/Premiere/FCP/开源NLE源码级/smart-cut/一致性UX 六路 + 本地基线 + 汇总矩阵；**关键结论**：①物化代理方向=全行业正统（Resolve 五通道/Premiere 双轨/FCP 转码同构）②我方 CRF14 代理 2.26GB/h 比行业帧内代理（40.5~187GB/h）小 15~50 倍 ③开源三家无一能在 1080p60 长 GOP 上流畅（我方痛=真实行业痛）④**「按导出节奏物化+短GOP导出级代理兼预览」无先例**（新颖性）⑤三处修订已采纳：代理强制短 GOP≤30（+10~20% 体积换 seek）、playhead+全量双驱动、追加式物化；⑥「高倍速保节奏」行业视为伪需求、「1x 档节奏一致」=弱刚需+差异化卖点（承诺口径收敛）。**下一步**：owner 九项手检 →「验收」→ 提交（1c/1d+1e/bug 修 三笔）→ Phase-2 物化代理（2-3 天）。
- **探针盲区类事故制度根治 + 同源手检（09-16，owner 令「同意，开始执行」）**：owner 质问「为何不用现有 GUI 自己倒腾」→ 全史取证=**同类事故复 6 次**（08-30/09-08/09-13/09-16×3）；根因=制度缺条（R2 未含「造探针前先盘探针」/R3 未定义「驱动谁」/R6 未约束探针口径自证），且查明**「自建 tk.Tk()」是既有探针惯例**（verify_gui/verify_gui2）——缺条纵容的普遍行为。**制度落地**：ASSET §七 **新增 R8「探针同源」**（验证工具必须同源：GUI 复用 main.py 的 main()、禁自定义复刻；探针 T0 结论入档前须自证口径与生产一致，未自证只算 T2/T3）+ R2 补强（造探针前先盘 .cache/v3ass/probe_*）+ R3 澄清（进程内驱动=真实产品对象）；§八 登记防膨胀理由。**首个合规产物** `.cache/v3ass/handcheck_samesource.py`：直接调 `main.main()`、monkeypatch `tk.Tk.mainloop` 拦截自检、不截屏（R3）、像素走 mpv screenshot-raw。**九项全通过**：加载 29804帧/MpvEngine/**视频区真实 1000×647**（自建版 1180×1=铁证）/4x 直通 active/片尾回绕 start=0/seek 后 tp 0.13~0.21s 推进/裸击=跳转 Ctrl=选段/空格翻转/帽 20/screenshot-raw 1000×647 有效。自建探针已废止删除。
- **09-16 二轮真机修复 + 自我复查（owner 报「点选还是不行/空格把红卡拉到开头」）**：**①密集区点选**根因=`_collect_candidates` 每 clip 端点 ±12px 命中区，真实 418~2211 clip（平均间距 1.48px）→ 命中区覆盖全轴、seek 不可达（实测 418clip/980px 下 12/12 全被吞）；修=**点击/拖动分离**（`_DRAG_START_PX=3`：无位移点击 mouseup 回落 seek，>3px 才抓端点）+ 命中带垂直约束；复验 **10/10 成功**、拖动正常。**②片尾回绕误触发（我上一轮引入）**：v1 `start>=末段末帧` 在末段数十秒内任意暂停→播放都会误触发拉回开头（owner 实测 0/424175）；v2 加 `not is_playing` = **自相矛盾**（toggle_play 先置 True 再调 _send_play→恒假→永久失效）；**v3 定案 EOF 标志**（`_play_ended_at_eof`：_sync_eof_stop 置、_send_play 消费、_seek 清）→ 仅「自然播完→再播」回绕。**③空格逻辑复查无 bug**（重入 depth 恒 1；我上轮「状态竞争」结论撤回）。**自我复查纠正两错**：我引入的「不可分辨即跳过」过滤器使可抓 clip 降为 0（回归，已撤回）；「is_playing 竞争」判断错误（已撤回）。554+90 绿；复验探针=verify_fixes_0916.py（V1 10/10、V2 clip_in、V3 三分支）。
- **分析结果磁盘缓存落地（09-16，owner「开工」；先行业调研再实现）**：**调研**（子代理串行 ×2 接力，报告 agents/research_analysis_cache/01_cache_practices.md 440 行 §0-§8）：NLE 缓存键设计/内容寻址 vs 元数据寻址（ccache 混合原则：大块不变用 size+mtime、小而易变用全哈希）/ffmpeg 生态无现成轮子（PySceneDetect stats CSV 非缓存，官方已废弃复用路径）/存储格式对比/失效迁移/交互语义/视频特有坑。**主会话本机 T0 复现**：sha256 2256MB/s（4.mp4 实测 1.58s）、npz 2.12MB+CRC32 捕获单字节损坏、Windows os.replace 句柄占用抛 WinError 5、5 处源码引用逐行核实为真。**主会话纠正调研两处**：4.mp4 实为 2.11GB（报告推断偏高）；**源 sha256 已在 media_info.py:982 算好**（键零额外成本，报告误当支出）；并对交互建议作实质修正（调研主张完全静默 → 改为首次提示一次）。**实现**=analysis_cache.py（新）+ preview_player 三处接线；**验收**=单测 20/20 + 同源探针四相（冷未命中/写入/热命中 18ms/参数变化不命中/损坏降级）+ 574+90 绿。**收益**：重导入 4min→18ms。
- **分析缓存 review（09-16，owner 令）**：发现并修复 4 处——**①缓存投毒（严重）**：`make_key` 在 media_info=None/空 sha 时构造空身份键→所有视频撞同一键；加固=`make_key`+`load`/`store` 双层 hex-sha 校验（抛 KEY_SOURCE_UNIDENTIFIED）+5 单测，探针当场验证拦截生效。**②重复写盘**：命中路径经 `_finish_analysis` 再落盘→加 `_analysis_loaded_from_cache` 跳过。**③换源残留**：`load_video` 未复位缓存交互状态→已补。**④numpy 真值歧义**：`len(arr or ())` 触发 ValueError→显式 None 判断（**靠同源探针复验暴露，单测未覆盖**）。复核通过：并发写 8 线程安全、数组可写/dtype 正确、sha 正则强校验、probe 前后比对、下游消费安全、主线程阻塞 3.8ms。**579+90 绿**；探针四相全过。

## 8. 状态快照·续（2026-09-16 深夜++·压缩点；取代此前所有快照中已被更新的事实项）

**本压缩点（09-16 全天）完成**：
- **五连修**（owner 真机手检驱动，全 T0）：①P0-A 末尾空流假播放（播完 current_frame_idx 停在末段末帧，再播=无可推之帧→引擎挂牌播放但 tp/源帧零推进；修=片尾回绕）②P0-B seek 后冻结（锚点取段首关键帧→长段深定位从段首回填，600s 段冻结 2.4s+；修=锚点前移到目标帧前最近关键帧+目标段强制 restart；复验首次推进 0.06~0.22s）③P1-C 点不到（暂停段带 15~h-25 覆盖全轴且先于 seek 判定；修=裸单击=跳转/Ctrl+单击=选段）④P1-E 空格（焦点被 mpv 子窗持有；修=即时抢焦点）⑤P1-F 卡顿（帽 24 超实测吞吐 1380fps；回撤 20）。
- **二轮修**（owner 报「点选还是不行/空格把红卡拉到开头」）：①**密集区点选**——根因=`_collect_candidates` 每 clip 端点 ±12px，真实项目 418~2211 clip（平均间距 1.48px）→ 命中区覆盖全轴、seek 不可达（实测 12/12 全被吞）；修=**点击/拖动分离**（`_DRAG_START_PX=3`：无位移点击 mouseup 回落 seek，>3px 才抓端点）+ 命中带垂直约束；**复验 10/10**。②**片尾回绕误触发**（我上一轮引入）——v1 `start>=末段末帧` 在末段数十秒内任意暂停→播放都误触发（owner 实测帧号 0/424175）；v2 加 `not is_playing` = **自相矛盾**（toggle_play 先置 True 再调 _send_play→恒假→永久失效，复查时发现）；**v3 定案 EOF 标志**（`_play_ended_at_eof`：_sync_eof_stop 置、_send_play 消费、_seek 清）→ 仅「自然播完→再播」回绕。③**空格逻辑复查无 bug**（重入 depth 恒 1；我上轮「状态竞争」结论撤回）。**自我复查纠正两错**：引入的「不可分辨即跳过」过滤器使可抓 clip 降为 0（回归，已撤回）；「is_playing 竞争」判断错误（已撤回）。
- **制度 R8「探针同源」**（owner 质问「为何不用现有 GUI 自己倒腾」→ 全史取证=同类事故复 6 次；根因=制度缺条）：验证工具必须与生产同源（GUI 复用 main.py 的 main()，禁自定义复刻）+ 探针 T0 结论入档前须自证口径一致；R2 补强（造探针前先盘已有探针）、R3 澄清（进程内驱动=真实产品对象）；§八 登记防膨胀理由。首个合规产物=`.cache/v3ass/handcheck_samesource.py`（九项全通过，含视频区真实 1000×647 vs 自建版 1180×1 的铁证）。
- **分析结果磁盘缓存**（owner 令「调研→开工」）：先行业调研（子代理串行×2，报告 `agents/research_analysis_cache/01_cache_practices.md` 440 行）+ 主会话本机 T0 复现（sha256 2256MB/s、npz CRC32 捕获损坏、Windows os.replace WinError 5、5 处源码引用核实）。**实现**=`analysis_cache.py`（键=sha256(源，复用 media_info 已算值)×参数指纹×后端×ffmpeg身份×格式版本；npz 单文件+CRC 自证+同目录原子写带重试；版本进键迁移；LRU+孤儿清理；**首次提示一次**——主会话修正了调研的"完全静默"建议）。**review 修五处**：①**缓存投毒**（make_key 对 None/空 sha 构造空身份键→所有视频撞同一键；加固=make_key+load+store 三层 hex 校验）②重复写盘 ③换源状态残留 ④numpy 真值歧义（探针复验暴露）⑤同类写法清扫（params_fingerprint）。**验收**=单测 27 个+同源探针四相（冷未命中/写入/**热命中 3ms**/参数变化不命中/损坏降级）+**581+90 绿**。**收益：4.mp4 重导入 4min→3ms**。

**压缩后第一步（owner 手检，六项）**：见 checklist Current（含"导入 4.mp4 最后一次等 4min"、"点密集区"、"播放中点远处"、"片尾附近空格"、"二次分析验缓存"、"4x 连播无冻结"）。
**「验收」后提交（四笔）**：①1c 笔 ②1d+1e 笔 ③bug 修笔（五连修+二轮修+R8）④缓存笔。
**⚠推送前拍板**：dsvc Rust 源码迁 `tools/dsvc/` 入库。
**恢复路径**：AGENTS.md → checklist.md（Current）→ ASSET_INVENTORY.md（§七 R1-R8 + §八决策）→ 本棒 §8（本条）+ §4.0i~§4.0m。

## 9. 状态更新（2026-09-23 傍晚·机器重启后状态恢复）

**事件**：机器 09-23 10:12 重启 → §8 所记「GUI pid 7460 存活（09-16 20:11 启动）」**已失效**（进程随重启消失，属 §8 预设的「进程消失属正常」情形）。工作区产品码零改动（09-18 后仅探针/文档/缓存产物有改动）。

**本会话动作（均为交接棒已授权动作）**：
- 按 §8 授权命令重启 GUI：`ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`（与 `启动剪辑工具.bat` 同款，cwd=仓库根）→ **pid 13776**，窗口标题「明日方舟剪辑工具」，可见未最小化；日志 `.cache/v3ass/gui_run_acceptance.log`（0 字节=正常，分析未跑过）。⚠ 本 GUI 依附本会话后台任务，会话退出后若 pid 13776 消失属正常，用同一命令重启即可。
- **手检前置条件逐项复核（全过）**：①`pytest -q`=**621 passed + 90 subtests**（31.6s 实跑）②`tools/preflight.py` 正常 ③预建缓存 npz 完整（`key_digest`≡文件名 `c44cb72a68d5`，states=424176，2210/2212 段，meta v2）④源文件 `D:\qq下载\920\4.mp4` 三项与缓存键输入全等：size=2265644549 / mtime_ns=1784276989506720000 / sha256=c2762556bde6（现场重算 2.13s）→ 导入后应命中缓存（保持默认设置 A_PT/0.7/128/400×225/16 不动）。
- checklist.md 头部 Updated + Current 已同步本条。

**下一步不变**：owner 六项手检（checklist Current）→「验收」→ 四笔提交（①1c ②1d+1e ③bug 修 ④缓存）→「推送」前 dsvc 入库拍板（迁 `tools/dsvc/`）。Phase-2 物化代理待另行「开工」。

## 10. 状态更新（2026-09-23 晚·手检第一步 FAIL 已修复）

**事件**：owner 手检第一步（导入 4.mp4 应弹「发现分析缓存」）**FAIL**——没弹提示，直接冷跑约 6 分钟。

**根因（T0，证据三重）**：
- `_try_load_analysis_cache` 需要 `self.media_info`（缓存键必须含源 sha256），而它由后台任务 `_start_media_info_probe` 产出（`probe_media` 对 2.1GB 源算 sha256，实测 ~5s）。
- 导入后**立即**点击 → `media_info` 尚为 `None` → 该函数**静默 `return False`**（原实现无任何日志）→ 直接进入完整冷分析。
- 证据：①GUI 日志 18:39:22 直接 `[analyze] worker start`（无任何 cache 行）②18:45:17 冷跑完成写回缓存，键仍 `c44cb72a68d5` ③离线用真实 `probe_media` 复算键 + `AnalysisCache().load()` → **精确命中**（2210 段）→ 证明缓存文件与键都好，是查询被跳过。
- 交接棒旧措辞「导入 4.mp4（最后一次等 ~4min）」有误导：真正口径是**等到媒体探测就绪**（源 sha256 算完）后再点，缓存查询才有效。

**修复**（`preview_player.py`）：
- ①`_start_analysis`：`media_info` 缺失且探测在飞（`_media_info_handle is not None`）→ **挂起请求**（`_pending_analysis_source` + 按钮文案「等待媒体探测…」），不再静默跳过；
- ②`_start_media_info_probe.on_success` → `_maybe_resume_pending_analysis()`：同源则自动续跑（命中缓存则秒开），换源则作废；
- ③探测失败（`on_error`）/落定仍无身份（`on_done`）→ 作废挂起 + **如实弹窗告知**，不悬死；
- ④两处静默点补取证日志（「跳过缓存查询：源身份未就绪」「未命中（键=…）」）；`_invalidate_analysis_for_reload` 清理挂起态。

**验证**：
- R8 同源探针（新）`.cache/v3ass/verify_immediate_click.py`：真实 `main.main()` + **导入后不等直接点击**（复现 owner 时序）→ `media_info_ready_at_click=false`、`pending_set=true` → 续跑 → **5.661s 命中**、弹「发现分析缓存」、2210 段/424176 帧 ✓
- 单测 `tests/test_analysis_cache_wiring.py` **12/12**（新增 5：探测在飞挂起不冷跑/落定续跑/换源作废/失败提示一次/重载清理）
- 全量回归 **626 passed + 90 subtests（29.4s）**
- GUI 已重启 **pid 24376** 载修复码（旧 pid 13776 已停）

**下一步不变**：owner 六项手检（现在第一步应弹提示；即便导入后立即点也会自动续跑）→「验收」→ 四笔提交 →「推送」前 dsvc 入库拍板。

## 11. 状态更新（2026-09-23 深夜·两个真机症状修复）

**事件**：owner 手检反馈两个症状——A)「红条反复横跳」；B)「1 倍速暂停再点播放，开头几秒帧数跑不满 60 帧」。

**根因 A（T0）**：`MpvEngine.play_dthrough(start_source_frame>0)` 把起点写进 per-file `start=`，而 demuxer 的初始定位会经 stream_cb `seek_fn` 变成 **dsvc seek 代**（探针 `svc_calls` 实证：`load` 后紧跟 `seek virtual_frame=11902`）。该代**只从目标段起推**，目标段之前的 `per_span` 全 0；而 `play_dthrough` **没有**像 `_seek_dthrough_tick_locked` 那样调 `ledger.rebase` → 实测轴 tick→源帧映射被 0 前缀整体拽偏，且随 span 事件逐段到达**阶跃变化**。真引擎探针：`start=12000` 读回 **23999**（误差 11999 帧）、每 ~0.3s 一次小跳。

**根因 B（T0）**：`_send_play` 每次都走完整 `play_dthrough` → `_ensure_loaded_locked` 因 URL 代递增而**整路重载**（mpv 重开流 + dsvc 从文件头 demux 到目标 + 解码器重新填充）。真引擎探针：重载恢复首 0.5s 窗 **0/18.6/23.5fps**；深定位更差——「起播→首帧推进」延迟随深度线性增长：**200s→1.83s / 1000s→7.37s / 3333s→29.2s**（≈0.14ms/帧）。对照**纯 resume** 首窗恒 **58.9~60.8fps**。另证 dsvc 自身 seek 很快（事件口径 0.25~0.75s，次线性），故瓶颈在 mpv 侧重载+重填，非 dsvc。

**修复**：
- `mpv_engine.play_dthrough`：`start_virtual_frame>0` 时同步 `ledger.rebase(...)`（与既有 seek 路径一致）。
- `mpv_engine` 新增 `_dthrough_resume_ok_locked()` + **同段 resume 快速路径**：已载 dthrough 流 + 源相同 + 段表逐段一致（区间/rate）+ 起点与当前呈现帧**同段** + 本代未 EOF → 直接 `_resume_locked()`，免重载；任一不满足即回落完整重载。
- `dthrough_ledger` 新增 `span_index_for_source_frame()`（名义表同段判定）。

**验证（全 T0）**：
- `probe_redbar_jump2/3`（真引擎+真 dsvc）：横跳回退 **1→0**、读回 12000（不再 23999）、frame_span 单调 [12000, 12591]。
- `probe_resume_stall`：resume 首窗 58.8~59.4fps（对照重载 23.5/35.7）。
- `probe_resume_fastpath`：同段 resume `svc_loads+0 / dthrough_id+0`、首窗 **63.3fps**；跨段与换段表均正确回落重载（各 +1）。
- **R8 同源 GUI 探针** `.cache/v3ass/verify_redbar_gui.py`（真 `main.main()` + 真缓存命中）：播放中红条 **0 回退**；暂停→播放后 5 窗全 **58.6~59.4fps**。
- 单测新增 6（起播重定基/同段免重载/跨段重载/段表变化重载/EOF 后重载/台账同段判定）→ **631 passed + 90 subtests（29.4s）**。
- GUI 已重启 **pid 31272**（旧 pid 24376 已停）。

**口径提醒**：症状 B 应看**右上角 OSD 的 FPS**（`d(tp)/dt × 源fps`）——直通档 rate>1 段的**源帧号**推进本就远超 60（那是抽帧倍率，不是掉帧）；用帧号判「跑不满 60」会误判（本会话首次 GUI 探针即踩此坑，已纠正）。

**下一步不变**：owner 六项手检（第 1 步已修，第 2/3 步即本次两症状）→「验收」→ 四笔提交 →「推送」前 dsvc 入库拍板。

## 12. Review（2026-09-23 深夜·本轮修复的自查结论）

owner 令「Review 一下有没有引入新的问题」。**结论：本轮两处修复未引入新问题（逐项验证），但 review 过程发现三处既有缺陷（对 HEAD 核验过，均非本轮引入）**。

### 一、本轮修复的安全性验证（全 T0，阴性=无新问题）
1. **rebase 前提验证**（`probe_start_seek_assumption`）：修复 1 依赖「`start>0` 必触发 dsvc seek 代」。对 6 个起点（60/600/6000/12000/12345/200000）各做干净起播，`dsvc_seek_called` **全部非空**、读回偏差**全部 0** → 前提成立，rebase 不会钉错映射。
2. **快速路径判定边界**（`probe_resume_fastpath`）：同段 → `svc_loads+0 / dthrough_id+0`（首窗 63.3fps）；**跨段 → +1 重载**；**段表变化 → +1 重载**；**EOF 后 → +1 重载**。四种情况均正确。
3. **回归**：631+90 全绿；无孤儿 mpv/dsvc 进程；GUI pid 31272 存活。

### 二、review 发现的既有缺陷（非本轮引入，已对 HEAD 核验）
- **a) `derive_spans` 每次播放主线程 ~820ms（P0 候选）**：`_send_play` 内联调用，**无缓存**。实测 4424 段表冷/热均 **818/820ms**（cProfile 栈：`dthrough_ledger.py:110/126 ← preview_player.py:2332 _send_play ← MainThread`）。这是症状 B 的**残余来源**——重载已修掉，但每次播放（含暂停→播放）仍付 ~0.8s 主线程卡顿。修复建议：按 `(timeline_revision, speedup 开关, factor, ui_rate, rate_cap, fps)` 缓存；编辑变更时失效。
- **b) 全源 sha256 主线程停顿（P1）**：`source_is_current()` / `assert_source_current()` / `complete_for_export` 各对 **2.1GB 源重算哈希**，实测主线程 **1613.8 / 1695.5 / 2493.2 ms**（栈转储：媒体探测完成回调、首次播放建 EDL、认证完成回调）。修复建议：按 `(size, mtime_ns)` 缓存，或移入后台线程 + 结果门。
- **c) 重载窗口红条瞬跳 0（P2，已部分被修复 2 覆盖）**：`_ensure_loaded_locked` 复位 `_source_frame = 0`，GUI 播放中无条件采信 → 重载型起播红条闪 0（实测 2 拍/~20ms）。同段 resume 已无此现象（快速路径不重载）；**跨段起播/编辑后重播仍会闪**。修复建议：`time_pos is None`（未读回）时不采信引擎帧号。

### 三、口径纠正（写进 checklist）
`probe_redbar_jump2` 的 `mapping_errors` 计数是**探针自身口径瑕疵**（拿 `tp×fps` 直比源帧，忽略了段表网格量化——rate>1 段本就不等距），非产品缺陷；判据应以**单调性/回退步数**与**落点偏差**为准（均为 0/0）。

## 13. 施工（2026-09-24 中午·owner「开工」：review 三缺陷 + 一处新发现全部修复）

### 修了什么（按收益排序）
1. **`derive_spans` 820ms → 13.7ms（60×，dthrough_ledger.py）**：原 O(kept×segs) 双循环（真实项目 152×2212，每切点重扫全表）。改「归一化一次 + 按起点排序 + 二分」。**parity 是硬约束**：排序会改变「列表序首个命中」语义 → 只在**无重叠**时走快路径，重叠回落**原始列表序**；**1728/1728 组合逐位一致**。⚠ 初版曾误用排序表做回落（我自己新增的重叠用例当场抓住），已修。
2. **身份校验 stat 快速路径（media_info.py）**：`source_is_current`（2.1GB×1.6~2.5s×主线程 3 处）+ `ToolInfo.is_current`（87MB ffmpeg.exe，764ms）→ 先比 `(size, mtime_ns)`，不一致仍回退全量哈希。**安全边界**：签名只在「能证明签名↔哈希对应」时记录——仅 `_tool_info()` 工厂（算哈希+前后 stat 比对）钉签名；外部构造（stale sha）仍全量哈希，**不误报**（专测钉死）。
3. **换载红条闪 0（mpv_engine.py）**：`snapshot_perf` 在本代未读回位置时 `source_frame=None`（原报复位值 0）。改在**引擎报告层**，GUI「播放中一律采信」契约不动。
4. **`bind_media_info` 锁内 2.4s（新发现，mpv_engine.py）**：424k 行时间轴派生原在引擎锁内做，渲染循环 `snapshot_perf` 同锁 → 播放中认证完成时整段冻结。改**锁外派生 + 锁内事务发布**（发布前复核源 stat）。附带：认证证据复验加 stat 快速路径（~700ms→0ms）、`certify_frame_pts` 后台 prime 签名、两处长循环每 4096 行让出 GIL。

### 验证（R8 同源 GUI 探针，真 `main.main()`）
| 指标 | 修前 | 修后 |
|---|---|---|
| resume 最坏单拍 | 5189ms | **8ms** |
| 跨段起播红条闪 0 | 2 拍 | **0** |
| 冷启动首次播放 | 786ms | **331ms**（一次性；热身后 ≈8ms） |

- 单测新增 5（derive_spans 乱序/重叠 parity、source stat 快速路径、ToolInfo 不误报、bind 锁外、snapshot None）→ **637 passed + 90 subtests**。
- GUI 已重启 **pid 34356**。

### 遗留（非阻塞，记录备查）
首次播放仍余一次性 ~331ms：认证绑定在后台线程，其 GIL 竞争拖慢主线程单拍（已加让出点，收益递减）。彻底消除需把绑定完全移出 Tk 进程或改分片流式校验，暂不做。

## 14. 追加修复（2026-09-24 下午·owner 报「点击跳转后红条不再随播放移动」）

**定性**：**非**第 13 节四项修复引入；属 09-16「密集区点选」二轮修**未提交代码**里的遗留缺陷（`git show HEAD:timeline_widget.py` 无该分支，已核验）。

**根因**：`timeline_widget._on_mouseup` 的「悬停候选但未拖动 → 回落为 seek」早退分支（09-16 加）只调 `on_seek_cb` 就 `return`。而 GUI 的 `on_seek_cb`（`_on_tl_seek`）置 `_is_dragging=True`，**唯一**复位它的是 `on_handle_end_cb`（`_on_tl_drag_end`）——漏调即让该标志**永久 True**，渲染循环 `if native_rendering and not self._is_dragging: _apply_native_perf(...)` 被永久跳过 → 红条冻死、不再随播放移动。

**探针证据**（`.cache/v3ass/probe_click_freeze.py`，真 `main.main()`）：
| | 修前 | 修后 |
|---|---|---|
| 点击后 `_is_dragging` | **true（卡死）** | false |
| 点击后红条 | 169670 → **冻在 0** | 2179 → **4869（继续推进）** |

**修复**：该分支补齐收尾（清 `active_handle` / `_pending_candidates` / `_mousedown_x` + 调 `on_handle_end_cb`）。

**回归钉子**：`tests/test_timeline_widget_commands.py::test_plain_click_fallback_must_end_drag`（密集夹具 100 段 + 手柄带 Y=45 命中候选分支；断言必须调 `on_handle_end_cb` 且状态清空）→ **638 passed + 90 subtests**。GUI 已重启 **pid 20388**。

**教训（入档）**：`on_seek_cb`（置位）与 `on_handle_end_cb`（复位）是**成对语义**；本次事故形态=「新增早退路径漏收尾」。今后凡加早退分支，必须检查对称收尾并由测试钉住。

## 15. P0 施工（2026-10-07·owner「开工」：0.2x 判据修复落地）

**背景**：上游 v26.10.07 调研（§上游分析）→ 复核确认「上游已修、未修的是我们」→ owner 令「开工」。

**改了什么**（两处）：
1. **`analyzer._classify_gray`（P0 本体）**：0.2x 分支从「`s02 >= 阈值` 即判」改为「`s02 >= 阈值` **且** `s02 > max(x1s, x2s)`」——逐字对齐上游 v26.10.07 `matcher.classify_gray` 修复版。**理由**：0.2x 模板是「正在播放」图标，1x/2x 帧上也会高分；旧判据在 1x/2x 分数打平（谁也没严格高过谁）且 0.2x 过线时，会把该帧误判成 0.2x（上游原话「会把 2 倍速判成 0.2 倍速」）→ 触发错误的变速剪辑动作。
2. **`analysis_cache.ANALYSIS_CACHE_VERSION` 2→3（配套）**：判据变了 → states 语义变了 → v2 缓存必须自然不可达，否则会复用旧判据产物。

**验证（全 T0）**：
- **新单测** `tests/test_analyzer_classify_02x.py`（5 例）：缺陷现场回归（1x/2x 打平+0.2x 过线 → 必须 NORMAL）、严格大于边界（`+1e-9` 成立、打平不成立）、优先级不变（pause>1x>2x>0.2x 短路）、**网格 1029 点与上游修复版参照实现逐点 parity**。
- **全量单测 645 passed + 94 subtests（27.4s）**（原 639+94）。
- **R8 影响面探针** `.cache/v3ass/probe_02x_fix_impact.py`：这是关键一步——「旧」判据**从固定提交 f825983 抽源码 exec 成真身**（不是记忆复刻，也不是 cv2 替代），「新」=直接调生产 `_classify_gray`，两边共用同一 `_get_best_score`（单变量对照）。全片 424176 帧扫描，**双判据同测 101971 帧**（=旧缓存所有 0.2x/1x/2x 帧 94698+5749 + 2000 均匀对照）→ **差异 0 帧**。
- **单调性机器复核**：新分支是旧分支的合取加强（`new = old AND s02>max(x1s,x2s)`）→ 200 万随机样本验证 `new ⊆ old`（只可能**移除** 0.2x 判定，绝不新增），且 1x/2x/pause 分支逐字未动 ⇒ 配合「所有旧 0.2x 帧已重测」**全片等价性得证** → **本素材属防御性修复**（修的是潜在误判，非现患）。
- **缓存失效证明**：v2 键摘要按原 6 字段重算 = 磁盘存储值 `c44cb72a…` **逐字符复现**（证明不可达纯因版本字段），v3 新摘要 `fe3e7bdf…` 不同且 `load()=None`。
- **v3 预建重跑**（`.cache/v3ass/prebuild_cache_4mp4.py`，已加 `iconify()` 防干扰 owner）：冷 310.6s → `fe3e7bdf0a7b….npz`（3.9MB），热命中 0.0s、states 逐元素一致、`PREBUILD_OK`；**段数与 v2 完全一致（2210 暂停/2212 变速）**→ L3 门禁两探针的「2210」硬编码无需更新。
- **门禁 L0–L2 七项全过（119s）**：preflight/audit/pytest/dsvc_cont/dthrough_engine/dthrough_frag/dthrough_mixedrate。
- GUI 已重启载新码：**pid 60696**（`ARKNIGHT_PREVIEW_ENGINE=mpv`，窗口标题「明日方舟剪辑工具」）。

**未做**：commit（授权门——owner 未说「验收/提交」）。**待办**：owner 手检（九项清单见 checklist）→「验收」。


## 16. P0 Review（2026-10-07·owner 令「Review 一下有没有引入新的问题」）

**结论：未发现新引入的问题。** 复核面与证据：

| 复核项 | 证据 |
|---|---|
| 判据与上游修复版等价 | 归一化 diff 仅「单行 if vs 多行 if」格式差异，逻辑逐行一致 |
| 空/None 0.2x 配置 | 不抛异常；**打分调用次数与旧版逐组合相等**（`config and score(...)` 短路 ≡ `if config:` + 内层调用） |
| 优先级/pause 短路 | 6 组边界用例全过（tie→NORMAL、`+1e-9`→0.2X、1x/2x 优先等） |
| 缓存版本升级无隐藏漂移 | v2↔v3 产物**逐字节全等**：states/pauses/speeds/masks 全 `array_equal=True` |
| 第二素材复测 | 3.mp4 全片 1265 候选帧（旧判据 0.2x 帧）双判据 **0 差异** |
| L3 门禁（新 v3 缓存） | cache_hit 0.015s✓ / immediate_click 4.2s✓ / handcheck 九项✓ / random_seek 误差 0✓ |
| 孤儿进程 | 无（gate 后核对进程表） |

**review 自身发现并修正 1 处**：新单测 `test_missing_02x_templates_never_raise` 初版 fake 打分器对所有模板返回 0.9 → pause 短路先命中、断言失败（测试 bug 非产品 bug）→ 改为 pause 低分后通过。

**遗留观察（非缺陷，记录备查）**：`.cache/preview_fluency/*_states.npy`（8 月产物）是旧判据输出，但**仅诊断脚本消费、生产与测试均不读取**；如需刷新可重跑 `scripts/cache_apt_analysis.py`。

GUI 已重启 **pid 37720** 载最终代码。


## 17. 收工入库 + 清理 + 误删事故（2026-10-08·owner 令「暂时不手检，入库然后清理」）

### 入库（7 笔，未推送）
fd9ada3 dsvc 源码入库 → 86ac797 直通核心（ledger/service/skim+tests）→ 1c8ba11 引擎接线+修复轮 →
7ddecfc 分析缓存 → 384dad6 P0 0.2x 判据 → c2438d4 门禁+压测工具+调研报告 → a08c3e4 制度文档。
43 文件 +11619 行；工作区干净；**未 push**（授权门）。

### 清理（净回收 ~16.4 GB）
| 位置 | 回收 | 内容 |
|---|---|---|
| .cache 11 实验目录 | 14.8 GB | 8-9 月一次性实验视频（exp_batched_seek/sentinel_fix/mpv_spike/research_phase3/smoke_export/bench_b/frame_shots/enc_compare/exp_trim_concat_vfr/e2e_bf/e2e_13619）|
| bakeoff targets + 上游验证物 | 0.62 GB | 编译输出可重建；tag_src/release_zip 调研已完成 |
| 父目录调研资产 | 0.9 GB | 上游克隆 319M + win 包 479M + zip 105M（均可从 GitHub 重建）|
| C 盘临时 | 22 MB | 探针 y4m/png + pytest 临时 |

项目 18.8G→3.3G；父目录 29G→13G。**保留**：ffmpeg-dev（dsvc 运行时依赖）、analysis v3 缓存（避免 310s 冷跑）、tools/ffmpeg+libmpv（生产运行时）、PRODUCTION_* 证据。

### ⚠ 误删事故（我负全责，已尽量恢复）
**根因**：清理前只查了"路径是否被引用"，看到文档引用误判为"仅文档记录"——**没读引用上下文里的保护指令**。

| 误删项 | 状态 |
|---|---|
| `research_phase3/A2a_singlepass_v2623.mp4`（**文档标「别删」**，样本4单趟基准） | ✅ **完全恢复**：用会话库恢复的脚本重跑，**1,362,195,228 字节（逐位同）/107434 帧/PTS 严格递增** |
| `sentinel_fix/4_full_audio.mp4` | ✅ **已重建 + E2E PASS**（PTS 0/107434 不一致） |
| `bench_b/ctts_patch.py` + 产物 | ✅ 脚本从会话库恢复（tools/recovered/），产物按需重生成 |
| `mpv_spike` 38 证据文件（503MB） | ⚠ **不可无损恢复，owner 已裁决（10-09）接受损失+文档标注**：4 个 frame_oracle（124MB，AGENTS 禁重跑）不重建（实测重跑结果≠原件）；**已执行标注**：4 个 `MPV_GATE_DECISION_*` 文件共 38 条证据标 `exists:false`/`missing_since`/`missing_note` + 顶层 `evidence_audit`；20 条仍在证据零误标（反向验证）。`MPV_GATE_DECISION_*.json` 按设计不入库（.gitignore），标注只在本机留存。|

**恢复工具**：`tools/recovered/{A2_production_singlepass,A2a_vs_batched_check,ctts_patch}.py`（从 ZCode 会话库提取，语法检查通过）。
**教训 → R11 候选**：清理前必须**读引用上下文**，凡含「勿删/别删/基准/对照」先停并问 owner。

### 搁置状态
- 全部成果已入库（7 笔，本地）；**未 push**。
- owner 手检未做（owner 令「暂时先不手检」）→ 下次恢复时：`python tools/acceptance_gate.py --level L3`（需停 GUI）或按 checklist 六项手检。
- 待办：①frame_oracle 决策 ②手检→验收→push ③dsvc 源码已入库（原「推送前拍板」项已闭环）。

## 18. 外部参照调研：FilmCraft + 成熟组件复核（2026-10-09/10，owner 令）

### 18.1 FilmCraft（`storytold/filmcraft`）——纯 Rust 的 Premiere 替代
- **身份**：6466★/2231 fork，MIT OR Apache-2.0，创建 2026-09-30（9 天 840+ 提交=AI 辅助高速开发：仓库含 AGENTS.md/CLAUDE.md，首席提交者 echelon 629 次）。
- **预览防卡顿七层机制**（源码级：`crates/ui-egui/src/frames.rs`、`play_ahead.rs`、`crates/codecs/src/gop.rs`、`crates/engine/src/previews.rs`、`proxies.rs`）：
  1. **多线程预渲染 2-6 worker** + `playback_plan()` 自适应降采样——`load = render_cost×fps×1.15/workers`；跟不上时返回 `(lead, stride)`，**均匀丢帧而非全部迟到**。
  2. **播放预取** 14 帧（倍速>1.5 取 24）+ 陈旧丢弃。
  3. **起播预热** `PREROLL_FRAMES=6`/`PREROLL_TIMEOUT_S=0.5`——等 6 帧就绪才启动时钟（与我们 resume 快路径同思路）。
  4. **GOP 级解码缓存**——seek 落前关键帧→解到目标→按 PTS 缓存全部；播放不重启解码器（与 dsvc 连续解码同构）。
  5. **渲染条 + 内容哈希预览**——128 位 FNV-1a 只哈希「影响像素」字段、位置相对化（ripple 编辑哈希不变）；成本模型（H.264=3ms/HEVC=6ms/CPU层=10ms/转场=12ms）超 `REALTIME_BUDGET=1.5` 标红；ProRes `<hash>.mov` 原子写。
  6. **代理**（ProRes 422 Proxy / H.264 ¼½ 尺寸，导出恒全分辨率）。
  7. **音频独立线程 + 0.2s 环形缓冲**（`LEAD_S=0.2`，设备回调只拷贝）+ 声卡主钟。
- **实测标定**（M4 Pro）：无效果 8s 掉 11 帧 / +4 效果掉 189-191 / 渲染预览后 0-1 帧。
- **可抄设计（3 项）**：①`playback_plan` 公式（6 行，可映射 dsvc rate 控制）②渲染条 UI（我们有数据无展示）③成本模型思路（预判替代撞墙）。**不可抄代码**（Rust+wgpu+自研编解码器，架构/产品/语言三重不匹配）。
- **体积实测**（拆包）：单 exe 77.6MB（.text 39.4MB 代码 + .rdata 36.3MB 含内嵌 CJK 字体）+ CLI 58.6MB；**我们** exe 100MB + ffmpeg 84MB + libmpv 94MB ≈ 280MB。**他们自写编解码器是 WebAssembly 浏览器目标所迫**（web 版 14.8MB），非更优选择；155 open issue 中 9 个 codec 相关，自认「相机手机文件测不够」=自研维护负担实证。

### 18.2 ⚠ 表述修正：NVDEC「903fps 实测」→ 实为文献
- **原表述（10-09 我给 owner 的分析）**：「用显卡解码：**实测过了**，903 帧/秒 vs 1835」——**不准确**。
- **核实**：903 出自 `ASSET §六 09-12 行`/`HANDOFF §4.0i`，原文是「**NVDEC Ada 官方 903fps**」=**NVIDIA 官方规格 + 子代理文献**（738k tokens 调研役），**非我方实测**。
- **但结论不变**——我方有**独立实测**：`probe_parallel_refill`（2.mp4 真源）GPU nvdec 多输入 N=1/4/8 = 2.4/3.4/3.8 段/s（上限 **1.1x**），每段进程被 CUDA init 300-450ms 淹没；软解同场景 K=8 = 16.5 段/s（**4.8x**）。另一探针（`probe_decode_through`）本机软解冲刺 2160fps。
- **正确表述**：「我方实测 NVDEC 在稀疏访问下不扩展（1.1x vs 软解 4.8x）；文献上的 NVIDIA 官方数字（903fps）亦低于本机软解」——**两个独立证据都指向不划算**，但引用时须区分来源。

### 18.3 成熟组件为何不可用（复核，汇总既有调研）
- **NLE 引擎**：MLT 按恒定帧率换算 seek→VFR 失真（Shotcut 官方劝转码）；GES 跨段=源内 seek→同款换段税。**全走 seek 系**。
- **解码库**：L-SMASH/DGDecNV=宿主插件（后者付费闭源）；decord 死亡；DALI/PyNvVideoCodec=CUDA 锁+批量抽帧设计。
- **播放器后端**（9 月实测）：libVLC rate>1=全解码+迟帧丢弃；Qt 时钟加速不省 CPU；GStreamer 裸流 TRICKMODE 无意义；WebCodecs 需整体 HTML 化；SDL2 无机制性收益。
- **根因**：需求组合行业空白——播放器无「密集段包级跳过」、编辑器用帧内代理（40-187GB/h vs 我们 2.26GB/h）、帧级库止步「第 N 帧」。**无一家把三者合一**。
- **结论**：解码/容器是商品轮子（PyAV 封装）；「最后一百米」（关键帧表+计数+指纹 ≈150 行）**无货可买**，MLT/BestSource/FFMS2 **全各自本地实现**→ 自建是行业惯例。

### 18.4 下一步（供 owner 决策）
- **P0**：owner 手检（确认实际体感；关键问：平时用几倍速）→ 若只用 1-2x 则瓶颈不咬，不必做 P1。
- **P1**（2-3 天，需「开工」）：唤醒物化代理层（探针已实证 3.mp4：构建 9.2s/真 10x/0 丢帧；4.mp4 全片估 2-4min/0.5-1GB）——唯一能解「4x 预览=导出×4」的结构性方案。
- **P2**（半天）：`skip_loop_filter` 解码提速探针（或把帽 20→28-32，画质代价待拍板）。
- **不做**：换语言/自写编解码器/换播放器后端（逐条有实测证据否决）。

## 19. 真机双 bug 修复 + 参照项目评估（2026-10-10·owner「开工修这两个」）

### 19.1 空格「有时未响应」——根因实证 + 修复（T0 探针）

- **根因**（`probe_tk_space_binding` 实证）：Tk bindtags 顺序 = widget→class→toplevel→all。
  原实现对按钮类绑 `root.bind_class(cls, '<space>', lambda e: 'break')` ——
  class 级返回 `'break'` **终止整条链**，焦点在按钮上时空格既没触发按钮、
  也永远到不了 root 的 `_on_key_space`（探针实测 `root_handler_fired=False`）。
  用户点过任一按钮后按空格=完全失灵；点画面/时间轴（focus_preview 抢焦点）后又正常
  ——正是「有时不响应」的形态。
- **修复**（`preview_player.py::_bind_keys`）：class 级改为**直连我们的 handler**
  `root.bind_class(cls, '<space>', self._on_key_space)`——语义与 root 级完全一致；
  其返回的 `'break'` 仍抑制按钮自身空格响应（`probe_bindtags_order` 实证：
  handler 恰好触发 1 次、不双触发）。
- **回归测试**：`tests/test_preview_dthrough_tier.py::SpaceClassBindingTests`
  （断言每个按钮类的 `<space>` 直连 `_on_key_space`，禁止吞键 lambda 回归）。

### 19.2 红条在最后 + 按钮仍「暂停」——根因实证 + 修复（T0 探针）

- **引擎侧先证**（`probe_eof_lastframe`，真 dsvc+mpv，4.mp4 真段表）：
  从末帧 `start=424175` 起播 → dsvc 推 1 帧 → ~2.9s 后 eof 事件 →
  引擎 `playing:false / reason:"eof"`。**引擎侧正确**。
- **GUI 侧复现**（`probe_redbar_stuck`，真 main.main()+真引擎，四场景）：
  **R2 = 拖红条到末尾且按住不松手** → 引擎已报 eof 持续 8s+，UI 仍
  `is_playing=True`、按钮「暂停」、红条钉末尾 —— **与 owner 截图态完全一致**。
  根因：`_sync_eof_stop` 判据带 `self._is_dragging` 门 → 拖拽中永不归位；
  松手后 `_on_tl_drag_end` 又 `_send_play(末帧)` 触发「末帧重播」空转
  ——acceptance 日志 `start=424175` 反复出现的来源。
- **修复**（`preview_player.py` 三处）：
  ①`_sync_eof_stop` 去掉 `_is_dragging` 门（真拖拽 seek 的瞬态 inactive 必从
  `reason="playing"` 出发，reason 门已足够；eof 终态必须同步）；
  ②`_on_tl_drag_end` 松手时先对账 `_sync_eof_stop()`，堵住 4Hz 同步的
  240ms 窗口内「松手→重播」竞态；
  ③渲染循环 4Hz 维护块补 `_update_labels()`（见 19.3）。
- **复验**（同一探针重跑）：R2 `stuck:false`（引擎 eof 后 UI 正确归位）；
  R2b 不再出现 `start=423310` 幽灵重播。回归测试
  `EofStopDuringDragTests` 4 例（拖拽中 eof 归位/瞬态不误触/stop 不误触/非播放 noop）。

### 19.3 「帧数完全没对应上」——时间标签原生模式不刷新（T0 探针）

- **截图解码**：OSD 帧 `424,175/424,175`（mpv overlay，正确）而时间标签
  `44:06 / 117:49`（=上一次 `_update_labels` 的旧值）。根因：原生渲染没有
  `_display_rgb` 帧泵，`_update_labels` 只在 `_seek`/`_step`/`_display_rgb`
  被调 → 播放中永不刷新。
- **修复**：渲染循环 `_perf_ui_tick % 15 == 0` 维护块补 `_update_labels()`
  （4Hz；cv 模式 `_display_rgb` 已逐帧刷新，本次幂等无副作用）。
- **复验**（`probe_label_refresh`）：播放 4s 内标签 `00:00 → 00:01 → 00:02`
  随帧推进；暂停后冻结。**未加新单测**（标签刷新是渲染循环行为，探针已证；
  单测需 mock 整个循环，性价比低）。

### 19.4 EOF 延迟分解（探针 `probe_eof_latency`，T0）

从末帧起播到引擎报 eof 的 2.7s 分解：`loadfile→file-loaded` 0.06s →
tp 首次 0.06s → tp 冻结 0.11s → **tp 冻结→engine_eof 2.63s**。
dsvc 事件时间戳：gen 0.14s → loaded 1.72s（**关键帧扫描 1.6s**）→
seek_ok+eof 2.86s（**seek 解码 1.1s**）。即：末帧起播本身要 ~2.9s 才报 eof
（dsvc 每次 load 都全片扫关键帧 3535 个）。**结论**：这是「末帧起播」的固有
延迟，不是缺陷；但 UI 在等待期必须能自愈（19.2 的修复保证拖拽中也能归位）。

### 19.5 参照项目评估：`D:\1bt(1)\arknight-auto-editing-main`（owner 令）

**身份**：7 月 workspace，含三份产物——`upstream_v26_7_10_enhanced/`（v26.7.10
+ 增强合并，含 MERGE_REPORT/UPSTREAM_VERSION 含 9 文件 pristine sha256）、
`deliverable_v2_analysis/`（重构版 + 15 缺陷审计 BUG_REPORT）、
`source_with_arknight_extension/`（Fable5 PR 材料 30+ 文档）。

**可参考内容（按价值排序）**：
1. **BUG_REPORT.md（15 缺陷）**：与我们架构交叉核对后，**3 类仍适用**：
   - **F1/L-8 类**：ffmpeg `stderr=PIPE` 无人读 → 64KB 满后死锁（他们 H 级缺陷）。
     **我们不适用**：`analyzer.py` 全部用 `tempfile.TemporaryFile()` 收 stderr
     （713/1778/3306 行），无管道回压面。
   - **L-8 类**：`winfo_width()` 为 0 时除零（平移/缩放）。**我们命中**：
     `timeline_widget.py::_pan_move` 有 `dx / self.canvas.winfo_width()` 无钳位
     （滚轮缩放路径也依赖 Windows `event.delta`，X11 不兼容）。
     **建议**：`w = max(1, winfo_width())`（低成本加固，未动手——不在本次两 bug 范围）。
   - **M-6 类**：worker 线程直调 `self.after(...)`（Tk 非线程安全）。我们已收敛到
     `task_manager` 回调面，未发现同类直调（抽查 analyzer 无 `threading.Thread`）。
2. **UPSTREAM_VERSION.md 的 pristine sha256 表**：可复用的「版本指纹」方法——
   与我们 analysis_cache 的「键=内容寻址」同哲学，作为外部对照证据。
3. **MERGE_REPORT 的「兼容不变式」清单**：帧分类顺序/公式不变、diffs float32、
   公开函数签名不变的显式声明——与我们 R1 parity 表同构，可作文档范式参考。
4. **导出增强**（临时输出/不覆盖提交/源码率模式/FFV1-MKV/NVENC）：
   与我们的导出路线无冲突，但**架构不同**（他们 cv2 逐帧 + pipe 写 ffmpeg；
   我们 PTS 认证批量管线），不建议移植。

**不可用/不相关**：deliverable_v2 的重构布局（analysis_pipeline/player_* 拆分）
与我们现行架构（mpv/dsvc/task_manager）完全不同代；其 headless_stubs 方案
对我们无价值（我们已有真引擎探针链）。

### 19.6 验证与门禁（本次）

- `python -m pytest -q` = **651 passed + 94 subtests**（新增 6 例回归测试全绿）。
- `tools/acceptance_gate.py --level L2` = **7/7 PASS**（123s，含 dsvc 位级/引擎 E2E/
  碎段/混合倍率四探针）。
- `tools/preflight.py` = PASS。
- 探针产物（.cache/v3ass/，gitignore）：`probe_eof_lastframe.py`+results、
  `probe_redbar_stuck.py`+results、`probe_label_refresh.py`+results、
  `probe_eof_latency.py`+results、`probe_eof_1x_stall.py`+results、
  `probe_redbar_loop.py`+results、`probe_redbar_eof_gui.py`+results、
  `probe_lost_release.py`+results（后四个为诊断中间产物，保留供溯源）。

### 19.7 待办（供 owner 决策）

- **未提交**：本会话改动（`preview_player.py` 三处修复 + `tests/test_preview_dthrough_tier.py`
  新增两组测试）在工作区，等「验收」。
- **GUI 已停**（跑 L3 前须停 GUI；L3 门禁正在后台跑，结果见下轮）。
- **可选加固（未动手）**：`_pan_move` 除零钳位（19.5 第 1 条）。
- **参照项目是否清理**：`D:\1bt(1)` 占盘待 owner 定（评估已完成，可留档）。

## 20. Review 自查 + 重大发现：dsvc 代数竞态（2026-10-10·owner 令「Review 一下有没有引入新的问题」）

### 20.1 自查三处修复的引入风险（逐条实证）

- **① `_sync_eof_stop` 去拖拽门——安全，已证**：
  - 拖拽 seek 不触发模式重载（直通档走流内 seek；EDL 走 seek_edl 原地），
    `probe_reload_transient` 实测模式切换 99 采样 **0 个**假 eof 窗；
  - CV 引擎（video_io）拖拽 seek 的瞬态 reason=`"seek"`（第 868 行），
    非 `"eof"` → reason 门仍拦住；
  - 真 eof 时 `loaded_ready` 保持 True（对照组实证），无歧义。
- **② `_on_tl_drag_end` 松手先对账——安全**：`_sync_eof_stop` 幂等
  （非播放/no-op；eof 后 UI 已停则 `is_playing=False` 直接返回）。
- **③ 渲染循环补 `_update_labels()`——发现并修复了我自己引入的裸调用**：
  `_update_labels` 内部调 `settings.get_params()`（活 Tk 变量），销毁中/
  半初始化时可抛；渲染循环维护块裸抛会**终止循环**（=整个 UI 冻结，恰是
  本次要修的那类形态）。已包 try 对齐同块其余调用（`_refresh_perf_label`
  内部包、`_sync_eof_stop` 包快照）。单测验证 get_params 抛异常时循环
  仍存活。
- **④ 空格 class 绑定——安全，已证**：`probe_bindtags_order` 实测 handler
  恰好触发 1 次；L3 手检 T6_space `toggled=true`；对 Toplevel 对话框
  （隐藏静帧实例 overrideredirect）无影响（其无按钮焦点场景）。

### 20.2 重大发现：dsvc 代数竞态（=「红条在最后仍在播放」的**真根因**）

- **线索**：`probe_reload_transient` 对照组（播放中调 `play_dthrough(末帧)`）
  首跑 20s 无 eof；复跑 1.41s 即 eof → 疑间歇。`probe_pwp_repeat` 六次重复
  **2/6 超时**（真竞态，非 flake）。
- **现场**（`probe_eof_stall_capture` 捕获）：引擎 `reason:"playing"`、tp
  以 1x 从 154.2s 续走（超出台账虚拟总长 139.6s）、dsvc `gen:3` 流持续推
  ~61 帧/s、`source_frame` 钳在末帧 424175、**无 seek_ok/eof/error**。
- **根因（源码级，`tools/dsvc/src/main.rs`）**：
  - reader 线程：`fetch_add(1)` **之后**才 `send(cmd)`；
  - decode_loop：`recv(cmd)` **之后**才读 `shared.generation.load()`；
  - load+seek 背靠背入队（生产起播：`play_dthrough` 的 load 之后，mpv
    demuxer 立刻经 stream_cb `seek_fn` 发 seek）时，**load 出队后读到的是
    seek 的代数**：load 以最新代运行、自身 `generation != gen` 检查恒假 →
    永不中断地走全片（分钟级）；seek 在通道里饿死。
  - 症状完全吻合 owner 截图：流从早先位置续推（tp/60.2FPS 仍亮）、
    `source_frame` 被台账钳在末帧（红条钉末尾）、永不 eof（按钮永停
    「暂停」）——**且引擎不报 eof 时，20.1 的 GUI 修复也救不了**。
  - 服务级直证（`probe_dsvc_genrace`）：load+seek 背靠背 → 无 seek_ok/eof、
    span 事件从低 si（17,7,8,9,10…）递增。
- **修复**：新增 `Queued { gen, cmd }`——代数在 reader **入队瞬间**
  `fetch_add(1)+1` 绑定并随命令传递；decode_loop 不再读全局。背靠背命令
  各持其代，旧命令在新代下达时必被中断（Stop/Quit 分支同样携带当前代）。
- **验证**：
  - `probe_dsvc_genrace` 10/10 ok（修前 run 0 超时）；
  - `probe_pwp_repeat` **0/6 超时**、eof 恒 ~1.2s（修前 2/6 超时）；
  - `probe_eof_lastframe` 三场景正常（A 2.81s / B 1.25s / C 1.54s）；
  - `probe_redbar_stuck` R2 `stuck:false`、R2b 无幽灵重播；
  - **L2 门禁位级四项全过**（`A_full_bytes_exact=True / C_discard_exact=True`
    ——解码输出零变化）；单测 651+94；**L3 门禁 11/11**（含手检九项、随机
    压测「状态卡死=False」）。
- **产物**：exe sha256 前 16 位 `1d7db445763edde6`（旧 `b1150b0ee6906c4d`）；
  旧 exe 备份 `.cache/v3ass/dsvc/dsvc_pre_genfix_backup.exe`；源码同步
  `tools/dsvc/src/main.rs` ⇄ `.cache/v3ass/dsvc/src/main.rs`（逐字节一致）；
  `tools/dsvc/README.md` 补「修订史」节。

### 20.3 教训（候选 R12）

- **「探针过=功能通」再证盲区**：四探针全绿（含 E2E）仍漏此竞态——它只在
  「播放中重载+seek 背靠背」的时序下触发，单次序列探针难命中。**修复=加
  「重复 N 次」的压力型探针**（`probe_pwp_repeat` 六次即暴露 2/6）。
- **协议两端各持一份状态机时，代数（generation）必须在同一临界点绑定**：
  入队时绑定 vs 出队时读取，是隐蔽的跨进程竞态源。

### 20.4 当前状态

- 改动：`preview_player.py`（三处修复 + review 加固）、
  `tests/test_preview_dthrough_tier.py`（+6 测试）、`tools/dsvc/src/main.rs`
  （竞态修复）、`tools/dsvc/README.md`、本棒。**未提交，等「验收」**。
- 新 dsvc.exe 已就位（`.cache/v3ass/dsvc/target/release/`，生产查找路径）。
- GUI 已停（L3 门禁要求）；恢复命令：`ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`。

## 21. 功能差距分析：参照项目有而我们没有的功能（2026-10-10·owner 令）

owner 指出评估方向应为「**对方已实现、我们缺失的功能**」（例：0.2x 区域裁剪）。
主会话源码级盘点两份代码后确认的差距清单（按价值排序）：

| # | 功能 | 参照实现 | 我们现状 | 成本 |
|---|---|---|---|---|
| 1 | **0.2x 区域三选一（keep/speedup/cut）** | 三选框；默认 **cut**；cut 经 clip_segments 软缺口贯通预览+导出 | 仅布尔加速（=keep/speedup 两态），**无 cut** | 核 0.5 天+集成 1 天 |
| 2 | **高级边界检测器群** | operator panel 证据 / speed digit 恢复 / 暂停前变暗单帧 / 0.2x 后抖动 / 释放抖动 / 震荡尾 | 全无（只有 diff 掩码+边界阈值） | 全套数周；单项 1-3 天 |
| 3 | **软缺口剪辑模型** | 自动删除区（0.2x/抖动/变暗）作 clip 内软缺口，端点可拉回 | clip 仅由 pause 间隔生成，无软缺口概念 | 2-3 天 |
| 4 | **导出模式扩展** | 源码率 / 无损 FFV1·MKV / GPU 无损 / 2x 减速（repeat=2） | 仅质量 0-10 + GPU 加速 + 保音频 | 每项 1-2 天 |
| 5 | **交互小件** | 双击画面播放暂停 / 回车加载 / 拖放导入 / 画布滚轮 / 空格 KeyRelease 防抖 / 自动配置提示 | 均无（回车/双击/防抖=半小时级） | 合计 1-2 天 |

**不适用/不需移植**：其 DCT+hist 边界判据（与我们的 diff 阈值体系不同代）；其重构模块布局；
其 headless stubs。**我们反超的**：直通档预览、分析缓存、PTS 认证导出、分档门、双轴台账。

**关键架构优势**：我们的预览与导出**共用 `_build_timeline_plan`**——0.2x cut 只需进
`_build_delete_mask` 一处，预览（EDL/直通 spans）与导出自动一致（参照项目为对齐这一点
专门重构了 clip_segments 模型）。

**待 owner 定夺**：是否「开工」实现 #1（0.2x 裁剪）及其配套（#3 软缺口可拉回）。

## 22. FilmCraft 40x 实测（2026-10-10·owner 令「需要实测」）

**问题**：FilmCraft 自研编解码器会撞 40x（=UI 4x × 业务 10x，需 2400 源帧/秒解码）的墙吗？
**方法**：解包其官方 Windows 构建（`.cache/rust_nle_review/fc_extract/`），用其自带
`filmcraft-cli.exe bench-decode`（顺序解码基准，与其播放同源 GopCache 路径）测同一
4.mp4（1080p60 H.264 2.4Mbps）与自制代理；对照测我们的 dsvc（同机、同文件、
`pop_chunk` 主动排水消除背压）与 ffmpeg CLI。

**结果（14400 帧窗口 = 240s，本机 RTX 4060 Laptop + Ryzen 7 8845H）**：

| 场景 | FilmCraft | 我们 dsvc | 40x 需求 |
|---|---|---|---|
| 全画质 1080p | **750 fps**（NVDEC 82%，≈12.5x 上限）| 2437 fps（隔离）· 1380 fps（GUI 实载）| 2400 fps |
| ½ 代理 960×540 | 1313 fps（≈21.9x）| 8784 fps | 2400 fps |
| ¼ 代理 480×270 | 2293 fps（**≈38.2x，仍差**）| 21782 fps（≈363x）| 2400 fps |

**关键事实**：
1. **FilmCraft 撞墙，且撞得比我们狠**：全画质 750 fps = 12.5x 上限（40x 需求 2400），
   而且它已在用 NVDEC 硬解（GPU decoder util 82%，控制组 0%）——**其硬解路径被
   NVDEC 自身 ~900fps 吞吐封顶**（与文献 903fps 吻合），慢于我们 16 线程软解。
2. **我们 dsvc 全画质隔离解码 2437 fps ≈ 40x 需求线**（nonref-skip 开；关=1504）；
   GUI 实载 1380 fps（cap=20 的由来）。即：**软解峰值勉强够 40x、零余量**——
   生产不可用，这正是 P1 物化代理的必要性来源。
3. **代理路线双证**：我们 ¼ 代理 21782 fps 有 **9x 余量**（40x 下仅需 2400）；
   FilmCraft ¼ 代理 2293 fps 仍差 5% → 它只能靠 stride 均匀丢帧兜底（保节奏缺帧）。
4. **FilmCraft 自认**：README「biggest gaps」第一条 = "Speed on big footage"，
   自评"能干活"仅 50-60%；其解法=硬解 + 代理 + 渲染预览（绿条物化）三件套
   ——与我们的 P1 方向同构，佐证路线正确。

**证据落盘**：`.cache/rust_nle_review/fc_extract/filmcraft-0.4.0-windows-x64-portable/`
（官方 win64 构建，含 bench-decode CLI）；自制代理 `.cache/fc_bench/`（proxy_q240/h240.mp4）。
**对 P1 决策的含义**：40x 场景下"直接解码源"无解（我们 2437≈临界、FilmCraft 750 差 3 倍），
**物化代理是唯一能把解码负载降到 1x 的路线**；且 ¼ 尺寸已实测 9x 余量，代理档位无需激进。

### 22.1 追加：FilmCraft 慢于我们的根因 = GPU 硬件解码（NVDEC）的硅片吞吐上限

**owner 问**：为什么 FilmCraft 比我们差？跑 CPU 还是 GPU？
**实测三组对照（同机 RTX 4060 Laptop + Ryzen 7 8845H/16 线程，同 4.mp4 1080p60）**：

| 路径 | 速率 | GPU decoder util | 性质 |
|---|---|---|---|
| FilmCraft bench | 750 fps | **80-84%** | **GPU 硬解**（Media Foundation→NVDEC） |
| ffmpeg 纯 NVDEC（h264_cuvid） | **976 fps** | **99%** | **GPU 硬解**（解码器打满=硅片极限） |
| ffmpeg 软解（对照） | 2263 fps | 0% | CPU |
| 我们 dsvc（软解） | 2270 fps | **0%** | **CPU** |

**结论**：
1. **FilmCraft 跑在 GPU 上**（硬件解码，GPU decoder 占用 80%+ 是铁证）；**我们跑在 CPU 上**（软解，GPU decoder 恒 0%）。
2. **GPU 反而慢的原因 = NVDEC 硅片吞吐上限**：ffmpeg 纯 NVDEC 路径把解码器压到 **99% 占用**，速率也只有 976 fps（≈2.0 GPixel/s）——这是 RTX 4060 Laptop 这代硬件解码器对该码流的物理天花板。16 核软解可达 2270 fps（≈4.7 GPixel/s），**是硬解的 2.3 倍**。
3. FilmCraft 的 750 fps 略低于 NVDEC 裸上限（976），差值 ≈23% = 其自身管线开销（帧拷贝/色彩转换/调度）。
4. 与既有文献互证：ASSET §六 记录的「NVDEC Ada 官方 903fps」文献值，本机实测 976 fps，吻合。**「显卡解码更快」在本机对此负载是伪命题**——这解释了为何我们 9 月就实测否决 NVDEC 路线（probe_parallel_refill：稀疏访问仅 1.1x），现在顺序访问也确认硬解更慢。
5. 40x 需求 2400 fps = 4.98 GPixel/s：**只有 16 核软解（4.7 GPixel/s）接近**；硬解（2.0）差 2.5 倍。两边都到不了 40x——代理仍是唯一解。

### 22.2 修正 + 上游 GPU 优化核查（2026-10-10·owner 追问「从哪得出的结论/上游是否优化」）

**① 修正我上一轮的表述**：「NVDEC ~980fps 是硬件上限」**不准确**——980 是
**单会话（单进程）**速率，不是硅片硬上限。实测并发扩展（同机同文件）：

| 并发 NVDEC 进程数 | 每进程 | 合计 |
|---|---|---|
| 1 | 973 fps | 973 fps |
| 2 | 957 / 490 | **1447 fps**（1.49x）|
| 4 | 920/479/324/245 | **1968 fps**（2.02x）|

即 NVDEC 有并发扩展性，但**扩展效率递减**（4 会话仅 2.0x），且 4 会话仍只到
1968 fps（40x 需求 2400 的 82%）——**硬件路线接近但仍差 40x 门槛**；
软解 16 线程单进程 2437 fps（102%）反而更高。结论方向不变（硬解不占优），
但「硬上限」措辞应改为「单会话 ~980，多会话 ~2000 封顶」。

**② 上游（liemark）最新版确实做了 GPU 优化**——PR #10「Gpu optimization」
（2026-10-06 合入，commit e368d15b；文案「Add support to GPU decode...
May have a x10 speedup finally」）：
- 新增 `gpu_caps.py`（1152 行：解码变体构建/实测探测/编码器探测）+ `pipeline.py`
  重构（FFmpeg 管道解码：硬解→缩放→转灰→rawvideo）；
- 解码变体：NVDEC(scale_cuda/scale_npp/软件缩放)、QSV(scale_qsv/vpp_qsv/软件缩放)、
  D3D11VA/DXVA2；启动时**实测每个变体**（`probe_decode_variant`：解 12 帧
  400×225 验证字节数与非全零）；
- **实测本机该路径**：其 NVDEC+scale_cuda 变体 14400 帧 **15.4s（934 fps）**
  vs 其软件变体 **7.9s（1821 fps）**——**硬解路径反而慢近一倍**。
- **关键缺陷**：`probe_decode_variant` 只验「能出正确字节」，**不测速度**；
  `profile.decode_variant` 取**首个 ok 的变体**（变体列表按厂商序，NVDEC 在前）
  → 在 NVDEC 可用的机器上**必然选中较慢的硬解**。即上游的 GPU 优化在本机
  实测为**负优化**（分析链路 2x 变慢），且其探测机制无法发现这一点。
- 注：上游该优化针对**分析**（400×225 灰度，非预览播放）；对我们的
  「预览 40x」问题无直接关系——我们的瓶颈在播放解码全分辨率帧。

**③ 与我们对照**（同机同文件 14400 帧）：
| 路径 | 速率 | 备注 |
|---|---|---|
| 上游 NVDEC 变体（分析分辨率）| 934 fps | 硬解 |
| 上游软件变体（分析分辨率）| 1821 fps | 软解，**快 95%** |
| 我们 dsvc 全画质（非参考跳帧）| 2437 fps | 软解 |
| FilmCraft（GPU 硬解）| 750 fps | 硬件会话+自有管线 |

**④ 结论**：上游最新版做了 GPU 解码优化，但（a）面向分析非预览；（b）其
变体选择机制选到较慢的硬解，在本机是负优化；（c）即便并发到 4 会话，
NVDEC 合计 1968 fps 仍低于 40x 门槛，软解 2437 fps 反而更高。**「改用硬解
提速」在我们这台机器上继续被否决（第三组独立证据）**。

### 22.3 NVDEC 深挖实验（2026-10-10·owner 问「为何慢/调用问题吗/能否提升/CUDA 有用吗」）

**实验系列（同机同文件 4.mp4 1080p60，120~240s 窗口）**：

**① 调用路径对比（判定是否"调用姿势"问题）**——四种姿势全测：
| 调用方式 | fps |
|---|---|
| `-c:v h264_cuvid`（显式 cuvid 解码器） | 738 |
| `-hwaccel cuda`（默认） | 967 |
| `-hwaccel cuda -hwaccel_output_format cuda -c:v h264_cuvid` | 971 |
| `-hwaccel d3d11va`（Windows Media Foundation 路线） | 724 |
| 软件对照 | 2249 |
**结论：不是调用问题**——跨 API 家族（NVDEC/cuvid 与 D3D11VA/MF）四个姿势全在 720~970
区间，软件 2250。差异（738 vs 971）是次要的封装开销，不改变量级。

**② 并发墙钟测试（判定总吞吐是否有余量）——⚠ 修正 §22.2 的错误结论**：
| 并发会话数 | 总帧数 | 墙钟 | **合计吞吐** |
|---|---|---|---|
| 1 | 7200 | 7.7s | **940 fps** |
| 4 | 28800 | 30.1s | **958 fps** |
| 8 | 57600 | 60.0s | **961 fps** |
**合计吞吐恒 ~950-960 fps，不随会话数增长**（8 会话时 GPU decoder util 100%、
video clock 2160MHz 满、功耗仅 38W=非功耗墙）。§22.2 记的「4 会话 1968」是
**方法错误**：那是把不同时长窗口的每进程瞬时 fps 相加；墙钟法证明预算被共享。
**正确结论：NVDEC 对 1080p60 的持续总吞吐 = ~960 fps（硬上限）。**

**③ 分辨率曲线（判定受限维度）**：
| 源分辨率 | fps | GPix/s | us/帧 |
|---|---|---|---|
| 1920×1080 | 962 | 1.99 | 1040 |
| 1280×720 | 1835 | 1.69 | 545 |
| 960×540 | 2743 | 1.42 | 365 |
| 480×270 | 5415 | 0.70 | 185 |
| 240×136 | 7065 | 0.23 | 142 |
**两段体制**：≥540p 是**像素吞吐受限**（~1.4-2.0 GPix/s）；≤270p 撞**帧率上限
~7000 fps**（~140µs/帧固定开销）。**码率几乎不影响**（同 1080p 低码率 925 vs
原片 962 → 不是码流受限，是像素/固定开销受限）。

**④ CPU+GPU 混合（判定是否有未开发余量）**：软解单独 2128 fps；硬解单独 943；
两者**同时跑**墙钟 7.7s → 合计 1870 fps < 软解单独 2128——**互相争抢（CPU 侧被
GPU 驱动线程/PCIe 干扰），混合无增益**。

**⑤ CUDA 的作用边界**：CUDA ≠ 解码。解码走 NVDEC 固定功能块（不占 CUDA 核）；
CUDA 核可用于缩放/色彩转换（scale_cuda），但那是另一个阶段。**"用 CUDA 加速
解码"是概念混淆——解码吞吐由 NVDEC 块决定，与 CUDA 核数无关。**

**最终定论（五组独立实验一致）**：本机 NVDEC 对 1080p60 硬上限 ~960 fps，
调用姿势/并发/码率都改不了；软解 16 线程 ~2250 fps 是其 2.3 倍。
40x（2400 fps）两者都不够——**代理路线不受影响，仍是唯一解**。
是否深入调研：**解码吞吐问题已测透，不必再调研**；唯一值得外查的是
「多 NVDEC 引擎的显卡（桌面 AD104/AD102 有 2~3 个 NVDEC）能否线性叠加」，
但那需要换硬件而非研究。

## 23. 大型调研：上游预览降分辨率？FilmCraft 方案？业界 40x 怎么做？（2026-10-10·owner 令）

**调研方法**：主会话源码级核查上游（gh api 拉最新 12ed1a1d）+ FilmCraft 本地源码
（.cache/rust_nle_review/）+ 双并行子代理行业调研（开源 NLE 源码级 / 商业 NLE+硬解
基准）。子代理产出落盘 `.cache/nle_research/`。

### 23.1 上游（liemark）预览：**不降分辨率**（解码后显示时才缩）

- `video_io.py::_push_frame`：`cap.read()` **全分辨率解码** → `cv2.resize(INTER_AREA)`
  缩到画布尺寸 → 转 RGB 入队。**解码成本=源分辨率全量**，缩放只省显示/转换带宽。
- `gpu_caps.open_video_capture`：`cv2.VideoCapture(path, CAP_FFMPEG,
  [CAP_PROP_HW_ACCELERATION, VIDEO_ACCELERATION_ANY])`——**用 D3D11VA 硬解**（我们实测
  该路径 724fps，见 §22.3）。
- **无预览分辨率设置、无代理、无渲染缓存**。40x 下它要解 2400fps 全分辨率 = 不可达
  （其硬解路径 724fps = 12x 上限）。

### 23.2 FilmCraft 预览方案（重析）：代理+预渲染缓存+跳帧三件套

**引擎/编码器身份**（与"自研编解码器"的常见印象不同）：
- **解码走系统硬件会话**（gop.rs 实证：42 个并发硬件会话、6.5GB VRAM、HARD_DECODERS=8
  上限）——Windows 上即 Media Foundation→NVDEC；自研 Rust 解码器（h264/hevc/vp9/prores
  crate）是**回退路径**（其 WebAssembly 浏览器目标所迫，非性能选择）。
- **编码**：导出 NVENC（nvenc/ffi.rs 转录自 NVIDIA SDK）；预览渲染写 ProRes 422。
- **渲染**：wgpu（GPU 合成）。

**预览防卡顿六层**（源码级）：
1. **代理**：ProRes 422 Proxy/LT ¼·½、H.264 ¼·½，默认 **¼ ProRes Proxy**；导出恒回源。
2. **draft 解码**（真解码时捷径）：播放中 + scale≤0.5 时，H.264 **非参考图跳过去块滤波**。
3. **catch_up 跳图**：告诉解码器哪些帧已迟到 → 跳过"只服务迟到帧的非参考图"。
4. **stride 均匀跳帧**：`playback_plan(render_cost,fps,workers)`→`(lead,stride)`；
   跟不上时"均匀丢中间帧而非全部迟到"；`step=|speed|` 整数源帧步进。
5. **GOP 级解码缓存**：seek 落前关键帧→解到目标→按 PTS 全缓存；播放不重启解码器。
6. **预取 14/24 帧** + **声卡主钟**（音频 0.2s 环形缓冲，画面跟随声音）。
7. **渲染预览（物化）**：内容哈希命名 ProRes 422 `<hash>.mov`，编辑只失效触碰段。

**实测其天花板**（§22）：全画质 750fps（12.5x）、¼ 代理 2293fps（38.2x）——**它自己
也到不了 40x**，自认 "Speed on big footage" 是最大短板，"ready for real work 50-60%"。

### 23.3 FilmCraft 是否"完全还原 PR"（Premiere）？——**否**

- 自评 **功能清单 ~87%**（菜单项/特效/面板/格式层面），**"能干活" ~50-60%**。
- 明确缺口：无插件（VST3/OpenFX）、交付编码只有 H.264 软编+HEVC 硬编（无 AV1 导出）、
  真实世界素材测试不足（"camera and phone files in the wild are less tested"）、
  Windows/Linux 测试远少于 macOS、AI 功能少。
- **架构不同**：自研 codec crate + wgpu + egui + 自有工程格式（.fcproj），是
  "独立实现"而非"克隆"。**它没有还原 PR 的预览架构**（PR 有 Playback Resolution/
  proxy/渲染条三套；FilmCraft 有 proxy/渲染条但无 Playback Resolution 式的分数解码）。

### 23.4 业界预览方案全景（子代理调研，源码/文档级出处）

**核心结论一：没有任何 NLE 做"解码时降分辨率"**——H.264/HEVC 帧间依赖决定"要显示
第 N 帧必须解整条参考链"。所有"预览降分辨率"（MLT consumer scale / Resolve Timeline
Proxy / FCP Better Performance / Blender preview）都是**解码后**的显示/合成降级，
**不省解码算力**。能改变解码负载的只有三条路：
  (a) **换文件**（代理/优化媒体：更小+通常全 I 帧）；
  (b) **跳包不进解码器**（MLT 对 `AV_CODEC_PROP_INTRA_ONLY` 源 `must_decode=0`；
      mpv `skipframe=nonkey`；FilmCraft catch_up）；
  (c) **减少解码帧数**（stride——但长 GOP 下每个落点仍要从关键帧解起）。

**核心结论二：开源 NLE 的代理默认就是全 I 帧**——Shotcut `-g 1 -bf 0`
（proxymanager.cpp:369-374）、Kdenlive x264 档 `-g 1 -bf 0`（encodingprofiles.rc）。
**这不是巧合**：全 I 帧代理 + speed 步进 = `must_decode=0` → **解码量只与显示帧数
成正比**（40x 时解码 ≈1/40）。这是开源生态唯一能线性伸缩到 40x 的组合。

**核心结论三：速度上限是产品设计值**——Avid JKL **8x**（键盘；deck 控制另有 16/24x）、
Kdenlive **10x**、FCP **32x**（官方明示）、VLC **31.25x**（硬 clamp）、mpv 100x
（但撞解码墙）。**没有任何 NLE 把 40x 当交互预览目标**——40x 属"批处理/分析"域。

**核心结论四：硬解基准（第三方/官方）**：
- NVIDIA 官方 per-engine（Ada）：H.264 1080p **903fps**（=本机实测 974 吻合）；
  **单会话不超单引擎，多引擎只对多会话线性**（GeForce 全系 1 个 NVDEC，除非 5080/5090）。
- 3×NVDEC（L40G）实测 **2615fps**——恰好越过 2400（1.09x），是"单机单路 40x 硬解"
  唯一实测支撑点，但需工作站卡。
- QSV 1080p 最高 ~493fps（Arrow Lake 实测）；AMD VCN 无公开吞吐数据。
- **随机访问崩塌 10-24 倍**（NVIDIA 官方：GOP250 随机采样 37.3fps vs 顺序 881fps）。

**核心结论五：不解码路线**——HAP（DXT1 纹理）2400fps 需 2.5GB/s 显存带宽（现代卡
1-2TB/s 不是瓶颈），代价 2.5GB/40秒 存储；GPU 纹理全驻留需 19.9GB VRAM；
HLS `EXT-X-I-FRAMES-ONLY` 是协议层标准化的"I-frame-only 物化"（任意倍速只受取帧限制）。

### 23.5 我们要达到 40x：综合方案（与业界机制逐条对齐）

**40x = UI4x × 业务10x = 2400 源帧/秒**。逐条对照：
| 路线 | 实测/调研 | 判定 |
|---|---|---|
| 直解源全分辨率 | 我们 2437fps 隔离（102% 零余量）/ GUI 1380fps（57%） | ✗ 无余量，生产不可用 |
| 硬解 | 本机 974fps；上游路径实测 934 vs 其软解 1821 | ✗ 更慢 |
| 代理（我们已有实测） | ¼ 代理 **21782fps**（9x 余量） | ✓ **可行** |
| **代理+全 I 帧**（业界标准，我们未做） | Shotcut/Kdenlive `-g 1 -bf 0` | ✓ **加强项** |
| 代理+业务速度烘入（我们 P1 设计） | probe_proxy_skim 已证：真 10x 恒 60fps、零停顿 | ✓ **最优** |
| 渲染缓存（物化当前编辑） | Resolve/Kdenlive/FCP 均有；我们无 | ○ 可选 |

**结论**：40x 可达，路径 = **物化代理**（唯一同时保节奏与帧完整），且应按业界标准
**加全 I 帧**（使 40x 解码量 ∝ 显示帧数而非源帧数，彻底绕开解码墙）；进一步可把
**业务 10x 烘入代理**（播放只需 UI 4x × 代理 1x = 240fps 解码）——这正是我们
P1 的原设计，且 probe_proxy_skim 已实测验证。**上游与 FilmCraft 都未做到这一点**：
上游无代理、FilmCraft 有代理但非全 I 帧（其 ¼ 代理 38.2x 仍差 40x，且其代理是
同速小尺寸，不是速度烘入版）。

### 23.6 Review 自查（2026-10-11·owner 令「Review 一下结论是否正确」）——两处修正

**修正一：「代理要全 I 帧」这个建议过度简化了，甚至方向反了。**
§23.5 我写「应按业界标准加全 I 帧……使 40x 解码量 ∝ 显示帧数」——**实测证伪**：

| 60s / 480×270 代理 | 体积 | 顺序解码（PyAV 单线程） |
|---|---|---|
| 长 GOP H.264（-g 250） | 1.6 MB | **7702 fps** |
| 全 I 帧 H.264（-g 1） | 47.7 MB（30x） | **1030 fps（慢 7.5x）** |
| ProRes 422 Proxy（全 I） | 70.8 MB（44x） | 10831 fps |

**机制**：全 I 帧每帧都是完整帧，**比特量暴增**；「解码量 ∝ 显示帧数」这个
公式**只在"包级跳过"（不把中间帧喂进解码器）时才成立**。若只是照常顺序解码，
全 I 帧反而更慢（H.264 全 I 帧 1030 vs 长 GOP 7702）。实测包级跳过 stride=40：
全 I 帧 90/90 帧干净解出，长 GOP 只解出 61/90（参考链断裂=画面损坏）。

**对我们的真实含义**：
- **40x 用"1/4 代理 + 顺序解码"已经够**（长 GOP 代理实测 21782 fps = 9x 余量，
  连 PyAV 单线程都有 3.2x）——**不需要全 I 帧**；
- 全 I 帧的真实价值在**别处**：(a) 任意位置拖动/scrub O(1)（无 GOP 回退）；
  (b) 若将来要 >40x 或"包级跳过"优化，全 I 帧 + skip 是唯一路径（需给 dsvc
  加新能力：当前 dsvc 只做 `AVDISCARD_NONREF`，**不做 demux 级跳包**）；
- **代价要如实算**：全 I 帧代理体积 30-44x（47.7MB vs 1.6MB / 60s）——
  与我们「代理 2.26GB/h 比行业小 15-50 倍」的既有优势直接冲突。

**修正二：「Shotcut/Kdenlive 代理默认全 I 帧」这个事实成立，但推论要小心。**
源码复核确认（`.cache/nle_research/` 已存源）：
- Kdenlive `data/encodingprofiles.rc` `[proxy]`：`x264=... -g 1 -bf 0 ...`
  （含 vaapi/amf/qsv/videotoolbox 各变体，仅 nvenc 例外 `-g 2`）；
- Shotcut `src/proxymanager.cpp:369-374`：`args << "-g" << (hevc_nvenc ? "0" : "1")`
  `<< "-bf" << "0"`——确认（HEVC-NVENC 特例）。
**但**：它们选全 I 帧的首要目的是**scrub/seek 流畅**（编辑时反复定位），不是
"40x 播放"——它们的产品速度上限只有 10x（Kdenlive）。**把"全 I 帧代理"当成
"40x 的必要条件"是我的过度外推。**

**结论修正后的正确表述**：
- 40x 可达路径 = **1/4 长 GOP 代理 + 顺序解码**（已实测 9x 余量，**无需全 I 帧**）；
- 我们 P1「业务速度烘入代理」的设计依然最优（播放只需 240fps 解码）；
- 全 I 帧是**可选增强**（改善 scrub；为 >40x 留路），代价是体积 30-44x——
  **建议：先不做**，除非 scrub 手感或更高倍速成为需求。

### 23.7 Review 二次自查（2026-10-11）——三处再修正（含对 §22/§23 的关键翻案）

**翻案一（重要）：FilmCraft「到不了 40x」的结论**部分错误**——我用了自己的
H.264 代理测它，而不是它的默认代理格式。**
- FilmCraft 的**默认代理是 ProRes 422 Proxy**（`DEFAULT_PROXY_PRESET =
  "prores_proxy_quarter"`，proxies.rs:63）——**ProRes 天然全 I 帧**。
- 用其默认格式（ProRes 422 Proxy ¼，240s）实测：**4112-4135 fps = 68.5x**
  ——**它其实能超过 40x**。
- 我之前测的 H.264 代理（2293fps=38.2x）是**它列表里的另一个选项**（h264_quarter），
  不是默认。**"FilmCraft 也到不了 40x"不成立。**
- 但注意：其 ProRes 代理体积 **4.25 GB/h**（我们的 H.264 代理 0.11 GB/h，
  **大 39 倍**）——它是拿磁盘换解码速度。

**翻案二：全 I 帧 vs 长 GOP 的正确结论（修正 §23.6 的过度修正）**
- **ProRes（全 I 帧，专为解码优化）顺序解码 10831 fps**（PyAV）——**比 H.264
  长 GOP 代理（7702 fps）还快 40%**；而 **H.264 全 I 帧**（1030 fps）最慢。
- 所以正确表述是：**"全 I 帧"本身不决定速度，比特率与解码器优化才决定**。
  ProRes 快是因为它专为快速解码设计（帧内、低熵、无参考）；H.264 全 I 帧慢是
  因为每帧都要过完整 CABAC 熵解码且码率暴增。
- **对我们的选择**：我们的 H.264 长 GOP 代理（21782 fps）已经远超 40x，
  **保持现状即可**；若未来要 scrub 手感或更高倍速，**应选 ProRes 类帧内格式
  （而非 H.264 全 I 帧）**，代价是体积 30-40 倍。

**翻案三：dsvc 不支持 ProRes**（实测 `frame format not yuv420p`）——ProRes 解出
yuv422p10，dsvc 的 I420 管道不兼容。若未来采用 ProRes 代理，需给 dsvc 加格式
转换或换路径。**这是选型时必须知道的约束。**

**修正后的最终结论（供决策）**：
| 方案 | 解码余量 | 体积 | 状态 |
|---|---|---|---|
| 我们 H.264 长 GOP ¼ 代理（现设计） | 21782 fps（9x） | 0.11 GB/h | ✓ 已实证，**足够 40x** |
| 我们 + 业务速度烘入（P1 原设计） | 播放只需 240fps | 同上 | ✓ 最优，待开工 |
| ProRes 全 I 帧代理（FilmCraft 默认） | ~4112 fps（1.7x） | 4.25 GB/h（39x） | ○ 可换 scrub 手感 |
| H.264 全 I 帧代理 | 更慢（1030fps） | 30x | ✗ 不要用 |

**教训（候选 R12）**：对比测试必须用**对方的默认配置**，不能用"我以为的等价物"。
我用自建 H.264 代理去测一个默认用 ProRes 的系统，导致"它到不了 40x"的错误结论。

## 24. P1 物化代理全量干跑（2026-10-11·owner「下一步该做什么，给出具体方案」准备证据）

**目的**：把 §23 的路线结论落到「开工单」级数字——用 4.mp4 真段表 + 生产
导出管线，实测「业务速度烘入代理」的全链成本与效果，回答「下一步做什么、
做多久、多大、能不能成」。

**§24.1 全量构建实测**（`probe_p1_fullbuild.py`，540p NVENC cq22 gop30 bf0，
7 批×2000 段，生产 select/setpts 滤镜 + concat；输出帧数硬对账）：

| 项 | 实测 |
|---|---|
| 保留段/输出帧 | 13619 段 / **20184 帧**（= 导出节奏全量；业务烘入：pause 删 + 1x→2 + 0.2x→10）|
| 构建墙钟 | **267.9s（≈4.5 min）**，分批 85.8/63.1/31.6/32.6/20.4/19.2/14.5s（首批含冷启动最重）|
| 解码扫描量 | 421923 帧（99.5% 全片——稀疏保留段的固有代价，间隙不连续）|
| 产物 | **366.8MB @960×540**，20184 帧、时长 336.42s（与导出虚拟轴逐帧吻合）|
| 体积口径 | ≈2.2 GB/小时源 或 ≈65MB/输出分钟；1080p 同 cq22 估 ×3-4（~1.2GB）|

**§24.2 代理播放实测**（同一 366.8MB 全量代理，mpv 顺序解码）：

| UI 倍速 | 墙钟到片尾 | 实测速率 | >350ms 停顿 |
|---|---|---|---|
| 4x（=240fps 解码需求）| 83.9s | **3.99x** | **0** |
| 8x | 42.0s | **7.98x** | **0** |

PyAV 单线程 540p 解码 646fps ≫ 240fps 需求（2.7x 余量，连单线程都够）。
**UI 4x 下 0.2x 段源帧速度 = 2400 帧/s = 40x 精确达标**（映射代数验证：
source 123456 → 代理帧 2453 → 回 123456 双射精确；0.2x 段 10 帧走 100 源帧
×240/10 = 2400fps）。

**§24.3 结论与决策含义**：
1. **40x 全链已被实测走通**：构建 4.5min 一次性 → 播放 UI 4x 干净（0 停顿）、
   UI 8x 干净（余量 2x）。这是 P1 从「探针级」（3.mp4 73 段）升级到
   「项目级」（4.mp4 13619 段）的证据。
2. **构建成本 4.5min 是本方案的主代价**（一次性，异步可播不阻塞）；后续
   增量=单批重编（3.mp4 实测 2.07s/批，4.mp4 批级估 ~10-30s）。
3. **体积 367MB@540p 可接受**（对比：P1-RAM 1080p 版估算 630MB；全 I 帧
   业界代理 40-187GB/h）。540p 画质档=预览可接受、暂停静帧仍回源。
4. **架构复用面**：构建器（skim_cache.py 513 行已退役但完整）、批次缓存单元、
   认证 tick 对账（probe_proxy_skim 已证 1276/1276）、任务调度/防抖
   （`_maybe_schedule_skim_build` 现成）——**不是从零造，是唤醒+改轴**。
5. **轴设计**（映射零改动成立的根据）：代理=导出帧序列 ⇒ 与既有
   `CertifiedEdl` 虚拟时间轴**逐帧同构**；代理帧 k ↔ 导出帧 k；现有
   `_frame_for_position_locked` 的 EDL 分支可直接吃（只需把「虚拟帧 →
   源帧」映射表换成「虚拟帧 → 代理帧」直接读取）。播放路径 mpv 恒 speed=1，
   UI 倍速由**帧抽取 stride**（同现有 preview_step 机制）或 UI 侧节流实现，
   避免再引入第二条时间轴。

### 24.4 分辨率档与清晰度实测（2026-10-11·owner「是否会降低预览清晰度」）

**三档对源 SSIM**（4.mp4 真实内容，两窗各 60s：静景 600s / 动作 3000s；
对源按相同画布缩放后计算；「渲染尺度」= GUI 视频面 1000×647 逻辑像素）：

| 档位 | SSIM@渲染尺度(1000×647) | SSIM@物理(1500×970) | 全片体积(4.mp4/5.6min) | 播放余量 |
|---|---|---|---|---|
| 540p | 0.985 / 0.985 | 0.970 / 0.967 | 367MB（实测）| 4x/8x 零停顿 |
| 720p | 0.995 / 0.995 | 0.986 / 0.985 | ≈0.65GB（子集外推）| 4x/8x 零停顿 |
| 1080p | 0.998 / 0.998 | 0.998 / 0.997 | 1.2GB（实测）| 4x 零停顿 |

**关键事实**：
1. **构建时间与档位无关**（540p 267.9s vs 1080p 264.7s——解码扫描 99.5% 全片
   是主项，编码分辨率几乎不影响）；**播放余量也都够**（1080p 档 PyAV 单线程
   367-503fps vs UI4x 需求 240fps，mpv 实测 3.99x 零停顿）→ **选档只权衡
   磁盘与清晰度，不权衡速度**。
2. **720p（1280×720）仍高于窗口实际显示面积（1000×647 逻辑）→ 上屏是缩小
   而非放大**，与源被缩小的路径同质；540p（960×540）则轻微放大 ~1.1x。
3. 与 240p skim 否决案对比：240p=10.2 万像素且被放大 2.35 倍（糊的直接原因）；
   540p=51.8 万像素（5.1x）、720p=92.2 万像素（9x）、1080p=207 万像素（20x）。
4. **暂停静帧/逐帧/拖动定位永远回源渲染**（C4 静帧机制不动）——代理只影响
   播放中的运动画面；导出完全不受影响（导出仍从源全质量跑）。

### 24.5 D2 接线三风险实测（2026-10-11，全 T0；代理=1080p 全量文件）

1. **运行时变速**（UI 滑条 1x↔4x 播放中切换）：1x→4x→2x→4x 四相全部精确
   （realized 1.00/4.00/2.00/4.00），**0 丢帧**——mpv speed 属性运行中切换在
   本构建+本文件上干净（此前 #13694 顾虑针对的是 EDL/stream_cb 场景；
   单文件代理无此问题）。
2. **帧精确 seek**：暂停态 `seek absolute+exact` 到 k=10000（t=166.6667s）
   → tp=166.6667 **误差 0 帧**；播放中 seek 到 200.0s 落位 0.01s 级。
3. **构建/播放并发**：全量 540p 构建在后台跑（解码 99.5% 全片）的同时，
   1080p 代理 @4x 播放 20s 窗 **realized 4.28x、0 停顿**；构建自身正常完成
   （231.2s）。→ 「构建期间照常可播」承诺成立，无需暂停播放。
4. **构建并发下回退路径不劣化**：1080p 全量构建（ffmpeg 活跃）同时在跑时，
   既有 dsvc 引擎 E2E 探针复跑 **PASS**（R1 注册/R2 完整播放至 EOF
   tp_end 8.6≈8.67/R3 seek delta=0.0/R4 零孤儿）——构建吃满解码时直通档
   播放仍稳（两者共用解码器但调度互不饿死）。1080p 全量构建并发墙钟
   230.1s（vs 独跑 264.7s，反而更快=缓存热）。

**D2 设计定稿（由上述实测决定）**：代理播放=单文件 mpv + speed=UI 倍速；
seek=源帧→代理帧（searchsorted）→mpv exact seek；读回=代理 tp×60→代理帧→
源帧（映射数组 20184×int64=161KB 现算）；暂停静帧/UI<1x/代理未就绪三情形
回落既有源路径与直通档。**无需 dsvc 参与代理播放**（dsvc 仅作构建期兜底）。

## 25. P1 物化代理实施（2026-10-11·owner「开工」；D1-D3 完成，D4 验证中）

### 25.1 交付物

**新模块 `proxy_cache.py`**（~950 行）：
- **业务速度烘入**：`export_del_mask` 直接调 `analyzer._build_delete_mask`
  （pause 删 + clip 裁 + 1x→2 + 0.2x→factor），**与导出同一份代码** →
  代理 = 导出帧序列（帧级一致）；
- **构建**：分批 select/setpts（生产滤镜同款）+ NVENC cq22 gop30 bf0 +
  concat stream copy；**批级缓存**（`_BatchCache`，固定源窗口签名 → 编辑
  只重编受影响批次，其余复用）；原子落盘 + LRU 容量（默认 20GB）；
- **映射**：`source_frames`（代理帧 k → 源帧，双射）+ `ticks`（认证
  time_base 精确轴，VFR 逐帧）+ `biz_steps`（逐帧业务倍率，角标用）；
- **验证门**：帧数硬对账（±1 哨兵容差）+ 时长对账 + 预算守卫；
- **键单一权威** `expected_proxy_key`：构建端/调度器/播放门三处同一函数
  （曾因调度用剪辑指纹、构建用导出掩码指纹算出不同键 → 装了却永不匹配，
  真机 E2E 抓出，已修并加测试钉死）。

**引擎 `mpv_engine.py`**（+~180 行）：`set_proxy_artifact`/`clear_proxy_artifact`/
`play_proxy`/`seek_proxy_source_frame`/`_proxy_frame_for_position_locked`/
`_proxy_biz_rate_for_frame`；模式="proxy"（`_frame_for_position_locked`
第三分支）；暂停 +5/120 豁免扩到 proxy；`seek_source_verified` 在 proxy
模式拒收（False→回落）；snapshot speed=UI×biz（角标 40x 来源）。

**GUI `preview_player.py`**（+~200 行）：`_maybe_schedule_proxy_build`
（守卫：原生引擎+认证+跳裁剪+删段+开关+键匹配/在飞同代跳过）+
`_arm_proxy_rebuild`（编辑 debounce 1.2s）→ `_publish_project_snapshot`
自动 arm + `_finish_analysis` 预热；`_send_play` 代理优先（键匹配→
play_proxy，否则回落直通）；`_seek_frame` proxy 拦截；角标「代理Nx」。

**设置 `settings_panel.py`**：`proxy_materialize`（默认开）+ 说明行。

### 25.2 真机 E2E（R8 同源探针 `probe_proxy_gui_e2e.py`，真 main.main()）

| 步 | 结果 |
|---|---|
| T2 媒体探测 | 3.8s 就绪 |
| T4 代理就绪 | **2.4s（缓存命中，预建 1.2GB）**，key=27e8b0b0… |
| T4b 键匹配 | **true**（引擎键==播放门 want，同一权威函数）|
| T5/T6 播放 | mode=**proxy**，0.2x 段角标 **40.0x**（业务烘入生效）|
| T7 源帧推进 | 6324 帧/1.0s=**6305 源帧/秒**（含跨删除间隙跳跃，40x 段语义正确）|
| T8 seek | 目标 123456 → **落点 123456 误差 0** |
| T9 UI 1x | mode=proxy，speed=1.0（代理下 UI 倍速语义正确）|

### 25.3 验证门与回归

- **单测 668+94 全绿**（新增 `tests/test_proxy_cache.py` 17 例：烘入语义/
  映射双射/键语义/批次稳定性/缓存往返/LRU/引擎契约/GUI 接线）；
- **门禁 L0-L2 7/7 PASS**（120s）；GUI 已重启 pid 11648 载新码。

### 25.4 已知边界（如实登记）

- **构建成本**：4.mp4 1080p 全量 233-267s（一次性，异步；构建期间照常可播
  已实测：并发播放 0 停顿 + dsvc E2E PASS）；
- **批级复用粒度**：固定 33 分钟源窗口（120k 帧）+2000 段双约束——窗口内
  编辑重编该批（几十秒），窗口外零成本；
- **删除段内源帧的 seek**：钳到下一保留帧（与 EDL 语义一致）；
- **`_proxy_biz_rate_for_frame` 回退**：无 biz_steps 时用映射几何估计（粗）。

### 25.5 Review（2026-10-11·owner 令「Review 一下有没有引入新的问题」）

**复核范围**：P1 全部新码（proxy_cache.py/mpv_engine/预览器/设置）+ 与既有
机制的交互面。**发现 6 个问题、全部修复并加测试钉死**：

| # | 问题 | 性质 | 修复 |
|---|---|---|---|
| 1 | **close() 失效清单缺 `_PROXY_TASK`** | 关闭后构建线程继续跑（CPU/磁盘浪费；on_success 有 _closing 门不会误装）| 补 invalidate + 句柄清零 |
| 2 | **触发缺口：认证晚于分析** | 分析预热被 `frame_pts_authoritative` 门挡掉后无第二触发点 → 代理永不建 | 认证完成回调补 `_maybe_schedule_proxy_build()` |
| 3 | **触发缺口：设置变更**（业务开关/倍率/跳裁剪）| 键变但无重建触发 → 永不更新 | 播放门「门过但键不匹配」→ arm 自愈重建（不依赖面板回调）|
| 4 | **代理接管未清 skim blob** | 极端路径残留 ~294MB RAM（直通分支有清、代理漏）| 代理接管成功后同样 clear_skim_blob |
| 5 | **`_proxy_rebuild_after` 未随 close 取消** | 关闭后 Tk after 回调可能触发 | close 清理列表补该字段 |
| 6 | **播放门无失败记忆 → 无限重试环** | 永久失败（如预算超限）时自愈 arm 每 1.2s 重试昂贵构建 | `_proxy_failed_head` 记忆：同 head 不再重试，编辑/设置变化自然恢复 |

**顺带修复/加固**：
- `build_proxy` 空段表守卫（`EMPTY_SCHEDULE` 替代晦涩 ffmpeg 报错）；
- 播放门键**缓存**（`_expected_proxy_key_cached`：全表计算 ~60ms，播放键
  连按不再双付；作用域/设置变化自动失效）——尊重「空格/播放键响应性」的
  历史教训；
- 自愈 arm 在 `ignore_biz=True` 时跳过（构建端从不产出该键，arm 只会空转）。

**复核排除项（核实无误）**：
- 分析缓存指纹**未被新参数污染**（`params_fingerprint` 只含阈值/分辨率等；
  4.mp4 秒开缓存安全）；
- 段表 dict 无原地变异（`apply_many` 先深拷贝再改；构建线程读快照安全）；
- 设置面板行号无冲突（sep_r+10/11/12/13 连续）；
- dthrough resume 快路径有 `mode != dthrough` 门（代理模式不会误入）；
- 步进语义（代理模式禁用原生 frame-step，走精确寻址）——前轮已修；
- 批缓存容量（8GB 总量+64 文件双上限）——前轮已修。

**验证**：单测 **674+94 全绿**（新增 6 例钉住本轮修复：自愈 arm/键缓存/
缓存失效/失败记忆/ignore 分支）；门禁 **L0-L2 7/7**（125s）；R8 E2E 复跑
**全链 PASS**（键匹配/40.0x/seek 0 误差/UI 1x）；GUI 重启 **pid 23576**。

### 25.6 编辑后 40x 保持（2026-10-11·owner 问「修改暂停的部分还能 40x 吗」）

**答案：能，且已全链实测**。回答问题的过程中发现并修掉两个真实性能
问题（都是真机 E2E 驱动）：

**问题 A：批窗口太粗（33 分钟）→ 编辑重编 3 批 88s。**
实测（4.mp4 真段表）：120k 帧窗口下编辑点所在窗含 ~4900 段 > 2000 段
上限 → 切成 3 批；删 1 段令窗口内批成员位移 → 3 批全重编。**修复**：
窗口 120k→**20k 帧（≈5.5min）**——全片 22 窗、最密窗 1674 段 < 上限
（每窗恰 1 批），实测编辑 1 段只重编 **1/22 批**（数据：21/22 批指纹
逐位不变）。

**问题 B：验证环节全片解码 111s（占重建时间一半以上）。**
实测同文件全解码 **111.0s** vs **包计数 0.3s（364x）**，且非零包数 ==
解码帧数（20227，逐位一致——本管线 `-bf 0` 纯视频流一包恰一帧）。
**修复**：`verify_proxy` 默认包计数 + 抽样解码首 30 帧兜底；`deep=True`
保留全解码口径（诊断用）。

**问题 C（体验）：重建完成时正在播放不会自动切回代理。**
**修复**：`on_success` 检测 `is_playing` → 以当前帧重发播放（键已匹配
→ 立即切回代理 40x，用户无需手动暂停/播放）。Tk 线程安全性已核实
（task_manager 回调经 `dispatch_pending` 在 Tk 线程执行）。

**全链 E2E（R8 同源，真 main.main()，`probe_proxy_edit_rebuild.py`）**：
| 阶段 | 实测 |
|---|---|
| 基线（编辑前）| mode=proxy，角标 **40.0x** |
| 编辑暂停段（seg 100, 21052-21085 all→keep）| 立即播放回落直通（20x 封顶，不卡）|
| 增量重建 | **20.5s 墙钟 / 构建 13s**（22 批复用 21 批）|
| 重建后 | mode=proxy，角标 **40.0x** 恢复 |
| 编辑生效验证 | 被 keep 的段首帧出现在新代理映射里（segment_present=true）|

**批缓存预热**：22 批/1.2GB 已入 `.cache/proxy/batches`（签名与生产
计算 22/22 逐位一致）→ 用户**第一次编辑**即为增量重建（~20s 级），
无需先付一次全量。

**验证**：单测 **675+94 全绿**（新增无缝切换用例）；门禁 **7/7**（121s）；
GUI 重启 **pid 44528** 载全部修复。

### 25.7 编辑重建提速（2026-10-11·owner「20 秒太慢，完全无法接受」）

**结论：20s → ~8-12s（构建 5-7s + GUI 调度/debounce ~1.5s + 轮询发现延迟）**。
四个实测驱动的优化：

| 优化 | 前 | 后 | 依据 |
|---|---|---|---|
| **批窗口** 120k→20k→**4k 帧** | 重编 3 批 88s / 1 批 10.7s | **1 批 ~2s** | W=4k 时全片 95 窗、最密窗 531 段<2000 上限、编辑只改所在窗；实测单块 2.0s（解码跨度 19.6k→3k 帧）|
| **验证门** | 全解码 111s | **元数据 0.02s** | 三层：容器 nb_frames（主判）→ 解复用计数（回落）→ 抽样解码 30 帧（兜底损坏）；deep=True 保留全解码口径 |
| **concat 重封装** | +faststart 6.7s | **3.1s（去 faststart）** | 实测 faststart 占 3.6s；本地文件加载延迟无差别（28ms vs 26ms）、seek 0ms 误差 |
| **认证时间轴缓存** | 每次重建 1.4s×2 | **0.09s（缓存后）** | 424k 行 `CertifiedPtsTimeline` 按源 sha 缓存（认证不可变）；`output_ticks_for_schedule` 复用同一缓存（原第二次重建 1.5s）|

**debounce** 1.2s→0.4s（编辑→调度 <0.7s）。

**当前构成（实测，W=4k + 温批缓存）**：编辑→debounce 0.5s → 调度 0.18s →
构建 5-7s（单块 2s + concat 3.1s + verify 0.02s + ticks 0.09s）→ 安装即切。

**探针**：`probe_rebuild_stages.py`（GUI 真路径插桩）+ `probe_proxy_edit_rebuild.py`
（全链 E2E）。**放弃项**：清单式播放（mpv concat demuxer 在 >22 条/混合时长时
只读首块，时长求和漂移 1.5-15s；单文件重封装语义可靠，保留）。

**验证**：单测 675+94 全绿；门禁 7/7（126s）；E2E：编辑→回落直通（不卡）→
~10s 后自动切回代理 40.0x，编辑内容出现在新代理（segment_present=true）。

## 26. D 盘清理（2026-10-12·owner「按 Tier 1+2 清」；回收 31.67 GB）

**触发**：owner 令「分析 D 盘占用 → 给清理方案 → 按 Tier 1+2 清」。全程
只读审查先行，逐项 grep 验证引用关系后执行。

**回收**：D 盘剩余 119.95 → **151.62 GB**；`.cache` 38.9 → **7.26 GB**。

**删除明细（Tier 1 测试残渣 18.4 GB）**：`proxy/stage_*.mp4`×4（faststart
对比）4.43GB；`v3ass/` 11 个基准目录 13.3GB（proxy_build_test/bench_breakdown/
bench_e2e/chunks_v2/proxy_batch_warm/bench_w2000/proxy_quality/bench_rebuild/
proxy_d1_test/bench_split/proxy_batch_test）；实验清单文本 15 个。
**（Tier 2 过期代理 13.29 GB）**：`proxy/` 根 12 个十六进制命名 mp4（历次
编辑版本代理，非默认键）。

**保留清单（逐项验证在位）**：
- 运行时依赖（代码硬编码）：`dsvc/target/release/dsvc.exe`（`dthrough_service.py:23`
  DEFAULT_EXE）、`ffmpeg-dev/`（:25 DEFAULT_FFMPEG_BIN）；
- **默认代理** `proxy/27e8b0b05319fe8956e6d61658f1381a.mp4`（+npz，GUI 默认设置
  键，导入 4.mp4 即命中）；`proxy/batches/`（109 个批缓存，增量重建用）；
- 文档保护资产：`research_phase3/A2a_singlepass_v2623.mp4`（样本 4 单趟基准，
  上次误删事故主角）、`sentinel_fix/4_full_audio.mp4`（导出验证产物）；
- 调研证据：`fc_bench`/`nle_research`/`rust_nle_review`（Tier 3，默认保留）。

**清理后验证（全过）**：单测 **675+94 绿**；门禁 **L0-L2 7/7**（128s，含真
dsvc 四项）；受保护资产 6/6 在位；GUI 重启 **pid 17196**。

**未动项（归属不明待 owner）**：`ArknightsPathFinding/tmp`（1.89GB，gh/gh_real
疑似克隆）、`pathfinding_workspace`（1.8GB）、`D:\work`（14.3GB 疑似他项目）。

## 27. 会话交接（2026-10-12·上下文压缩前快照）

**当前工作区状态（全部未提交）**：
- 新增：`proxy_cache.py`（~1100 行 P1 构建器）、`tests/test_proxy_cache.py`（24 例）；
- 修改：`mpv_engine.py`（代理模式 +246）、`preview_player.py`（P1 调度+两 bug 修复
  +449）、`settings_panel.py`（代理开关 +20）、`tests/test_preview_fps_osd.py`、
  `tests/test_preview_dthrough_tier.py`、`tools/dsvc/src/main.rs`（代数竞态修复
  +46）、`tools/dsvc/README.md`、`ASSET_INVENTORY.md`、`HANDOFF`（本棒）。

**P1 最终性能（W=4k 窗口 + 温批缓存）**：编辑→可播 **0.67s**（=旧行为 0.58s，
编辑后立即走直通档不卡）；后台重建 **~10s**（单块 2s + concat 3.1s + 验证 0.02s）
→ 自动切回代理 **40.0x**。旧系统无 40x 能力（20x 封顶），**非反向修改**（A/B 实测：
旧 0.58s vs 新 0.67s，停顿均为 0）。

**待 owner 手检（GUI pid 17196）**：
1. 导入 4.mp4 → 控制台 `[proxy] 物化代理就绪`（应 2-3s=缓存命中）；
2. UI 4x 播放 → 右上角「**代理40.0x**」+ 画面清晰 + 零卡顿；
3. 播放中改暂停段 → 立即继续播（不卡）→ ~10s 后角标自动回「代理40.0x」；
4. 播放中拖/点时间轴 → 落位精确、红条跟随；
5. 关「物化代理」开关 → 回落「直通Nx」（旧行为）。

**手检通过后**：「验收」→ 建议两笔提交：①P1 物化代理（proxy_cache.py +
mpv_engine + preview_player P1 部分 + settings_panel + test_proxy_cache）；
②两 bug 修复（preview_player 空格/eof + dsvc 竞态 + 对应测试）。或按 owner 粒度。

**未决/风险项**：
- 代理构建为**单文件重封装**（1.2GB 写盘 ~3s 为物理下限）；如需更快可选 540p
  档（约 4-5s，画质 0.985）——owner 未拍板；
- `proxy/` LRU 上限 20GB 已恢复余量（现 2.37GB）；
- 历史遗留：`.cache/proxy` 的 npz 孤儿（对应已删 mp4）无害保留。

**归档收尾（本会话）**：①`asset_index.json` 审计基线自 09-04 冻结未刷新，
积压 212 项「新增未登记」告警（全为 probe_/verify_/HANDOFF_ 类登记资产与
会话探针，零异常）→ 已 `--write` 重建（77→289 项，纯增量 0 删除），复跑
audit **289/289 一致零漂移**；②ASSET §六 表格一处空行（上轮插入遗留）已
修，全表连续；③ASSET §六/§八 已登记 P1 落地行与 D 盘清理行。

**归档完整性审计（10-12 owner 令「分析下是否足够完整，有没有遗漏」）**：
逐项核对工作区↔文档后修复 **11 处遗漏/漂移**，全部实证：
- **§六 补 4 行**：09-23/24 三症状修复（横跳/冻死/主线程停顿）、10-10 双 bug
  + dsvc 代数竞态、1e 非参考帧丢弃、FilmCraft 40x 实测+NVDEC 天花板——原
  全部只在棒/checklist 中，ASSET 未登记；
- **§六 结构修复**：7 行单格行（09-13~09-16 时代，行内无列分隔）被
  `tools/preflight.py` 解析器**静默丢弃**（能力清单少 7 条）→ 补齐列分隔，
  preflight 能力 56→**63 条**；1 行 7 格（09-13 立项）第 7 格渲染时丢失 →
  合并；全 5 表列数一致性机器验证通过；
- **§三 刷新**：提交状态停留 09-08（"未提交 6 文件 +1655/−38 + 新增 3"）
  已全面过期 → 改为 10-12 实测（未提交 10 文件 +2636/−97 + 新增 proxy_cache
  等 2 件；已入库清单补直通栈/缓存/工具）；
- **§八 状态行 6 处**：选项2/P1-RAM/1c/造轮子审计/上游调研/P1 定案——
  停留"施工中/待开工"已过期 → 标已收口（附入库提交号）；
- **§八 补 2 行**：10-09 mpv_spike 证据损失决策（owner 接受+标注）、
  10-10 双 bug+竞态修复（待验收）；10 月行按时间序重排；
- **§二 运行时依赖补 2 行**：dsvc.exe + ffmpeg-dev（代码硬编码、10-12
  清理确认为受保护资产）；
- **§四 探针目录补录**：09-13~10-12 四个世代 40+ 探针名（原目录只到 09-12）；
  另修正 2 处探针名（probe_proxy_spec→proxy_spec_probe、probe_shader_embed
  →shader_embed_probe，与实际文件名不符）；
- **§十 工具表补 3 行**：acceptance_gate/gui_stress/conftest（R10/R8/R9 的
  机械版原未列入）；
- **悬空引用标注**：§六 干跑行证据里 `proxy_build_test/full_*.mp4` 已在
  10-12 清理删除 → 注明"数字已固化+JSON 读法"；
- **§五 交接链**：补 VERIFIEDSEEK 入库状态（10-08 随 c2438d4 入库）。
**验证（全过）**：preflight 63 条解析正常；audit 289/289；单测 **675+94 绿**
（28.9s）；全表列数一致性机器扫描 5/5 OK。

## 28. 播放内核调研 + MDK 实测（2026-10-13·owner「按你的推荐执行」；全 T0）

**缘起**：owner 从 NipaPlay-Reload 生态提出五个播放内核（mdk/video_player/
libmpv/media_kit/liberika）问是否值得深入调研。复核后：**四个后端**（NipaPlay
官方文档明示 Media Kit=libmpv 的 Flutter 封装，不是两套内核）；其中唯一
值得实测的=**MDK**（WangBin=QtAV 作者；唯一声称「Optimized Continuous
seeking…Suitable for timeline preview」的闭源内核；liberika 无剪辑语义、
video_player 无 Windows）。

**方法（R8 口径自证）**：自建 C++ 探针 `mdk_probe.cpp`（MSVC 14.44 + MDK
SDK v0.39.0；R2 自建理由=官方 DecodeFps 每帧 printf+fflush 拖慢且无 seek
计时）；同源（4.mp4 2.27GB/7069.9s/424176 帧）、同机、同计时（seek 下发→
首帧解码完成）。**坑一枚**：MDK 打不开中文路径（status=0x80000000）→ 用
NTFS 硬链接（零拷贝）绕开。

**结果**：
| 测试 | MDK | 对照（我们） |
|---|---|---|
| T1 顺序解码（60s 窗/全片） | 1273fps / **1903fps** | dsvc 2029fps（隔离）|
| T1 硬解 D3D11/DXVA/CUDA | 786/660/951fps（**全劣于自家软解**）| NVDEC 976fps（同构）|
| T2 随机 seek 300 点 | fast **45.5ms** / accurate **61.7ms** | 换段税 30-80ms/段 |
| T3 碎段几何 500 点串行 | 42.7ms/次（官方 Timeline 配方 42.3ms，**零加速**）| 直通档逐段兑现 |
| T3 连发 500 点 | **498/500 Cancelled(-2)=seek 合并**（只兑现最新）| 帧精确逐段呈现 |

**关键裁决**：MDK「连续 seek 优化」实为 **seek 合并/去抖**（不降低单次
seek 成本、碎段掠过大面积丢帧），**无 40x 能力、无包级跳过、核心闭源+
密钥制** → **不换内核**（第三次独立闭环：后端文献/FilmCraft/MDK 全指向
「问题不在内核在碎段拼接解码账，解=物化」；P1 已落地 40x）。

**产出**：报告 `agents/research_player_kernels/01_mdk_bench.md`（含复现
命令）；ASSET §六+§四 登记；audit 289/289 零漂移；checklist 头部本轮记录。

**遗留**：①`.cache/kernel_review/` 2.4GB（SDK 85M/clone 24M/切块 18M）
可删或保留；②4.mp4 处理建议（分段录制首选/局部分析 2-3 天/预切分下策）
未执行，待 owner。

### 28.1 补测三件（2026-10-13·owner「都补，开工」；全 T0）

owner 令补齐首轮登记的三个盲区，全部完成：

**① MDK 落点精度（T4）**：fast seek **1060ms≈64 帧**（落关键帧）/ accurate
**7ms≈0.4 帧**——对照我们认证轴 seek-verified **0 误差**；MDK 的"快"与"准"
必须二选一，我们不需要。已并入报告 01_mdk_bench.md §T4。

**② liberika 实跑烟测**：官方 v0.2.1 Windows x64 C ABI 包（`erika_capi.dll`，
Python ctypes 驱动，4.mp4 硬链接）。打开→Ready 333.7ms；随机 seek（30 点）
mean **196.5ms**/max 360.6；落点 p50 **12.4ms=0.75 帧**（**非帧精确**，
API 只有 `seek(micros)` 无帧号）；**回显判别器**（半帧偏移目标）证实
**real_landing** 非回显；播放推进 0.94x 正常。→ 比 MDK 慢一量级、无剪辑
语义，**不引入**（与源码级判定一致）。报告=01_mdk_bench.md 附录 A。

**③ P1 跨源冒烟：2.mp4（重复 PTS ~21% + VFR 疑难源）全链 PASS**：
导入 0.19s → 分析（冷跑落缓存 1.5MB key `789ea0564afa`）→ **首次全量构建
135s / 3337 帧 / 217MB / 55.6s 内容** → 4x 播放 mode=proxy → **源帧推进
3372-3444/秒=40.0x 达标** → 编辑 seg0 auto→all（删段 683）→ **回落 dthrough
20x 不卡** → **重建 0.15s**（批缓存命中，新 key `fbd3611f…`）→ 自动回
proxy → **暂停态 seek 20000→20000 delta=0 帧**。**「P1 通用可用」盲区关闭**。
报告=02_proxy_cross_source.md；探针=`probe_proxy_cross_source.py`。

**探针口径修正三枚（产品零缺陷）**：①编辑须真实变更（`all`→`all` 空操作
不触发重建）；②seek 目标须选确定保留帧（全帧 50000 落在全删段 213 内，
按设计钳到 50008，delta=8 是正确语义）；③探针须补 R9 弹窗补丁（首跑被
「已缓存」showinfo 挂死 20 分钟，CPU 空转——**R9 第三次咬人，补丁已加**）。

**登记**：ASSET §六 +3 行（MDK 行补 T4/liberika 行/P1 跨源行）、§四 ⑤ 世代
补录；checklist 头部补测段。audit 复跑 289/289 零漂移（报告在 agents/，
探针在 .cache，均不影响资产基线）。

### 28.2 P1 代理 cq 瘦身扫描（2026-10-13·owner「执行」；全 T0）

「保 1080p 又瘦身」提案落地：生产同源参数（`analyzer._video_encoder_args`
实读，NVENC p4 + `cq=18+(10-q)`）逐档扫描 + **全片权威构建**。

**关键方法学**：窗口外推不可信——碎段拼接（13619 段）编码开销远大于连续
窗口，cq22 窗口外推 404MB vs 实测全片 1205MB（差 3 倍）→ cq24/cq26 直接跑
生产入口全量构建拿权威数字。

**结果（4.mp4 全片，三档 source_frames 逐位一致）**：

| 档位 | 体积 | vs cq22 | render SSIM | 解码 rtime |
|---|---|---|---|---|
| cq22（现状预建）| 1204.9MB | — | 0.99732 | 24.4s |
| **cq24（推荐）** | **969.0MB** | **-20%** | 0.99648 | 19.8s |
| cq26 | 788.2MB | -35% | 0.99530 | 18.8s |

**反直觉发现**：低 cq 解码更快（比特量小）——再次印证「解码墙在比特量
不在帧数」。单帧抽检含 ±1/±2 邻居对齐诊断（三档最佳均 delta=0，无系统
偏移）；单帧噪声大（动作帧 ±1 帧掉 0.02）不作判据。

**待 owner 拍板**：维持 cq22 / 切 cq24 / 切 cq26。切换=改 `ProxySpec.quality`
默认值 + 重建（4.mp4 全量 208s 一次性）。三份实验代理留 `.cache/proxy/`
（cq24/cq26 各一份）待选定后清理；窗口中间产物 739MB 已清理（数字固化
在 `probe_cq_sweep_results.json`）。

**报告**：`agents/research_player_kernels/03_cq_slim_sweep.md`；
探针：`probe_cq_sweep.py` / `probe_p1_cq24_build.py` / `probe_p1_cq26_build.py`。

### 28.3 净室 prompt + graphify 过滤脚本（2026-10-24·owner「执行」）

**① graphify 试用（代码知识图，13.4 万星 Apache-2.0）**
- 与预览性能**无关**；对"接手 agent 理解 69k 行代码的调用关系"有价值。
- 原版实测：169 文件 → 4511 节点/11329 边/11.4s；**17.9% 节点来自 .md**
  → `explain` 出现"被《六、已证明能力表》引用"噪音。
- **过滤版**（只扫产品代码、剔除标准库）：34 文件 → **1400 节点/1.2s**，
  explain 干净。
- **能力边界（复核修正）**：`explain` 可信（带行号）；`path` **3/5 正确**，
  根因=函数内局部 import 不被 AST 建模 + "两模块本无直接依赖"时返回真实但
  反直觉的最短路径。**首轮曾误判"path 不可靠"，复核后修正**（多组测试+
  networkx 直验才定谳——R5 教训：勿用单反例概括）。
- 固化：`tools/graphify_build.py`；环境 `.cache/graphify_env`（187MB 隔离 venv）。

**② 净室盲测 prompt**：`agents/research_player_kernels/CLEANROOM_PROMPT.md`
- 目的：让独立 agent 在不知道历史结论的前提下重新构想预览方案。
- 修正 4 处必须项（审计发现）：补删 4 份泄题文档、删 7 个方向性测试、
  声明 skim_cache.py 不可删、声明 dsvc.exe 与 HEAD 不同代。
- **实搭验证通过**：worktree → 7 junction → 删泄题 → 自证扫描全空 →
  清理 → 主工作区依赖零损伤（dsvc/libmpv/ffmpeg/analysis 均完好）。
- 残余风险（已登记在 prompt 附录）：skim_cache.py 不可删、dthrough 是现状
  提示、git 历史无法物理阻断（只能靠阶段 5 前禁读约束）。

**登记**：ASSET §五（文档分工 +2）、§十（工具表 +1）；checklist 头部。
**未改动**：产品码零改动（P1 仍待验收）。
