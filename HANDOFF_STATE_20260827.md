# HANDOFF — 当前状态（2026-08-27，上下文压缩前快照）

> 本文是**压缩上下文前的权威现状文档**，自包含。接手人先读这一份；
> 细节可再查 `HANDOFF_PYAV_CONSOLIDATION.md`（整合+退役全过程）、
> `HANDOFF_PREVIEW_ACCURACY.md`（预览定帧+指纹）、`RESEARCH_TIMESTAMP_PANORAMA.md`（时间戳调研）。
> `HANDOFF_CURRENT.md` / `HANDOFF_TIMING.md` 为更早期的历史快照，内容部分已过时。

---

## 项目速览（接手先看这里）

**这个项目是什么**：明日方舟可视化"剪暂停 + 变速"工具（纯 Python + Tk GUI，入口 `main.py`）。
用文件夹里的模板图片识别视频每一帧的状态（暂停/1x/2x/0.2x/正常），按暂停前后帧差异决定
暂停是否保留，然后自动剪掉无效片段并导出。详见 `README.md`。
**怎么跑**：`python main.py`（依赖见 `pyproject.toml`，`av` 锁 13.1；GUI 需 Tk）。

**运行管线（一次完整流程的顺序）**：
打开视频 → **探测**（PyAV 出 `MediaInfo`）→ **认证**（ffmpeg oracle 出帧 PTS 证）→
**分析**（PyAV 解码 + `cv2.matchTemplate` 模板匹配，出 `states`/`diffs`）→
**预览**（mpv / Cv 引擎）→ **导出**（ffmpeg 按删除区间拼接出片）。

**名词**：`A_PT` = 分析解码后端 "ffmpeg_sw_passthrough" 的别名，**现在底层已是 PyAV**
（`_analyze_video_pyav_filter`），`ARKNIGHT_A_PT_IMPL=ffmpeg` 可切回 ffmpeg CLI。

**文档导航**：本文件 = 权威现状。`HANDOFF_PYAV_CONSOLIDATION.md`（整合+退役细节）
与 `HANDOFF_PREVIEW_ACCURACY.md`（预览定帧+指纹）是配套细节；
`HANDOFF_CURRENT.md` / `HANDOFF_TIMING.md` / `HANDOFF_PREVIEW.md` /
`AV_REVIEW_AUTOVERIFICATION.md` / `CLAUDE_*.md` 均为**更早期历史快照，多处已过时**，
与本文件冲突时**以本文件为准**。

---

## 0. 一句话现状

**ffprobe.exe 已彻底退役并物理删除**。元数据探测、分析解码、音轨探测全部走 **PyAV**；
帧 oracle 与导出仍留 **ffmpeg.exe**；OpenCV 解码后端冻结。帧认证从"双工具配对
(ffmpeg+ffprobe)"改绑为"**仅 ffmpeg**"（schema 1→2），**5 张证（4 生产 + 1 夹具）全部
v2，旧 v1 证已清零**（就地重编码，不重跑 oracle）。随后做了一轮空间清理：项目体积
**25G → 5.6G**（回收 ~19.4G 调试缓存/实验视频/闲置 bundle，详见 §4.6）。全量单测
**408 + 90 子测试**绿。工作区干净（仅 `.zcode/` 未跟踪）。

---

## 1. 提交记录（分支 `fix/preview-pacing-metrics`，新→旧）

