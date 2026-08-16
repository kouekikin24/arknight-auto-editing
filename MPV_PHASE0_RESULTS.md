# libmpv Phase 0 结果报告

报告日期：2026-08-06（历史基线）  
权威增量更新：2026-08-13  
对应规格：`MPV_PHASE0_SPEC.md`  
总判定：**INCONCLUSIVE**

### 2026-08-13 权威增量

- Gate 状态不变：G0/G1/G2/G3/G4/G6 为 `BLOCKED`，G5 为 `NOT_RUN`。自动化回归通过
  不等于 libmpv Gate 放行，生产 `MpvEngine` 仍未开始。
- 本轮验证器/证据收口后，全仓显式 unittest 为 `290 tests passed`；专用
  `PYTHONPYCACHEPREFIX` 下的源码 `compileall` 通过，`git diff --check` 无错误（仅既存
  LF/CRLF 转换提示）。这些是代码回归结果，不是 libmpv Gate 放行。
- 四个真实样本完整 oracle 都已完成。每份均为 2 个重复 PTS、1 个非单调 PTS且
  `BLOCKED`；样本 1/4 “超时未出报告”的旧说法已作废，本轮没有重跑完整长扫描。
- 当前工作前的冻结点为
  `.cache/mpv_spike/baseline-20260812-current/baseline_manifest.json`，文件 SHA-256 为
  `5a4b6171165ecf2662628c6ccf9f48004e56ec21e0c7079212260ed3c3c00187`。HEAD 为
  `52b7aef40b55429d35f2b131a3d0e8abbdf65e5c`，冻结时 tracked diff SHA-256 为
  `bc64c1755bbea3fbbaeef16ae7c7685cacfe4ae57843d8177cc9db02aff4afa2`（215254 bytes）。
  四视频 SHA-256 经 path/size/mtime_ns 核验后复用，未重新读取完整视频。
- 该冻结点的 completeness 为 `BLOCKED`：缺少 preview golden、export golden 和 clip
  persistence baseline。因此阶段 0 是“基线和护栏已落地但 golden 未闭环”。
- 生产线收口新增 `ProjectState` 输入校验、半开 clip 右边界修复、快照所有权隔离、
  `TaskManager` scope 提交前/最终提交复核、stale submit 拒绝、成功回调异常转 error，以及
  每次分段导出唯一 run 目录。阶段 2/4 核心已落地但仍在收口。阶段 3 已完成
  FFmpeg-only 写出与 FFmpeg 缺失硬阻塞；仍缺统一 `MediaExporter`、真实 PTS/VFR/非零
  起始时间、完整音频策略和 GPU 实际失败后的 CPU retry，是主要生产阻塞。
- 最新验证：本轮导出专项 `26 tests passed`，全仓 `258 tests passed`，`compileall` 与
  `git diff --check` 通过（仅既存 LF/CRLF 转换提示）。这些结果没有覆盖真实 WID 像素、
  内容锚点、干净机分发或四个真实导出 golden，不能提升 Gate 状态。
- 生产导出不再使用 imageio/OpenCV writer；FFmpeg 不可用时整段/分段均在打开媒体前失败。
  逐帧取消、writer 启动失败与提前 EOF 都会清理残片且不覆盖旧成品。`pyproject.toml` 已声明
  实际依赖 `imageio-ffmpeg`，但当前无可用 `uv`，`uv.lock`/`requirements.txt` 尚未正规同步，
  仍是阶段 3 发布阻塞，不能手工伪造哈希。
- G2 审计确认真实冲突帧 3/4/5/7 与约 318 秒后的可观察 A/V 事件不可能同处一个 10 秒窗口；
  冲突窗口与事件窗口现已由双 manifest 绑定 bundle 契约隔离；bundle 创建/临时自验/发布前
  重验及 12 项专项测试均通过。该 bundle 固定为 `structural_precondition_only`、
  `time_authority=none`、`ready_for_source_anchor=false`，仍不能作为 Gate 或 EDL 时间权威。

### 2026-08-13 双窗口证据增量

- 已从现有样本 3 reconciliation 生成并复验隔离的
  `.cache/mpv_spike/pts_normalize/3_pts_conflict_evidence.v1.json`，SHA-256 为
  `a6e5b9f777f8eba8a20890057bea76adb8d83d3521a53f000031b6d132413b81`。它绑定源媒体
  SHA-256 `c4bda334c64a68856caf19fb2f7ec396c9533bf7b18e4cc3316fbabac8e5d976`、完整
  29804 帧和冲突索引 `[3,4,5,7]`，状态固定为 `BLOCKED`，无时间权威、不可供生产消费，
  未进入普通 frame-oracle loader。
- 按既定上限只执行了一次样本 3 `[316,326)` schema v3 有界候选勘测，报告为
  `.cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326.v3.json`，SHA-256 为
  `d3bc2ead7cae489644d5a3dcb934525a425652d1a58f0c20590f479baa16b18b`；联系图 SHA-256
  为 `9c80a5ba1243aea815f69774c7e9aef5a94ba1c7de845fc6820e5fbfe6ba00f4`。报告状态为
  `CANDIDATES_FOUND`，候选 3 个（请求坐标约 316.1、316.2、325.4），但
  `media_pts_authority=none`、`can_register_source_anchor_directly=false`；候选没有被
  自动升级为人工 observation，也没有生成代理。
- 为人工复核从同一源文件提取了两段带声 1 秒诊断片段（不属于 observation）：
  `3_av_event_candidates_316_326_review/start_315.8_316.8.mp4`（SHA-256
  `127c4b9139200a903f502ce1b5221aa976b4804ee675d324f46ecec963402fdc`）和
  `3_av_event_candidates_316_326_review/end_325.0_326.0.mp4`（SHA-256
  `4de8c5d9d092088246cf3f5d692058e5d268c39ebf9b96c3cdcb26f3c05a30b1`）。代表帧确认
  画面变化，但没有独立听觉确认或 10 ms 内容同步证据，未创建 source-anchor 或代理。

## 1. 结论

当前证据已经证明三件事：

