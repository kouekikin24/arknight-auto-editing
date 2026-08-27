# HANDOFF — PyAV 整合（ffprobe/ffmpeg→PyAV，导出除外）
日期 2026-08-27 · 分支 `fix/preview-pacing-metrics` · 状态：**三阶段已实施完成，全部通过验收**

> 前序交接：HANDOFF_PREVIEW_ACCURACY.md（预览帧精确+指纹验戳）、RESEARCH_TIMESTAMP_PANORAMA.md（四维调研）。
> 本文档自包含，接手人读这一份即可继续实施。

---

## 0. 一句话现状（2026-08-27：ffprobe 已彻底退役）

整合**已完成**，且 **ffprobe.exe 已彻底退役**：元数据探测、分析解码、音轨探测全部走
PyAV；帧 oracle 与导出仍留 ffmpeg.exe；OpenCV 解码后端冻结。**认证从"双工具配对
(ffmpeg+ffprobe)"改绑为"仅 ffmpeg"**，schema 由 1→2，既有 4 张证已**就地重编码迁移**
（不重跑 oracle，逐字节保留 oracle 报告/pts 表/判定），迁移后全部 `certified=True`。
av 13.1（FFmpeg 7.x）与打包 ffmpeg.exe 7.1 的 `scale=area+gray` 输出逐位一致。
全程单测绿（408 + 90 子测试）。
**注意**：`tools/.../ffprobe.exe` 是 gitignored 的 87MB 本地二进制，代码已完全不引用；
是否物理删除留给 owner 决定（见 §10）。

---

## 1. 任务目标与已拍板的决定

**目标**：把 `ffprobe.exe` 彻底退役；`ffmpeg.exe` 除"导出/帧 oracle"外的用途整合进 PyAV；OpenCV 解码后端冻结不再维护。

| 决定 | owner 拍板 | 落地状态 |
|---|---|---|
| 帧 PTS oracle / 认证表 | **留 ffmpeg.exe** | ✅（认证改绑仅 ffmpeg，schema v2） |
| 分析解码 | **迁 PyAV** | ✅ |
| 元数据探测 / 音轨探测 | **迁 PyAV** | ✅ |
| ffprobe.exe | **彻底退役** | ✅（代码清零 + 4 证迁移） |
| PyAV 版本 | **锁 13.x 对齐 ffmpeg.exe 7.1** | ✅ |
| 导出 | **留 ffmpeg.exe** | ✅ |
| OpenCV 解码后端 | **冻结** | ✅ |

### 关键约束（历史 → 现状）
历史：帧 oracle 认证把 **ffmpeg+ffprobe 两个 sha256 都写进认证文件名**，强制双工具配对，故"二进制不能删"。
**现状（2026-08-27 已改）**：认证改绑**仅 ffmpeg**（`_ffmpeg_tool_verified`），文件名去掉
ffprobe 段、schema 1→2；既有 4 证已就地重编码迁移（`.cache/migrate_certs_v2.py`，不重跑
oracle）。帧 oracle 本身只用 `verify_mpv_frames.build_ffmpeg_command`（ffmpeg-only），
所以去 ffprobe 不影响 oracle 证据。

---

## 2. 版本对齐（已实施并实证，结论落定）

| 组件 | FFmpeg 代际 | libavcodec | `scale=area` vs ffmpeg.exe 7.1 |
|---|---|---|---|
| ffmpeg.exe / ffprobe.exe | 7.1 | 61 | 基准 |
| av 18.1.0（整合前的环境） | 8.x | 62 | **±1 灰阶漂移**（实测，0 分类翻转） |
| **av 13.1.0（已安装并锁定）** | 7.x | **61 ✅** | **逐位一致**（全片验证） |

- **已做**：`pyproject.toml` 依赖锁定 `av~=13.1`（13.x）。理由：实测证明 14+（FFmpeg 8 代，
  libswscale 9）相对 7.1 有 ±1 灰阶缩放漂移；13.x（libswscale 8）与 7.1 同代，输出逐位一致。
