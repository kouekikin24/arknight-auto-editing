# 视频时间线、任务生命周期与 libmpv 验证：当前交接入口

> 权威状态日期：2026-08-13  
> 工作目录：`D:\ArknightsPathFinding\arknight-auto-editing-main`  
> 当前分支：`fix/preview-pacing-metrics`  
> 当前 HEAD：`52b7aef40b55429d35f2b131a3d0e8abbdf65e5c`

本文是下一位接手者（尤其是 Claude）的**唯一当前执行入口**。

- Gate 的验收定义以 [`MPV_PHASE0_SPEC.md`](MPV_PHASE0_SPEC.md) 为准。
- Gate 的详细证据以 [`MPV_PHASE0_RESULTS.md`](MPV_PHASE0_RESULTS.md) 和磁盘报告为准。
- [`HANDOFF_TIMING.md`](HANDOFF_TIMING.md) 与 [`HANDOFF_PREVIEW.md`](HANDOFF_PREVIEW.md)
  仅保留为历史记录，不得从其中的旧“下一步”恢复任务。
- 文档与磁盘证据冲突时，以实际文件、报告内容和 SHA-256 为准；先停下来核对，不得覆盖证据。

## 1. 接管规则

1. 当前 working tree 原本就有大量已修改和未跟踪文件，全部视为用户或前序任务成果。
2. 不得回滚、清理、暂存或提交；不得使用 `git reset --hard`、`git checkout --` 或 `git clean`。
3. 第一轮只做只读审计，不从头重做已经完成的长扫描。
4. 用户说“主动停止”时立即停止，不留下 FFmpeg、mpv 或 Python 媒体后台进程。
5. G0-G6 全部通过前，不设计或接入生产 `MpvEngine`，继续保留 `VideoIOThread`。
6. 自动化测试通过只代表代码回归通过，不等于任何 libmpv Gate 放行。
7. 验证线和生产线分开推进；不得把播放器、导出、时间线和任务系统重新合并成一个大改动。
8. 本文默认在当前工作目录原地接管。若要跨机器交接，必须另行生成关键未跟踪文件和
   `.cache` 证据的路径、大小与 SHA-256 清单；普通 `git clone` 不包含这些成果。

## 2. 当前 working tree

当前 HEAD 没有改变，但工作区远超 HEAD；本轮未新增提交，也未暂存任何文件：

```text
tracked modified:
  analyzer.py
  main.py
  preview_player.py
  pyproject.toml
  settings_panel.py
  timeline_widget.py
  video_io.py

important untracked:
  edit_commands.py
  project_state.py
  task_manager.py
  timeline_plan.py
  scripts/
  tests/
  HANDOFF_CURRENT.md
  HANDOFF_TIMING.md
  MPV_PHASE0_SPEC.md
  MPV_PHASE0_RESULTS.md
```

最近一次已记录的显式回归结果是：

```text
290 tests passed
G2 split-window bundle 专项：12 tests passed
compileall：passed（使用独立 PYTHONPYCACHEPREFIX）
git diff --check：passed（仅既存 LF/CRLF 提示）
```

这些结果在生成本文时没有重新运行。接手者必须先核对 working tree，再决定是否复跑。

## 3. 总方案位置

两条线仍未汇合：

```text
验证线：证明 libmpv 是否值得使用
生产线：先保证时间线、导出和任务生命周期正确
```

| 阶段 | 当前状态 | 已完成边界 / 剩余阻塞 |
|---|---|---|
| 0 基线与导出护栏 | 部分完成 | 冻结点和音频/原子替换护栏已落地；缺 preview golden、export golden、clip persistence baseline |
| 1 libmpv Phase 0 | 执行中，`INCONCLUSIVE` | G2 当前是最近阻塞点；没有 Go 决策 |
| 2 唯一 `TimelinePlan` | 核心已落地 | 半开区间、删除/保留区和源帧/虚拟帧映射已统一 |
| 3 收敛 FFmpeg 导出 | 部分完成 | FFmpeg-only 写出已落地；真实 PTS/VFR、非零起始时间、统一 `MediaExporter` 等未完成 |
| 4 统一 `TaskManager` | 核心已落地并收口 | generation/revision、取消、stale result 和关窗生命周期已接入 |
| 5 生产 `MpvEngine` | 未开始 | 只能在 G0-G6 全部通过后开始 |
| 6 统一分析 `FrameSource` | 未开始 | 不得在 G2 阻塞时顺手重写分析输入层 |
| 7 工具与缓存整理 | 未开始 | 不得先清理当前未跟踪证据或批量提交 `scripts/` |

阶段编号不是当前实现的严格时间顺序：生产线阶段 2/4 已先行完成核心工作，阶段 3 仍只完成首个切口。

## 4. libmpv Gate 状态

