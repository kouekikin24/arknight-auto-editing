# HANDOFF — PyAV 整合（ffprobe/ffmpeg→PyAV，导出除外）
日期 2026-08-27 · 分支 `fix/preview-pacing-metrics` · 状态：**方案已批准、未动代码**（准备压缩上下文）

> 前序交接：HANDOFF_PREVIEW_ACCURACY.md（预览帧精确+指纹验戳）、RESEARCH_TIMESTAMP_PANORAMA.md（四维调研）。
> 本文档自包含，接手人读这一份即可继续实施。

---

## 0. 一句话现状

整合方案已获 owner 批准、尚未动工。核心决定：**ffprobe 探测 + ffmpeg 分析解码迁进 PyAV；帧 oracle 与导出留在 ffmpeg.exe；OpenCV 解码后端冻结；PyAV 降级到 13.x 与 ffmpeg.exe 7.1 同代对齐（owner"按你的建议"）**。因帧 oracle 把两个工具的 sha256 写进认证，**二进制不能删**——本次整合 = 统一"调用方式"到 PyAV，不是删工具。

---

## 1. 任务目标与已拍板的决定

**目标**：把 `ffprobe.exe` 的探测、以及 `ffmpeg.exe` 除"导出/帧 oracle"外的用途整合进 PyAV；OpenCV 解码后端冻结不再维护。

| 决定 | owner 拍板 |
|---|---|
| 帧 PTS oracle / 认证表 | **留 ffmpeg.exe**（稳；防篡改地基） |
| 分析解码 | **迁 PyAV** + 分类一致性验证 |
| PyAV 版本 | **降到 13.x 对齐 ffmpeg.exe 7.1**（"按你的建议"） |
| 导出 | **留 ffmpeg.exe** |
| OpenCV 解码后端 | **冻结**（保留代码、不维护；cv2.matchTemplate 例外照旧） |

### 关键约束（必须认清，别踩）
帧 oracle 认证把 **ffmpeg.exe 与 ffprobe.exe 的 sha256 都写进认证文件名**
（`frame_pts_certifier.py:105-106`），且强制 `tool_pair_verified`（`frame_pts_certifier.py:73`）。
所以只要 oracle 保留，**这两个二进制必须继续随包**。
→ 整合的本质 = 减少"要维护的 CLI 命令图 / 调用方式"，不是删二进制。这是保留 oracle 的固有代价，已告知 owner。

---

## 2. 版本对齐（已实证，接手直接用）

| 组件 | FFmpeg 代际 | libavcodec |
|---|---|---|
| ffmpeg.exe / ffprobe.exe | 7.1 | 61 |
| **当前 av 18.1.0** | 8.x | 62 |
| **av 13.1.0（拆 wheel 实证）** | 7.x | **61 ✅ 匹配** |

- **要做**：`pyproject.toml` 现约束 `av>=14,<19` **恰好排除 13.x**，需改为允许 13.x（建议 `av>=13,<19` 或 `av~=13.1`），装 13.1.0，并验证 `av.library_versions['libavcodec'] == (61, …)`。
- **⚠ 有锁文件**：仓库带 `uv.lock`（98KB）+ `requirements.txt`。改 `pyproject.toml` 后**必须同步锁文件**（`uv lock` / `uv sync`），否则依赖不一致。上一轮加 `av>=14,<19` 时若没更锁，这里要一并补齐。
- **为何对齐**：H.264 解码像素规范锁定、跨版本一致；真正怕版本差的是 **libswscale（缩放）**。阶段二用 `scale=area+gray`，同代 swscale → 缩放近逐像素一致 → 分类不漂，从根上消掉阶段二最大风险。
- **代价（已认）**：丢 av18 的 `hwaccel` 新接口（无关紧要，导出留 ffmpeg）；13.x 较旧但基础能力（解码/探测/取帧）够稳。
- **注意**：上一轮的指纹验戳 `_pyav_frame_hash64`（preview_player.py:1285 区）用 `frame.planes[0].buffer_ptr/buffer_size/line_size/width`——属 PyAV 老接口，13.x 应兼容，**降级后需实跑验证**（`.cache/probe_hash_verify.py` 可复跑）。
- 已下载的候选 wheel 在 `/tmp/avcheck_13.1.0/`（13.1.0=avcodec-61 已验）。

---

## 3. 三阶段实施计划（已批准，未动工）

### 阶段一｜元数据探测：ffprobe → PyAV
- 新增 PyAV 探测，产出与 `parse_ffprobe_json` **逐字段一致**的 `MediaInfo`。
- **保留** `_verify_tool`(ffprobe+ffmpeg) 的 ToolInfo 采集（供 oracle 绑定），只是**不再 spawn ffprobe 读元数据**。
- 验收：对测试视频（`D:\qq下载\920\2.mp4` 等）逐字段一致——fps/时长/帧数/音频/时基。