1. 本机可以显式加载一个 x64 `libmpv-2.dll`，创建和销毁 mpv 实例。
2. mpv 可以把真实视频渲染到 Tk 的 `TkChild` HWND；本机 SOURCE/EDL 重复加载和 headless seek 没有发生直接崩溃。
3. 现有半开区间、delete/keep complement 和虚拟帧映射的纯整数算法通过了单元测试。

这些结果只构成“继续验证 libmpv 值得做”的证据，**不构成正式接入 `MpvEngine` 的 Go 决策**。当前至少存在两个硬阻塞：

- 本机 DLL 缺少可追溯的来源归档、构建记录和许可证证据，不能作为正式可分发依赖。
- 四个真实样本的完整 oracle 都发现重复/非单调 PTS，且重复 PTS 对应不同画面 checksum；
  mpv 也记录了 `Invalid video timestamp` 警告，`frame / fps` 不能作为权威时间映射。

因此，当前不能宣称 EDL 边界逐帧正确，也不能宣称 WID 生命周期、焦点、DPI、打包或干净机分发已经通过。

## 2. Gate 总表

| Gate | 状态 | 已有本机证据 | 仍缺少的硬性证据 |
|---|---|---|---|
| G0 供应链与加载 | **BLOCKED** | 显式目录加载、DLL 实际路径、x64、SHA-256、mpv/API 版本和一次实例生命周期成功 | 来源归档及哈希、构建配置来源、许可证文件、可再分发结论、项目依赖声明 |
| G1 Windows/Tk WID | **BLOCKED** | `TkChild` 嵌入成功，`gpu-next`/1920x1080/`vo_configured=true`，40 次 SOURCE/EDL load 无直接失败 | 实际像素非黑验证、resize/最大化/最小化、DPI、跨屏、焦点、键盘路由、heartbeat 和 WID 关窗压力 |
| G2 帧与时间映射 | **BLOCKED** | 半开区间与整数映射通过；四份真实样本完整 oracle 均已完成 | 四样本均有不同画面共享 PTS；正规化代理、音频时间线与 A/V 同步尚未通过，当前 CFR 近似不可放行 |
| G3 EDL 正确性与切点压力 | **BLOCKED** | 四个样本的 headless `time-pos` seek smoke 无超时 | 解码画面/源帧 oracle、全部切点、连续播放、EOF、删除帧闪现和真实 WID/vsync 验证 |
| G4 SOURCE/EDL 切换 | **BLOCKED** | 同一 WID 实例完成 20 个 SOURCE/EDL 循环，共 40 次 load | 任意源位置保持、删除区编辑、吸附规则、generation 过期事件、暂停/速度恢复和黑屏时长 |
| G5 逐帧与倍速 | **NOT_RUN** | 只有手工按钮和 headless seek 基础能力 | 前进/后退逐帧图像正确性；2x/10x/20x/80x 实际推进率、资源占用和边界过冲 |
| G6 生命周期与分发 | **BLOCKED** | 50 次 headless create/load/stop/terminate 无异常，HANDLE `189 -> 189`，线程 `1 -> 1` | 独立子进程监督、完整 WID 生命周期、500 次切换、文件句柄验证、onedir 和无 Python/无系统 mpv 干净机 |

总判定规则如下：目前没有硬 Gate 被完整判定为 PASS；也没有足够证据把 EDL 路线判定为最终 FAIL。故总判定保持 **INCONCLUSIVE**，不能写成 PASS 或 GO。

## 3. 输入与基线

基线：`.cache/mpv_spike/baseline-20260806/baseline_manifest.json`

- 创建时间：`2026-08-06T03:14:40+00:00`
- Git branch：`fix/preview-pacing-metrics`
- Git HEAD：`52b7aef40b55429d35f2b131a3d0e8abbdf65e5c`
- 基线 tracked diff SHA-256：`3b07837e0e27b60f94ae07fb8074ac1a7d4004009bec743c5e0b83e5ee8f0f74`
- 四个视频均使用完整 SHA-256 固定。

| 样本 | 源帧 | delete 段 | keep 段 | EDL 虚拟帧 |
|---|---:|---:|---:|---:|
| `1.mp4` | 249097 | 2311 | 2312 | 81375 |
| `2.mp4` | 184293 | 683 | 683 | 16973 |
| `3.mp4` | 29804 | 108 | 108 | 1901 |
| `4.mp4` | 424176 | 2623 | 2623 | 107433 |

视频 SHA-256：

```text
1.mp4  00EE3CA0E0CAFC618572D92279D164911D0A2D6AD223207BF1F335EBB3426A66
2.mp4  789C37BED3A7DE325A6A40F2DAE7299B1ECC6B9EAD1979189A026E517EE51E98
3.mp4  C4BDA334C64A68856CAF19FB2F7EC396C9533BF7B18E4CC3316FBABAC8E5D976
4.mp4  C2762556BDE62E3402B227EEEB1371BA9D961D0441E39948DD739A7DFA2BE117
```

### 基线限制

这份基线保存了实验开始时的工作区，而不是当前验证器的完整冻结快照：

- 基线中的 `scripts/spike_mpv.py` SHA-256 为 `3934ccbbccf6a854a104c40ae286a7c3ba42dac9ac19bd0ac5ab6e3b9caf8f46`。
- 本报告复核时的脚本 SHA-256 为 `1502be54adb80862eabad45bfcccba2fe53116c7d1847343e60530e443fb6207`。
- 基线没有收录后续新增的 `MPV_PHASE0_SPEC.md` 和 `scripts/verify_mpv_frames.py`。
- stress/WID/lifecycle 报告没有引用 baseline manifest 哈希或验证器哈希。

所以现有结果应继续标为 exploratory。正式 Gate 复跑前，需要新增不可变的 run manifest，把 baseline、验证器、命令、阈值和每份结果相互绑定。

### 3.1 2026-08-09：run manifest harness 已落地