- **⚠ 工具链实情（与原假设不同，接手人须知）**：本机**没有 uv**，av 13.1.0 装在**全局**
  Python 3.11（无 venv）。仓库里 `uv.lock`(98KB)/`requirements.txt` 是 2026-07-15 的过期产物
  （生成于加 av 依赖之前），**与当前 pyproject 不同步**，且无 uv 可用无法 `uv lock`。
  当前安装/回退用 pip：`python -m pip install av==13.1.0`（回退 `av==18.1.0`）。
- **降级安全验证（已做）**：av 13.1 下复跑 `.cache/probe_hash_verify.py` → **27 点 ALL PASS**
  （含用 av18 建的指纹账本在 av13 解码下逐位通过，证明 H.264 解码+指纹管线跨版本位稳）；
  全量单测 415 + 90 子测试绿。
- 候选 wheel 备份：`/tmp/avcheck_13.1.0/av-13.1.0-cp311-cp311-win_amd64.whl`（13.1.0=avcodec-61）。

---

## 3. 三阶段实施计划（全部完成 ✅）

> ⚠️ **本节及 §4/§5 是"退役前"的设计快照**（当时还保留 ffprobe 元数据回退与双工具配对）。
> 2026-08-27 之后已**彻底退役 ffprobe**：元数据/分析/音轨全走 PyAV，无元数据 CLI 回退，
> 认证改绑仅 ffmpeg（schema v2）。**当前真实状态以 §0 / §1 / §10 为准。**
>
> 落地摘要：元数据探测与分析解码都默认走 PyAV；帧 oracle、导出、CvEngine、预览静帧链未动。
> - **回退开关**：仅 `ARKNIGHT_A_PT_IMPL=ffmpeg`（分析解码回退到 ffmpeg.exe CLI）。
>   元数据无回退（PyAV 唯一；原 `ARKNIGHT_MEDIA_PROBE=ffprobe` 已随退役删除）。
> - **后端标签**（settings_panel.py）：维持 "FFmpeg软件 A_PT（默认）" 不改名——PyAV 即 FFmpeg
>   库，名义成立；分发逻辑（`"A_PT"/"FFmpeg" in label`）不受影响。
> - **验证探针**（在 `.cache/`，已 .gitignore，不进版本库）：
>   `probe_pyav_metadata.py`、`probe_mediainfo_backend_diff.py`、`probe_scale_area_drift.py`、
>   `probe_analyze_fullfile_equiv.py`、`probe_certify_pyav.py`、`migrate_certs_v2.py`。

### 阶段一｜元数据探测：ffprobe → PyAV（现已唯一走 PyAV）✅
- PyAV 探测，产出与旧 `parse_ffprobe_json` **逐字段一致**的 `MediaInfo`。
- **只保留** `_verify_tool`(ffmpeg) 的 ToolInfo 采集（供认证绑定）；**退役时连 ffprobe 元数据
  回退也一并删除**（见 §0）。
- **落地**：`probe_media`（唯一入口）+ `_pyav_probe_payload`（media_info.py），把 PyAV 元数据
  拼成 ffprobe 形状的 dict 喂给现有 `parse_ffprobe_json`，复用全部校验。流/容器 `duration` 用
  `_round_to_microsecond` 对齐 µs 表示。
- **验收（实测）**：1/2/3/4.mp4 四个样本（含坏时间戳 2.mp4）ffprobe 路径与 PyAV 路径的
  `MediaInfo.as_dict()` **逐字段 IDENTICAL**（退役前的对照验证）。

### 阶段二｜分析解码：A_PT(ffmpeg CLI) → PyAV + 一致性验证 ✅
- 已实现 `_analyze_video_pyav_filter`（analyzer.py），经 `analyze_video_with_context` 分发；
  `ARKNIGHT_A_PT_IMPL` 默认 `pyav`，可回退 `ffmpeg`。
- **一致性验证（硬门槛）实测结果**：
  - 片段级（`probe_scale_area_drift.py`）：av18 对 7.1 有 ±1 灰阶漂移但 **0 分类翻转**；
    换 av13.1 后 3.mp4(1000 帧)/2.mp4(3000 帧) **逐帧 bit-identical**。
  - 全片级（`probe_analyze_fullfile_equiv.py`，2.mp4 184293 帧）：帧数、`states`、`diffs`
    **全部逐元素相同**（max_abs_delta=0.0），VERDICT PASS。未放宽任何阈值。