| 提交 | 内容 |
|---|---|
| 本提交（HEAD） | build：锁 opencv-python==4.13.0.92（本机验证版，非上游 5.0.0.93）；docs：记录锁定理由 + 样本 2/3 导出冒烟 PASS |
| `f2582bb` | docs：记录空间清理（25G→5.6G）、ffprobe/ffplay 物理删除、夹具证迁移、v1 证清零 |
| `f8d96c8` | docs：标注整合手账 §3-§5 为退役前快照；记录孤儿夹具证 + 退役决定 |
| `068b5e5` | **refactor：退役 ffprobe**——元数据/分析/音轨走 PyAV；认证改绑仅 ffmpeg（schema v2）；就地迁移 4 证 |
| `43ea61b` | 元数据探测默认切 PyAV（逐字段一致已验） |
| `937645e` | PyAV 分析解码后端（av.filter area+gray，与 ffmpeg 7.1 逐位一致）；冻结 OpenCV 解码；锁 av~=13.1 |
| `bd8628f` | PyAV 元数据探测后端（字段一致的 MediaInfo） |
| `bf621f2` | 指纹验戳暂停静帧（PyAV 计数+哈希定位）；A_PT 默认后端 |
| `3e672bb` 及更早 | 预览步进 / FPS 上限 / EDL 预览 / mpv 引擎等（本轮之前） |

---

## 2. 现在的架构分工（"谁干什么"）

| 角色 | 干什么 |
|---|---|
| **PyAV (av 13.1)** | 元数据探测（`probe_media` 唯一入口）、分析解码（`_analyze_video_pyav_filter`）、音轨探测（`_probe_audio_stream` 主）、预览静帧定帧（`_still_decode_pyav`） |
| **ffmpeg.exe 7.1** | 帧 PTS oracle（`verify_mpv_frames.build_ffmpeg_command`）、导出（3 模式）、`_verify_tool` 验签 |
| **OpenCV (cv2)** | 识别本体 `cv2.matchTemplate`（暂停/变速）、成像（resize/cvtColor/absdiff/mean）、帧数权威 `CAP_PROP_FRAME_COUNT`、冻结的解码后端 `_analyze_video_opencv`、CvEngine |
| **ffprobe.exe** | **已彻底退役**：代码零引用，二进制已物理删除（2026-08-27 空间清理） |

---

## 3. 认证方案（这次改动的核心，务必看清）

- **旧**：认证文件名嵌 `v1-{producer}-{ffmpeg}-{ffprobe}`，强制 `_tool_pair_verified`（双工具）。
- **新**：文件名 `v2-{producer16}-{ffmpeg16}.json`；`MediaInfo` 去掉 `ffprobe` 字段；
  `_tool_pair_verified` → `_ffmpeg_tool_verified`（只验 ffmpeg：verified+version_line+合法 sha256）。
- **`MediaInfo` 现字段**：`... , ffmpeg: ToolInfo | None`（**没有** `ffprobe` 了）。
  `complete_for_export` 只查 `ffmpeg.is_current()` 等。
- **迁移**：4 张生产证（1/2/3/4.mp4）已用 `.cache/migrate_certs_v2.py` 就地重编码为 v2，
  **逐字节保留** oracle 报告 / pts 表 / 头部判定，迁移后全 `certified=True`。
  **producer sha256 = 当前 `frame_pts_certifier.py` 的哈希**（前 16 位 `def34eddc6ac3507`）。
  ⚠️ **以后若改 `frame_pts_certifier.py`，producer 哈希变 → 所有证失效 → 需重迁**。
- **帧 oracle 本身只用 ffmpeg**，所以去 ffprobe 不影响 oracle 证据。

---

## 4. 关键技术事实

- **PyAV 版本**：`av 13.1.0`（libavcodec 61 = FFmpeg 7.x，libswscale 8）。`pyproject.toml` 锁 `av~=13.1`。
  **勿升到 14+**：av14+（FFmpeg 8.x，libswscale 9）的 `scale=area` 相对打包的 7.1 有 ±1 灰阶
  缩放漂移（虽不翻分类，但破坏"逐位一致"）。
- **逐位一致已实证**：av13.1 与 ffmpeg.exe 7.1 的 `scale={pw}:{ph}:flags=area,format=gray` 输出
  全片（2.mp4 184293 帧）`states/diffs` 完全相同。
- **回退开关**：仅 `ARKNIGHT_A_PT_IMPL=ffmpeg`（分析解码回退 ffmpeg CLI）。**元数据无回退**
  （PyAV 唯一；原 `ARKNIGHT_MEDIA_PROBE=ffprobe` 已随退役删除，代码里也删了 `_active_probe_backend`）。