`spike_mpv.py` 现在会在 `env`、`stress`、`lifecycle`、`wid` 的实际探针开始前生成
只写一次的 `mpv_phase0_run_manifest`，并在最终报告中反向记录该文件的 SHA-256。
绑定内容包括 baseline、当前样本及视频哈希、生成 EDL、provenance、规范化命令参数、
三个验证器文件和命令专属预登记阈值。

新增自动检查覆盖：

- 显式 run manifest 路径不得覆盖，也不得与结果报告共用一个路径；
- baseline 必须包含完整视频 SHA-256，且当前 meta、分析产物和视频必须仍与 baseline 一致；
- 阈值 manifest 的 kind、command、必填键、数值类型和有限性必须匹配；
- 结果逐项应用预登记阈值，超限标为 `fail`，缺指标标为 `blocked`；
- 缺少 baseline 或阈值时仍允许采集诊断，但报告强制带 `RUN_MANIFEST_INCOMPLETE`。

本机无 DLL/无 baseline/无阈值的真实 `env` CLI smoke 已验证上述降级语义：环境和 run binding
都保持 `blocked`，同时正常生成相互绑定的 run manifest 与结果报告。

这项改动只修复“证据无法相互追溯”的 harness 缺陷，**不会追认本节之前的历史报告**。
历史报告仍是 exploratory；G0～G6 的状态也没有因此变为 PASS。

## 4. G0：本机运行通过，供应链阻塞

证据：`.cache/mpv_spike/nipaplay_environment.json`

本机运行子检查结果：

```text
python-mpv       1.0.8
requested dir    D:\NipaPlay
loaded DLL       D:\NipaPlay\libmpv-2.dll
DLL SHA-256      9826B77FB42559752CD37A19191169A986AA4E929EB37705C3345D25A5F6D034
PE architecture  x86_64
mpv              v0.41.0-524-g5921fe50b
client API       2.5
runtime_status   pass
```

这证明本机探索用 DLL 可以被显式加载，不需要系统 PATH 中已有 mpv。DLL 内部配置指向 `mpv-winbuild-cmake`，但当前没有原始下载 URL、发布归档 SHA-256、可复现构建记录和许可证证据。环境报告因此正确保持：

```text
supply_chain_status = incomplete
status              = blocked
```

在取得可追溯构建之前，`D:\NipaPlay\libmpv-2.dll` 只能用于本机实验，不能复制进项目或发布包。

### G0 harness 收紧（2026-08-07）

环境探针现在要求 provenance manifest 绑定并实际验哈希五个本地文件：来源归档、DLL、构建记录、许可证、再分发说明。只有实际加载 DLL 的绝对路径和 SHA-256 都与 manifest 精确一致，且五份本地文件全部验哈希通过，`supply_chain_status` 才能为 `pass`。`source_url`、构建配置字符串或许可证备注不再具备通过 Gate 的能力。

`env`、`stress`、`lifecycle`、`wid` 都接受并向环境报告传递同一 `--provenance-manifest`。manifest 缺失、无效、文件缺失或哈希不匹配时，探针仍可运行以收集本机诊断，但报告固定为 `blocked`。当前尚未取得这五份真实证据，因此本节历史结果和 G0 总状态均不改变，仍为 **BLOCKED**。

## 5. G1/G4：WID 与切换的局部结果

证据：

- `.cache/mpv_spike/exploratory/3_wid_smoke.json`
- `.cache/mpv_spike/exploratory/3_wid_switch20.json`

单次 WID smoke：

```text
HWND class       TkChild
vo_configured    true
current_vo       gpu-next
video            1920x1080
EDL load         31.61 ms
mpv errors       0
queued errors    0
```

20 个 SOURCE/EDL 循环，共 40 次 load：

| 指标 | 结果 |
|---|---:|
| load failures | 0 |
| p50 | 14.12 ms |
| p95 | 72.29 ms |
| max | 189.11 ms |

这些数字说明同一实例反复 load 没有立即崩溃，但测试始终停在文件开头。它没有验证任意源帧位置保持、删除区内暂停编辑、恢复时吸附、旧事件隔离或黑屏时长。

此外，当前 WID harness 在 Tk 主线程同步等待 `file-loaded`，成功状态只检查最后一条 load。它不能替代正式的异步状态机验收。`keyboard_events=0` 也表示键盘路由尚未被实际测试。因此 G1 和 G4 都保持 **BLOCKED**。

## 6. G2：PTS 阻塞

四份 EDL manifest 均确认了以下纯数据性质：

- 删除区间先被规范化为半开区间 `[start, end)`。
- delete 与 keep complement 覆盖完整整数帧域。
- 保留帧的 `source -> virtual -> source` 算术往返通过抽样检查。
- 中文、逗号和 `%` 路径的 EDL 字节长度转义有单元测试。

但 EDL 当前仍使用：

```text
start_seconds = start_frame / fps
length_seconds = kept_frames / fps
```

这只是 CFR 近似，不是权威映射。

独立 PTS oracle 证据：`.cache/mpv_spike/frame_oracle/3_20260809.json`

oracle 使用 FFmpeg 7.1、单解码线程、`-copyts`、`showinfo` 和 `-fps_mode passthrough`，完整解析 `3.mp4` 的 29804 个解码帧。FFmpeg 进程返回 0，但时间线检查得到：

```text
time_base              1/15360
parsed_frames          29804
missing PTS            0
duplicate PTS          2
non-monotonic PTS      1
negative PTS step      -512 (1 occurrence)
authoritative timeline false
status                 BLOCKED
reason_codes           PTS_DUPLICATE, PTS_NON_MONOTONIC
```

2026-08-09 的四份完整报告均已落盘：

| 样本 | 权威报告 | 线程 | 完整帧数 / `pts_table` | 重复 PTS | 非单调 PTS | FFmpeg | 状态 |
|---|---|---:|---:|---:|---:|---:|---|
| `1.mp4` | `1_20260809_threads4.json` | 4 | 249097 | 2 | 1 | 0 | **BLOCKED** |
| `2.mp4` | `2_20260809.json` | 1 | 184293 | 2 | 1 | 0 | **BLOCKED** |
| `3.mp4` | `3_20260809.json` | 1 | 29804 | 2 | 1 | 0 | **BLOCKED** |
| `4.mp4` | `4_20260809_threads4.json` | 4 | 424176 | 2 | 1 | 0 | **BLOCKED** |

