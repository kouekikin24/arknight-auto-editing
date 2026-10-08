# 资产与能力清单（ASSET_INVENTORY）— 任何任务先翻这里

> **持久化约定**：本文件应提交进 git，且每份新交接的 §0 必须写
> "新会话先读 ASSET_INVENTORY.md"。否则它会像它要替代的叙述一样被压缩丢掉。
> 路径相对仓库根；`../` 指 `D:\ArknightsPathFinding\`。
> "故意不跟踪" = .gitignore 有明确注释（证据留盘），非遗漏。

## 一、外部参照

| 资产 | 路径 | 身份 | 用途 |
|---|---|---|---|
| 真·源码上游 | `../arknight-auto-editing-upstream` | liemark git(HEAD含PR#9) | parity 唯一基准 |
| 发布包 26.7.15/23 | `../arknight-*-win{-fix,}` | 非git,编译exe | 成品行为参照 |

## 二、运行时依赖

| 资产 | 路径 | 跟踪 |
|---|---|---|
| libmpv-2.dll | `tools/libmpv/dll/`（=mpv v0.41.0-926-ge034d612c，T0实测） | 故意不跟踪 |
| ffmpeg 7.1.0 | `tools/ffmpeg-7.1.0/` | 故意不跟踪 |
| PyAV 关键帧表 | `.cache/pyav_kf/*.npz` | gitignore,按size+mtime失效 |

## 三、源码模块（提交状态，2026-09-08 「开工」实施后核实）

已提交入库（历次验收）：`media_info.py` `pts_timeline.py` `frame_pts_certifier.py`
`timeline_plan.py` `project_state.py` `task_manager.py` `media_exporter.py`
`edit_commands.py` `certified_edl.py` `analyzer.py`(改) + `tests/` + `tools/` + `scripts/`。
当前未提交（三代叠加，待「验收」，共 6 文件 **+1655/−38** + 新增 3 文件）：
①C+/选项2 旧账(+773/−16)：`mpv_engine.py` `preview_player.py`
`tests/test_mpv_engine.py` `tests/test_preview_fps_osd.py` `ASSET_INVENTORY.md`；
②P1-RAM skim 实施(B0-B3+review F1-F6+现场 G1-G7)：`mpv_engine.py`(stream_cb/
两级切换/cap parity/快照 speed 字段,累计+540/−14) `preview_player.py`(调度接线/
开关清库门/取证埋点,累计+372/−14) `settings_panel.py`(+13,掠过开关)
`tests/test_mpv_engine.py`(累计+502/−1) `tests/test_preview_fps_osd.py`(累计
+155,含档位标签测试)
+ **新增** `skim_cache.py`(513行,构建/验证门/降档阶梯/stat守卫/cancel兼容)
`tests/test_skim_cache.py`(289行) `tests/test_preview_skim_rebuild.py`(273行)
+ `HANDOFF_VERIFIEDSEEK_20260905.md`(棒,随验收入库)；checklist/AGENTS 本地不入库。
测试基线：**504 passed + 90 subtests**（2026-09-12 压缩闸门复跑，覆盖最终取证代码）。

## 四、台架探针（.cache/v3ass/，gitignore）

标定calibrate / 端到端verify_gui2 / 色彩transfer_curve+mpv_color_params2 /
几何probe_b_geometry+default_size_probe / C排雷spike_c*+c4/c5/c6+duo_instance /
验收verify_c4_prod+step_latency_probe+c4_region_diag /
原生步进probe_frame_step(+diag_step) / 集成smoke_engine_step /
seek落点probe_seek_timepos(+probe_static_identity) /
选项2验收verify_option2_prod(+sweep/tier2) /
业务变速掉帧probe_biz_speed_drops(raw)+probe_biz_osd_engine(source引擎级,
NOT_REPRODUCED)+probe_biz_osd_edl(EDL复现成功,F0/F1/F2)+probe_biz_edl_skipframe
(trick-play判死,G1/G2/G3) / skim物化probe_proxy_skim(磁盘代理+编辑增量)
+probe_skim_ram_pipe(管道证伪)+probe_skim_ram_streamcb(RAM成功)+
probe_ram_footprint(足迹/天花板/规模)+probe_parallel_refill(零预存判死,
含p34最优seek/GPU多路) / skim实施验收probe_b0_2mp4_build(病理改判:tpad等EOF
=探针缺陷,Δ=0,单命令85.3s帧精确)+probe_b0_tlimit(-t截断11.5s定案生产形态)
+probe_skim_e2e(真引擎真构建8/8:Tier2真10x零掉帧/Tier1封顶3.0,review轮扩10/10
含播放中seek保Tier2)+probe_skim_scale1000(千段级:内联57099字符超Win32K实证
→script文件路径21.7s构建帧数全对)+probe_skim_phase_timing(分段:推导0.1s/
ffmpeg16.6s/验证2.1s@1418fps/入库2µs)+probe_upstream_decode_bench(上游对比
T0:cv2导入0.07s/read+resize+gray 129fps/grab 1884fps→4.mp4上游分析折算
62min vs 本fork 7.6min≈8x)+probe_repro_apt_errno22(Errno22无头复现:同路径
30s全程健康=实例特异)+probe_pythonw_spawn(pythonw进程池POOL_OK=无控制台
假说证伪)；**解码直通轮(09-12晚):probe_proxy_spec(编码规格横评)+
probe_shader_embed(+anime4k/,Anime4K内嵌挂载)+probe_decode_through(
lavfi机制+三坑)+diag_g64(64段图归因:setpts无t变量)+diag_rawpush(
rawvideo流二分:open回调漏uri参)+probe_dthrough_prod(生产PoC)+
probe_pskip_ab(包级跳过正确性+吞吐)+probe_skipframe_hybrid(锚点mad=0+
运行时切换冒烟)+probe_dthrough_e2e(全链路实弹)+probe_1a_wheels(现存轮子吞吐:cv2 推流181fps✗/PyAV demux天花板1457pkt/s<需2400→Python判死)**。结论见§六。
**一期施工探针（09-12 深夜++「开工」）**：dsvc/(Rust 服务工程,ffmpeg-the-third 3.0.2+serde,产物 dsvc.exe)+bakeoff_fft(+src/bin/bench.rs 解码基准)+bakeoff_rsmpeg(判负留证)+probe_dsvc_smoke(v3:服务级冒烟+PyAV 同规则对账+span 事件位级抽样)+probe_dthrough_engine(引擎级 E2E:R1 注册/R2 播放至 EOF/R3 播中 seek/R4 孤儿检查)+diag_rawseek(rawvideo 流 seek 语义)+diag_rawload(loadfile 签名变体枚举 A-E+引擎选项集 G/H/I/J)+dt_mpv.log(mpv 原生日志=排障利器)。环境：FFMPEG_DIR=.cache/v3ass/ffmpeg-dev/ffmpeg-7.1.1-full_build-shared、LIBCLANG_PATH=~/AppData/Local/Programs/Python/Python311/Lib/site-packages/clang/native。结论见§六。

## 五、决策与交接

- 交接链(已跟踪): HANDOFF_{STATE_0827,EXPORT_PERF_0830,PR_STRATEGY_0901,
  UI_REVIEW_0902,PAUSE_STILL_0903,V3ASS_0904,C4STILL_0904,
  NATIVESTEP_20260905}.md 等；未提交新棒: VERIFIEDSEEK_20260905.md
  （C++选项2全程，随「验收」同笔入库）；**当前头=VERIFIEDSEEK_20260905**。
- 决策(故意不跟踪): MPV_GATE_DECISION_*_v{1,2}.json、MPV_PHASE0_{SPEC,RESULTS}.md、
  PRODUCTION_{PTS_GOLDEN,VFR}*、arknight-preview-pack.zip、_wf_plan.json。
- 已跟踪: UPSTREAM_FIX_PR_DRAFT.md、AV_REVIEW_AUTOVERIFICATION.md。
- 用户证据(仓外): `C:\Users\AAA\Pictures\Screenshots\屏幕截图 2026-09-04 00*.png`。
- **文档架构(分工防 proliferation)**：AGENTS.md=规矩(自动读)；
  checklist.md=当前任务工单(本地不提交,已入.git/info/exclude)；
  HANDOFF_*.md=会话状态链；本文件=资产/能力/决策/铁律。新文档先登记分工。

## 六、已证明能力表（防漏组合；证据为gitignore探针，结论亦见交接）

| 能力 | 证明 | 可组合 |
|---|---|---|
| wid 嵌 mpv 到 Tk 子窗 | C6/mpv_host | G 双窗 |
| 双 vo=gpu 同进程稳定 | duo_instance | G 双窗 |
| 离屏 Toplevel 无任务栏+运行时改尺寸 | C6 | G/隐藏 |
| 单帧 Y4M 与播放位级一致(mad0.0001) | C4 | G/C4 |
| window截图=用户所见; video≠ | spike_c | 测量法 |
| 落帧:无修正=边界陈旧帧;+5/120=过冲+1 | landing/4 | 不可mpv定帧 |
| C3 互换:reload视频67ms/恢复51ms | spike_c | 2B/G 取舍 |
| osd_duration=1 杀闪且 overlay 活; osd_level=0 杀overlay(禁) | c4 | OSD |
| 内嵌暂停保留 fps 角标(延迟清) | c4 | OSD 复刻 |
| PyAV容器非线程安全 / resize与vo重配竞态 | 段错误 | 单worker+settle |
| 跨双屏被DPI重采样污染 | c4右条 | 测量须单屏 |
| **原生 frame-step/frame-back-step 帧精确**：6步前进+后退全部 bit-exact(mad≤0.024)，延迟0.3-17.2ms(均值8.6)，VFR源同样成立；校准偏移0.0即精确落帧 | probe_frame_step(2026-09-04,T0) | 取代C4步进/双窗G |
| **裸命令可用、input-command 元命令 -4**（此 v0.41.0-926 构建上不可用）；步进一律 `("frame-step",)`/`("frame-back-step",)` 直发 | probe_frame_step | 命令选型 |
| **site-packages python-mpv 无 `MPV.get_property()` 方法**：调用会走 __getattr__ 误读 "get-property" 属性→AttributeError(-8)；读状态用属性访问(p.time_pos/p.pause)或 observe_property（均实测可用） | probe_frame_step | 引擎读路径 |
| time-pos observe 在 frame-step 后立即更新(1/60步进可辨)；直读 C-API mpv_get_property 亦可用（raw ctypes），唯 wrapper 的 get_property 缺失 | probe_frame_step | 延迟测量法 |
| **暂停 exact seek 后 time-pos 报实际呈现帧 PTS（非目标值）**：52 seek×26点+16静止点扫描，可分辨点12/13支持、唯1在16.7ms模糊带；偏离落点∈[-2,+2]帧，「看」有信号 → 选项2（seek+看+补）老否决可复核，需黄金集全边界验证 | probe_seek_timepos+probe_static_identity(2026-09-05,T0) | ②G选项2 前置条件已验 |
| **screenshot-raw(window) 可用且 3.7ms（中位5次）** vs screenshot-to-file+文件轮询 81.6ms；返回内存 dict{data,w,h,stride=4w,format=bgr0}，bytes→numpy 0.02ms 免 PIL | 2026-09-05 计时探针,T0 | C4 优化(C+)主刀口 |
| python-mpv(site-packages) **无 RenderContext 封装**：render.h 路线需 ctypes 手写 | 2026-09-05 实测 | render.h 路线成本评估 |
| render.h(v0.39头文件,官方文档T1)：存在 **MPV_RENDER_API_TYPE_SW 软件渲染路径**（render到宿主内存，免GL/免窗口），但官方自评「非常慢」「不适合抽帧」；GL 正统路径死锁陷阱多（渲染线程禁调其他libmpv API/ADVANCED_CONTROL冻结core）；seek 语义=absolute 默认 exact（"slow"），无帧号寻址；frame-back-step 官方自认「可能非常慢且有时不符预期」（依赖 precise-seek 正确性） | 2026-09-05 补调研(代理恢复) | render路线/选项2 边角评估 |
| **time-pos 瞬态陷阱**：seek 后 0~50ms 内 time-pos 报**寻址目标值**（带下发命令的 9 位小数截断指纹，铁证）；落帧呈现后（~50ms，playback-restart）才报**实际帧 PTS**。旧调研「tp报目标值」与探针E「tp报实际帧」**都对，差在读的时机**。选项2 必须等 playback-restart 事件再读，否则瞬态误读→误判落准→静默错帧 | 微探针(2026-09-05,T0) | 选项2 设计约束 |
| **源存在系统性重复 PTS**：2.mp4 前403帧含 43 对相邻同 PTS（~21% 帧卷入；time_base=1/15360，OBS 类采集特征）。重复对内两帧 time-pos 无法区分 → 选项2「看」在此类目标上有盲区，需「tp粗定位+步数细定位」或 C4 兜底；密度按源各异，认证表可零成本普查 | 全精度复核(2026-09-05,T0) | 选项2 覆盖率/兜底设计 |
| **选项2 阶段0 全片扫描=GO**：2341点（全部1536关键帧+300重复段首+150段中+200VFR+150随机+6首尾）无修正seek 偏差∈{0,1,2}（只停早**零过冲**）、playback-restart 100%到达、瞬态早读假阳性0、唯一PTS点tp失配0；全片普查：重复PTS卷入 **26.68%** 帧（24576段,几乎全是2帧段）、VFR突变17016点 | probe_seek_sweep(2026-09-05,T0) | 选项2 预算=4步/超时1.2s 依据 |
| **Tier2 像素交叉验证 39/40**：唯一「不一致」点是两相邻帧与截图**同时位级一致**（mad 0.0/0.0，PTS不同的像素相同帧）——oracle 平局非 tp 说谎，用户观感无差；**重复段目标 34/34 全部落段首**（跨全片 run 2268~24138 抽样）→ 二期「计数走段内」前提成立，一期仍保守走 C4 | probe_sweep_tier2(2026-09-05,T0) | 选项2 可信度/二期扩展 |
| **选项2 端到端验收 PASS**：真引擎闭环 12/12 唯一PTS点 ok(exact) 零补步、墙钟 26.4-66.9ms（中位 36.8）；重复段首 2/2 放行落准；段内 2/2 拒收走 C4；C4 回归不变（0.497s/mad 0.18-0.21）。**再分析**：扫描中全部「停早」点均为重复段目标（tp 映射到段首的表象）；唯一PTS目标 ~1800 点**零偏差全落准**——经典「停早」 largely 是重复 PTS 现象，校正环在本源几乎不触发（留作他源安全网） | verify_option2_prod(2026-09-05,T0) | 选项2 验收依据 |
| **选项2 跨源验证 4/4 PASS**：2.mp4(184k帧)/3.mp4(30k)/1.mp4(249k)/4.mp4(424k) 全部唯一PTS点零补步落准（63/63）、重复段首 8/8 放行、段内 8/8 拒收；墙钟 21-90ms（中位 33-49ms）；片头密集重复段源（3.mp4）同样正确。注：3.mp4 首跑 FAIL 系探针把段内帧误标 edge，修探针后过——非产品缺陷 | verify_option2_prod×4(2026-09-05,T0) | 选项2 跨源可用性 |
| **上游预览耗时基线**（单线程 OpenCV，零 sleep 零贴层）：步进 grab+read ≈4ms；`cap.set(POS_FRAMES)`+read 寻址 30-76ms（随 GOP 距离）；首帧 ~33ms。**不保证帧精确**（POS_FRAMES 在 B 帧流有落点歧义）——我们选项2 速度已追平且精度反超 | 2026-09-05 真源实测,T0 | parity 对照锚点 |
| **GOP=120 固定**（2.mp4，1536 关键帧/184k 帧）：任意帧距前关键帧平均 60 帧 → 「关键帧锚定+frame-step 数步」路线（mpv-cut 式）平均 60 步×5-17ms≈0.6-1.2s **慢于 C4**，判死 | npz 账本统计(2026-09-05,T0) | 路线否决依据 |
| 认证表硬约束：PTS 必须**严格递增**+duration>0（media_info 验证器）→ 真实重复 PTS 建表时被单调化(+1 tick≈65µs)；引擎重复段名册按**相邻间隙<2ms**识别（_DUP_GAP_EPS_S，真实帧距≥8ms 两数量级分隔）；段首放行/段内拒收 | 验收探针排障(2026-09-05,T0) | 账本语义/dup 判据 |
| **EDL 真10x 路线实证：运行时 trick-play 判死，物化唯一可行**：`vd-lavc-skipframe=nonkey`（只解关键帧，HLS I-frame 流原理）在本项目几何下不可用——保留段中位 **11 帧 << GOP 120**，段内常无关键帧：G2(建引擎即开) **rt=0.0、>100ms 停顿 85 次、EOF 空转 12 轮**；G3(运行中动态切)选项设置成功(ok=True,value=nonkey)但**恶化**(rt 4.8→2.3、停顿 4→45 次、tp 倒退致 OSD 窗全废)；G1 基线复跑=F1(min 14.8)。**连续文件 speed=10 本身干净**(E2: rt 9.978、OSD 恒 60、软解 ~600fps 消耗可达) → 真 10x 必须**消灭拼接边界=物化成连续文件** | probe_biz_edl_skipframe(2026-09-07,T0) | skim代理路线依据/trick-play否决 |
| **高速掠过播放先例调研**（主会话串行，owner 授权「不并发子代理可串发」）：auto-editor(同赛道自动剪静音)=**export-first 无实时拼接预览**(复查交给NLE XML)；LosslessCut=预览仅**源文件 seek 定位**不连播拼接段，不支持格式走「FFmpeg-assisted playback」**生成全文件低质代理**播放(导出仍用原件)，smart cut=只在切点附近重编码；HLS RFC8216 trick-play=**预物化 I-frame-only 子流**(EXT-X-I-FRAMES-ONLY)+独立清单——工业级快进=物化流而非运行时跳解码；mpv EDL 官方文档(DOCS/edl-mpv.rst,gh实拉)=段(file,start,length)**切段必回源 seek、无任何预缓冲/平滑机制**(边界成本为格式固有，与 F1/G1 风暴实测吻合)；NLE render cache(Resolve Smart·User/Premiere 预览渲染文件/FCP render files)=时间轴渲染物化落盘+编辑失效+段级增量(T1 公认常识，本轮 Adobe/Wikipedia 网络超时未拉到原文)。**结论：没有成熟项目做实时拼接段高速播放；高速掠过的成熟答案一致=物化** | 调研(2026-09-07,gh api+WebFetch) | 真10x 方案唯一路线佐证 |
| **skim 代理端到端可行（编辑感知物化，全生产代码复用）**：3.mp4 真实 73 段/1276 保留帧，`export_pts_schedule`(NVENC 实探 `_pick_gpu_encoder`=h264_nvenc) 单命令建代理 **15.0s/32.1MB(1080p)**；分批路径(`_export_pts_video_batched`,batch=8) **9.2s** 更快（-ss 跳批间大间隙）；帧精确：written=1276✓ 时长 21.267s≡虚拟时间轴精确一致✓（PyAV 计 1277=已知末端哨兵保护帧，虚拟终点之外，验收闸门用 written+duration 或复用 `_verify_pts_export_container`）；**播放 speed=10：OSD 36/36 窗恒 60.0、tp 停顿>100ms 零、最大间隔 27.8ms**（对照 EDL 现场直播 10x：min14.4/94%窗<50）；**编辑增量实测：删段=concat copy 重拼 297ms 零重编码帧数精确✓；单批(8段)重编码=2.07s（拖边界代价）**。代理=无声(预览本就静音)。待开工设计点：2.mp4 规模首建需**自适应分批**（间隙<回填成本合批、否则分批，防全片解码）+GPU 解码加速选项+480p 降档选项；磁盘 1080p≈1.5MB/保留秒 | probe_proxy_skim(2026-09-07,T0) | P1 编辑感知双层预览提案实证 |
| **纯 RAM 掠过实证成功（零磁盘写入，真 10x）**：libmpv 官方 `mpv_stream_cb_add_ro`（ctypes 绑定 mpv/stream_cb.h，python-mpv 构造后注册 rc=0）自定义 `ram://` 协议直接吃 bytearray；媒体=生产 filter 图（-ss/-copyts/select/setpts 认证 tick 精确，单命令全局 PTS 轴）出 GOP2 无B帧 MPEG-TS→stdout→RAM（NVENC 本驱动**拒 GOP=1**：严格校验 GOP>B+1；3.mp4 73段/1276帧：构建 **6.2-6.6s、89.2MB=4.2MB/保留秒**，480p 变体 38.5MB）；播放 speed=10 整遍 EOF 2.33s=**有效 9.11x**、OSD **8/8 恒 60**、**tp_end=21.267s≡EDL 虚拟全长精确一致（PTS 保真→映射零改动成立）**、重播同净；唯一疵=每 load **位置 0.19-0.23s 处一次性 ~0.2s 启动探测停顿**（流中段零停顿；可预热规避：预 loadfile+暂停待命）；流内 seek 落点不准（TS 无索引：seek2.0→落7.78，611ms）→精确停顿/定位走源路径（双层设计本就如此）。**证伪两项**：命名管道喂 libmpv（不连接 \\.\pipe\，file-loaded 25s 超时×3）；TS 批块字节级拼接（批间 PTS 不连续→时间轴塌缩 6.9s+350ms 周期性停顿——增量重建须 `-output_ts_offset` 或 remux）；小探测参数（demuxer-lavf-o probesize/analyzeduration 缩小）致此构建 load 超时，不用。**前置定理（实测数推定）：任何实时读源方案封顶 ~5-6x=seek率墙**（每遍 73 次源内 seek×30-80ms；mpv EDL 与 OpenCV cap.set 两引擎同墙），真 10x 必须段内容预驻留（RAM 或磁盘） | probe_skim_ram_pipe+probe_skim_ram_streamcb(2026-09-07,T0) | P1-RAM 提案依据 |
| **P1-RAM 足迹/天花板/规模实测**：3.mp4（保留21.27s）四组合 blob 足迹=1080p GOP2 89.2MB(**4.20MB/保留秒**)/GOP30 51.4MB(2.42)/480p GOP2 40.0MB(1.88)/**480p GOP30 14.5MB(0.68)**；构建 6.2-6.7s 各组合相同。**天花板**(1080p GOP30,`cache=no`)：10x=OSD 4/4 恒 60（有效9.7x）；16x/24x→**速率饱和 14.3x=软解墙**、OSD 塌(10-15)→factor 建议≤12，480p blob 提顶（估>30x 未测）。**暂停态流内 seek 精确**（5.0→5.0，810ms）；运动中 seek 不可靠→生产先暂停再定位（与现有交互一致）。**mpv 须 `cache=no`**：否则 demuxer 缓存把 RAM 数据再复制一份（≤150MB）。**2.mp4 规模病理（开工首要诊断项）**：逐段独立 `-ss` 构建 median 42s/max 82s/min 1.5s、64段共2684s 不可接受；hwaccel cuda 同超时；足迹 1.21MB/保留秒正常；机制嫌疑=探针**合成段表**以单调化账本 tick 作 select 边界 vs `-copyts` 原始流时间在重复PTS源(26.7%帧)上随位置漂移→窗口错位延迟停机——生产必须走 `intervals_for_plan` 正规区间+自适应分批，逐段 -ss 病理开工先诊断；2.mp4 首建估计=单命令全区解码 ~3-6min 一次性(T2)/修复后数十秒(待测) | probe_ram_footprint(2026-09-08,T0) | RAM 预算表/倍速天花板/开工设计点清单 |
| **零预存活管线天花板=4.8x，真10x物理不可达（2.mp4 实测）**：唯一非物化路线=并行预取管线（N 解码实例分摊段回填）。实测（2.mp4 51min 真源，生产语义最优 seek）：**回填税**=seek 落前关键帧→段首平均 81 帧(max 121=GOP)/159ms 单worker——稀疏访问把解码账单乘 (回填+段)/段≈5-6 倍；10x 需 34.3 段/s≈3300 帧/s 1080p 高码率解码+34 seek/s，机器软解总吞吐仅 ~500-1000 帧/s：**sw K=1/4/8=5.0/14.0/16.5 段/s→上限 1.5/4.1/4.8x**（K=8 时 refill 159→279ms=全核饱和，GUI 同跑）；**GPU nvdec 不扩展**：单进程多输入 N=1/4/8=2.4/3.4/3.8 段/s（上限 1.1x），每段进程模式被 CUDA init ~300-450ms 淹没，但 **8 并发 nvdec 无会话墙**（16/16 ok）。对照：连续文件 10x 可行（E2 rt9.978=600帧/s 顺序解码）——墙的本质=稀疏性×回填税，非工程成熟度。**反讽**：8 worker 活管线瞬态 RAM ~0.8-2.4GB（解码工作集）> P1-RAM blob 14.5-100MB——「不依赖内存」的活方案实际更耗内存。残余杠杆（未测,T3）：skip_frame nonref 回填省 30-50%→估 ~7-9x；nvdec 持久多会话扩展性存疑——即使全拉满至多边缘 10x 且 CPU/GPU 满载，效果仍逊 P1-RAM（已证真10x+掠过仅~1核） | probe_parallel_refill+P34(2026-09-08,T0) | 零预存路线否决依据/物化必然性 |
| **业务变速掉帧机理（两轮探针修正版）**：第一轮 raw/source 模式（2.mp4,160Hz屏/60fps/hwdec=no 软解）：speed=2 rt=2.000 零丢帧；speed=3 rt=2.997 丢157/8s(显示限速180>160Hz)；speed=10 持续段丢 **~66-73% 消耗帧**(7951/20s 引擎版)、rt 9.4-10.0 解码受限、tp 爆发到达——但 **source 模式 OSD 公式模拟全程≥57.5 恒钳60，复现不出 35-45**；引擎级 source 复现（真账本+真段几何 64 短脉冲/密集/背靠背 350 次写速）同样 ≥57.5，**新鲜度失配假说被证伪**（staleness 中位0/max 67-133ms，tp 停滞>100ms=0）。**真机制=EDL 拼接边界风暴**：GUI `skip_trimmed` 默认 True → 日常播放走 EDL（"# mpv EDL v0" 拼接文件）；真实项目保留区≈73 个短段（3.mp4 states：中位 11 帧/0.18s），业务段几乎全是 0.2X 型 → factor=10 时**每 ~30-60ms 跨一个拼接边界=demuxer 源内 seek+关键帧回填** → 真实呈现停顿：F1(EDL+factor10) OSD **31/33 窗<50、min 14.4、35-45 带全中**，stale/fresh 双变体同跌=诚实读数；F2(EDL+factor3) **轮内稳态全 60**、仅重播启动瞬态 1-1.5s 跌(28-50)；F0(EDL+业务关=旧行为) 大体 54-60+密集尾区偶发 46.5（EDL 边界基线成本，先前已存在）。副产物：frame-drop-count **seek 时重置**（跨seek差分可为负）；EDL 模式 `_frame_for_position_locked`=Fraction 线性扫段，事件线程 210µs/call（source 模式 23µs） | probe_biz_speed_drops+probe_biz_osd_engine+probe_biz_osd_edl(2026-09-07,T0) | 业务变速掉帧归因（EDL边界×倍速）+修复依据 |
| **2.mp4「逐段构建病理」改判=探针缺陷；生产构建形态定案**：§六 probe_ram_footprint 行所记「逐段 -ss median 42s」真机理=**tpad 尾克隆等输入 EOF**——select 只在窗口传帧，带 clone_guard 的命令窗口结束后仍解码到文件尾（耗时∝段到EOF距离/~2200fps：首段82s/中段42s/末段1.5s 线性吻合；旧探针每段 last=True 全挂克隆）。尸检补盲区：**输出帧数全部正确**（晚段11/11、段0 13/13）=窗口一直匹配；**Δ=0**：4 位置 showinfo 地面真值↔账本 tick 完全一致（T=0 无歧义锚点；其余位置 mod-step 对齐残差 0），「单调化 tick 漂移」嫌疑证伪（2.mp4 账本=原始流 pts，头部帧1-5异常已裁决）。生产语义健康：批块（克隆只挂最后批+`-frames:v` 提前停）8段 **11.0s**；**单命令全量 85.3s/9.6MB(480p GOP30)/1266帧/21.1s≡虚拟**（帧数时长双精确）→2.mp4 规模首建定案 ~85s（原估 3-6min 过悲观）；**-t 输入侧截断**（last_end×tb−seek+1.5s 余量）消掉尾克隆 EOF 税：同窗口 **11.5s vs ~80s** → **生产命令形态定案**（-ss/-t/-copyts 输入侧三件套+扁平 select/setpts+tpad+setts 哨兵+-frames:v kept+1+mpegts pipe:1） | probe_b0_2mp4_build+probe_b0_tlimit(2026-09-08,T0) | skim 构建策略定案/0e 病理条目修正 |
| **P1-RAM 生产接线真机 E2E 8/8（skim_cache+引擎两级切换落地）**：真 MpvEngine+真 libmpv（headless vo=null）+真 NVENC 构建 3.mp4 73段：stream_cb 在引擎 start() 注册 rc=0；`build_skim_blob` 8.3s/14.5MB/1277帧(1276+1克隆)/21.27s≡虚拟；**Tier2**（blob 代数匹配自动切换）：mode="skim"、业务 factor=10 **speed 实达 10.0**、tp_end=21.267≡EDL 虚拟、EOF 1.9s、**mpv drop 0.0%**、重放同净；**Tier1**（清 blob 同请求）：mode="edl"、**speed 恰封顶 3.0**（request.preview_step_cap parity——GUI 一直发 3，f53d6dc 引擎漏接已补，作用 base×biz 乘积+UI 速率）；per-file `loadfile … cache=no` 被此构建接受；**暗雷一枚实装前拆掉**：`normalized_source_path` 会把 ram:// URL 揉成文件路径→file-loaded 路径校验永失配→pending seek/resume 卡死，`canonical_load_target` scheme 旁路修复。pytest **495+90 全绿**（新28例；旧断言 factor5→speed5.0 按 parity 改 3.0+cap10→5.0 反向） | probe_skim_e2e(2026-09-08,T0,两轮一致) | skim 实施验收/两级切换+cap parity 生效证明 |
| **上游解码速率对比（T0 同机同源 4.mp4）+ 上游身份定案**：cv2 打开 0.07s；上游 pass1 风格 read+resize(400x225)+gray=**129fps**、pass2 grab-only=**1884fps**；本 fork A_PT 解码 1835fps（=上游 14 倍）。4.mp4(424k帧/117.8min) 全程折算：上游两遍≈**62min** vs 本 fork 一遍≈**7.6min**（T2，分类成本按样本3锚点 931fps 端到端差分）。**身份**：A_PT+跳二遍=本工作区产出经 PR #9 合入上游（我方 dc6259a~52b7aef ↔ 上游 e9f4de1~b58bce0 同消息）；上游克隆①（liemark 07-23 HEAD）**默认后端仍 "OpenCV（默认）"**，我方默认 A_PT→8 月 PyAV 整合。分阶段计时：段表推导 0.1s/ffmpeg 拼装 16.6s/PyAV 验证 2.1s(1418fps@480p)/入库引用交接 2µs；验证门包计数化=遗留候选（~10x） | probe_upstream_decode_bench+probe_skim_phase_timing(2026-09-08,T0/T2) | 上游对比总表基准（克隆①修订版）/分析 8x 差异归因（解码 14x+两遍变一遍）/skim 分段成本模型 |
| **解码直通（包级跳过）全链路 T0 实弹**：PyAV 循环**包级跳过**（间隙包不喂解码器，零解码成本）+锚点关键帧恢复——与干净解码 **max-mad=0.0 位级一致**（锚点=关键帧=干净重启）；→ 分块内存流 → stream_cb rawvideo（CFR fps 参数=虚拟轴天然成立）→ mpv wid 真窗：4.mp4 64 段/1200s 窗（保留 25%≈真实统计）**298.8/300.8 完整走完/237ms 起播/最大停顿 752ms/I420/零存储零构建**；吞吐：冲刺 **2160fps（36x 源，7.3 核）**、纯 Python 循环独立解码 447fps（Part B）/E2E 全链含转换推流 281fps → **3.8x 虚拟**@1.39 核（E2E 实测口径）、**编译循环 10x 余量 2.4-3 倍**（10x 需 ~730-750fps；1835/750=2.4、2160/750=2.9）；音频必须静音（双钟失配 13s 停摆） | probe_pskip_ab+probe_dthrough_e2e+probe_dthrough_prod(09-12 晚) | **Tier2 改判依据**（§八 09-12）/一期 1a-1d 实施依据/物化备胎对照 |
| **lavfi-complex 语义与陷阱（mpv 0.41 本构建）+编码规格横评**：per-file loadfile 第 4 参自动恢复（T0 文档）/framedrop=vo=官方明文「still decodes and filters all frames」（解码直通理论基座）/**seek+图重建毒药 #16333 open@0.40→直通架构全禁 seek**/hwdec 需 vo=gpu（vo=null 下 nvdec 不启动=假反转陷阱）/setpts 无 `t` 变量（用 PTS 时基式）、sgn 不存在/**O(K) 表达式 eval 单线程毒药（64 项 min/max≈3.4ms/帧→2.0x）→ N 计数 `setpts=N/(60*TB)` O(1) 解**/skipframe 选项值只认 default·nonkey（no/大写拒绝）/双钟失配→预览静音；**AV1×MPEGTS 死**（ffmpeg7.1 muxer 写 bin_data，连自解都不认）；h264_nvenc **vbr-cq26 vs constqp qp6 同分辨率体积 3x**（240p 降档真凶之一）；Anime4K v4 MIT 内嵌挂载运行时设/卸 T0 机制可用（画质主观验收 pending）；**NVDEC Ada 官方 903fps < 本机软解 1835-2160**（硬解无优势反转） | probe_decode_through+diag_g64+diag_rawpush+probe_skipframe_hybrid+probe_proxy_spec+probe_shader_embed(09-12 晚)+子代理文献役 738k tokens | §4.0i 决策证据链/一期编码参数与陷阱清单 |
| **造轮子审计完成（Phase1-2 逐项判定）+rawvideo CFR 语义 T0**：纯 Python 判死实锤（cv2 推流 181fps✗/PyAV demux 天花板 1457pkt/s<需 2400 且 CPU 仅 0.98 核=逐包胶水结构性）；绑定双候选=rsmpeg 0.18.0（`ffmpeg7_1` feature）vs ffmpeg-the-third 3.0.2+ffmpeg-7.1（1a 首日 bake-off）；Windows 三件套雷=link.exe/libclang(未文档化坑)/FFMPEG_DIR（gyan/BtbN shared dev 官方姿势）；PyAV 自带 DLL 不可链接（hash 名无 .lib）；**独立进程>cdylib**；**mpv rawvideo CFR 语义**：speed=N=消费 N×60 帧/s、demuxer cache 字节制超限=纯背压不丢包、无音频=视频即主钟（**display-resample 禁用**：wid resize 跳帧+多屏 #10489）、framedrop=vo 保持（decoder 档在 audio=no 下死代码）、**stream_cb 无 URL 去重重载安全**、EOF=read 返 0→end-file(EOF)（废弃 IDLE 事件勿用）、**rawvideo seek 帧精确**（字节=帧号×帧大小，seek_fn(0)≥0 才 seekable）→跳转=流内帧级 seek；**speed 运行时切换官方 bug 族 #13694/#13513（0.41 未决）→逐段倍速机制改判=推流端节流**（业务速率编进推帧节奏、恒 speed=1、带宽 186MB/s、视觉等价；浏览器旁证 Chromium 40190553+Jump Cutter+SponsorBlock）；**首创性三度确认**（mpv 社区/GStreamer GstSegment 无 merge/安防 NVR 全 seek 跳段/ffmpeg8 segment=拆分/2025-26 零命中） | probe_1a_wheels+子代理×3 报告（agents 目录 agent_38c56ab9/4740788f/a9e484bf，共 672 万 tokens） | 棒 §4.0j/一期施工单（Step0 preflight 三件套+1a bake-off+1b 恒速语义+流内 seek） |
| **上游预览考古 T0（git 克隆 ce10360 源码直证）+ 上游生态全景（GitHub API/ls-remote T0）**：上游预览管线=cv2.VideoCapture 单 cap 专用 IO 线程**顺序解码+grab 丢帧**（speedup_1x/0.2x/分段变速全=步幅丢帧，video_io.py:92-207）+`CAP_PROP_POS_FRAMES` 唯一 seek（>30 帧才 seek，video_io.py:30,50-52）+Tk PhotoImage 每帧重建+after(16) 轮询（preview_player.py:373-397）——**无拼接预览（跳裁剪区=逐帧吃或 seek）、无音频（PR #2 ffplay 子进程且起播不 seek=音画不同步缺陷未修）、无任何预览物化**；预览被注释钉死 OpenCV（settings_panel.py:68「preview/export still use OpenCV」）；发行包核对：v26.7.23 exe≡ce10360（pyc 特征串互证）、v26.7.15≈8f2dc2e（07-15 补编译）；**GitHub：7-23 后零提交、唯一分支 main、8 tags 至 v26.7.23 为止；4 issue 无一涉预览、播放相关 PR 仅 #2；5 fork 除我方外全落后；liemark 无播放器积累仓**；「上游有而我们没有」=8 条 UI 管道技巧，**值得吸收 3 条：seek 命令合并去重（video_io.py:264-277）/150ms 节流刷图（preview_player.py:312-328）/有界丢帧队列 maxsize=2 满丢旧（video_io.py:123-128）** | 子代理×2 报告（agents 目录 agent_60b5a30f/agent_314dd539）+克隆直查 | 「基于上游方案」路线**关闭**（无架构可取）；3 条技巧=1c 可吸收项 |
| **非 MPV 后端文献裁决（owner 放开「放弃 MPV」）**：libVLC=media_new_callbacks+rawvid demuxer 可喂 I420 且 DEMUX_SET_TIME 帧精确（rawvid.c T0）但 rate>1=**全解码+迟帧丢弃**（video_output.c drop-late-frames T0）+Tk 嵌窗雷更多；Qt Multimedia=playbackRate=时钟加速+丢帧不省 CPU（TimeController 源码 T0），QVideoSink 外供帧形态可做但须整套 Qt 运行时+双事件循环；GStreamer=appsrc 外供帧正统先例+TRICKMODE 解码子集档（对裸流无意义），**2026-02 起 Centricular 官方 pip wheels（gstreamer-bundle）打包痛点实质缓解**但 bundle 200-400MB；**WebCodecs/WebView2=唯一解锁 GPU 硬解+AV1 硬解+音频同步的路线**，但 pywebview 不可嵌 Tk（issue #405）=UI 整体 HTML 化才可行；SDL2=去 mpv 最小兜底（UpdateYUVTexture+CreateWindowFrom，渲染环 200-400 行，音频自建）；pyglet/OpenCV 判死；**NVR 智能回放=设备端出流+客户端全解码跟拍+离散档位**（海康 PlayM4 SetPlaySpeed 等，旁证我方客户端侧方案更激进正确）；**GES nlesource.c 源码直证：跨段=对源元素 ACCURATE\|FLUSH 源内 seek**（search T2→T0 升级）；**「外供帧渲染+平滑无级变速」无任何成品后端——我方恒速推帧+包级跳过+流内 seek 恰是该组合的正确实现** | 子代理报告（agents 目录 agent_8c811a24） | 「放弃 MPV」裁决：**不值得换**（三大毒药已架构规避，换=作废全部已验证数字）；SDL2 存档应急预案；WebCodecs 条件性重估（CPU 预算≤2 核/强需 AV1 时优先重估） |
| **一期 1a+1b 施工实测（09-12 深夜++「开工」，全 T0）**：**dsvc.exe（Rust 解码直通服务）位级验收**——4.mp4 64 段 pushed=18061/**span0+span2 起始各 3 帧 vs PyAV 干净解码 mad=0.0 字节全等**/seek 虚拟帧 9000→27.6s 完成对账吻合/per_span ±1=源 VFR 抖动；bench 吞吐 decoded 2029fps/kept 1606fps=**10x 需 750 的 2.7 倍余量**；**服务四语义（自造核心）**：①send_packet EAGAIN 须泵式重发（帧级多线程解码器收发互斥，吞包=-21% 帧丢失）②段尾流水线压 ~线程数 帧→**段切换 flush=空包 drain 吐尽+flush_buffers 复位**（复位无害：下一段锚点关键帧重启）③**包按 dts 序到达 pts 乱序→段推进必须按 dts**（dts≥段尾⇒无更早 pts 包）+未来包扣留池 ④drain 哨兵须 EAGAIN 重试；**引擎接入六坑（mpv/libmpv 0.41）**：①loadfile=`<url><flags><index><options>`（4 参字符串=options 落 index 槽被 except 吞=静默丢全部 per-file 选项——**skim cache=no 假绿修正**）②stream_cb 注册名=纯 scheme ③**framedrop=vo×keep_open=yes 组合在 rawvideo CFR 流致死**→per-file framedrop=no ④read 被 cancel 打断返 -1（0=EOF 杀流）⑤回调异常（NameError 等）不炸进程但 demuxer wedged——mpv log-file 原生日志排障 ⑥rawvideo demuxer 四件套必须 per-file。**引擎级 E2E PASS**：R1 注册/R2 完整播放至 EOF（mode=dthrough/speed 恒 1.0/tp_end×60=411≈dsvc_kept 413 自洽——VFR 源名义值不可信）/R3 播中 seek delta=0.033s/R4 零孤儿；**504+90 绿**（2 处 loadfile 契约断言更新） | dsvc/工程+probe_dsvc_smoke+probe_dthrough_engine+diag_rawseek+diag_rawload+dt_mpv.log | 1c 台账映射前提（per_span 实测）/1d DoD 基线/mpv 陷阱清单 |
| **一期 1c 施工实测（09-12 深夜++「开工」续期，全 T0）**：**双轴台账定案**——名义轴（dsvc locate 算式逐位镜像 `ceil(floor((b-a)*fps)/rate)`，fp 行为跨语言一致）编码 seek/start= 命令；实测轴（dsvc span/eof per_span）解码 time-pos 读回，段内线性插值、累计误差被逐段实测钳住——**禁名义值落地**（3.mp4 名义 520 vs 实测 413 的映射修正）；seek 后 rebase 重定基（实测轴原点=落点 tick、截断首段剩余槽估计、名义轴永远全表）；`derive_spans`=plan 保留段×业务段（闭区间 [s,e]、列表序首中优先=_biz_speed_step parity），rate=业务×UI 取整 ≥1。**新坑三枚**：①`_command_seek_locked` 暂停态 +5/120 修正只豁免 skim——直通 CFR 轴同样无源 PTS 边界病，扩 `not in ("skim","dthrough")`（否则暂停寻址偏 2.5 tick）②**服务层 eof/span 事件缺 gen 门**——旧代尾 eof（seek 竞态）会把新流误标 EOF 致 mpv 断流（1b 隐患，1c 修复）③Mock 真值劫持（`getattr(io,"dthrough_active")` 对 Mock 恒真）。**分档门语义**：原生+UI≥1x+跳过裁剪区开+有删段+开关开+生效速率（UI×业务段峰值）>3x→直通；失败回落 Tier1；直通接管时跳过 skim 补建。**1c-④ 裁决**：150ms 节流/有界队列架构内已有；seek 去重=dsvc 世代抢占+mpv 队列天然承担=「架构性吸收」。**测试 542+90 全绿**（新增 38）；E2E 复验 PASS | dthrough_ledger.py（新）+dthrough_service.py+mpv_engine.py+preview_player.py+tests×4 | 双轴模型=任何 CFR 虚拟轴流回读的通用解/1d DoD 待真机/手检清单在 checklist Current |
| **直通档解码预算限速（09-13 凌晨真机首测 T0；⚠根因结论已被下行二次修正行部分推翻——rate100 场景真实存在（探针为证）但非 owner 当时实际场景）**：owner 真实段表 10x 手检掉帧（OSD 27.9-32.9）→ **根因=解码账单 rate×60 帧/秒与 rate 成正比**：0.2x×factor10×UI10=rate100 需 6000fps > dsvc 2029fps → mpv 缓冲耗尽**间歇冻结**（探针 A 相 wall−虚拟=5.8s 冻结实测；饿死形态=冻结非均匀降速，OSD 低读数=采样窗撞冻结）。**修复=derive_spans rate_cap=20**（0.2x 区域预览 20x 通过、指针靠实测轴保精确=Tier1 cap 同哲学）；B 相总停顿 0.18s 逐段 60fps（GUI 同跑）=CAP_FIX_VERIFIED | probe_dthrough_mixedrate.py（A/B 双相停顿账口径） | **铁律级教训：业务加速×UI 倍速必须过解码预算**；合成段表（全 rate=1）探不出此雷=真实段表手检不可省 |
| **掉帧诊断二次修正：碎段换段税（09-13 凌晨 3 点档 T0）**：owner 指出滑条上限 4x、截图 UI 1x → 首轮「rate100 解码账」诊断对其实际场景**证伪**（1x 账单 600fps）；**真实根因=碎段换段税**——保留段中位 ~11 帧，0.2x 段 rate10 每 ~18ms 换段 × 每段固定成本（dsvc 重定位+锚点回填+流内 seek ~30-50ms）→ ~30fps。**修正分档门（双条件）**：UI>3x 且任一 span 呈现帧 ≥60 → 否则 Tier1（单文件零换段免疫换段税）；rate_cap=20 保留作高 UI 速解码账守卫。**架构定论**：碎段密项目 >3x 真掠过被换段税挡死（换语言/换架构同罪——上游顺序解码、Tier1 封顶 3x、直通换段税是同一面墙的三种撞法），解锁=dsvc 小间隙连续解码（解码账 ~600-900fps 可行，待立项）；UI 1x 真导出节奏需抬 Tier1 3× 业务帽（mpv 顺序解码天花板 600fps，边际，待拍板） | probe_dthrough_mixedrate.py+碎片化项目实测 | **探针必须覆盖碎段分布**（合成段表全长段=系统性盲区）；直通档适用面=长段项目，碎段项目待 dsvc 改造 |
| **§4.0m 小间隙连续解码施工实测（09-13 凌晨++，owner 睡前「开工」；合成源位级+真实源性能双验收，全 T0）**：dsvc 重构三件=帧归属按帧 pts 二分（span_of）+ **扣留池整体移除、全包 dts 序直喂** + dts 推段逐边界自适应（连续 iff 间隙帧≤下段回填+cap；恒等式 restart ⟺ anchor>b+cap ⟹ 锚点包未流过=干净重建自动成立）。**v1 潜伏缺陷修复=段尾 ~3 帧微损坏**：v1 扣留池按 pts≥段尾扣未来包，而 B 帧的未来参考 P 包恰是 pts≥段尾∧dts<段尾，扣留令其 B 依赖先喂=参考链断裂（合成源实测 units 57-59，此前测试只采样段首未覆盖）。
| **碎段直通解锁实测（09-13 凌晨++，§4.0m 兑现）**：合成 B 帧源（x264 b_adapt=0 强制 kf@2/4/6）A 相 240 帧逐字节全等（模式 [restart,cont,restart,cont]、per_span [60]×4=跨间隙 B 帧参考链位级正确）+B 相全 restart 合法表 180 帧全等；碎段性能探针（200 岛×11 帧×间隙 30×混合 rate4/32=owner 形态 UI4x）**A 相纯播放窗停顿 0.02s/虚拟 6.17s=FRAG_SMOOTH_VERIFIED**；守卫曾放宽 60→2、**09-13 晨真机 T0 整条移除**（0.2x 岛 cap32 呈现 0.34 帧 <2 → 整单打回 Tier1+skim=糊+12x；探针直调 play_dthrough 绕过 GUI 门=盲区；直通接管即 clear_skim_blob 清场）+rate cap 20→32；cont/restart 比例可观测（A=137/20）；env DSVC_CONT_GAP_FRAMES 钳位 [0,1000]（负值取消=防非法 restart；cap0=零间隙连续=退路）。探针=probe_dsvc_cont.py+probe_dthrough_frag.py；回归 546+90 绿+E2E PASS（EOF 采样容差 3→5 帧=口径修正）+mixedrate CAP_FIX_VERIFIED；GUI pid 44148。
| (提案未证) G双窗原生呈现≈80-100ms且不碰内嵌状态 | 本轮分析 | 探针D已证可被原生步进替代 |
| **真机 bug 五连修（09-16，owner 手检驱动，全 T0）**：P0 末尾空流假播放（片尾回绕）/P0 seek 后冻结（dsvc 锚点前移+目标段强制 restart；首次推进 0.06~0.22s）/P1 点不到（裸单击=跳转、Ctrl+单击=选段）/P1 空格（即时抢焦点）/P1 卡顿（帽 24→20，实测吞吐 1380fps）；553+90 绿+五探针全 PASS。**另：行业调研六路（报告 agents/research_proxy_materialize/00~07）=物化代理方向获全行业印证、体积小 15~50 倍、组合无先例。**
| **分析结果磁盘缓存（09-16 落地，T0）**：键=sha256(源，复用 media_info 已算值)×参数指纹×后端×ffmpeg身份×格式版本；npz 单文件+CRC32 自证+同目录原子写（Windows WinError 5 重试）；版本进键迁移；LRU+孤儿清理；首次提示一次。**实测：4.mp4 重导入 4min→18ms**；单测 20/20+同源探针四相+574+90 绿。调研报告=agents/research_analysis_cache/01_cache_practices.md（440 行）。|
| **缓存 v1 致命缺陷修复（09-16，T0；R8 第 7 次同类事故）**：真实 analyzer 产物的 pauses 带 **ndarray** `local_del_mask`（`analyzer._analyze_pause_mask` 产物），v1 对其 `json.dumps` → TypeError 被"缓存是纯加速层"兜底吞掉 → **真实产物从未写成功过**；且 `_store_analysis_cache` 无条件打印"已缓存"=**日志谎报**（探针 V1b 手搓无 mask 的简化 pauses 故全绿=假绿）。**v2 修复**：mask 抽为 npz 独立成员（`masks_concat` uint8 + `mask_lengths` int64，meta 留 `__npz_mask__` 占位；必须**移除**原 ndarray，只加占位仍会撞 JSON）→ 载入按长度切片还原 ndarray（缺失/None/无字段三态区分）；`store()` 失败**带 code 抛出**（STORE_META_NOT_SERIALIZABLE / STORE_WRITE_FAILED / STORE_MKDIR_FAILED）由调用方如实上报；提示框 MB 口径统一为真实文件字节（原误用 len(states)=帧数）；`_unpack_pauses` 内部不一致（长度和≠拼接体/索引越界）**抛错令整次载入失败**（宁可回退完整分析不返回残缺 mask）；`load`/`describe` 改 `with np.load` 免句柄滞留。**验收**：单测 40/40（含真实 build_segments 产物往返位级一致、mask 三态、值溢出/二维拒绝、下游删除帧集合一致）+**同源探针 v2 六相**（真实 3.mp4 全链路：29804 帧/65 暂停段，写入 222KB/12ms、载入 2ms、states/diffs/mask/speeds 逐字节一致、下游删除 27903 帧一致、参数变化不命中、损坏降级）+**594+90 绿**。⚠旧探针 `verify_analysis_cache.py` 已标注"假绿勿引用"，现行判定一律用 `verify_analysis_cache_v2.py`。|
| **缓存 v2 复查（09-16「Review 一下有没有引入新的问题」，T0；修 3 处自引入缺陷）**：①**`_lossless_cast` 误拒浮点**（整数/浮点同用 `array_equal` → float64 的 0.1→float32 被判"有损"而拒真数据）→ 判据分离：整数逐元素相等 / **浮点只要求"有限值不溢出为 inf"**（float64→float32 舍入是目标格式定义非损坏）/ NaN 进 NaN 出无损；②**模板指纹未冻结**（`make_key` 现算，store 与 load 各自重算 → 两次间模板被改则键不一致、语义错位，且白算两遍 9.6MB sha256）→ 分析请求发起时算一次**冻结进 snapshot**；③**测试依赖 CWD**（`tf() != "none"` 从非仓库目录跑假失败）→ 显式传仓库根 + 新增 CWD 口径自证用例；另清理 conftest 无用 import。**验收**：单测 60/60 + 探针 v2 六相复跑全过 + **621+90 绿**。|
| **4.mp4 分析缓存预建（09-16 owner 令，T0）**：R8 同源探针 `prebuild_cache_4mp4.py`（驱动真实 `main.main()`+真实默认参数 A_PT）→ **P1 冷启动 337.1s 落盘 3.9MB**（424176 帧/2210 暂停段/键 `c44cb72a68d5`）+ **P2 热命中 0.0s 且 states 逐元素一致**；独立探针 `verify_prebuild_hit.py` 走真实 GUI「导入→点分析」实测 **6ms 完成 + 弹「发现分析缓存」**。键一致性三重核实（OpenCV fps≡PyAV average_rate≡GUI self.fps；ffmpeg 身份一致；模板指纹一致）。**探针纠错**：完成判据 `ttk.cget("state")=="normal"` 恒假致假阴性 120s，改判 `_analysis_handle is None and states_array is not None`。|