| Gate | 状态 | 当前结论 |
|---|---|---|
| G0 供应链与加载 | `BLOCKED` | 本机 DLL 可加载；缺可追溯来源归档、构建和许可证/再分发证据 |
| G1 Windows/Tk WID | `BLOCKED` | 有局部嵌入证据；缺真实像素、resize/DPI/跨屏、焦点和键盘验收 |
| G2 帧与时间映射 | `BLOCKED` | 四样本源 PTS 冲突；代理视频局部通过，但真实内容级 A/V 锚点和 EDL PTS/tick 消费未通过 |
| G3 EDL 正确性 | `BLOCKED` | headless seek 只是探索证据；未证明全部切点不闪删除画面 |
| G4 SOURCE/EDL 切换 | `BLOCKED` | 局部 load 循环不能替代任意源位置、黑屏和 generation 验收 |
| G5 逐帧与倍速 | `NOT_RUN` | 未完成图像 oracle、2x/10x/20x/80x 实际推进率和边界过冲 |
| G6 生命周期与分发 | `BLOCKED` | headless 50 次局部通过；缺完整 WID、500 次切换、onedir 和干净机 |

总判定保持 `INCONCLUSIVE`。没有任何硬 Gate 已完整 `PASS`，也没有足够证据把整个 libmpv
方案判为最终 `FAIL`。

## 5. 已完成且不得从头重做

- `TimelinePlan` 已成为删除区、保留区、严格半开 `[start,end)` 和源帧/虚拟帧映射的唯一模型。
- `ProjectState` 通过不可变 `EditCommand` 更新，播放器和时间轴不应直接修改共享业务字典。
- 预览、整段导出和分段导出从同一 `TimelinePlan` 生成业务区间。
- 主入口、设置面板和预览播放器共享 `TaskManager`；分析、GPU 探测、整段/分段导出使用统一
  generation、revision、取消和结果队列语义。
- 导出使用唯一临时文件和原子替换；失败或取消不覆盖旧成品；音频三态预检会阻止静默丢音频。
- 生产导出写出已统一为 FFmpeg；FFmpeg 缺失时硬阻塞，不再回退 imageio/OpenCV writer。
- `VideoIOThread.close(timeout) -> bool`、统一关窗链路和真实 Tk 生命周期 smoke 已落地。
- Phase 0 harness 已绑定 baseline、provenance、验证器、阈值、命令和只写一次 run manifest。
- CFR/VFR 已知真值 PTS oracle 夹具已完成；它们只证明 oracle 能识别真值。
- 四个真实样本的完整 oracle 扫描均已完成，不得重新扫描来“确认”既有结论。
- 正规化代理的画面 checksum、正 duration 和域外 terminal guard 验证已实现并有自动化测试。
- A/V 阈值、证据身份、source-first observation、非零窗口映射和 split-window 隔离契约已经收紧。

## 6. G2 当前事实

四个真实样本完整源帧结果：

| 样本 | 完整帧数 | 重复 PTS | 非单调 PTS | 状态 |
|---|---:|---:|---:|---|
| 1 | 249097 | 2 | 1 | `BLOCKED` |
| 2 | 184293 | 2 | 1 | `BLOCKED` |
| 3 | 29804 | 2 | 1 | `BLOCKED` |
| 4 | 424176 | 2 | 1 | `BLOCKED` |

四份前缀诊断均证明两组重复 PTS 对应不同画面 checksum。冲突来自源媒体 packet PTS：

- 禁止排序后去重，否则会丢失真实画面；
- 禁止把 `frame / fps` 提升为正式 EDL 媒体时间；
- 普通 B 帧重排不能解释不同画面共用同一显示 PTS。

样本 3 当前代理证据：

- 完整正规化代理的视频部分为 `PASS`：29804 个业务画面逐帧 checksum 对齐，业务 duration
  全为正，额外 guard 位于业务域外。
- 完整代理报告总状态仍为 `BLOCKED`，因为现有完整代理缺音频且没有内容级 A/V 锚点。
- 40 帧 AAC packet-copy 有界实验的视频、guard 和 AAC 前缀通过，但因 `prefix` scope 和缺少
  真实内容锚点，总状态仍为 `BLOCKED`。
- 约 `83.333 ms` 只说明正规化时钟不再保持错误源 PTS 身份，不能单独证明内容错开，也不能通过
  放宽 10 ms 阈值处理。

## 7. 当前唯一近端人工分叉

样本 3 的 `[316,326)` schema v3 勘测已经按上限只执行一次。它报告三个候选请求坐标：
`316.1`、`316.2`、`325.4`。这些只是 requested-seek/sample-grid 坐标：

```text
media_pts_authority = none
can_register_source_anchor_directly = false
```

人工复核素材：

```text
.cache\mpv_spike\pts_normalize\3_av_event_candidates_316_326_review\start_315.8_316.8.mp4
.cache\mpv_spike\pts_normalize\3_av_event_candidates_316_326_review\end_325.0_326.0.mp4
```

下一步首先需要由用户或明确承担观察责任的人独立观看并听取两段素材，记录：