四份报告都位于 `.cache/mpv_spike/frame_oracle/`，`pts_table` 行数与完整源帧数相等。
此前“样本 1/4 超时且未生成报告”的记录已经过时。样本 3 用 1/4 线程得到的完整表逐项一致；
显式 `--threads` 只调节采集性能，不放宽权威性规则。

四份 `1/2/3/4_20260809_prefix20.json` 进一步给出画面证据：

```text
duplicate_distinct_checksum_count = 2
duplicate_same_checksum_count     = 0
pts_conflict_assessment.status    = conflict
automatic_sort_or_deduplicate_allowed = false
```

也就是说，两组重复 PTS 分别对应不同的解码画面，不是同一帧的无害重复。冲突存在于源媒体
packet PTS；普通 B 帧重排不能解释不同画面共享同一显示 PTS。排序后去重会丢真实画面，
因此被明确禁止；`frame / fps` 也不能替代正式媒体时间。

同时，四个 mpv stress 报告都记录了 `Invalid video timestamp`：

| 样本 | 警告次数 |
|---|---:|
| `1.mp4` | 26 |
| `2.mp4` | 28 |
| `3.mp4` | 5 |
| `4.mp4` | 21 |

这并未证明 mpv/EDL 路线最终不可用，但已经足以阻止“这些素材是普通 CFR，因此可权威使用 `frame / fps`”这一结论。
当前独立 source-frame/PTS oracle 已建立；在 EDL writer 真正消费获准的 PTS 表前，G2 仍保持阻塞。

探针现在不会再把 PATH 中存在 `ffprobe` 当成 PTS 已验证；只有真正的逐帧 oracle 和已被 EDL writer 消费的 PTS 表才有资格解除该阻塞。当前 EDL writer 仍明确使用 CFR 近似，因此即使未来发现 `ffprobe`，该 Gate 也不会自动变成 PASS。

### 6.1 2026-08-09：合成 CFR/VFR oracle 已完成

新增 `scripts/generate_pts_oracle_fixtures.py`，生成两个短片和对应 truth manifest：

```text
.cache/mpv_spike/pts_fixtures/cfr.mp4
.cache/mpv_spike/pts_fixtures/cfr.truth.json
.cache/mpv_spike/pts_fixtures/vfr.mp4
.cache/mpv_spike/pts_fixtures/vfr.truth.json
```

夹具使用无损 H.264/MP4、`1/1000` 时间基和唯一灰度二进制帧 ID。最后追加的终止哨兵
只用于让 MP4 给最后一个真实帧提供结束时刻，truth manifest 明确记录它，且允许容器
在 EOF 时省略它；业务帧本身没有复制、删除或依赖 OCR。

`scripts/verify_mpv_frames.py` 现在支持：

- 保存完整根级 `pts_table`，逐项保留 `n/pts/pts_time/duration/duration_time`；
- `--truth-manifest` 模式下用同一次 FFmpeg 解码输出灰度帧，验证 marker、互补 bit、
  唯一帧 ID、解码顺序、PTS、duration 和视频 SHA-256；
- manifest 哈希或视频哈希不匹配时在解码前返回 `TRUTH_MANIFEST_INVALID`；
- 任意缺帧、重复 ID、顺序错误或 PTS/duration mismatch 都只能得到 `BLOCKED`。

实际 CFR/VFR 夹具均得到：

```text
status=PASS
authoritative_frame_timeline=true
frame_id_alignment.reason_codes=[]
```

这只证明 oracle 能识别一个已知真值时间线，不改变四个真实样本的 Gate 状态；样本 3
仍有重复/非单调 PTS，EDL writer 仍未消费该表，因此 G2 继续保持 **BLOCKED**。

### 6.2 2026-08-09：正规化代理探索仍未放行

证据目录：`.cache/mpv_spike/pts_normalize/`。样本 3 的 40 帧前缀已做四条探索：

1. 仅重封装或用 setts 改 packet PTS/DTS：仍有重复/非单调 PTS。
2. 解码后按画面顺序重建 PTS，并无损 H.264 重编码：PTS 单调且已输出的 39 帧 checksum
   逐项一致，但 40 个真实画面只输出 39 帧，丢失末帧。
3. 额外编码一帧 guard：输出 41 帧，前 40 帧 checksum 与源逐项一致，但末 guard 的
   duration 为 0，通用 oracle 正确保持 `BLOCKED`。
4. 强制 CFR：输出 40 帧且 PTS 表面通过，但 39/40 checksum 不一致并大量重复首帧，路线无效。

代理已建立显式 guard 业务域：全部真实画面保留且逐帧 checksum 对齐，guard 不进入业务帧域，
所有真实帧 duration 为正；manifest 绑定源 SHA-256、完整生成命令、FFmpeg/编码器与验证器版本。
样本 3 的 40 帧前缀和完整代理均已按顺序验证。音频时间线与 A/V 同步已单独运行，
但当前证据仍为 `BLOCKED`，不能据视频结果宣布代理可用。

### 6.3 2026-08-09：样本 3 正规化代理与音频/A-V 验收

当前脚本 `scripts/pts_normalized_proxy.py` SHA-256 为
`0e96ff1b6729103a349cb725325fdeb6f148bd36e0f33eb41071be836c2f7d94`。

40 帧前缀最终证据：

- manifest：`.cache/mpv_spike/pts_normalize/3_proxy40_guard_v8.manifest.json`
- verify：`.cache/mpv_spike/pts_normalize/3_proxy40_guard_v8.verify.json`
- `scope=prefix`、`video_validation.status=PASS`、业务帧 40/40 checksum 对齐、业务帧
  duration 全部为正、guard 位于索引 40 且不在 `[0,40)`；总状态因 `SOURCE_PREFIX_ONLY`、
  音频和 A/V 尚未完成而保持 `BLOCKED`。

完整样本 3 最终证据：