- PyAV 解码→`scale={pw}:{ph}:flags=area`→`gray`，替换 `_ffmpeg_sw_passthrough_cmd`+`_analyze_video_ffmpeg_sw_passthrough`；`_classify_gray` 及下游**不动**。
- 关键对齐：`flags=area` 须与 ffmpeg 7.1 的 swscale area 一致（版本对齐后应近逐像素）。
- 验收（**硬门槛**）：真实视频分别跑旧 A_PT 与新 PyAV，逐帧比对 `states/diffs/scores`，不得越过分类阈值；不达标→调缩放实现或回退，**绝不放宽阈值**。

**⚠ 阶段二必须吃透的现状结构（`_analyze_video_ffmpeg_sw_passthrough`, analyzer.py:566-735）**：
1. **帧数 oracle = OpenCV `CAP_PROP_FRAME_COUNT`**（:585-586）——这是"帧数权威"，不是要冻结的解码后端；但它常**高估**，靠 EOF 兜底（见 5）。迁 PyAV 后帧数口径必须与认证表一致，**不得漂**。
2. **ffmpeg 子进程 → stdout 管道**，逐帧读 `bpf=pw*ph` 字节 raw 灰（`_read_exact`）。迁 PyAV 后不再走管道——**实际落地用 `av.filter.Graph`**（`buffer→scale={pw}:{ph}:flags=area→format=gray→buffersink`），逐帧 push/pull，零拷贝读 Y 平面。选 av.filter 而非 `frame.reformat` 是因为 reformat 用默认双线性、无法指定 `flags=area`，只有 av.filter 能逐字复刻 CLI 滤镜链、保证逐位一致。
3. **分类在 `ProcessPoolExecutor`**：`_worker_init(configs,thresholds,proc_res)` + `_worker_classify_gray`/`_worker_classify_gray_scored`，按 `chunk=max(4,len//(n_workers*2))` 分发。**这套并行分发保留不变**。
4. **`diffs` 在主循环算**（:666 `cv2.mean(cv2.absdiff(gray, prev_gray))`），不在 worker；`cv2.absdiff` 属成像不属解码，**照旧保留**。
5. **EOF 兜底**：`got<total` 时接受实际流长（:710-716"metadata 常高估 FRAME_COUNT"）。迁 PyAV 后要保留"以实际解出帧数为准"的语义。
6. `states` 预分配 `total`、按 index 填；`_BoundaryTracker`(want_context)、`diag_scores/diag_luma`(want_diagnostics) 行为须保持。

### 阶段三｜OpenCV 解码后端冻结 ✅
- `_analyze_video_opencv` 已加冻结 docstring（"FROZEN decode backend — kept as a fallback,
  no longer maintained"）；默认后端为 A_PT（现为 PyAV 实现）。
- **cv2.matchTemplate（识别本体）与成像/预览照旧**——不是"解码后端"，不在冻结范围。
- CvEngine/VideoIOThread 遵守长期约束，保留。

### 明确不动
帧 oracle（ffmpeg.exe）、导出（ffmpeg.exe）、打包的 ffmpeg/ffprobe 二进制、CvEngine、预览静帧 PyAV 主链（现已是）。

### 连带要拍板/留意的两处
1. **后端标签与分发**（settings_panel.py:95,102-104,423-424）：现下拉是
   `"FFmpeg软件 A_PT（默认）" / "OpenCV（回退）"`，分发按标签含 "A_PT/FFmpeg" 走
   `ffmpeg_sw_passthrough`。当 A_PT 的**实现换成 PyAV** 后，标签"FFmpeg软件 A_PT"
   名义上仍成立（PyAV 也是 FFmpeg），但要决定是否改名（如"FFmpeg(PyAV) A_PT"）
   以免误导。**属低危连带项，实施时顺手定**。
2. **回退策略（建议做）**：探测与分析都建议留一个**开关/环境变量**，能在
   "PyAV 路径"与"原 ffprobe/ffmpeg 路径"间切换。万一 PyAV 探测或解码在某视频上
   异常，可一键退回旧路径，不至于卡死核心功能。核心管线改动，务必留后路。