## 七、铁律（唯一权威源；交接只引用不复制）

- **R1 parity强制**：引擎移植/改动先出 parity 表再写码。模板三列：
  `功能 | 上游(liemark)有无+行为 | mpv有无+行为`，性质标"砍/差异/新增/持平"。
- **R2 先盘再造**：实现前 grep 上游+本表§六+已有探针；没盘完不许造轮子。**造探针同样适用（09-16 事故补强）**：写新探针前必须 `ls .cache/v3ass/probe_*` 并读同类探针——已有可复用/可扩展的即复用或改造；确需自建时，在探针 docstring 首段写明「为何不能复用已有探针」。
- **R3 数据先行**：先找可直读定谳信号写一行测量计划；卫生=单屏/无损PNG/DPI感知。
  探针首行必须 `from probe_guard import assert_measurement_hygiene` 并调用；
  方案/计划模板强制带 **parity表+ACH矩阵** 两栏，缺栏视为不合规（opt-in→默认）。
  **「进程内驱动」的定义（09-16 事故澄清）**：指在进程内驱动**生产的真实对象**（GUI=复用 main.py 的 `main()`，拦在 mainloop 前跑自检；引擎=驱动真实 Engine 实例），**不是**另建一个「近似的」复刻实现。
- **R4 清单维护(带牙齿)**：每次更新交接必须同一次提交触碰本文件；
  新能力/资产当场登记§五/§六，否则视为不存在。