- manifest：`.cache/mpv_spike/pts_normalize/3_proxy_full_v2.manifest.json`
- video verify：`.cache/mpv_spike/pts_normalize/3_proxy_full_v2.verify.json`
- `scope=full`、业务帧 `29804/29804` checksum 对齐、业务帧 duration 全部为正、guard
  位于索引 29804 且不在业务域；`video_validation.status=PASS`。
- 源末端 `7630592` ticks，归一化末端 `7629824` ticks，差 `-768` ticks（`-0.05 s`）。
  该差值绑定在 manifest，不能用 `frame / fps` 掩盖。

音频/A-V 最终证据：

- run manifest：`.cache/mpv_spike/pts_normalize/3_proxy_full_v2.audio_av_v5.run.json`
- verify：`.cache/mpv_spike/pts_normalize/3_proxy_full_v2.audio_av_v5.verify.json`
- 源为 AAC、48 kHz、双声道，完整解码 `23298` 个音频帧，首 PTS 为 `480` samples；
  音频时间线连续性通过。`full_v2` 代理没有音频流，报告 `PROXY_AUDIO_MISSING`。
- A/V 归一化相对源 PTS 身份偏移范围为 `-50 ms .. +33.333 ms`，跨度约 `83.333 ms`。
  这是旧 schema 报告中的身份诊断，不是已测得的内容失步；不能据此拉伸音频或放宽
  `10 ms` 内容阈值。当前可复核阻塞项是代理缺少音频及没有四区内容锚点，总状态仍为
  `BLOCKED`，`proxy_ready_for_gate=false`。本次历史验证脚本 SHA-256 为
  `8bc6b8fb2705a8a0df409ce77b86bc82cc353d9447902fcc581f7fe2a246136a`。

这条证据只证明视频 guard 约束已经满足，并明确证明当前视频-only 代理不能作为可用代理；
它不放行 G2，也不允许接入生产 `MpvEngine`。

### 6.4 2026-08-10：验证器证据闸门收紧与有界音频复用

在继续任何音频代理实验前，先修正了 `scripts/verify_proxy_audio_av.py` 的证据边界：

- A/V 阈值必须是有限正数；`NaN`/`Inf` 只能得到 `BLOCKED`。
- 音频验证器重新加载并校验源 oracle、代理 oracle、完整业务 checksum/duration/PTS、terminal guard、full scope 和源/代理文件 SHA-256，不能只信任外部的 `video_validation.status`。
- oracle 现在记录源媒体 SHA-256 和观测到的唯一像素格式；源/代理像素格式不一致（包括 10-bit/4:4:4 被静默转成 `yuv420p`）会阻塞。
- `allow-no-audio` 只影响无音频产品策略，不能跳过视频正规化 offset 检查。

当时完整 188 个 unittest 全部通过。旧的 2026-08-09 代理报告缺少新的 oracle SHA/像素格式绑定，重新诊断时明确得到
`VIDEO_EVIDENCE_INVALID`、`PROXY_AUDIO_MISSING` 和 `BLOCKED`；这不是长扫描失败，也不能改写旧报告。

另新增 `scripts/generate_av_sync_fixtures.py` 和 `scripts/verify_av_sync_fixtures.py`：合成视频帧和 48 kHz 无损 PCM 脉冲分别按视频 PTS 与音频采样索引验证，已知真值锚点误差为 0；人为平移 20 ms 会 `BLOCKED`。这只验证采样时钟验证器，不代表真实样本代理可用。

随后只对样本 3 的 40 帧前缀做了一次 AAC packet-copy mux，未重跑任何完整长样本：

- manifest：`.cache/mpv_spike/pts_normalize/3_proxy40_hardened_20260810_audio_copy_v3.manifest.json`
- proxy：`.cache/mpv_spike/pts_normalize/3_proxy40_hardened_20260810_audio_copy_bound_v3.mp4`
- verify：`.cache/mpv_spike/pts_normalize/3_proxy40_hardened_20260810_audio_copy_v3.verify.json`
- mux 截止点为业务末端之后一个 `1/15360` tick，保留 41 个视频帧（含域外 guard），同时只复制 31 个 AAC 帧；视频业务帧、guard 和像素格式验证均为 `PASS`。
- 音频前缀为 48 kHz 双声道 AAC，`31` 帧、`31744` samples，源/代理逐帧 PTS、采样数、格式和 checksum 均为 `PASS`；这只是“能保留 AAC 前缀”的证据，不是完整音频时间线通过。
- 总状态仍为 `BLOCKED`：缺少真实内容锚点且 scope 为 `prefix`。`-16.667 ms .. +33.333 ms`
  的 `50 ms` 身份偏移和约 `4.667 ms` 的边界末端误差均为诊断信息，不参与内容 Gate，
  也不构成内容锚点证据。

样本 3 前缀的源 PTS 在 `n=4/5` 为 `1280 -> 768` ticks，所以严格递增的正规化时钟必然
不再保持错误的源 PTS 身份。这个数学事实只解释身份诊断，不能否决或证明内容同步。
当前“严格正规化视频 + 原 AAC packet-copy”路线仍不可放行，原因是没有真实内容锚点且
只有 prefix scope；不得把音频强行对齐错误源 PTS，也不得事后放宽内容阈值。

### 6.5 2026-08-10：content-anchor 捕获闭环与 `v4` 有界复核

验证器和捕获工具进一步收紧：

- content-anchor observation 只能引用结构化 window manifest；每个窗口绑定媒体、oracle、
  音频帧、PNG/WAV、capture 工具、FFmpeg 和实际命令。
- 验证时会把同一帧和音频窗口独立重提取到新临时路径，并与已绑定 PNG/WAV 做 SHA-256
  比对；伪造产物后同步更新外层哈希仍会 `BLOCKED`。
- 捕获和 AAC 代理均使用 UUID 临时文件与不可覆盖硬链接发布；并发抢占目标时保留竞争者
  文件，只清理本进程拥有的临时文件。
- prefix 的 `av_sync.status` 现在聚合视频、音频、内容锚点和阈值四个子 Gate，不再只看
  content-anchor 状态；创建 manifest 时语义无效的 observation 会在占用输出路径前拒绝。