### 阶段一"逐字段一致"的口径
- **必须严格一致**（语义字段）：`avg_frame_rate / r_frame_rate / duration / 帧数(nb_frames) /
  time_base / sample_rate / channels / has_audio / start_pts / duration_ts / vfr_status`。
- **允许措辞差异**（信息字段）：`codec_long_name / format_long_name` 等描述串，
  ffprobe 与 PyAV 措辞可能不同，不参与一致性判定。
- 验证视频：`D:\qq下载\920\2.mp4`（坏时间戳主样本）；如需更快迭代，可另找短视频，
  但最终验收必须含 2.mp4 这种坏时间戳文件。

---

## 4. 实施所需代码地图（字段/函数/行号，均已读透）

### media_info.py
| 项 | 行 | 说明 |
|---|---|---|
| `probe_media` | 1109 | 主入口：`_verify_tool`(ffprobe+ffmpeg)→spawn ffprobe→`parse_ffprobe_json` |
| ffprobe 调用 | 1139 | `[ffprobe,-v,error,-print_format,json,-show_format,-show_streams,src]` |
| `parse_ffprobe_json` | 972 | → `_parse_payload`(938)→`_parse_video`(861)/`_parse_audio`(904)→`MediaInfo` |
| `VideoStreamInfo` | 560 | 字段见 §5 |
| `AudioStreamInfo` | 598 | 字段见 §5 |
| `MediaInfo` | 634 | 字段见 §5 |
| `_tool_pair_verified`/`_require_tool_pair` | 519/544 | 工具配对（**保留**） |
| `_verify_tool` | 1082 | 取 version+sha256（**保留**） |
| `resolve_ffmpeg_path`/`resolve_ffprobe_path` | 1058/1030 | 路径解析 |
| 验证助手 | 788-857 | `_required_int/_required_text/_optional_fraction/_optional_frame_rate/_optional_int/_optional_text` |

### analyzer.py
| 项 | 行 | 说明 |
|---|---|---|
| `_ffmpeg_sw_passthrough_cmd` | 413 | A_PT 命令：`-vf scale={pw}:{ph}:flags=area,format=gray` + `-fps_mode passthrough` + `-frames:v N` |
| `_analyze_video_ffmpeg_sw_passthrough` | 566-735 | A_PT 分析主流程（**阶段二替换对象**）；帧数 oracle:585、管道读帧:648、并行分类:631、diffs:666、EOF 兜底:710（结构详见 §3 阶段二） |
| `_read_exact` / `_worker_init` / `_worker_classify_gray(_scored)` | — | 管道定长读 / worker 初始化 / 分类函数（**并行分发保留**） |
| `_analyze_video_opencv` | 440 | OpenCV 分析（**阶段三冻结**） |
| `DECODE_BACKEND_*` | 330-340 | 后端常量（opencv/ffmpeg_sw_passthrough/a_pt 别名） |
| 导出快速滤镜 / 刻度精确 / 逐帧兜底 | ~1500 / 1644 / ~2110 | **导出，不动** |

### frame_pts_certifier.py（oracle，不动，但要知道耦合）
| 项 | 行 | 说明 |
|---|---|---|
| 工具配对要求 | 73 | `ffprobe/ffmpeg 非空且 tool_pair_verified` |
| 认证文件名 | 105-106 | `{ffmpeg.sha256[:16]}-{ffprobe.sha256[:16]}.json` |
| oracle 命令 | 209/664 | `scripts/verify_mpv_frames.build_ffmpeg_command`（ffmpeg 全量解码+showinfo） |

### preview_player.py（上一轮成果，勿回退）
- `_pyav_frame_hash64`（~1285）：Y 平面 `[::16,::16]` blake2b-64，建表+验戳共用。
- `_still_decode_pyav`：计数定帧+指纹验戳校正。**降级 av 后需验证此链仍绿。**

### settings_panel.py（连带，见 §3"连带要拍板"）
| 项 | 行 | 说明 |
|---|---|---|
| 后端下拉 | 95,102-104 | `"FFmpeg软件 A_PT（默认）" / "OpenCV（回退）"` |
| 标签分发 | 423-424 | 标签含 `A_PT/FFmpeg/ffmpeg` → 走 ffmpeg_sw_passthrough |

---

