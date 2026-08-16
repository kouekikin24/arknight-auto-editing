# libmpv Phase 0 验证规格

状态：执行中  
范围：只新增实验脚本和缓存产物，不修改正式播放器  
决策：全部硬性 Gate 通过后，才允许设计或接入正式 `MpvEngine`

## 1. 本阶段回答的问题

1. 指定的 libmpv DLL 能否在无系统 mpv 的 Windows 环境中可靠加载。
2. mpv 原生子窗口能否稳定嵌入当前 Tk 界面。
3. 2311/2623 段 EDL 能否正确播放、定位并跨越切点。
4. EDL 播放与完整源片编辑能否保持同一源帧语义。
5. 反复加载、切换和关闭是否存在崩溃、死锁或资源增长。

本阶段不修改：

- `preview_player.py`
- `video_io.py`
- `analyzer.py`
- 导出行为
- 默认预览引擎

## 2. 两种播放模式

```text
播放剪辑结果
  -> EDL_PLAYING
  -> 删除区在虚拟时间线中不存在

暂停、拖轴、逐帧
  -> SOURCE_PAUSED
  -> 加载完整源片，可以查看和恢复删除帧
```

从删除区恢复 EDL 播放时，才允许吸附到下一段保留区。暂停编辑时不得把源片位置强制吸附到保留区。

## 3. 固定输入

权威样本：

| 样本 | 源帧数 | skip 段数 | 主要用途 |
|---|---:|---:|---|
| `1.mp4` | 249097 | 2311 | 大量切点 |
| `2.mp4` | 184293 | 683 | 日常代表样本 |
| `3.mp4` | 29804 | 108 | 快速回归 |
| `4.mp4` | 424176 | 2623 | 最大压力样本 |

实际视频、段表、代码和环境必须由每次运行的 `baseline_manifest.json` 固定，不能只依赖文件名。

### 3.1 不可变 run manifest

`env`、`stress`、`lifecycle`、`wid` 在实际 libmpv/Tk 探针开始前必须生成一份
`mpv_phase0_run_manifest`。harness 默认在结果旁生成唯一文件；显式传入
`--run-manifest` 时使用只写一次语义，目标已存在即拒绝运行，不能覆盖旧证据。

run manifest 同时绑定：

- 完整视频哈希的 `baseline_manifest.json` 及其 SHA-256；
- 当前 meta、baseline 中该样本的全部分析产物、源视频及实际生成 EDL 的 SHA-256；
- `spike_mpv.py`、`verify_mpv_frames.py`、`timeline_plan.py` 的 SHA-256；
- 规范化命令参数、预期结果路径和 provenance manifest；
- 命令专属的预登记阈值文件及其完整内容。

阈值文件格式：

```json
{
  "schema_version": 1,
  "kind": "mpv_phase0_thresholds",
  "command": "stress",
  "thresholds": {
    "seek_failures_max": 0,
    "duration_error_frames_max": 1.0,
    "load_seconds_max": 10.0,
    "seek_p95_ms_max": 300.0,
    "seek_max_ms_max": 1000.0
  }
}
```

上面的数值只是结构示例，不代表产品门槛已经获批。各命令要求的键和单位如下：

| 命令 | 必填阈值键 | 单位/含义 |
|---|---|---|
| `env` | `require_runtime_status`, `require_supply_chain_status` | 都必须登记为字符串 `pass` |
| `stress` | `seek_failures_max`, `duration_error_frames_max`, `load_seconds_max`, `seek_p95_ms_max`, `seek_max_ms_max` | 次数、帧、秒、毫秒、毫秒 |
| `lifecycle` | `repeat_min`, `failures_max`, `handle_growth_max`, `thread_growth_max` | 次数/计数 |
| `wid` | `switch_loads_min`, `load_failures_max`, `load_p95_ms_max`, `load_max_ms_max`, `keyboard_events_min`, `queued_errors_max` | 次数、次数、毫秒、毫秒、次数、次数 |

缺少 baseline/阈值、样本哈希发生变化、阈值 command 不匹配、验证器缺失或出现未支持的阈值键时，
run manifest 固定为 `blocked`。结果报告仍可用于诊断，但会带
`RUN_MANIFEST_INCOMPLETE`，不能成为正式 Gate PASS。只有完整绑定时，harness 才会按预登记阈值
生成逐项 `preregistered_threshold_evaluation`；任何超限都会明确标为 `fail`。