没有重跑任何完整长样本。只基于样本 3 的既有 40 帧正规化代理生成并复核：

- `.cache/mpv_spike/pts_normalize/3_proxy40_hardened_20260810_audio_copy_v4.manifest.json`
- `.cache/mpv_spike/pts_normalize/3_proxy40_hardened_20260810_audio_copy_v4.oracle.json`
- `.cache/mpv_spike/pts_normalize/3_proxy40_hardened_20260810_audio_copy_v4.verify.json`
- `.cache/mpv_spike/pts_normalize/anchors_v4/`

`v4` 的 41 帧 oracle 为权威 `PASS`；报告中视频 `PASS`、AAC 前缀 `PASS`、阈值配置
`PASS`，但 content anchors 为 `BLOCKED`，总原因只有
`AV_CONTENT_ANCHORS_NOT_PROVIDED` 和 `SOURCE_PREFIX_ONLY`。捕获的帧 `0/5/20/39` 均位于
同一暂停界面，音频虽非全静音，却没有可独立指认的画面事件与声音事件。因此八份真实
PNG/WAV 窗口只保留为诊断素材，没有伪造 observation/manifest，G2 继续 `BLOCKED`。

### 6.6 2026-08-11：source-first 登记、非零窗口与 10 秒事件勘测

本批把真实 A/V 事件改为必须先于代理生成登记：

- `scripts/verify_proxy_audio_av.py` 已加入 source-only anchor manifest 的 API/CLI。登记文件只写一次，
  绑定源媒体 SHA-256、源 oracle、结构化 source observation、源音频解码证据、源帧/音频窗口序列、
  capture/创建/验证脚本与 FFmpeg；proxy 字段、公式推导事件、伪造 observation、篡改或事后登记
  均应 fail closed。
- `scripts/pts_normalized_proxy.py build` 本批已把 `--source-anchor-manifest` 设为强制参数，并在
  生成前、生成结束且代理发布前、证据发布前复核同一 source-anchor；代理 manifest、generation、
  source-decode evidence 三处绑定该记录，`verify` 再检查 SHA-256、源媒体/oracle、scope、帧域、
  显式映射和登记时间。
- 代理层已支持 `source_start_frame=K`，形成 `source [K,K+N) -> proxy [0,N)` 的严格映射；生成命令
  使用 `trim=start_frame=K:end_frame=K+N`，source/decode、generation、frame mapping 与 proxy `N`
  的域外 terminal guard 均携带该窗口。source-anchor 登记、paired observation loader 与最终
  content evaluator 现在都重推导 `proxy_index = source_index - source_start_frame`；合成
  `source [17,57) -> proxy [0,40)` 已通过，错误映射即使 checksum 重复也会 `BLOCKED`。锚点
  schema 已升级到 v2，旧 identity-only 证据不会被静默解释为新映射。

没有生成新长媒体。只运行了样本 3 起始 10 秒的有界事件候选勘测：

- JSON：`.cache/mpv_spike/av_event_scout/3_start10s_v3.json`
- contact sheet：`.cache/mpv_spike/av_event_scout/3_start10s_v3.png`
- 视频 `100` 个样本 @ `10 fps`，音频 `500` 个窗口 @ `20 ms`；最大画面变化分数
  `0.33685185185185185`，最大音频峰值 `220`，候选事件数 `0`。
- 报告状态为 `NO_USABLE_AV_EVENT`。联系图仍是暂停界面，无法独立指认同时发生的画面事件与
  声音事件，因此禁止把该窗口登记为 source-anchor PASS。

该勘测批次当时的全仓回归为 `219 tests passed`；2026-08-12 最新回归见顶部权威增量。
当时的 `compileall`、关键 `py_compile` 与 `git diff --check` 均通过；
这次勘测和自动化回归都不是 G2 PASS。当前不得生成新的完整样本 3 代理，也不得进入真实 WID、
G3-G6 或生产 `MpvEngine`。下一步只在有界范围内寻找可观察事件，先登记 source-only anchor，再
生成对应带音频代理并验证 EDL 边界。G2 继续 **BLOCKED**。

### 6.7 2026-08-11：scanner v2 的 `[174,184)` 非零源窗口

在起始 10 秒没有事件后，只对样本 3 的源时间窗口 `[174.0,184.0)` 再执行了一次 scanner
schema v2；没有运行完整扫描，也没有生成任何代理：

- JSON：`.cache/mpv_spike/av_event_scout/3_174s_10s_v1.json`，SHA-256
  `8801085e087fc8f2a32db8abed135faa2fcf8e588f2acd54519141195aa34413`。
- contact sheet：`.cache/mpv_spike/av_event_scout/3_174s_10s_v1.png`，SHA-256
  `13296976af953aad3b8ec4ae7746938a31e33c45a91de9f3cd0818302ec62c19`。
- 报告为 `schema_version=2`、`scope=source_window`，绑定 `start_seconds=174.0`、
  `duration_seconds=10.0`、`end_seconds_exclusive=184.0`。源文件 SHA-256 为
  `c4bda334c64a68856caf19fb2f7ec396c9533bf7b18e4cc3316fbabac8e5d976`。
- FFmpeg SHA-256 为 `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`；
  scanner SHA-256 为 `808fd935e375a152113a4b9777b3e0ee310310282ec5aa2a05dc0ac0d9f5b2ee`。
- 视频采样 `100` 帧 @ `10 fps`，音频采样 `500` 个窗口 @ `20 ms`，最大音频峰值 `218`。
  `candidates=[]`，最终状态为 `NO_USABLE_AV_EVENT`。

联系图始终带暂停界面；虽然菜单内容有变化，但没有可独立指认的音频瞬变与同步画面事件。
因此没有创建 observation/source-anchor，没有生成该窗口代理，也不能据此宣布 G2 通过。
G2 继续 **BLOCKED**，生产 `MpvEngine` 仍不得开始。

## 7. G3：headless seek 结果及边界

证据：`.cache/mpv_spike/exploratory/{1,2,3,4}_stress.json`