- **样本**：`D:\qq下载\920\{1,2,3,4}.mp4`；2.mp4 为坏时间戳主样本。
- **打包二进制**：`tools/ffmpeg-7.1.0/bundle/ffmpeg-7.1-essentials_build/bin/`（ffmpeg.exe 在用；
  ffprobe.exe 已无引用，待处理）。

---

## 4.5 ⚠️ 运行时产物依赖（新克隆/换机必看）

以下关键产物**都是 gitignored**、不在版本库。**同一台机器上压缩上下文不受影响**
（工作区保留）；但**新克隆/换机则全部缺失**，需要重建或自带：
- `.cache/media_info/frame_pts/*/v2-*.json` —— 4 张已迁移的 v2 认证。缺了要**重新认证**
  （重跑 ffmpeg oracle，受"不重跑长扫描"约束，慎）。
- `.cache/pyav_kf/*.npz` —— 预览指纹关键帧表（每视频首次建表约 75–85s）。缺了预览自动重建。
- `.cache/migrate_certs_v2.py` 等探针/迁移脚本 —— 不入库；迁移**已跑完**，一般不再需要。
- `tools/ffmpeg-7.1.0/.../bin/ffmpeg.exe` —— 导出/认证/oracle 都靠它；新克隆须自行放置
  （provenance 见 `tools/opencv-runtime-manifest.json`）。`ffprobe.exe` 同目录、代码已不用。