## 4. Gate

### G0：供应链与加载

要求：

- 使用 `--provenance-manifest` 固定 DLL 构建来源、版本、架构、SHA-256 和许可证。
- manifest 必须同时声明五个本地文件及各自 SHA-256：来源归档、实际 DLL、构建记录、许可证、再分发说明；五个文件必须全部存在并通过实际哈希校验。
- `source_url`、构建配置字符串、许可证备注只允许作为补充元数据，任何非空字符串都不能代替上述本地证据文件。
- `python-mpv` 必须延迟导入。
- 通过显式 `--mpv-dir` 或 `MPV_DLL_DIR` 加载，不依赖用户 PATH。
- 记录实际加载 DLL 的绝对路径和 SHA-256，并要求二者与 manifest 中的 `dll.path`、`dll.sha256` 同时精确匹配。
- 在无系统 mpv 的环境中可以创建并销毁一个 MPV 实例。

通过标准：环境探针退出码为 0；`provenance_files.status`、`loaded_dll_matches_provenance_path`、`loaded_dll_matches_provenance_sha256` 和 `supply_chain_status` 全部为 `pass/true`，报告同时包含 wrapper 与 libmpv 版本。manifest 缺失、无效或证据验哈希失败时，允许继续收集本机运行诊断，但 G0 必须保持 `blocked`。

manifest 的最小结构如下；相对路径以 manifest 所在目录为基准：

```json
{
  "schema_version": 1,
  "kind": "mpv_phase0_provenance",
  "source_url": "https://example.invalid/path/to/source-archive.7z",
  "archive": {"path": "source-archive.7z", "sha256": "<64 hex>"},
  "dll": {"path": "libmpv-2.dll", "sha256": "<64 hex>"},
  "build": {"path": "build-record.json", "sha256": "<64 hex>"},
  "license": {"path": "LICENSE.txt", "sha256": "<64 hex>"},
  "redistribution": {"path": "REDISTRIBUTION.txt", "sha256": "<64 hex>"}
}
```

### G1：Windows/Tk WID

要求：

- 使用已经创建 HWND 的专用 `tk.Frame`，不把 Canvas 当渲染目标。
- 验证 resize、最大化、最小化/恢复、DPI、跨显示器和焦点。
- 关闭 mpv 默认按键，所有产品快捷键仍由 Tk 处理。
- mpv 回调只写有界队列；只有 Tk 主线程更新控件。

通过标准：无黑屏、错位、键盘丢失或关窗死锁。

### G2：帧与时间映射

要求：

- skip 段先裁切、排序、合并，再求 complement 得到 keep 段。
- 所有帧区间统一为半开区间 `[start, end)`。
- EDL 路径使用 `%<UTF-8 字节数>%<字符串>` 转义。
- 检查段首、段尾、首帧、末帧以及确定性随机点。
- 检查 `source -> virtual -> source` 往返。
- 独立 oracle 必须保留完整 `pts_table`，不得用 `frame / fps` 补造媒体时间。
- 合成 CFR/VFR 夹具必须为每个源帧编码唯一像素 ID，并提供不可变的 truth manifest：
  帧顺序、PTS/duration ticks、时间基、ID 布局和视频 SHA-256 都必须绑定。
- truth 模式必须在同一次 FFmpeg 解码中同时采集 `showinfo` 和灰度帧，按解码顺序
  对齐 ID；不得用 OCR、独立猜测或复制/删除业务帧伪造 VFR。
- 合成夹具通过只证明 oracle 实现正确，不能替代四个真实样本的 PTS、画面和 WID 验收。
- 真实源出现重复/非单调 PTS 时，禁止排序或去重。允许继续评估的唯一替代分支是
  **版本化正规化代理**，且必须保留独立源 oracle 与代理 oracle，不能把代理结果反写成
  “原片 PTS 正常”。
- 代理业务帧域必须显式登记为半开区间 `[0, N)`；终止 guard 只能位于索引 `N`，
  不得计入业务帧数、source/virtual 映射或后续 EDL 帧域。
- 非零源窗口必须显式登记为 `source [K,K+N) -> proxy [0,N)`；构建命令必须同时携带
  `source_start_frame=K` 和 `business_frames=N`，解码使用对应的 `trim=start_frame=K:end_frame=K+N`。
  source oracle、同次 source decode、generation、manifest、guard 和后续 EDL 必须消费同一
  frame mapping，不得退回 `source_index == proxy_index` 的隐式假设。