| 样本 | seek 点 | 失败 | p50 | p95 | max |
|---|---:|---:|---:|---:|---:|
| `1.mp4` | 100 | 0 | 87.03 ms | 173.15 ms | 242.35 ms |
| `2.mp4` | 100 | 0 | 98.11 ms | 151.92 ms | 198.30 ms |
| `3.mp4` | 10 | 0 | 84.99 ms | 157.62 ms | 157.62 ms |
| `4.mp4` | 100 | 0 | 78.04 ms | 130.80 ms | 169.37 ms |

这些结果来自 `vo=null`，只检查虚拟 `time-pos` 是否在约两帧/40ms 容差内到达目标。它没有读取实际解码帧，也没有证明删除帧未闪现。stress harness 目前还不会因为 `Invalid video timestamp` 日志或 duration error 自动失败。

因此可写的结论只有：**EDL 可以被本机 libmpv 加载，并能在抽样虚拟时间点完成 exact seek**。不能写成“切点逐帧正确”或“真实 GUI 播放流畅”，G3 保持 **BLOCKED**。

## 8. G5：未运行

以下项目没有结果文件：

- SOURCE 模式逐帧前进/后退各 100 次的图像 oracle。
- 长 GOP 冷/热后退延迟。
- 2x、10x、20x、80x 的实际推进率和资源占用。
- 高倍速跨 EDL 边界时的过冲、停顿和删除帧显示检查。

G5 状态为 **NOT_RUN**，不能从 headless seek 延迟推导。

## 9. G6：headless 生命周期局部通过，分发阻塞

证据：`.cache/mpv_spike/exploratory/3_lifecycle_50.json`

```text
iterations       50
failures         0
HANDLE count     189 -> 189
Python threads   1 -> 1
local status     pass
```

这证明 50 次 headless create/load EDL/stop/terminate 没有观察到 Python 线程或 HANDLE 增长。它没有创建 Tk/HWND，没有执行 SOURCE/EDL 任意位置切换，也没有用独立父进程监督原生崩溃；当前 harness 还只是记录资源计数，并不根据增长斜率判失败。

以下项目均未运行：

- 完整 WID 生命周期 50 次。
- 单实例 500 次 SOURCE/EDL 切换。
- 关闭后独占打开源视频的句柄验证。
- frozen `onedir` 自检。
- 无 Python、无系统 mpv、离线的干净 Windows 机器。
- 中文、空格、逗号、分号和 `%` 路径的打包产物验证。

所以 G6 保持 **BLOCKED**。

## 10. 自动化验证

当前执行结果：

```text
python -m unittest discover -s tests -p "test_*.py"
258 tests passed

python -m compileall -q .
passed

python -m py_compile analyzer.py main.py preview_player.py settings_panel.py task_manager.py `
  timeline_plan.py timeline_widget.py video_io.py scripts/spike_mpv.py `
  scripts/verify_mpv_frames.py scripts/generate_pts_oracle_fixtures.py `
  scripts/pts_normalized_proxy.py scripts/verify_proxy_audio_av.py `
  scripts/generate_av_sync_fixtures.py scripts/verify_av_sync_fixtures.py scripts/prefix_audio_copy.py `
  scripts/capture_av_content_anchors.py `
  tests/test_pts_normalized_proxy.py tests/test_verify_proxy_audio_av.py `
  tests/test_av_sync_fixtures.py tests/test_prefix_audio_copy.py `
  tests/test_content_anchor_manifest.py tests/test_capture_av_content_anchors.py `
  tests/test_nonzero_content_anchor_mapping.py
passed