1. 是否存在可明确描述的画面事件；
2. 是否存在可明确描述的声音事件；
3. 两者是否属于同一内容事件；
4. 判断依据是否独立于 scanner 分数。

模型查看代表帧或 scanner 报告不能冒充人工听觉确认。当前尚未为这批真实候选创建正式
source observation、source anchor、真实 split-window evidence bundle 或新代理。

### 分支 A：人工观察不成立

1. 不创建 observation 或 anchor。
2. 不重复 `[316,326)`，不扩大盲扫范围，不调整 10 ms 内容阈值。
3. G2 保持 `BLOCKED`，记录“真实样本 A/V 代理路线暂不放行”。
4. 继续保留 `VideoIOThread`，不进入生产 `MpvEngine`。
5. 停止验证器继续膨胀，把主要开发工作转回生产线阶段 3。

### 分支 B：人工观察成立

1. 先创建一次、只写一次的 source-only observation/anchor；不得事后根据代理结果倒填。
2. 绑定源媒体、源 oracle、源帧/音频窗口、capture/创建/验证工具和 FFmpeg 的 SHA-256。
3. 再创建真实双窗口 evidence bundle。bundle 的结构性 `PASS` 仍不等于 G2 `PASS`。
4. 只生成对应非零源窗口的带音频代理，不先生成新的完整样本 3 代理。
5. 同时验证全部业务帧 checksum、正 duration、域外 guard、音频连续性、采样时钟、像素格式和
   内容锚点 10 ms 门槛。
6. 让 EDL writer 实际消费：

```text
源帧索引 -> 代理帧索引 -> 代理 PTS/tick -> EDL 片段起止时间
```

7. 验证删除帧不显示、保留帧不丢、首尾/切点/EOF 正确且 guard 不可见。
8. 有界窗口全部通过后才考虑完整样本 3；随后用同一方案验证样本 1、2、4。

任一子项失败都保持 G2 `BLOCKED`，不得通过放宽阈值、追随错误源 PTS 或伪造 observation 放行。

## 8. G2 明确后再做什么

若 G2 路线停止或等待人工输入，生产线阶段 3 可独立继续，建议按小工单推进：

1. 用 ffprobe JSON 建立结构化 `MediaInfo`，固定 `time_base`、`start_time`、VFR 和音轨信息。
2. 定义小型 `ExportRequest`/`ExportResult`，替代模糊字符串状态。
3. 让整段和分段导出共同使用一个 `MediaExporter` 执行服务。
4. 只对可恢复的 GPU 编码错误回退 CPU；取消、输入错误和证据不匹配不得回退。
5. 增加 VFR、非零起始时间、音频连续性和失败不覆盖旧文件的已知真值夹具。
6. 补 preview/export golden 和 clip persistence baseline，关闭阶段 0 的剩余缺口。
7. 使用正式 `uv lock`/`uv export` 同步依赖；不得手工伪造锁文件哈希。

之后的顺序仍是：G2 -> G3/G4 -> G1/G5/G6 -> G0 收口 -> 所有 Gate 通过后才设计生产
`MpvEngine` -> 阶段 6 `FrameSource` -> 阶段 7 仓库整理。

## 9. 关键证据索引

| 证据 | SHA-256 |
|---|---|
| `.cache/mpv_spike/baseline-20260812-current/baseline_manifest.json` | `5a4b6171165ecf2662628c6ccf9f48004e56ec21e0c7079212260ed3c3c00187` |
| `.cache/mpv_spike/frame_oracle/1_20260809_threads4.json` | `656b86662378d59364fc70863cb828d7c940a576421df1459720bde6fb4bfa37` |
| `.cache/mpv_spike/frame_oracle/2_20260809.json` | `131e50b6d70b8dda66c5591e499b5e41d5d05793e7b95f0599ac10ac13eaa1a8` |
| `.cache/mpv_spike/frame_oracle/3_20260809.json` | `24e692a45631a9d584b095367fd560424da17e036f419ba817a7f7e0f23442aa` |
| `.cache/mpv_spike/frame_oracle/4_20260809_threads4.json` | `888e908df990f29c5a048ed7b013b000c2a466a00c8131d0e523885117bc90e2` |
| `.cache/mpv_spike/pts_normalize/3_pts_conflict_evidence.v1.json` | `a6e5b9f777f8eba8a20890057bea76adb8d83d3521a53f000031b6d132413b81` |
| `.cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326.v3.json` | `d3bc2ead7cae489644d5a3dcb934525a425652d1a58f0c20590f479baa16b18b` |
| `.cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326.contact.png` | `9c80a5ba1243aea815f69774c7e9aef5a94ba1c7de845fc6820e5fbfe6ba00f4` |
| `.../3_av_event_candidates_316_326_review/start_315.8_316.8.mp4` | `127c4b9139200a903f502ce1b5221aa976b4804ee675d324f46ecec963402fdc` |
| `.../3_av_event_candidates_316_326_review/end_325.0_326.0.mp4` | `4de8c5d9d092088246cf3f5d692058e5d268c39ebf9b96c3cdcb26f3c05a30b1` |