- 代理必须证明 `N` 个真实画面全部保留并与源解码顺序逐帧 checksum 对齐；所有真实帧
  duration 必须为正。guard 若被容器解码，可单独为零 duration，但通用 oracle 仍应严格
  报 `BLOCKED`，只能由代理专用验证器确认该问题完全局限于业务域外 guard。
- 代理不得从 `frame / fps` 补造源媒体时间。当前 Phase 0 路线只允许从已绑定源 oracle 的
  正 duration ticks 重建单调 PTS；不满足该前提时继续 `BLOCKED`。
- 写一次代理 manifest 必须绑定源视频 SHA-256、源 oracle、同次代理编码采集的源画面
  checksum 证据、代理 SHA-256、完整实际/规范化命令、FFmpeg 可执行文件及版本、
  libx264 标识和验证器哈希。任一文件或工具变化都使旧报告失效。
- 完整样本不得为了补 checksum 再单独重跑一次已完成的源 PTS 长扫描；源 checksum 应在
  代理编码的同一次必要解码中采集，并与已有完整 PTS/duration 表逐项绑定。
- 验证顺序固定为样本 3 的 40 帧前缀、完整样本 3、音频时间线、A/V 同步；视频域单独
  PASS 时，总状态仍必须保持 `BLOCKED`。
- 音频/A-V 验证器必须先自证：阈值只能是有限正数；必须重新加载并校验源 oracle、代理
  oracle、完整业务 checksum/PTS/duration、terminal guard、`full` scope 和源/代理媒体
  SHA-256，不能只读取外部 `video_validation.status`。oracle 必须记录唯一解码像素格式，
  源/代理格式不一致（例如 10-bit/4:4:4 被静默转为 `yuv420p`）保持 `BLOCKED`。
- 音频验收必须使用完整 FFmpeg `ashowinfo` 解码证据：记录选中的音轨、codec、采样率、
  声道布局、首帧 PTS（允许 AAC priming 的非零起点）、每帧 `nb_samples`、PTS 连续性、
  总样本数和首尾时间；缺 PTS、gap/overlap、零样本、解码失败或格式中途变化均为
  `BLOCKED`。不能只抽样首尾帧代替完整音频时间线。
- 源有音频而代理没有音频时必须报告 `PROXY_AUDIO_MISSING`；源和代理都有音频时，
  预注册的 packet-copy 路线要求采样率、布局、总样本数和每一帧 PCM checksum 全部一致。
  源无音频/代理无音频只能在显式策略下标为 `NOT_APPLICABLE_PASS`，不能默认为通过。
- 视频归一化相对源 PTS 的身份偏移，以及音频首尾相对错误源 PTS 的边界变化，只能作为
  `diagnostic_only` 证据；它们不能单独证明内容失步，也不能靠放宽阈值变成内容同步证明。
  `max_identity_offset_span_seconds` 必须保持有限正数，但只约束诊断器配置。
- G2 内容证据采用双窗口契约，不能再要求一个 manifest 同时提供
  `start`、`pts_conflict`、`middle`、`end` 四区。第一类是来自完整 reconciliation 的
  **PTS-conflict evidence**：它只证明源身份、解码画面顺序、逐帧 checksum 和重复/非单调
  PTS 冲突结构，必须固定为 `time_authority=none`、`production_consumer_allowed=false`；不得
  作为 EDL 起止时间、媒体时间或 Gate PASS 的依据。
- 第二类是独立的 **A/V event evidence**：必须来自真实源内容观察，并绑定可复核的画面事件、
  音频事件和音频采样时钟。scanner 的 `CANDIDATES_FOUND` 只允许定位候选窗口，不能自动生成、
  替代或冒充人工 source observation，也不能仅凭公式推导事件。
- split bundle 只能把两类窗口绑定为结构前提，固定为
  `structural_precondition_only`、`ready_for_source_anchor=false`；即使其结构校验为 `PASS`，也不
  构成 source-anchor、A/V 内容或 G2 PASS。只有真实观察成立后，才允许登记独立、只写一次且
  仅含源侧字段的 source-anchor manifest；之后才允许启动对应的非零有界代理生成。