- `tools/libmpv/` —— 预览用的 libmpv（gitignored）。
- 样本视频在 `D:\qq下载\920\`（仓库外路径），验证要用。

---

## 4.6 空间清理记录（2026-08-27，25G → 5.6G）

| 已删除 | 回收 | 说明 |
|---|---|---|
| `.cache/mpv_spike/certified_edl/{3_frames,3_ref}` | 12.5G | 3 号样本调试逐帧 PNG（3801 张），一次性诊断产物 |
| `.cache/norm_test` | 5.5G | 时间戳归一化实验对照视频（A–G + raw.h264），实验已结案 |
| `tools/ffmpeg-7.1.1` | 349M | 零引用的第二个 bundle（代码硬编码用 7.1.0，见 `media_info.py:27`） |
| `.cache/pycache-codex-*` ×10 | 148M | 历史审计 pycache 快照 |
| `tools/ffmpeg-7.1.0/.../{ffprobe,ffplay}.exe` | ~85M | 代码零引用；ffprobe 物理删除=owner 决定 1 落地 |

未删：`PRODUCTION_REAL_SAMPLE_20260816/1/1_certified_export.mp4`（1.5G 认证成品实物，
owner 拍板留）。剩余 5.6G = `.cache` 3.2G（frame_shots/证书/oracle 证据等运行中有用的）
+ 导出产物 2.1G + tools 303M + .git 40M。清理后全量单测复跑 408+90 绿。

---

## 4.7 版本锁定收尾 + 真实导出冒烟（2026-08-27）

**OpenCV 锁版本（纠偏后的决定）**：`pyproject.toml` 锁 `opencv-python==4.13.0.92`。
注意这与上游 `requirements.txt`/`uv.lock` 写的 5.0.0.93 **不同**——本机 uv.lock 是从上游
带过来的旧锁（连 pyproject 里的 av/imageio-ffmpeg 都没有，早已脱节），实际运行环境
（系统 Python 3.11.9）装的是 4.13.0.92，**全部一致性证据（指纹链 27 点、bit-identical
探针、408+90 单测、golden）都在它下面产出**。锁 5.0.0.93 反而会强制升级到未验证版本。
若日后要对齐上游 5.x，必须先复跑一致性探针再换锁。uv.lock 待有 uv 环境时 `uv lock` 重生。

**真实导出冒烟（ffprobe 物理删除后的首次真实链路验证）**：
`scripts/run_real_sample_certified_export.py --stems 3,2 --output-root .cache/smoke_export_20260827`
- 样本 3：PASS——证书直接命中 v2（PASS_WITH_HEAD_ANOMALIES，不重扫），导出 1901/1901 帧，
  PyAV 数帧验收 PASS。
- 样本 2（坏时间戳）：PASS——683 段 16973/16973 帧，头部 [0,5) 丢 2 帧属既有
  head-anomaly 裁决的预期行为，验收 PASS。

---

## 5. 留给 owner 的决定（都在 `HANDOFF_PYAV_CONSOLIDATION.md` §10）

1. ~~是否物理删 `ffprobe.exe`~~ **已落地（2026-08-27 空间清理时删除，含 ffplay.exe）**。
2. **帧数权威保留 OpenCV `CAP_PROP_FRAME_COUNT`**（未迁 PyAV）：PyAV `stream.frames` 可能低估
   → 截断分析；cv2 只会高估、有 EOF 兜底更安全。若要彻底去这个 OpenCV 依赖，需先验证。
3. ~~第 5 张孤儿证~~ **已迁移（2026-08-27）**：cfr.mp4 夹具证已重编码为 v2 并自校验
   certified=True（PASS，12 帧）；同时删除全部 5 张旧 v1 证，`.cache` 已无 v1。
4. **0.2X 裁剪 / 夹心并入**：仍为后续方向，未动。

---

## 6. 长期约束（逐字保留，别踩）

- 不直接 push upstream；不用 `git reset --hard` / `git checkout --` / `git clean`。
- owner"主动停止"即停，且不留 FFmpeg/mpv/Python 媒体后台进程。
- **不重跑四个完整 frame oracle 长扫描**（迁移之所以"就地重编码"就是为了守这条）。
- 不放宽 10ms 内容阈值；禁止 frame/fps 时间戳回退造正式 EDL/媒体时间。
- CvEngine/VideoIOThread 保留；不运行未审计的 `arknight-preview-pack.zip`。
- 解码后端优先 A_PT；导出与帧 oracle 留 ffmpeg.exe。

---

## 7. 验证 / 运行

- 全量单测：`python -m pytest tests/ -q` → **408 passed + 90 subtests**。
- 收口探针（4 证在 PyAV 默认下 `certified=True`）：`.cache/probe_certify_pyav.py`。
- 迁移脚本（已跑过，可复跑）：`.cache/migrate_certs_v2.py [--write]`。
- 其它探针在 `.cache/`（gitignored）：`probe_pyav_metadata.py` / `probe_mediainfo_backend_diff.py` /
  `probe_scale_area_drift.py` / `probe_analyze_fullfile_equiv.py` / `probe_hash_verify.py`（指纹链 27 点）。

---

## 8. 文件地图（本轮改过的）

| 文件 | 改动 |
|---|---|
| `media_info.py` | 删 `ffprobe` 字段/`resolve_ffprobe_path`/`_active_probe_backend`；`probe_media` 唯一 PyAV 入口；`_ffmpeg_tool_verified` |
| `frame_pts_certifier.py` | 认证改绑仅 ffmpeg；schema→2；文件名去 ffprobe |
| `media_exporter.py` / `pts_timeline.py` | 去 ffprobe 校验/溯源 |
| `analyzer.py` | `_probe_audio_stream` 改 PyAV 主；删 ffprobe_path 参数；帧数权威保留 cv2 |
| `preview_player.py` / `settings_panel.py` | 拆 ffprobe_path 管线；删设置里 "FFprobe 路径" 输入框 |
| `scripts/{certified_edl_preview,run_production_pts_golden,run_real_sample_certified_export}.py` | 去 `--ffprobe` / ffprobe 输出探测改 PyAV |
| `tests/*` | 7 个测试文件重写为单工具/PyAV |
| `pyproject.toml` | 锁 `av~=13.1` |

---

## 9. 下一步候选（按价值）

1. ~~物理删 `ffprobe.exe`、补迁第 5 张夹具证~~ **均已完成（2026-08-27）**。
2. 后续方向：0.2X 裁剪 / 夹心并入（检测侧规则）；时间戳半无损注入（mkvmerge，外部需求触发）。
3. 可选加固：指纹报警器、新视频建表提示。