## 4b. ⚠⚠ 测试冲击（阶段一最大的坑，务必先读）

`tests/test_media_info.py`（**80 处**引用 probe/ffprobe/parse）分两类，命运不同：

| 测试类 | 行 | 测什么 | 阶段一影响 |
|---|---|---|---|
| `MediaInfoParsingTests` | 123 | 直接喂 JSON 给 `parse_ffprobe_json` 测**解析** | 若保留 `parse_ffprobe_json` 函数则**照旧通过**；建议保留该函数不删 |
| `MediaInfoProbeTests` | 567 | **mock `media_info.subprocess`** 断言 probe_media 的 ffprobe 命令/版本绑定 | **必崩**：`test_probe_binds_tool_paths_and_hashes`(588 用 `side_effect=[ffprobe_ver,ffmpeg_ver,completed]` 恰好 3 次 subprocess)、`test_probe_failure_has_structured_code`(614)、`test_probe_rejects_mixed_tool_versions`(630)、`test_source_mutation_during_probe_is_rejected`(665) |

**接手人必须**：探测迁 PyAV 后重写 `MediaInfoProbeTests`——版本验证 `_verify_tool` 仍走 subprocess（保留），元数据改 PyAV；相应地把"第 3 次 subprocess=读元数据"的断言改为对 PyAV 的 mock/真实调用。其余测试（`test_export_preflight` / `test_pts_timeline` / `test_preview_media_info` 等）只吃 `MediaInfo` 数据结构，字段一致即可过。

---

## 5. PyAV 元数据字段映射（阶段一对照表）

`VideoStreamInfo`（来自 ffprobe `-show_streams` 的 video 项）：
`index, codec_name, codec_long_name, width, height, pixel_format(pix_fmt), time_base, start_time, duration, avg_frame_rate, r_frame_rate, frame_count(nb_frames), start_pts, duration_ts, validation_errors`

`AudioStreamInfo`：
`index, codec_name, codec_long_name, sample_rate, channels, channel_layout, sample_format(sample_fmt), time_base, start_time, duration, bit_rate, start_pts, duration_ts, validation_errors`

`MediaInfo`：
`source_path, source_sha256, source_size, source_mtime_ns, format_name, format_long_name, duration(format.duration), start_time(format.start_time), video_streams, audio_streams, vfr_status, frame_pts_authoritative, frame_pts_certification, validation_errors, ffprobe(ToolInfo), ffmpeg(ToolInfo)`

**vfr_status 逻辑**（`parse_ffprobe_json:996-1003`）：avg 或 r 为 None→`unknown`；相等→`rate_match`；不等→`rate_mismatch`。

**PyAV 侧取值提示**（阶段一实现用）：
`container=av.open(path)`；`container.format.name/.duration`；video stream `codec_context.name/width/height/format.name(pix_fmt)/time_base/average_rate/guessed_rate/frames/duration/start_time`；audio stream `codec_context.name/rate/channels/layout/format.name/duration/start_time`。**注意**：坏时间戳文件上 `avg_frame_rate / nb_frames` 的算法可能与 ffprobe 有细微差异——这正是阶段一验收要盯的。

---

## 6. 本轮累积知识（防压缩丢失，结论性）

- **PyAV GPU 编码已跑通**：`output.add_stream("h264_nvenc",rate=fps,hwaccel=HWAccel(device_type="cuda"))`+`pix_fmt="cuda"`，喂普通 yuv420p 帧即可；RTX 4060 双重验证（合法 h264/30 帧/渐变正确）。证据 `.cache/research/pyav_nvenc_test.mp4`。**注意**：该新 `hwaccel` 接口是 av18 的，降到 13.x 可能没有——无碍，导出留 ffmpeg。
- **导出三模式**：快速滤镜 / 刻度精确(`_pts_select_setpts_video_filter`) / 逐帧兜底(OpenCV 解码+ffmpeg pipe)。PyAV 化导出的真实差距=**音频混音(~100 行,最险)+性能倒退+重新验证**，非"挑选/拼接"（逐帧兜底已是 Python 版）。结论：**导出继续用 ffmpeg.exe**。
- **工具链三 FFmpeg 版本**：OpenCV=4.4 / ffmpeg.exe=7.1 / PyAV=7.x（av13.1，已对齐 7.1）。身份关键路径（指纹）已全走 PyAV 自洽。
- **OpenCV 不可替代**：`cv2.matchTemplate`(analyzer.py:86,125) 是暂停/变速检测算法本体（认画面），ffmpeg/PyAV 只解码不识别。