- source-anchor 必须绑定源媒体 SHA-256、源 oracle、结构化 source observation、源音频解码
  证据、源帧/音频窗口序列、capture/创建/验证脚本与 FFmpeg；出现任何 proxy 字段、伪造
  observation、事后登记或绑定变化都必须 `BLOCKED`。后续 content-anchor 中的每个锚点还必须
  绑定源/代理业务帧 checksum、各自音频采样时钟中的 sample index、非空人工观察说明，以及
  源/代理的 PNG 画面和 WAV 音频窗口；内容误差阈值必须有限、为正且不得大于预登记的 `10 ms`。
- 正规化代理 builder 必须强制接收上述 source-anchor manifest，并在 FFmpeg 启动前、生成结束且
  代理发布前、证据发布前分别复核源身份、oracle、scope、覆盖帧域和文件记录；代理 manifest、
  generation 与同次 source-decode evidence 必须绑定同一记录。代理 `verify` 必须再次校验 SHA-256、
  登记时间、源身份、oracle、scope、帧域与显式 frame mapping，不能只信任已有 PASS 状态。
- source-anchor/content-anchor 的非零窗口支持只有在 consumer 按
  `proxy_index = source_index - source_start_frame` 逐项重推导后才算完成；代理视频层能够生成
  非零窗口本身不构成 A/V 内容 Gate PASS。
- 每个 PNG/WAV 窗口必须绑定媒体 SHA-256、oracle、具体音频帧、capture 工具、当前 FFmpeg
  和实际命令。验证器必须用同一绑定媒体和 FFmpeg 独立重提取到新临时路径并逐文件比对
  SHA-256；只检查文件头、已有哈希或声明命令不能算内容证据。捕获与报告均只写一次，
  并发发布不得覆盖或删除其他进程已创建的目标。
- 音频/A-V 验证必须另写一次性的 `mpv_phase0_audio_av_run_manifest`，绑定视频 manifest
  与验证报告、源/代理 SHA-256、完整 FFmpeg 命令、FFmpeg 和验证脚本哈希、阈值及音频
  解码摘要；不能把音频结果回写成修改旧视频 manifest 的理由。
- A/V 的独立合成真值必须同时使用视频 PTS 时钟和音频采样时钟：合成帧中放置已知闪光
  或唯一 ID，音频中放置已知采样范围脉冲；验证器必须能证明同一锚点误差在阈值内，
  并对人为平移的负例返回 `BLOCKED`。这类夹具通过只证明验证器实现正确，不替代真实样本。
- 双窗口结构证据完成后，仍必须依次通过真实 source-only observation、非零有界代理的逐帧
  checksum/duration/guard 验证、音频采样时钟验证、真实内容锚点验证，以及 EDL writer 对代理
  PTS/tick 的实际消费；上述任一环节缺失都不得宣称 G2 PASS，也不得进入依赖 G2 的正式验收。

通过标准：所有保留帧映射精确；被删除帧明确返回“不在 EDL”，不得隐式吸附；
合成夹具的 ID 顺序、PTS 和 duration 全部与 truth manifest 一致；四个真实样本要么本身
没有重复、非单调或缺失 PTS，要么使用通过上述逐帧画面、guard、duration、音频与 A/V
验证的版本化代理；EDL writer 还必须真正消费获准路线的 PTS 表。任何一项缺失都保持
`BLOCKED`。

### G3：EDL 正确性与切点压力

要求：

- `1.mp4` 和 `4.mp4` 使用完整段表，不得先合并或删减段数。
- 验证文件加载、跨段 seek、连续播放和 EOF。
- 对切点前后画面做帧校验，记录零删除帧闪现。
- 分别记录冷启动和热运行的 p50/p95/p99/max。

通过标准：边界正确率 100%，删除画面闪现 0 次；性能阈值必须在运行前写入结果配置，不能看完结果再改。

### G4：完整源片与 EDL 切换

要求：

- 记录源片帧位置。
- 异步 load，等待 `file-loaded`。
- 映射并恢复位置、暂停状态和速度。
- 长按方向键只执行 latest-only/coalesced seek。

建议门槛：切换 p95 不高于 300ms，最大值不高于 1s；若真实机器达不到，应记录产品可接受阈值后重新决策，不能静默放宽。

### G5：逐帧与倍速

要求：