- **R5 ACH强制(治确认偏误)**：任何症状/停滞诊断，先列≥2个竞争假说，
  建§九证据矩阵，用"不一致证据"淘汰，禁止单假说找支持。
- **R6 事实/判断分离+结论分级(ICD203)**：结论必须分开"观测事实"与"我的推断"，
  置信度给依据，并定级：**T0**=位级实测/逐位一致，**T1**=多源验证可复现，
  **T2**=单源推断，**T3**=猜测。**门禁：生产代码只准嵌入 T0/T1；T2/T3 必须
  显式标注且不得进生产，须 owner 确认后才可升格**（对齐 1Ark 铁律⑧）。
- **R7 制度封版**：本轮规范化后**只维护、不新增**制度件；确需新增须先登记
  §八 并标"防膨胀"理由。治文档陈旧/膨胀（借鉴 1Ark D-12）。
- **R8 探针同源（09-16 事故后新增，登记 §八）**：**验证工具必须与生产同源**——驱动产品行为时复用生产的真实构造代码与真实入口（GUI=main.py 的 `main()`；引擎=真实实例），**禁止自定义复刻**（复刻版必然与产品漂移，且漂移点正是缺陷藏身处）。自建复刻前须先穷尽复用路径并记录「为何不能复用」。
  **探针产出的 T0 结论入档前，须自证「探针口径与生产一致」**（打印关键中间量、或与产品同源）；口径未自证的测量结果只算 T2/T3，不得作为定案依据、不得进生产代码（与 R6 门禁对齐）。
  **R8 补充·事件层同源（09-24 新增，来源=「点击后红条冻死」事故）**：驱动 GUI 交互时，**优先注入真实事件**（`widget.event_generate("<Button-1>"/"<B1-Motion>"/"<ButtonRelease-1>")`）走真实绑定链，而非直调 `_on_mousedown`/`_on_mouseup` 回调——后者跳过绑定层与事件顺序，**恰恰漏掉「状态机早退漏收尾」这类缺陷**（09-24 事故：`_on_mouseup` 早退分支漏调 `on_handle_end_cb` → `_is_dragging` 永久为真 → 红条冻死；直调回调的探针与单测都测不出，只有真事件路径暴露）。
  **能力边界（T0 实测）**：`event_generate` **要求窗口真实映射**（`withdraw`/未映射时事件不投递，绑定不触发）；OS 级真鼠标（`SendInput`）能力存在但需真实桌面焦点——**两者都只能跑可见桌面会话，不能进 CI**。故分层：**CI 层**用「直调回调 + 状态契约断言」（如 `test_every_mouse_path_closes_drag_state` 遍历四条鼠标路径断言收尾）；**桌面层**用 `tools/gui_stress/random_seek_stress.py` 做真事件随机压测（落点/延迟/状态标志实时读取）。