---

## 7. 执行清单（已全部完成 ✅）

1. ✅ **改依赖**：`pyproject.toml` 锁 `av~=13.1`；装 `av==13.1.0`（libavcodec==61 已验）；
   复跑 `.cache/probe_hash_verify.py` → 27 点 ALL PASS（指纹链在 13.x 绿）。
2. ✅ **阶段一**：`probe_media_pyav` + 逐字段对比（4 样本 IDENTICAL）→ 默认切 PyAV。
3. ✅ **阶段二**：`_analyze_video_pyav_filter`（av.filter area+gray）→ 片段+全片一致性
   （184293 帧 states/diffs 逐元素相同）→ 默认切 PyAV。
4. ✅ **阶段三**：`_analyze_video_opencv` 加冻结 docstring。
5. ✅ **全程** `python -m pytest tests/ -q` → 415 passed + 90 subtests。
6. ✅ **更新**本文档 + HANDOFF_PREVIEW_ACCURACY.md 对应小节。

---

## 8. 风险与约束（实测后结论）
- **阶段二缩放漂移**：已实证——av13.1 与 7.1 逐位一致（全片 184293 帧），风险消除。
  av18 有 ±1 灰阶漂移（0 分类翻转），故锁定 13.x。
- **PyAV 元数据差异**：已实证——坏时间戳 2.mp4 上 fps/帧数/时基与 ffprobe 完全一致，无差异。
- **ffprobe 退役**：完成（代码清零 + 4 证迁移）；认证现仅绑 ffmpeg。
- **指纹链兼容**：av 降级后复跑 27 点验证已绿（含跨版本账本）。
- **长期约束**：解码后端优先 A_PT；0.2X/夹心推迟；不 push upstream；不用 `git reset --hard` 等；
  owner"主动停止"即停且清后台进程。

---

## 9. 提交记录与边界
- **指纹批次**：`bf621f2` feat: fingerprint-verified paused stills via PyAV count+hash
  positioning; A_PT default backend（6 文件 + 3 文档，排除 .zcode/）。
- **阶段一**：`bd8628f` feat: PyAV metadata probe backend with field-identical MediaInfo and
  env rollback switch（media_info.py + test_media_info.py）。
- **阶段二+三+依赖锁**：`937645e` / `43ea61b`（analyzer.py PyAV 分析解码、冻结标注、
  pyproject 锁 av~=13.1、media_info 默认切 PyAV）。
- **ffprobe 退役**：本次提交（认证改绑仅 ffmpeg/schema v2、4 证迁移、删全部代码/测试/脚本的
  ffprobe、音轨探测迁 PyAV）。
- 验证探针 + 迁移脚本在 `.cache/`（.gitignore，不入库）。测试基线：408 passed + 90 subtests。

---

## 10. 遗留给 owner 的决定（2026-08-27）
1. **是否物理删除 `tools/.../ffprobe.exe`（gitignored，87MB）**：代码已完全不引用，它现在是
   孤儿文件。我未删除（非我创建、删除不可逆）——留/删由你定；留也无害。
2. **帧数权威保留 OpenCV `CAP_PROP_FRAME_COUNT`**（未迁 PyAV）：PyAV `stream.frames` 可能
   **低估**实际帧数 → 会截断分析；而 cv2 只会高估、靠 EOF 兜底，安全。若你要彻底去这个
   OpenCV 依赖，需先验证 PyAV frames≥实际（否则回退），可后续做。
3. **第 5 张孤儿证**：`.cache/.../frame_pts/38edc85f.../v1-*.json` 对应
   `.cache/mpv_spike/pts_fixtures/cfr.mp4`（12 帧测试夹具，非生产样本）。迁移脚本只处理了
   4 个生产样本，这张未迁。无测试加载它（单测全绿），属无害遗留；若要彻底可补迁或删除。
4. **0.2X 裁剪 / 夹心并入**：仍为后续方向，未动。