- 完整源片模式下前进/后退一帧必须保持源帧语义。
- 冷/热各执行 100 次后退，记录 p50/p95/max。
- 实测 2x、10x、20x、80x 的推进量、CPU/GPU 和切点过冲。
- `frame-back-step` 视为可能触发精确 seek，不预设其即时性。

通过标准：逐帧结果正确；性能是否通过按预先登记的产品阈值判定。

### G6：生命周期与分发

要求：

- 连续执行 50 次创建、加载、切换、停止和关闭。
- 记录 Python 线程数、Windows HANDLE 数和失败次数。
- 主线程执行 observer 注销、停止、`terminate()`，然后销毁 HWND。
- frozen `onedir` 在无 Python、无系统 mpv、中文/逗号/% 路径环境验证。

通过标准：0 崩溃、0 死锁、0 文件占用残留；资源计数不存在持续线性增长。

## 5. 当前执行命令

生成当前工作区基线：

```powershell
python scripts/spike_mpv.py baseline --full-video-hash
```

探测 wrapper 和 DLL：

```powershell
python scripts/spike_mpv.py env
python scripts/spike_mpv.py env --mpv-dir C:\path\to\mpv --provenance-manifest C:\path\to\provenance.json
```

生成并验证完整 EDL：

```powershell
python scripts/spike_mpv.py build-edl --meta .cache\preview_fluency\1_meta.json
python scripts/spike_mpv.py build-edl --meta .cache\preview_fluency\4_meta.json
```

生成独立 PTS oracle 合成夹具并验证已知真值：

```powershell
python scripts/generate_pts_oracle_fixtures.py --output-dir .cache\mpv_spike\pts_fixtures --force
python scripts/verify_mpv_frames.py .cache\mpv_spike\pts_fixtures\cfr.mp4 `
  --truth-manifest .cache\mpv_spike\pts_fixtures\cfr.truth.json
python scripts/verify_mpv_frames.py .cache\mpv_spike\pts_fixtures\vfr.mp4 `
  --truth-manifest .cache\mpv_spike\pts_fixtures\vfr.truth.json
```

生成并验证独立音画采样时钟夹具：

```powershell
python scripts/generate_av_sync_fixtures.py --output-dir .cache\mpv_spike\av_fixtures --force
python scripts/verify_av_sync_fixtures.py .cache\mpv_spike\av_fixtures\av_sync.mp4 `
  .cache\mpv_spike\av_fixtures\av_sync.truth.json
```

长真实样本可显式调整 FFmpeg 解码线程数；默认 `--threads 1` 是可重复基线，改变线程数
必须保留在报告命令中并与同一素材的基线结果比较：

```powershell
python scripts/verify_mpv_frames.py D:\qq下载\920\3.mp4 `
  --threads 4 --output .cache\mpv_spike\frame_oracle\3_20260809_threads4.json
```

只有先取得真实事件 observation 并只写一次登记 source-anchor 后，才可按下列模板构建有界
正规化代理；`<pre_registered_source_anchor.json>` 不是现有 PASS 证据，当前不得直接执行：

```powershell
python scripts/pts_normalized_proxy.py build D:\qq下载\920\3.mp4 `
  --source-oracle .cache\mpv_spike\pts_normalize\3_source40.oracle.json `
  --source-anchor-manifest <pre_registered_source_anchor.json> `
  --source-decode-output .cache\mpv_spike\pts_normalize\3_proxy40_guard_v8.source_decode.json `
  --source-start-frame <K> `
  --business-frames 40 `
  --output .cache\mpv_spike\pts_normalize\3_proxy40_guard_v8.mp4 `
  --manifest .cache\mpv_spike\pts_normalize\3_proxy40_guard_v8.manifest.json

python scripts/pts_normalized_proxy.py verify `
  .cache\mpv_spike\pts_normalize\3_proxy40_guard_v8.manifest.json `
  --oracle-output .cache\mpv_spike\pts_normalize\3_proxy40_guard_v8.oracle.json `
  --output .cache\mpv_spike\pts_normalize\3_proxy40_guard_v8.verify.json --threads 4
```

完整样本 3 只能在 source-first 有界窗口的视频、音频、内容锚点和 EDL 边界全部通过后构建；
当前条件未满足，以下命令只保留为后续绑定模板：