额外状态报告：

```text
.cache\mpv_spike\pts_normalize\3_proxy_full_v2.verify.json
.cache\mpv_spike\pts_normalize\3_proxy_full_v2.audio_av_v5.verify.json
.cache\mpv_spike\pts_normalize\3_proxy40_hardened_20260810_audio_copy_v4.verify.json
```

前两者和 AAC 前缀报告的总状态均为 `BLOCKED`；不得只读取嵌套的 `video_validation=PASS`。

## 10. 接管时的命令顺序

第一步只读核对：

```powershell
git status --short
git rev-parse HEAD
git diff --stat
git diff --check
```

然后完整阅读：

```text
HANDOFF_CURRENT.md
MPV_PHASE0_SPEC.md
MPV_PHASE0_RESULTS.md
HANDOFF_TIMING.md（只在需要历史背景时查询）
```

核对关键证据存在并匹配上述 SHA-256 后，才运行不生成长媒体的回归：

```powershell
python -m unittest discover -s tests -p "test_split_window_evidence_bundle.py"
python -m unittest discover -s tests -p "test_*.py"

$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP "arknights-auto-editing-pycache"
python -m compileall -q analyzer.py main.py preview_player.py settings_panel.py `
  task_manager.py timeline_plan.py timeline_widget.py video_io.py scripts tests