git diff --check
passed (only existing LF/CRLF conversion warnings)
```

258 个自动化测试全部通过；其中 G2 夹具、冲突诊断、正规化代理、音频/A-V，以及生产线
时间轴/任务生命周期/导出护栏测试覆盖：

- CFR/VFR 唯一帧 ID 编码、互补 bit 损坏检测和 truth manifest 哈希阻断；
- 两份短片的真实 FFmpeg 解码、完整 `pts_table`、ID 顺序、PTS 和 duration 对齐。
- 正规化代理的 guard 业务域、全业务帧 checksum/duration、prefix/full 权威性和 manifest
  命令/哈希绑定。
- `ashowinfo` 音频帧的 priming 起点、连续性 gap/overlap、格式变化、完整 PCM checksum
  比对、代理缺音频和 A/V 偏移阈值。
- AAC packet-copy 前缀的逐帧字段/checksum、strict-prefix scope、源/视频/工具 manifest 绑定、
  terminal guard 保留和正规化偏移下界。
- 真实内容锚点契约的四区要求（`start`、`pts_conflict`、`middle`、`end`）、帧 checksum 绑定、
  音频 sample 时钟误差、缺失锚点和篡改证据的 fail-closed 判定。
- 真实短合成素材上的 FFmpeg PNG/WAV 捕获与跨输出路径独立重提取、伪造内容阻断，以及
  捕获/代理原子发布竞态不覆盖或删除竞争者目标。
- source-first 登记的生成前/生成中/事后证据绑定，以及非零窗口 `source [K,K+N) ->
  proxy [0,N)` 在登记、paired observation 和最终 evaluator 三层的独立重推导；重复 checksum
  不能掩盖错误索引。
- scanner schema v2 的 `source_window` 起止边界、源/工具 SHA-256 绑定，以及无可用事件时
  `NO_USABLE_AV_EVENT` 的 fail-closed 结果。

原有测试继续覆盖：

- 半开区间 normalize/complement。
- source/virtual 整数映射。
- EDL UTF-8 路径转义和文本生成。
- 导出音频段数预检与未确认拒绝。
- 独立 showinfo PTS parser 对干净、缺失、重复、非单调和 FFmpeg 错误的状态判定。
- `TimelinePlan` 的严格半开区间、补集、映射、指纹和 legacy analyzer 兼容性。
- 音轨不存在、FFmpeg 硬依赖不可用和音轨探测失败的导出预检。
- FFmpeg-only 写出、缺依赖早期阻塞、逐帧取消/启动失败/提前 EOF 的残片清理与旧成品保护。

它们没有覆盖真实 WID 像素、人工交互、干净机打包或真实导出成片；而且样本 3 的实际
音频/A-V 证据仍为 `BLOCKED`，因此测试全过不改变 Gate 的 `BLOCKED`/`NOT_RUN` 状态。

## 11. 导出音频护栏

本阶段加入了导出音频的三态预检：源片确实无音轨时报告 `no_stream`；保留段过多或音轨探测失败时默认拒绝，并在 UI 中要求用户明确确认无声导出。FFmpeg 是所有生产导出的硬依赖，缺失时不可通过“允许无声”绕过。快速路径和逐帧路径都返回结构化 `audio_mode`，不会再把探测失败报告成“无音轨”。该护栏已用带 AAC 和无音轨的短片做 smoke 验证。

这仍不解决真实 PTS、VFR、音画同步、音频时间线和四个完整成片的人工验收，因此不应把它写成“导出系统已完全修复”。

## 12. Phase 1：TimelinePlan 已落地

新增纯 Python 模块 `timeline_plan.py`，并接入 `analyzer.py` 与 `preview_player.py`：

- 所有 delete/keep 区间统一为严格半开 `[start, end)`；越界和反向区间直接拒绝，不再隐式改变编辑结果。
- `TimelinePlan` 保存 canonical delete/keep 区间、虚拟帧前缀和稳定 fingerprint。
- `source_to_virtual` 对删除帧返回 `None`，不会偷偷吸附；需要吸附时必须显式调用 `snap_source`。
- 预览跳裁剪和整段导出都从同一类 plan 取得区间；旧的 `build_delete_set` 保留为兼容包装。
- 导出结果携带 timeline fingerprint，可将结果与编辑快照对应起来。

这一阶段没有把未经验证的 PTS 强行塞进 `TimelinePlan`；整数源帧坐标和真实媒体时间仍是两个待验证层。

## 13. 下一步

按阻塞关系，下一步应依次执行：

1. 代理 guard、逐帧 checksum/duration、验证器自检和 AAC 前缀复用均已完成；最新 `v4`
   仍因 `AV_CONTENT_ANCHORS_NOT_PROVIDED`、`SOURCE_PREFIX_ONLY` 为 `BLOCKED`，不得接入生产。
2. 双窗口契约、隔离 conflict evidence 与发布竞态测试已完成；`[316,326)` schema v3 有界勘测
   也已按上限只运行一次并得到 3 个候选。下一步必须独立人工核对候选处是否同时存在可指认的
   画面事件和音频事件；scanner 的 sample-grid 坐标不得直接写成 source observation 或媒体 PTS。
3. 人工观察若不成立，立即停止该窗口路线并保持 G2 `BLOCKED`；不得扩大盲扫、重复运行同一
   窗口、伪造 observation 或调整 10 ms 内容阈值。若观察成立，先只写一次登记 source-only
   observation/anchor，再创建双窗口 bundle；其结构 PASS 仍不等于 G2 PASS。
4. source-anchor/content-anchor consumer 的显式非零映射已完成合成验证；取得真实预登记事件后，
   有界窗口必须同时通过视频、音频、内容锚点，并让 EDL writer 消费
   `源帧索引 -> 代理帧索引 -> 代理 PTS/tick` 时间表；之后才考虑完整样本 3，不重跑样本 1/4
   已完成的完整 oracle 扫描。
5. PTS/代理路线明确后，完成真实 WID 边界播放、图像验证和任意源位置的 SOURCE/EDL 切换。
6. 补逐帧、倍速、EOF、黑屏、焦点、resize/DPI 和完整 WID 生命周期。
7. 取得可追溯、许可证明确的 libmpv 构建，随后做 `onedir` 和干净机验收；G0-G6 全部通过前继续保留 `VideoIOThread`。

不可变 run manifest 已完成；后续所有正式 Gate 复跑都必须携带完整 baseline 与命令专属阈值，
旧的未绑定报告不得混入新的判定。

`TimelinePlan` 和共享 `TaskManager` 已落地；正式 `MpvEngine` 及其生产接口仍必须等待上述
G0-G6 硬 Gate 完成，当前继续保留 `VideoIOThread`。
# Authority override and G2 review result - 2026-08-14

- Independent Codex review replaces human observation for the existing immutable sample-3 evidence.
- No scanner, full oracle, or full sample-3 proxy was rerun.
- The 316.1/316.2 candidates differ by about 180 ms; 325.4 differs by about 380 ms. Both exceed the fixed 10 ms threshold.
- Result: `NO_USABLE_AV_EVENT`; no source observation/anchor, bundle, or proxy was created. The candidate route is closed and G2 remains `BLOCKED`.
- Phase 0 remains `INCONCLUSIVE`: G0/G1/G2/G3/G4/G6 are `BLOCKED`, G5 is `NOT_RUN`, and production `MpvEngine` remains prohibited.

## Production-line continuation - 2026-08-14

The approval line and production line are intentionally synchronized but independently gated. The production export entry points now use immutable `ExportRequest` snapshots and `MediaExporter` for both full and ranged output. Ranged staging, validation, cancellation, and atomic publication are centralized in that service. This work does not claim any MPV Gate passage: 314 unittest cases pass, while G2 remains `BLOCKED` and `VideoIOThread` remains the active playback path.

The controlled-edit resilience rule is active: `503 Service Unavailable` is recorded as `EDIT_CHANNEL_DEGRADED`, preserving the exact patch transaction and its pre-edit hashes for resumption. It is not treated as a code failure, a test result, or a reason to switch approval/production lines.

An explicit real-tool probe smoke also succeeded on the existing `av_sync.mp4` fixture using the paired 6.1.1 FFmpeg candidate under `D:\弹弹play\ffmpeg`. MediaInfo parsed both streams, raw time bases, start/duration ticks, and audio format, and verified both executable hashes. The result remains deliberately not `complete_for_export` because no authoritative frame-PTS certification was supplied. Tool provenance and license evidence are still required for G0.