```powershell
python scripts/pts_normalized_proxy.py build D:\qq下载\920\3.mp4 `
  --source-oracle .cache\mpv_spike\frame_oracle\3_20260809.json `
  --source-anchor-manifest <pre_registered_source_anchor.json> `
  --source-decode-output .cache\mpv_spike\pts_normalize\3_proxy_full_v2.source_decode.json `
  --output .cache\mpv_spike\pts_normalize\3_proxy_full_v2.mp4 `
  --manifest .cache\mpv_spike\pts_normalize\3_proxy_full_v2.manifest.json

python scripts/pts_normalized_proxy.py verify `
  .cache\mpv_spike\pts_normalize\3_proxy_full_v2.manifest.json `
  --oracle-output .cache\mpv_spike\pts_normalize\3_proxy_full_v2.oracle.json `
  --output .cache\mpv_spike\pts_normalize\3_proxy_full_v2.verify.json --threads 4
```

音频时间线和 A/V 同步必须使用独立验证器；即使视频报告为 `video_status=PASS`，总状态
也必须等待此命令通过：

```powershell
python scripts/verify_proxy_audio_av.py verify `
  .cache\mpv_spike\pts_normalize\3_proxy_full_v2.manifest.json `
  --video-report .cache\mpv_spike\pts_normalize\3_proxy_full_v2.verify.json `
  --run-manifest .cache\mpv_spike\pts_normalize\3_proxy_full_v2.audio_av_v5.run.json `
  --output .cache\mpv_spike\pts_normalize\3_proxy_full_v2.audio_av_v5.verify.json
```

当前样本 3 的完整代理视频为 `PASS`，但上述历史音频/A-V 报告为 `BLOCKED`（代理无音频，
且没有真实 source-only observation 与内容锚点），因此这不是 Gate PASS。约 `83.333 ms`
只记录源 PTS 身份变化，不得解释成已经测得的内容失步。

传入同一份 provenance manifest 运行：

```powershell
python scripts/spike_mpv.py stress --meta .cache\preview_fluency\4_meta.json --mpv-dir C:\path\to\mpv --provenance-manifest C:\path\to\provenance.json
python scripts/spike_mpv.py wid --meta .cache\preview_fluency\2_meta.json --mpv-dir C:\path\to\mpv --provenance-manifest C:\path\to\provenance.json
python scripts/spike_mpv.py lifecycle --meta .cache\preview_fluency\3_meta.json --repeat 50 --mpv-dir C:\path\to\mpv --provenance-manifest C:\path\to\provenance.json
```

正式 Gate 运行还必须额外传入同一份完整 baseline 和命令专属阈值：

```powershell
python scripts/spike_mpv.py stress --meta .cache\preview_fluency\4_meta.json --mpv-dir C:\path\to\mpv --provenance-manifest C:\path\to\provenance.json --baseline-manifest .cache\mpv_spike\baseline-YYYYMMDD\baseline_manifest.json --threshold-manifest C:\path\to\stress-thresholds.json
```

证据不完整时这些命令仍可运行，以便采集 runtime/WID/lifecycle 数据；其报告必须携带同一份 manifest 的验证结果，并保持 Gate 状态为 `blocked`，不能据此宣布供应链通过。

## 6. 决策规则

```text
G0 失败
  -> 不允许继续 WID/EDL 性能结论；先解决 DLL 来源与许可证

G0 通过、G2/G3 失败
  -> EDL 路线 No-Go；保留 OpenCV 播放器

G2/G3 通过、G4/G5 部分失败
  -> 评估混合方案，不承诺完整替换

G0-G6 全部通过
  -> Go；进入 TimelinePlan 后的正式双引擎接入
```

无论 mpv 结果如何，下一项正式生产代码改造仍是统一 `TimelinePlan`。
# Authority override - 2026-08-14

- The project owner authorizes independent Codex evidence review to replace the previous human-observation step.
- Existing immutable sample-3 candidate clips were reviewed without rerunning scanners or full PTS oracles.
- The nearest visual/audio changes differ by about 180 ms at 316.1/316.2 and about 380 ms at 325.4; both exceed the fixed 10 ms content threshold.
- Result: `NO_USABLE_AV_EVENT`. Close the candidate route. Do not create a source anchor, bundle, or proxy from it. G2 remains `BLOCKED`.
- A 503 from the editing service is infrastructure state, not a Gate result. Preserve the current patch transaction and resume it after the controlled editor recovers.