- **上游 v26.10.07 调研（09-24，owner 令「深入分析」；T0，API 直连 + 本地克隆交叉）**：上游 2026-10-06 发布 v26.10.07（`cb85d940`，Release body **空**，资产 `26.10.6-win.zip`），距我们基线 v26.7.23（`ce103609`）约 2.5 月。**主变更**=①PR #10 **GPU 加速重构**（`e368d15b`，+5373/−1703；`analyzer.py` 拆成 app_core/matcher/pipeline/exporter/gpu_caps；声称 NVDEC+GPU 匹配 **14.24×**、2400/2400 分类一致；匹配后端 CUDA 0.100 / CPU批量 0.674 / OpenCL 2.13 / cv2直算 2.53 ms/帧，自动测速选路；导出阶梯+真实进度）②PR #12 **合入第三方 fork 修复**（`e3451ba3`，来源 tryptophawa **非我们 fork**；含 **0.2x 判据修复**=要求 0.2x 分数须为三类最高，因 0.2x 模板「正在播放」图标在 1x/2x 帧上亦高分；另导出重写/打包脚本/timeline_widget 去 mask 回调/video_io 去 resize 缓冲）。**与我们关系**：**我们独有 11 文件上游全 404**（mpv 预览栈/dthrough/认证 EDL/分析缓存/skim 等）；上游独有 GPU 栈与模块化。**性能口径警示**：不可直接比——我们 4.mp4 **0.79ms/帧**（A_PT 已优化），上游 27.27 是 OpenCV 串行基线；我们端到端 320s ≈ 解码 227s（A_PT 1869fps，硬地板）+ 92s 未重叠/尾差（原文「231+67」串行相加有误，见文末 review 修正）。**本机**=RTX 4060 Laptop（同上游测试机）/OpenCL 可用但已剔除/torch 未装。**裁决（待 owner；经 09-24 review 修正）**：不直接 merge（架构分叉+timeline_widget/video_io 双向冲突+毁预览栈）；分级吸收 **P0=0.2x 判据修复**（防御性；生产同源复测 1200 帧 0 差异）→ **P1'=CUDA 匹配**（端到端 320s→约 230-260s，-20~28%；上限受解码地板 227s 约束）→ **P2=NVDEC 硬解**（真正的主杠杆，解码占端到端 71%）→ P3=导出阶梯。**⚠ 已剔除 OpenCL**（上游实测仅 1.19×，比我们现状 16 进程池慢 4.6×）。**P1 前置**=先建「分类一致率」验证口径（上游 max|Δ|≤1e-6），否则 GPU 化可能静默改分类污染剪辑决策。**⚠ review 修正三处（09-24）**：①原「0.2x 影响验证」用 cv2 取帧（生产是 ffmpeg scale=area），非同源→重做后 0 差异（结论不变、过程曾不可信）；②原「337s=解码231+匹配67」**前提错误**——`_AsyncClassifier` 明确重叠，实测端到端 1327fps/320s=解码227s+92s 尾差；③原「OpenCL 67s→20s」**严重高估**（上游自家表格 2.13 vs 2.53ms=1.19×）。**⚠ 0.2x 方向复核（owner 追问「最新上游是否仍有 0.2x 问题」）：否——上游已修，未修的是我们**（v26.10.07 源码两路径 + 发布 exe 字节码双重证实：`classify_gray` 含 `s02`/`max`、`classify_from_scores` 含 `best_speed`/`np.maximum`，此二局部仅修复版存在；我们 `analyzer.py:158` 与 v26.7.23 规范化比对逐字相同）。全文+修正附录 `.cache/upstream_review/ANALYSIS_v26.10.07.md`。
- **P0「0.2x 判据修复」落地（10-07，owner 令「开工」；T0 全链验证）**：`analyzer._classify_gray` 0.2x 分支改「须三类最高」——`s02 >= 阈值 且 s02 > max(x1s,x2s)`（逐字对齐上游 v26.10.07 `matcher.classify_gray` 修复版语义）；**配套 `analysis_cache.ANALYSIS_CACHE_VERSION` 2→3**（states 语义变 → v2 缓存必须不可达，否则复用旧判据产物）。**验证**：①新单测 `tests/test_analyzer_classify_02x.py` 5 例（缺陷现场/严格大于边界/优先级不变/**网格 1029 点与上游修复版参照实现 parity**）；②全量 **644 passed + 94 subtests**；③**R8 影响面探针** `.cache/v3ass/probe_02x_fix_impact.py`——「旧」判据从固定提交 f825983 **抽源码 exec 成真身**（非记忆复刻），「新」=直接调生产 `_classify_gray`，共用同一 `_get_best_score`（单变量）；全片扫描 **双判据同测 101971 帧**（旧缓存所有 0.2x/1x/2x 帧 + 对照）→ **差异 0 帧**；修复只会移除 0.2x（不会新增）且 1x/2x/pause 分支逐字未动 ⇒ 全片等价，**本素材属防御性修复**；④缓存失效证明：v2 键摘要按原 6 字段重算=存储值 `c44cb72a…` **逐字符复现**，v3 `fe3e7bdf…` 不同且 `load()=None`。**配套**：4.mp4 预建缓存按 v3 重跑完成（冷 310.6s → `fe3e7bdf…npz` 3.9MB，热命中 0.0s，`PREBUILD_OK`；**新产物段数与 v2 完全一致=2210 暂停/2212 变速** → L3 两探针 2210 硬编码无需更新）。
- **R10 发布前门禁（09-24 新增，owner 令「把发布前必跑清单固化」）**：`tools/acceptance_gate.py` 四层清单，一条命令跑完，**判定自动化**（读各探针 results JSON 的 verdict 字段；无 JSON 的解析 stdout `[verdict]` 行；探针缺失=SKIP 并打印原因，杜绝静默漏跑）。分层与耗时（T0 实测）：
  | 层 | 内容 | 依赖 | 耗时 |
  |---|---|---|---|
  | L0 | preflight + inventory_audit | 无 | ~0.1s |
  | L1 | pytest 全量（含事件契约） | 无 | ~30s |
  | L2 | dsvc 位级 / 引擎 E2E / 碎段 / 混合倍率 | dsvc.exe + 3/4.mp4 | ~90s |
  | L3 | 同源手检九项 / 缓存命中 / 立即点击 / 随机跳转压测 | **可见桌面会话** | ~75s |
  **全层 11 项 ≈194s**。用法 `python tools/acceptance_gate.py [--level L3] [--list] [--only KEY]`；退出码 0=全过 / 1=有失败 / 2=环境缺失。
  **首次实跑即抓到真问题（门禁价值实证）**：`dthrough_engine` 的 R3 出现 ~25% 偶发 FAIL。根因=**探针自身的坐标轴错误**（`seek_dthrough_frame` 收**虚拟 tick**（压缩后 ~520），探针却传 `源秒×60`=19569 的**源帧号** → 越界 seek，mpv 行为不定：clamp 到流尾=偶然 PASS、直接忽略=tp 停旧值=FAIL）。修=按名义轴累加前序段保留帧数求 target_tick；修后**连跑 6/6 稳定 PASS**。**教训**：门禁不仅抓产品缺陷，也抓**探针口径缺陷**——这正是它必须自动化、不能靠人记的原因（该 bug 手工跑"看起来是 PASS"，因为偶发）。
- **R9 测试禁触真实 UI（09-16 事故后新增，登记 §八）**：**测试与探针不得调用真实模态对话框**（`tkinter.messagebox.*` 等会阻塞并需要人点确定的东西）。此类调用在无头/CI 下**挂死而非报错**（失败信号=超时，极难定位），在本地则弹出**无主窗口污染开发者桌面**，并会误导排查方向（09-16 实测：我在自己的接线测试里传 `templates_missing=True` 却只 patch 了 `showinfo` → 真实 `showwarning` 弹出并永久阻塞；`timeout 8` 实测 exit 124，而 `pytest -q` 整体因顺序巧合仍报"全绿"，桌面上那个无主弹窗被 owner 看到、我最初还错判为别的项目）。
  机械版=**`tests/conftest.py` 的 autouse 夹具**（`_no_blocking_dialogs`）：全局把 showinfo/showwarning/showerror/askyesno/askokcancel 替换为记录器，永不阻塞；需断言"确实弹了窗"的用例取 `dialog_recorder` 夹具。今后任何人新增测试都不会再踩。

## 八、决策登记（ADR-lite，唯一权威；防"静默默认"）

| 日期 | 决策 | 上下文 | 选择 | 后果 | 状态 |
|---|---|---|---|---|---|
| 09-04 | mpv剥离业务变速 | 引擎拒播速度策略,日用保护 | 忽略业务参数按原速+提示 | 业务加速静默失效(重大失误) | ①已修复(f53d6dc 已提交) |
| 09-04 | 静帧=C4隐藏截图回贴 | 需帧精确+同管线 | 隐藏mpv+Y4M+截图+Tk贴 | 步进~660ms偏慢 | 步进已原生化(f53d6dc)；C+挤水分0.82→0.45s；选项2闭环定位21-90ms(四样本T0)已实现待验收，C4降级为兜底(重复段内/EDL/验证失败) |
| 09-04 | 制度封版 | 规范化完成,防膨胀 | 只维护不新增(R7)+T0-T3门禁(R6) | 制度件冻结 | 已定 |
| 09-05 | 选项2复活并实施 | 老否决(「tp报目标值,无读回」)被微探针推翻:瞬态报目标/落定报实际,差在读的时机;frame-step位级精确可调 | seek(无修正)+playback-restart门+tp账本float精确对账+≤4步frame-step补齐+复核,失败/重复段内/EDL回退C4;+5/120修正仅存于旧路径 | 点轴0.45s→21-90ms且帧精确;双保险(闭环+C4兜底) | 已实现,4/4样本验收PASS,待owner手检+「验收」 |
| 09-07 | 业务变速掉帧归因(f53d6dc parity缺口)·两轮修正 | owner报UI 1x右上角FPS偶发35-45并质疑第一轮结论;第二轮证实:**真机制=EDL拼接边界风暴**(skip_trimmed默认True→日常播放走EDL,保留区≈73短段,业务几乎全0.2X型→factor=10时每30-60ms跨拼接边界=demuxer源内seek→真实停顿,F1复现31/33窗<50/min14.4);第一轮「snapshot新鲜度失配」假说**被证伪**(stale/fresh双变体同跌,staleness≤133ms);「cap=3一行治愈」修正为「cap=3治愈EDL稳态(F2轮内全60),启动瞬态1-1.5s仍跌」;上游`_preview_step_plan`cap=3未移植=R1 parity缺口仍成立 | 候选:A=cap parity(**F2实证EDL稳态有效**,推荐);A+=EDL模式业务加速策略再议(裁剪本身已是快进,10x扫保留区意义存疑——产品决策);C=EDL事件线程减负(Fraction线性扫段210µs/call);B=OSD业务感知降级为可选(读数本就诚实);零代码缓解=取消「0.2x区域加速」勾选或factor降3 | 两轮探针T0;第一轮两处结论已修正 | **已实施**(09-08「开工」:cap parity 落地=Tier1 地板,引擎接 request.preview_step_cap 作用于 base×biz 乘积+UI 速率,E2E 实证封顶恰 3.0) |
| 09-07 | 真10x终选候选=P1-RAM纯内存掠过(owner反感提前写硬盘) | owner「有点反感提前写入硬盘」→串行调研+三轮探针:seek率墙定理(实时方案≤5-6x,两引擎同墙)+先例(AE RAM Preview=T1公认;LosslessCut低质代理;HLS物化I-frame流;auto-editor export-first)+libmpv官方stream_cb API;管道/TS批拼接/小探测参数三路证伪后,**单命令全局PTS TS→RAM→ram://协议=真10x实证**(9.11x/OSD 8/8恒60/tp_end≡虚拟全长/重播净;唯一疵0.2s启动停顿已定位可预热) | P1-RAM:认证tick filter图(生产同款)出GOP2-TS→bytearray驻RAM(4.2MB/保留秒,480p=1.8;大GOP可再降)→mpv_stream_cb_add_ro注册ram://→play_edl等价播放(映射零改动,PTS保真实测);编辑=删段弃RAM块~0ms/拖边界重取单段~1-2s/重分析全量预取6.6s(3.mp4实测;2.mp4需自适应分批设计);会话重启=重新预取(易失性=零磁盘的代价,期间Tier1=live EDL+cap3兜底);预热(预loadfile暂停待命)规避启动停顿;精确停顿/定位仍走源路径;若需流内精确seek→RAM内MP4(faststart)变体(设计点) | P0 cap地板不变(预取未就绪回退+R1 parity缺口);RAM足迹2.mp4规模估~630MB(1080p GOP2)/~270MB(480p)——降档与大GOP为开工调优点;增量重建教训:TS拼接需-output_ts_offset或remux | **已实施**(09-08「开工」,详见下行) |
| 09-08 | P1-RAM+P0地板实施落地(「开工」口令) | B0先诊断:0e所记2.mp4「逐段构建病理」**改判=旧探针缺陷**(每段挂tpad尾克隆→克隆等输入EOF→窗口外解码到文件尾,耗时∝距EOF线性;Δ=0账本≡原始pts漂移嫌疑证伪;尸检输出帧数全对=窗口一直匹配);生产形态定案=单命令+**-t输入截断**(11.5s vs ~80s同窗口),2.mp4规模首建85.3s帧精确(原估3-6min过悲观) | 实施:skim_cache.py(构建+验证门[TS同步字节/帧数kept+1/时长≡压缩轴/预算]+看门狗取消+BUDGET降档阶梯480→360→240)+引擎(stream_cb迁移/**canonical_load_target scheme旁路**[ram://被Path.resolve揉碎→file-loaded校验永失配暗雷]/_skim_id代号URL/per-file cache=no/_biz_speed_cap两级[Tier1=request cap,Tier2=12]/skim模式三处对齐)+GUI(_maybe_schedule_skim_build守卫链+_publish_project_snapshot单漏斗debounce1.2s+设置开关skim_preview默认开) | pytest **504+90**全绿(自查review F1-F6+owner现场两轮 G1-G7:seek_edl保Tier2/skim免修正/源模式撤cap/stat守卫/**TaskContext无cancel_event致构建秒崩(owner现场4.mp4全程Tier1真凶)**/**千段级内联57099字符超Win32K→script文件**/降档估算表补档/角标OSD档位标签(掠过10.0x·保底3.0x·源)/**开关关时清已装blob**/**预算两次上调400→800→1200MB(owner 4.mp4 现场 895MB 预估被拒实测驱动,保留量反推≈9.9min)**);probe_skim_e2e真机**10/10**三轮一致;probe_skim_scale1000=1000段21.7s帧数全对;probe_skim_phase_timing=分段计时(推导0.1s/ffmpeg16.6s/验证2.1s@1418fps/入库2µs);probe_upstream_decode_bench=上游对比T0(129fps vs 1835fps);上游基准已纠正:克隆①带A_PT(我们PR#9)分析同量级;遗留候选:增量remux(权重↑)/验证门包计数化/预热loadfile/factor UI钳12;已知限界:校准工具cap3硬编码/构建与Tier1播放CPU竞争窗/4.mp4级首建3-6min(T2待现场真数)/communicate瞬态2x | **实施+两轮review+两轮现场修复完成,待owner复测(当前GUI)+「验收」** |
| 09-12 | **Tier2 改判：RAM skim 退役 → 解码直通（包级跳过）** | owner 否决 240p 糊（保留 29.8min；根因=估标表悲观 3x + constqp qp6 浪费）；溯源后 owner 令「打破物理规律」（允许串行子代理+换语言+flclash 代理）；调研发现 seek 墙未覆盖「零 seek 顺序解码+输出过滤」分支；9 探针/诊断文件+子代理文献一役（738k tokens） | **一期=1a Rust 解码服务**（cargo/rustc 1.97.1 已装；ffmpeg dev 库经 flclash 127.0.0.1:7890 下载 shared dev 包）**+1b 引擎三级模式**（≤3x 源/EDL 原画质，>3x rawpush://rawvideo 直通；暂停/定位照走源路径；大跳转=锚点关键帧重进场）**+1c GUI 分档接管**（开关复用/角标「直通」档）**+1d 测试门**（504+90 不回退+DoD：4.mp4 真实段表 10x 全程/停顿<300ms——原型单次 752ms，readahead/分块调参达标/CPU≤4 核/解码层抽帧 mad=0）；skim_cache 休眠保留；PyAV 原型留对照；二期=逐段业务倍速移植/极端碎段 | 后果：零存储零构建原画质 10x（余量 2.4x）；新增编译服务件；真实段表接线=1b/1c 工作项；G7 保留量推断修正（9.9→29.8min，895MB=240p 档估算）；Errno 22 本轮分析干净未复现（取证埋点在岗） | 状态：**施工中——1c 完成（双轴台账+分档门+E2E 复验 PASS+542+90 绿，见棒 §4.0l+§7），进行至 1d DoD 真机** |
| 09-12 | **造轮子审计完成+逐段倍速机制改判（推流端节流）** | owner 令「Phase1-2 调研是否存在重复造轮子，串行子代理不限量」；本地 T0×2+子代理×3（672 万 tokens，报告留 agents 目录） | 判定：FFI 绑定（rsmpeg vs ffmpeg-the-third 双候选 1a bake-off）/demux 解码/swscale/管道/渲染全用现成轮子；**真自造仅六件套且三度确认业界零先例**（段表匹配器/包跳过状态机/锚点恢复/段切换 flush/推帧节奏映射/控制协议）；**逐段倍速并入一期**（推流端节流：业务速率编进推帧节奏、mpv 恒 speed=1——规避 #13694/#13513 speed 切换 bug 族，带宽 1.86GB/s→恒 186MB/s，60Hz 视觉等价）；**跳转升级=rawvideo 流内帧级 seek**（seek_fn+size_fn，帧号×帧大小）；1b 增补=禁 display-resample/EOF 事件规范/独立进程架构 | 后果：一期更简（无 speed 切换面）、带宽恒定、跳转轻量化；新增编译服务件与 preflight 三件套（link.exe/libclang/FFMPEG_DIR）；**交接棒发现非本会话并行条目（rsmpeg 选型），已共存合并待 owner 确认会话状态** | 状态：**施工中（1c 完成，1d 待真机，见棒 §4.0l+§7）** |
| 09-12 | **上游基线调研+放弃 MPV 裁决：维持解码直通** | owner 令「基于上游方案展开调研，探索更直接更优的预览流畅方案，允许大量串行子代理，甚至放弃本地 MPV；网络走 flclash；本地两份上游克隆（实为 v26.7.15/v26.7.23 exe 发行包+1 份 git 克隆 HEAD=ce10360）」；串行子代理×3（本地考古/GitHub 全景/后端文献，首轮额度墙重试即过） | 三线裁决：①**上游考古 T0**——预览=cv2 顺序+grab 丢帧+POS_FRAMES seek+Tk 轮询，无拼接预览/无音频/无物化，可取仅 3 条 UI 技巧（seek 命令合并去重/150ms 节流刷图/有界丢帧队列）并入 1c；②**GitHub 全景 T0**——7-23 后零提交，issues/PRs/forks/releases 零预览工作，无隐藏路线；③**后端文献**——VLC/Qt/GStreamer 零机制性收益，SDL2=应急预案存档，WebCodecs=条件性重估（CPU≤2 核/强需 AV1），NVR+GES nlesource.c 直证双旁证验证现架构 | 后果：一期施工单不变；「基于上游/放弃 MPV」双关闭；1c 新增 3 条可吸收项；报告原文 agents 目录 agent_60b5a30f/agent_314dd539/agent_8c811a24 | 状态：**调研闭环，一期施工中（1c 完成，1d 待真机，见棒 §4.0l+§7）** |
| 09-13 | **立项：dsvc 小间隙连续解码（owner 已批，压缩前未动工）+ 机器上限提升挂起** | owner 目标定案「4x 预览=导出×4 且流畅（碎段项目）」；承接碎段换段税定案（§六 09-13 行） | 施工单六步（棒 §4.0m 全文）：①dsvc 自适应边界（连续 iff 间隙帧≤锚点回填+16；连续=不冲洗+无锚点跳过+池不丢间隙包；span 事件加 mode 字段）②位级重验 mad=0+跨边界 B 帧用例（最险一刀）③碎段探针（11 帧岛×30 间隙×混合倍率 A/B+cont/restart 比例）④门放宽 60→2+解码预算帽校准 20→32⑤全量回归⑥真机 4x 手检→DoD | 兑现表：流畅/普通段节奏/指针=100%；慢动作段封顶 ~32×（理想 40×，2400fps>2029 上限）=该段 80%、仍为导出该段 3.2 倍；补齐路径挂起=GPU NVDEC 探针/skip_loop_filter；⚠推送前决策=dsvc 源码在 .cache 应入库 | 状态：**已批待开工（收「开工」按棒 §4.0m 动工，工期约半天）** | **→✅09-13 凌晨++施工完成（实录=棒 §7 末条+checklist §4.0m 段）：位级+性能双验收绿，剩 owner 手检+两笔提交。**
| 09-16 | **新增 R8「探针同源」+ R2/R3 补强（治复发 6 次的探针盲区类事故）** | owner 问「为什么你的方案不直接用现有的 GUI，而是自己倒腾一个？印象里不止一次」→ 全史取证确认**同一类事故两个月内复发 6 次**（08-30 GUI 盲区 / 09-08 探针缺陷改判 / 09-13 守卫盲区 / 09-16 三连：seek 口径错、自建 GUI 1px、分析子进程弹窗） | 根因=制度缺条：R2 只管「造轮子」未含「造探针前先盘已有探针」，R3「进程内驱动」措辞未定义「驱动谁」（可被误读为「另起近似物」），R6 只约束结论进代码、未约束探针口径自证。**R8**：验证工具必须与生产同源（GUI 复用 main.py 的 main()；禁自定义复刻）+ 探针 T0 结论入档前须自证口径与生产一致。**防膨胀理由**：非新流程，仅把已存在的隐性要求写成一句话；预计年会触发数十次、每次可省 1 小时~1 天方向性误判，属「低成本高杠杆」条款。 |
| 09-16 | **二轮修（密集区点选+片尾回绕）+ 分析缓存落地（R8 首个合规周期）** | owner 报「点选还是不行/空格把红卡拉到开头」+ 令「调研→开工」缓存 | **二轮修**：密集区点选根因=clip 端点 ±12px 命中区在 418~2211 clip 下覆盖全轴（实测 12/12 被吞）→ 修=点击/拖动分离（`_DRAG_START_PX=3`）+垂直约束，复验 10/10；片尾回绕 v1→v3 三次演进（v2 自相矛盾已纠正）→ **EOF 标志语义**。**分析缓存**：调研 440 行→实现 analysis_cache.py→review 修五处（含**缓存投毒**加固）→ 单测 27+探针四相+581+90 绿，**4min→3ms**。**制度**：R8 探针同源（治同类事故复 6 次）。 |
| 09-16 | **新增 R9「测试禁触真实 UI」+ tests/conftest.py 机械夹具（治"测试弹模态窗挂死"事故）** | owner 两次看到桌面**无主弹窗**「Templates missing」并问"是哪个项目"；我初判为他项目（全盘搜索该文案只在我方代码，但漏了运行中的测试）→ 实测定位=**我自己当晚新增的接线测试**：传 `templates_missing=True` 却只 patch `showinfo` → 真实 `showwarning` 弹出并**永久阻塞**（`timeout 8` exit 124；wiring 文件 `timeout 60` 只跑完 2 例）。而 `pytest -q` 因用例顺序巧合仍报"全绿"，掩盖了它 | 根因=制度缺条：测试卫生只管"别依赖真实视频"，未管"**别触碰真实 UI 模态**"；且此类失败的信号是**挂死/超时**而非报错，无头环境尤其难定位。**R9**：测试与探针不得调用真实 `tkinter.messagebox.*` 等阻塞式人工交互。**机械版**=`tests/conftest.py` autouse 夹具 `_no_blocking_dialogs`（全局替换 5 个弹窗函数为记录器；需断言弹窗的用例取 `dialog_recorder`）。**防膨胀理由**：一个 40 行夹具替掉一整类事故（已实测复现+反证），且把"必须 patch 弹窗"从口头纪律变成默认行为——属于"一次投入、永久兜底"。 |

## 九、ACH 矩阵（R5 的工具；症状诊断必用）

模板：行=证据，列=竞争假说，格=一致/不一致/?；选"不一致最少"者。
实例(c4右条,2026-09-04)：见正文分析；H2跨屏DPI重采样以0不一致胜出，
H1帧偏移/H3色彩差各3不一致被淘汰。若当时强制ACH,误判第一步即死。
实例(业务变速FPS 35-45,2026-09-07,**两轮**)：第一轮矩阵漏了真凶——
H1普遍性能回归(S0对照rt=1.004零丢帧,淘汰)、H2 2x段掉帧(S1 rt=2.000
零丢帧,淘汰)、H3解码崩溃致呈现<60(raw呈现~160/s,弱不一致)、
H4=10x风暴+snapshot新鲜度失配(当时"胜出")。owner质疑后第二轮：
H4的失配半环**被引擎级复现证伪**(staleness≤133ms,stale/fresh双变体
同跌)；H5=**EDL拼接边界×倍速风暴**(skip_trimmed默认True,F1复现
31/33窗<50含35-45带,F0业务关仅偶发46.5,F2 cap3稳态全60)以0不一致
胜出。教训：**假说空间必须先枚举运行模式**(source/EDL)再列机制，
第一轮全部探针都跑在 source 模式=测错了模式。

## 十、硬机制工具（R1-R6 的机械版；先跑再动手）

| 工具 | 机制化条款 | 行为 |
|---|---|---|
| `tools/probe_guard.py` | R3 | 探针开头断言单屏+DPI+无损，不达标拒跑 |
| `tools/preflight.py` | R1/R2 | 打印§六能力+上游parity提醒+R1-R6勾单 |
| `tools/inventory_audit.py` | R4 | 比对 asset_index.json 报新增/消失；`--write` 重建基线 |
| `asset_index.json` | R4 | 机读资产索引(Backstage 目录的 json 版)，audit 的登记基线 |