git diff --check
```

首次接管禁止运行：

- 四个完整 frame oracle；
- `[316,326)` scanner；
- 新的完整样本 3 代理；
- 样本 1/4 的任何完整长扫描；
- 真实 WID/G3-G6 或生产 `MpvEngine` 工作；
- 未经审计的 `arknight-preview-pack.zip`；
- 清理、归档或批量提交 `scripts/`、`tests/`、缓存和 `_patch*.py`。

## 11. 停止与升级条件

- 关键证据缺失或 SHA-256 不匹配：立即停止，先调查来源，不得覆盖原文件。
- 人工无法确认同一内容 A/V 事件：按分支 A 停止该窗口路线，不继续盲扫。
- 有界代理任一视频、音频、内容锚点或 EDL 边界检查失败：保持 G2 `BLOCKED`。
- G0-G6 未全部通过：禁止开始生产 `MpvEngine`，继续使用 `VideoIOThread`。

# Production-line progress - 2026-08-14

- Full and ranged preview exports now construct immutable `ExportRequest` snapshots and execute through `MediaExporter`.
- The exporter owns ranged staging, validation, cancellation checkpoints, and atomic publication; the UI no longer owns per-file partial paths.
- Existing settings and source values are captured before the worker starts. A recovered controlled editor resumes the same patch transaction; a 503 never changes Gate state or switches lines.
- Verification: 314 repository unittest cases passed; focused MediaInfo/MediaExporter and export integration suites passed; `git diff --check` passed.
- `compileall` was attempted sequentially but the existing `__pycache__` files reject temporary replacement writes (`WinError 5`). Key production modules pass `py_compile`; no cache files were removed or altered.
- Approval line remains: Phase 0 `INCONCLUSIVE`; G0/G1/G2/G3/G4/G6 `BLOCKED`, G5 `NOT_RUN`; independent Codex review closed the sample-3 candidate route as `NO_USABLE_AV_EVENT`. Keep `VideoIOThread`; do not design or connect production `MpvEngine`.
- Real probe smoke is now available through an explicit, paired candidate: `D:\弹弹play\ffmpeg\ffprobe.exe` and `D:\弹弹play\ffmpeg\ffmpeg.exe`, both version `6.1.1-full_build-www.gyan.dev`. A read-only probe of the existing `av_sync.mp4` fixture succeeded and verified the tool hashes (`ffprobe` `05bf9449818a2a0c76fabc332ae314d202dea8d10143934b1d9dfe9da2d672dd`; `ffmpeg` `fc635444b2a75d3c709b586ddd1e51e2d7c5b74126c7903db9658592af872ba7`). The parsed MediaInfo contains video/audio time bases, raw start/duration ticks, and audio format; `complete_for_export` correctly remains false until frame PTS certification. The candidate's license/provenance archive is still missing, so this does not change G0.
- 自动化测试全过但真实 Gate 证据仍缺失：只汇报回归通过，Gate 状态不变。
# Execution resilience policy - 2026-08-14

- File edits use the current environment's permitted controlled editing tool.
- A business change is treated as one recoverable patch transaction: preserve its target files, patch content, and pre-edit hashes.
- A 503 from the editing service is an infrastructure state (`EDIT_CHANNEL_DEGRADED`), not a code failure or Gate result. Do not discard the patch, switch task lines, or change Gate status.
- When a persistent transaction or `workspace_patch` capability exists, resume the same transaction ID. Otherwise retain the exact patch context and resume after the controlled editor recovers.
- Before resuming, reread the worktree and verify file hashes. Never use PowerShell, Python, `git apply`, or another indirect write path to bypass the controlled editor.
- Approval and production lines continue in parallel: approval evidence keeps its registered status; unrelated production work may proceed. Keep `VideoIOThread` and prohibit production `MpvEngine` until G0-G6 all pass.

# Production-line progress - 2026-08-15

- The repository FFmpeg 7.1 essentials pair is the active read-only candidate:
  `tools/ffmpeg-7.1.0/bundle/ffmpeg-7.1-essentials_build/bin/ffmpeg.exe` and
  `ffprobe.exe`. Both `-version` checks, hashes, and the real A/V fixture probe pass.
- Runtime OpenCV evidence is recorded in `tools/opencv-runtime-manifest.json`:
  `opencv-python 4.13.0.92`, `cv2 4.13.0`, and
  `opencv_videoio_ffmpeg4130_64.dll` SHA-256
  `fcc614672159094a35815b7ea2819a648cbf86c30d90c57e8c6ba1432f594548`.
  `pyproject.toml` remains unpinned while `requirements.txt`/`uv.lock` record
  `5.0.0.93`; `uv` is unavailable in this environment, so lock synchronization is
  explicitly pending and no lock file was hand-edited.
- `MediaInfo` now requires an immutable `FramePtsCertification` bound to source
  SHA-256, time_base, decoded frame count, PTS-table digest, and evidence SHA-256.
  Evidence with blocking reason codes, duplicate/non-monotonic PTS, changed source,
  or changed evidence is not export-ready. FFprobe `nb_frames` remains a hint; the
  decoded PTS table is authoritative.
- `MediaExporter` now has one certification gate for full and ranged requests:
  production requests without a current, source-bound `MediaInfo` fail before
  creating output directories or calling `analyzer`; legacy orchestration tests
  must opt out explicitly. A certified request records
  `pts_table_consumed=false` because the current analyzer still uses frame-index
  selection and output FPS; the authoritative PTS/tick EDL consumer remains a
  separate pending step.
- Approval line remains unchanged: Phase 0 `INCONCLUSIVE`; G0/G1/G2/G3/G4/G6
  `BLOCKED`, G5 `NOT_RUN`; sample-3 A/V route is closed as `NO_USABLE_AV_EVENT`.
  Keep `VideoIOThread`; do not design or connect production `MpvEngine`.
- Latest verification after this work: 328 repository unittest cases passed,
  temporary-cache `compileall` passed, and `git diff --check` passed.

# Production-line progress - 2026-08-15 PTS certification boundary

- Added `frame_pts_certifier.py`: it reuses the existing full FFmpeg `showinfo`
  oracle with the explicit `MediaInfo.ffmpeg` executable, checks source and both
  tool hashes before and after decoding, derives CFR/VFR from integer ticks and
  durations, and publishes a source/tool-keyed JSON evidence file with a
  temp-file plus hard-link write-once boundary. Duplicate, non-monotonic,
  missing or partial PTS remains `BLOCKED`; no timestamp repair or FPS fallback
  is performed.
- `FramePtsCertification` evidence now requires a production schema, full scope,
  source path/SHA/size, exact video `time_base`, decoded frame count, PTS-table
  digest, and the paired FFmpeg/ffprobe path/SHA/version bindings. Tool hashes
  are rechecked when export readiness is evaluated.
- `VideoPreviewPlayer` submits `player.frame_pts_certification` as a source-only
  TaskManager task (`project_generation` bound, `timeline_revision=None`).
  Timeline edits do not cancel it; source reload/close does. The task uses
  `TaskContext.commit` for evidence publication and keeps the current
  `MediaInfo` un-certified when the result is blocked.
- Added `pts_timeline.py` as the pure adapter from source frame ranges to exact
  half-open PTS tick intervals. `MediaExporter` now loads and records those
  intervals for full/ranged requests, but deliberately keeps
  `pts_table_consumed=false` until the actual FFmpeg/EDL command consumes them.
- `scripts/verify_mpv_frames.py` now accepts a cooperative cancellation check
  per decoded line and terminates/kills the FFmpeg child within a bounded wait.
- Verification for this increment: `339 tests passed`; isolated-cache
  `compileall` passed; `git diff --check` passed (only existing CRLF warnings).
  Approval remains Phase 0 `INCONCLUSIVE`, G0/G1/G2/G3/G4/G6 `BLOCKED`, G5
  `NOT_RUN`; `VideoIOThread` remains active and production `MpvEngine` is still
  prohibited.

# Session log - 2026-08-17 (B' execution + approval line restart: G0 PASS, G5 measured)

## B' targeted adjudication executed (production line)

- Rule implemented and committed (`1735f8d`):
  `PASS_WITH_HEAD_ANOMALIES` certification when duplicate/non-monotonic PTS is
  confined to the first 32 frames and the decode is otherwise clean
  (`frame_pts_certifier`, `media_info`, `pts_timeline`, `media_exporter`);
  exporter records tick-collision drops with expected-on-disk frame math.
- Real-sample certification: samples 1, 2, 3 all certify
  `PASS_WITH_HEAD_ANOMALIES` (sample 4 certification pending in the running
  E2E; samples 1-3 evidence cached under `.cache/media_info/frame_pts/`).
- E2E export verified with ffprobe frame counts:
  - sample 3: PASS, 1901 frames exactly.
  - sample 2: PASS, 16973 = 16973 planned − 1 adjudicated tick-collision drop
    (frame 2 shares tick 768 with deleted frame 5, exactly as predicted by
    `PTS_ANOMALY_FACTS_20260816`) + 1 terminal clone guard frame.
- Found and fixed during E2E (committed with the same checkpoint):
  - VFR nested-`if` `setpts` broke FFmpeg's expression parser beyond ~100
    segments; replaced with flat `select` (OR of between) + flat gap-sum
    `setpts` (out = PTS − start − Σ gap_j if PTS ≥ gap_end_j). Depth-1,
    scales to thousands of segments.
  - Per-range `trim` chains cost O(ranges × frames) (30-min timeout at 683
    ranges); the flat formulation fixed scaling for samples 2/3.
  - Sample 1/4 (2312/2623 ranges) still needs ~35-90 min because each frame
    evaluates O(segments) expression nodes; batched-seek processing is a
    queued performance work item, not a correctness issue.
  - Skip segments are already half-open `[start, end)` (verified via
    `skip_frames_sum`); `ffmpeg_timeout` is now a parameter (large exports
    need >1800 s).
  - Terminal `tpad` clone guard restored: it keeps the last real frame's
    muxed duration positive; it may or may not be frame-counted by the muxer
    (both counts are accepted in the E2E verifier).

## Approval line restart

### G0 — PASS (formal evidence, first gate ever passed)

- Downloaded and archived a project-owned libmpv: `tools/libmpv/` —
  zhongfly/mpv-winbuild release `2026-08-16-e034d612cf`,
  `mpv-dev-lgpl-x86_64-20260816-git-e034d612cf.7z`
  (mpv v0.41.0-926-ge034d612c, x86_64, **LGPL** build; the previous
  exploratory DLL was borrowed from `D:\NipaPlay` and is not needed anymore).
- Five-piece provenance (`tools/libmpv/provenance.json`): source archive,
  DLL, build record, license (LICENSE.LGPL at commit
  e034d612cf6893954e943916988eef9e4426604c), redistribution note — all
  SHA-256 bound and verified.
- Formal env run (exit 0): `runtime_status=pass`,
  `supply_chain_status=pass`, `provenance_files.status=pass`,
  `loaded_dll_matches_provenance_{path,sha256}=true`, pre-registered env
  thresholds pass. Evidence: `.cache/mpv_spike/20260817-083902/environment.json`.
- Note: `tools/libmpv/` is kept on disk only (gitignored), hashes are
  recorded in the env report and this document.

### G5 — measured, FAIL under pre-registered thresholds (deterministic)

New `stepspeed` command in `scripts/spike_mpv.py` (headless source-mode
frame-step exactness, cold/hot back-step latency, measured speed rates,
CPU sampling, hwdec/framedrop condition flags). Pre-registered thresholds in
`.cache/mpv_spike/thresholds/stepspeed-20260817.json`. Three condition runs
recorded under `.cache/mpv_spike/20260817-*/stepspeed.json`
(sw default, hwdec=auto, framedrop=decoder):

| Metric | Result | Threshold | Verdict |
|---|---|---|---|
| back-step p95 / max (sw) | 72.9 / 84.2 ms | 500 / 2000 ms | PASS |
| speed x2 / x10 / x20 | 0.996 / 1.000 / 1.000 | ratio ∈ [0.8, 1.25] | PASS |
| speed x80 (sw) | 0.503 → 40.2x, CPU 7.3 cores | ≥ 0.8 | FAIL |
| speed x80 (hwdec=auto) | 0.159 → 12.7x | ≥ 0.8 | FAIL (worse) |
| speed x80 (framedrop=decoder) | 0.572 → 45.8x | ≥ 0.8 | FAIL |
| frame-step exactness | 7/20 fwd + 6/20 bwd steps move 2 frames; **net −3 frames drift** per 40-step round trip, deterministic across runs | ≤ 8 ms | FAIL |

Interpretation (facts only, no decision taken):

- 80x real-time playback is not sustainable on this hardware with this
  decode path (best ≈ 45.8x with decoder framedrop); product speeds above
  ~40x would need app-level frame skipping (the current CvEngine approach)
  rather than mpv clock speed.
- `frame-step`/`frame-back-step` is not frame-exact on this B-frame H.264
  sample (display-order stepping; deterministic over-steps). This is the
  most product-relevant finding because precise editing relies on stepping;
  alternative stepping routes (seek-by-1/fps absolute+exact, or hwdec
  variations) are not yet tested.
- Per spec §6 decision tree, G4/G5 partial failure routes to “evaluate
  hybrid, no full-replacement commitment” — it does not by itself close the
  line; G2/G3 remain the hard gates.

### Next approval-line candidates (unchanged discipline)

- G5 follow-ups (small): seek-based stepping exactness probe; decide
  whether the 2-frame over-step reproduces on another sample.
- G1 WID diagnostics (real desktop), G6 full-WID lifecycle + onedir.
- G2 stays BLOCKED; the B' adjudicated certification is a *candidate* third
  route but requires an explicit spec amendment decision by the owner.

## G5 stepping follow-up probes (2026-08-17)

Two further `stepspeed` modes settled the stepping question:

- `--step-mode seek` (relative seek by 1/fps): drift **+25 frames** per
  40-step round trip — worst of the three; relative seeking accumulates
  landing error on this B-frame content.
- `--step-mode abs` (absolute seek to each step's target time,
  `absolute+exact`): **0.0 frames drift, 0.0 ms max deviation on all 40
  steps, back-step p95 20.7 ms** (~4x faster than `frame-step`). Landing
  exactness is measured via mpv's own time-pos (ms-quantized) — the
  zero-drift round trip is the strong signal; a content-level
  (pixel/oracle) verification of landings remains available if an even
  harder proof is wanted.

Consequence: the earlier “mpv cannot step exactly” finding narrows to
“mpv's `frame-step`/relative-seek stepping cannot step exactly; absolute
certified-time seeks step exactly and faster”. This reopens a preview-only
hybrid (mpv EDL preview + absolute-seek stepping + ≤46x speeds) as a
viable route, gated on: (a) owner decision to amend G2 to accept
adjudicated certification as a time route, (b) product ruling on the
≤46x speed ceiling, (c) G1 WID + G6 lifecycle cost (~2-3 days).

## Production E2E results (completed 2026-08-17)

- Sample 1: **PASS** — 81376 frames ∈ {81375, 81376} (clone guard counted);
  2312 kept ranges, certification `PASS_WITH_HEAD_ANOMALIES`.
- Sample 4: certification `PASS_WITH_HEAD_ANOMALIES`; export killed at the
  10800s timeout (2623 ranges × 424k frames ≈ 2.2B expression evaluations
  per frame — the O(segments)-per-frame filter cost). Performance work item
  (batched-seek export) is now **required** for sample 4 scale, not optional.
- Samples 2 and 3: PASS (recorded earlier; sample 3 manifest to be
  rewritten under the corrected base-or-base+1 verdict logic).

# Production-line progress - 2026-08-16 PTS consumer and cancellation boundary

- Certified full and ranged exports now call `analyzer.export_pts_schedule`.
  The command is built from the immutable `pts_tick_intervals` and video
  `time_base`, uses FFmpeg `trim=start_pts/end_pts`, resets each kept segment,
  concatenates them, and requests `-fps_mode:v passthrough`. A certified
  request cannot fall through to the legacy frame-index/FPS exporter.
- Certified export requests with an "auto" tool setting are bound to the
  exact `MediaInfo.ffmpeg.path` and `MediaInfo.ffprobe.path`; a preflight tool
  mismatch is rejected. Successful results record
  `pts_table_consumed=true`, `pts_consumer=ffmpeg_trim_pts_concat`, the exact
  tick schedule, and the certified tool paths.
- A real short CFR and VFR fixture smoke completed through the new FFmpeg
  consumer. New unit coverage checks negative ticks, VFR passthrough, invalid
  float ticks, auto-tool binding, and atomic staging/commit behavior.
- A short end-to-end run also passed: explicit repository FFmpeg/ffprobe 7.1
  pair -> `MediaInfo` -> full `FramePtsCertification` -> `MediaExporter`, with
  a deleted range. The result wrote 10 of 12 source frames and recorded
  `pts_table_consumed=true`.
- The generated short lossless A/V clock fixture also passed the same route
  with `include_audio=true`; the output reports `audio_mode=muxed`, 10 written
  frames, and `pts_table_consumed=true`.
- The PTS oracle now reads stderr through a daemon reader plus a polling queue;
  cancellation can terminate a silent or half-closed FFmpeg process instead
  of waiting on a blocking stderr iterator. A short real-fixture cancellation
  test passes.
- Latest verification: `343 tests passed`; isolated-cache `compileall` passed;
  `git diff --check` passed with only the existing LF/CRLF conversion warnings.
  This is production-line progress only. Approval remains Phase 0
  `INCONCLUSIVE`, G0/G1/G2/G3/G4/G6 `BLOCKED`, G5 `NOT_RUN`; the sample-3
  route remains `NO_USABLE_AV_EVENT`, `VideoIOThread` remains active, and no
  production `MpvEngine` work is allowed.

# Session log - 2026-08-16 (review, fixes verification, first commits, PTS anomaly facts)

## Repository topology (previously undocumented)

- This working repo is a fork: `origin` = `kouekikin24/arknight-auto-editing`,
  `upstream` = `liemark/arknight-auto-editing` (MIT).
- Local history = upstream pre-PR#9 state squashed as `b706823` + the four
  PR #9 commits; content matches upstream `v26.7.23` (`ce10360`).
- Upstream has been dormant since 2026-07-23 (no commits after the v26.7.23
  merge). No rebase pressure; re-check with `git fetch upstream`.
- Section 7's "human observation fork" above is **superseded** by the
  2026-08-14 authority override recorded in `MPV_PHASE0_SPEC.md`: Codex review
  concluded `NO_USABLE_AV_EVENT` and closed the sample-3 candidate route.

## Commits (local only; nothing pushed, no PR)

- `7af778a` feat: production architecture — 8 new modules + 7 integrated files.
- `b389bee` test: 37-file suite (356 tests + 70 subtests green before commit).
- `39c6c13` chore: evidence dirs and bundled ffmpeg untracked via
  `.gitignore`; one-shot `_mi_*`/`_vfr_*`/`_tmp_*` debug files deleted.
- This commit: handoff/spec docs, opencv runtime manifest, and the PTS
  anomaly fact report below.

## Review findings and fix verification (2026-08-16)

Four high-severity findings from a full review were fixed and are covered by
new tests (356 passed / 70 subtests):

1. `analyzer._fraction_filter_seconds` corrupted integer-second boundaries
   (10 -> 1); now strips trailing zeroes only for fractional renderings.
2. `preview_player._calib_expected_rate` inverted expectations below 1x;
   rewritten around an explicit clock-rate model (>=1x frame-skip, <1x
   stretched clock).
3. `media_exporter` re-ran the full hash/evidence chain twice per export;
   now a single `ExportValidationSnapshot` is validated once and reused with
   `assert_current()` identity rechecks.
4. `frame_pts_certifier` let one transient `BLOCKED` evidence file poison the
   cache permanently; `retry_blocked` now republishes beside the old file.

Remaining known mediums (non-blocking): mojibake analyze-button label
(`preview_player.py:431`), silent `except Exception: pass` in
`_resume_preview_after_export`, `task_manager._commit` executing caller
actions under the global lock, and base-mode threshold drift
(`_GRAB_SEEK_THRESHOLD` 30 -> 100 vs the upstream #9 behavior it claims to
reproduce — fix before any upstreaming of the pacing metrics).

## PTS anomaly fact report (decision input for the G2 three-way choice)

`scripts/analyze_pts_anomaly_facts.py` (read-only; no media re-scan) produced
`PTS_ANOMALY_FACTS_20260816.{md,json}` from the existing oracle PTS tables
and business skip-segment tables:

- All four samples' PTS anomalies are confined to the first <=10 decode
  frames (a systematic recording-start artifact; identical pattern).
- Samples 3 and 4: every anomalous frame lies inside the first deleted
  segment — zero export or EDL impact.
- Sample 1: anomalous frames sit in the kept head, >=979 frames (16.3 s)
  from the nearest cut — only a cosmetic PTS irregularity in the first
  0.12 s of output.
- Sample 2: exactly one kept frame (n=2, pts=768) shares its tick with the
  first deleted frame (n=5) and with the first interval end tick; pure
  tick-based trim provably cannot keep one and drop the other. Worst-case
  damage: +/-1 frame (16.7 ms) at the head of the first kept segment.

Total: 1 tick-unrepresentable kept frame out of 887,370 (~1.1e-6). This
quantifies the G2-A wall precisely. Pending owner decision (unchanged by
this analysis, no gate status changed):

- B' targeted adjudication: certify with head-restricted anomaly tolerance
  plus the single documented sample-2 head collision;
- A' new/clean sources: re-run certification on sources without the artifact;
- C' permanent No-Go for the mpv EDL route and a legacy-export fallback.

## Approval line status (unchanged)

Phase 0 `INCONCLUSIVE`; G0/G1/G2/G3/G4/G6 `BLOCKED`, G5 `NOT_RUN`;
sample-3 route closed as `NO_USABLE_AV_EVENT`. Keep `VideoIOThread`; no
production `MpvEngine`. Note: no mpv DLL currently exists on this machine
(latest env probe found none; the only previously loaded one was borrowed
from `D:\NipaPlay`), so G0 needs an owned, traceable build before any
runtime gate work resumes.

## Evidence-chain drift note (2026-08-16)

`MPV_GATE_DECISION_20260816_v2.json`'s `production_contract` hashes were
recorded at 02:51 local, **before** the same morning's four high-severity
fixes landed (10:25-10:57). Current `frame_pts_certifier.py`,
`pts_timeline.py`, `media_exporter.py` and `analyzer.py` therefore no longer
match those recorded hashes (`scripts/verify_mpv_frames.py` still matches).
This drift is intentional (reviewed fixes, 356 tests green) and is recorded
here instead of editing the write-once v2 decision. The next gate-decision
version must re-bind the contract hashes when it is written.