### 阶段二｜分析解码：A_PT(ffmpeg CLI) → PyAV + 一致性验证
- PyAV 解码→`scale={pw}:{ph}:flags=area`→`gray`，替换 `_ffmpeg_sw_passthrough_cmd`+`_analyze_video_ffmpeg_sw_passthrough`；`_classify_gray` 及下游**不动**。
- 关键对齐：`flags=area` 须与 ffmpeg 7.1 的 swscale area 一致（版本对齐后应近逐像素）。
- 验收（**硬门槛**）：真实视频分别跑旧 A_PT 与新 PyAV，逐帧比对 `states/diffs/scores`，不得越过分类阈值；不达标→调缩放实现或回退，**绝不放宽阈值**。

**⚠ 阶段二必须吃透的现状结构（`_analyze_video_ffmpeg_sw_passthrough`, analyzer.py:566-735）**：
1. **帧数 oracle = OpenCV `CAP_PROP_FRAME_COUNT`**（:585-586）——这是"帧数权威"，不是要冻结的解码后端；但它常**高估**，靠 EOF 兜底（见 5）。迁 PyAV 后帧数口径必须与认证表一致，**不得漂**。
2. **ffmpeg 子进程 → stdout 管道**，逐帧读 `bpf=pw*ph` 字节 raw 灰（`_read_exact`）。迁 PyAV = 改成迭代 `container.decode` 拿帧、`frame.reformat/resize` 成 (ph,pw) gray——**不再走管道**。
3. **分类在 `ProcessPoolExecutor`**：`_worker_init(configs,thresholds,proc_res)` + `_worker_classify_gray`/`_worker_classify_gray_scored`，按 `chunk=max(4,len//(n_workers*2))` 分发。**这套并行分发保留不变**。
4. **`diffs` 在主循环算**（:666 `cv2.mean(cv2.absdiff(gray, prev_gray))`），不在 worker；`cv2.absdiff` 属成像不属解码，**照旧保留**。
5. **EOF 兜底**：`got<total` 时接受实际流长（:710-716"metadata 常高估 FRAME_COUNT"）。迁 PyAV 后要保留"以实际解出帧数为准"的语义。
6. `states` 预分配 `total`、按 index 填；`_BoundaryTracker`(want_context)、`diag_scores/diag_luma`(want_diagnostics) 行为须保持。

### 阶段三｜OpenCV 解码后端冻结
- `_analyze_video_opencv` 保留但标注"冻结/不再维护"；默认后端维持 A_PT（其后即 PyAV）。
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
- **工具链三 FFmpeg 版本**：OpenCV=4.4 / ffmpeg.exe=7.1 / PyAV=8.x（本次拟降到 7.x 对齐）。身份关键路径（指纹）已全走 PyAV 自洽。
- **OpenCV 不可替代**：`cv2.matchTemplate`(analyzer.py:86,125) 是暂停/变速检测算法本体（认画面），ffmpeg/PyAV 只解码不识别。

---

## 7. 接手人按序执行清单

1. **改依赖**：`pyproject.toml` `av>=14,<19` → 允许 13.x；装 `av==13.1.0`；验证 `av.library_versions['libavcodec']` 首元素==61；复跑 `.cache/probe_hash_verify.py` 确认指纹链在 13.x 仍绿。
2. **阶段一**：写 PyAV 探测函数 → 与现 `probe_media`(ffprobe) 逐字段对比脚本（`2.mp4` 等）→ 一致后接线。
3. **阶段二**：PyAV 分析解码（area+gray）→ 旧 A_PT vs 新 PyAV 逐帧 `states/diffs/scores` 一致性验证 → 通过再接线。
4. **阶段三**：标注 `_analyze_video_opencv` 冻结。
5. **全程** `python -m pytest tests/ -q` 保持 409+90 绿。
6. **更新**本文档 + HANDOFF_PREVIEW_ACCURACY.md 的对应小节。

---

## 8. 风险与约束
- **阶段二缩放漂移**：对齐 7.1 后应大幅缓解，仍需逐帧一致性验证把关（最大风险）。
- **PyAV 元数据差异**：坏时间戳文件上 fps/帧数可能与 ffprobe 不同（阶段一验收把关）。
- **二进制不能删**：oracle 绑两工具 sha256（已说明）。
- **指纹链兼容**：av 降级后 `_pyav_frame_hash64` 需实跑验证。
- **长期约束**：解码后端优先 A_PT；0.2X/夹心推迟；不 push upstream；不用 `git reset --hard` 等；owner"主动停止"即停且清后台进程。

---

## 9. 未提交改动与边界
- 上一轮成果（指纹验戳+调研文档）**仍未提交**，等 owner 重启验收：改动集=`mpv_engine.py / preview_player.py / pyproject.toml / settings_panel.py / tests/test_mpv_engine.py / tests/test_preview_fps_osd.py / HANDOFF_PREVIEW_ACCURACY.md / RESEARCH_TIMESTAMP_PANORAMA.md`。
- **本整合任务尚未改任何代码**。注意它也需改 `pyproject.toml`(av 版本)、`media_info.py`、`analyzer.py`——与上面未提交集有 `pyproject.toml` 交叠，**建议先让 owner 验收提交上一轮，再开整合分支/提交**，避免混在一起。
- 测试基线：`python -m pytest tests/ -q` → 409 passed, 90 subtests。
