# HANDOFF — 样本4导出性能专项 + 版本排查（2026-08-30）

> 本文件是**本次会话**的交接。宏观现状仍以 `HANDOFF_STATE_20260827.md` 为准（ffprobe 退役、
> PyAV 整合、证书 v2 等）。本文件聚焦这次新做的：**样本 4 导出性能（分批寻址）** + 一堆版本排查。
> 分支 `fix/preview-pacing-metrics`，最新提交 `074b39d`，工作区干净（仅 `.zcode/` 未跟踪）。

---

## 0. 一句话现状

**样本 4（2623 段 × 42 万帧）导出超时问题，分批寻址方案已实测验证可行（视频-only 约 50 分钟、
输出 PTS 与认证表逐帧一致），但还停留在 `.cache` 原型，未整合进生产代码（阶段三未做）。**
期间顺手做了大量版本/环境排查，结论都记录在下文。

---

## 1. 四套代码/产物在哪（别搞混）

| 目录 | 是什么 |
|---|---|
| `arknight-auto-editing-main` | **你的工作区**（fork，带 PTS 认证导出 `export_pts_schedule`，分批寻址要进这里） |
| `arknight-auto-editing-upstream` | 干净上游克隆（liemark，main @ ce10360 = v26.7.23），带独立 `.venv`（opencv 5.0.0.93、imageio-ffmpeg 供 ffmpeg） |
| `arknight-auto-editing-v26.7.23-win` | 官方打包成品（上游 build），`剪暂停260723.exe` |
| `arknight-auto-editing-v26.7.15-win-fix` | 官方打包成品（更早带 fix 版），`剪暂停.exe` |

样本在 `D:\qq下载\920\{1,2,3,4}.mp4`。4 号最长（117.8 分钟、424176 帧、2623 个保留段）。

---

## 2. 样本 4 导出性能专项（核心工作）

### 2.1 病因（已确诊）
认证导出走 `analyzer.export_pts_schedule`。VFR 分支用 `_pts_select_setpts_video_filter`：
一条 `select='between(pts,...)+...'`（2623 项）+ `setpts` 平铺 `if()` 间隙扣除。**每帧解释执行
全部 ~5246 项表达式** → 424k 帧 × 2623 段 ≈ 2.2×10⁹ 次解释求值 → 超 3 小时超时。
`_MAX_PTS_EXPORT_RANGES=4000` 拦不住（管解析不管耗时）。

### 2.2 轻方案（逐段原生 trim/concat）——已实测，**出局**
- 3 号（干净 VFR）逐帧一致 PASS；**2 号（坏时间戳）FAIL**：帧数错、输出被压成匀速、内容错位。
- 原因：trim/concat 依赖 ffmpeg 逐段整理时间戳，坏时间戳源上会自作主张补匀速；**平铺 select 用
  认证 tick 做显式算术，就是为扛坏时间戳设计的，不可替换**。

### 2.3 分批寻址（保留平铺 select 语义，按批寻址）——已实测，**成立**
原理：把 2623 段切成每批 ~100 段，每批 `-ss` 跳到批首 + `-copyts` 保原始 PTS + 批内平铺
select/setpts（只含批内段、只减批内间隙），各自编码后 `concat demuxer -c copy` 拼接。
**关键**：concat 列表必须写**精确 `duration` 指令**（按全局时间表算每批真实跨度），否则批间漂 1 帧。
数学等价已数值验证；`-ss`+`-copyts` 保 PTS 已实测（`.cache/probe_seek_pts.py`）。

**实测结果**（脚本 `.cache/exp_batched_seek.py`、`.cache/validate_no_baseline.py`）：

| 样本 | 段数 | 分批结果 |
|---|---|---|
| 3 | 108 | ✅ PTS 1901/1901 一致 |
| 2（坏时间戳） | 683 | ✅ PTS 16973/16973 一致（轻方案在这翻车） |
| 4 | 2623 | ✅ PTS 107433/107433 一致，视频-only **~50 分钟**编完 |

**设计文档**：`DESIGN_EXPORT_BATCHED_SEEK.md`（含阶段二全部实测结论）。

### 2.4 阶段三（未做，下一步）
把分批寻址整合进 `export_pts_schedule`：① VFR 大段数时启用分批路；② 音频同步分批（批内
atrim/concat）；③ 可选并行批编码（50 分钟→十几分钟）；④ 回归 1/2/3 号 + 全量 pytest。

---

## 3. 版本排查结论（都已定论，别再纠结）

- **预览流畅度**：v26.7.15-fix 与 v26.7.23 的预览代码（`_render_loop`/`toggle_play`/`video_io.py`）
  **逐行相同**，打包库也相同（OpenCV 4.12.0.88 + Python 3.12）。**1x 预览客观上无差异**，体感差异是误判。
  两版 2x/4x 预览卡顿都真实存在（高倍速跟不上，正常）。
- **导出失败**：v26.7.23 在 4 号上失败**不是 A_PT 的锅**（A_PT 只管分析解码，不碰导出）。是段数太多的
  老问题。上游 v26.7.23 只是比 v26.7.15 多了条"快速滤镜路径"（段数少快、段数多照样超时）。
- **"无可用编码器"**：误报。机器有 RTX 4060，nvenc 实测可用。是当时 16 线程分析占满系统，
  上游 5 秒 GPU 探测超时所致（代码注释里自己承认这个假阴性）。系统空闲时检测正常（认得出 h264_nvenc）。
- **下载慢**：是到 GitHub 主站/成品 CDN 的链路慢且不稳（git clone、release 资产），但 codeload 和
  清华镜像很快。以后拉源码用"下载 zip"别用 `git clone`。

---

## 4. 环境/工具现状（本次有变化）

- **OpenCV 锁定 `==4.13.0.92`**（pyproject.toml）——本机实际运行版本，所有一致性证据都在它下面产出。
  上游锁 5.0.0.93，但本机 5/29 装的 4.13，uv.lock 是从上游带过来的旧文件，本机从未按它同步。
  **要对齐上游 5.x 必须先复跑一致性探针**（matchTemplate 数值可能漂移）。
- **uv.lock 已重新生成**（cf210f6/670b841）：补入 av 13.1.0、imageio-ffmpeg，opencv 按新锁 4.13.0.92。
- **ffprobe.exe / ffplay.exe 已物理删除**；ffmpeg bundle 只剩 7.1.0（tools/）。
- 磁盘：项目从 25G 清到 5.6G。
- 全量测试基线：**408 passed + 90 subtests**。
- 装了 `pyinstxtractor-ng`（解 exe 用）；uv 0.12.7 装在用户环境。

### 监控工具（`.cache/`）
- `monitor_export.ps1`：加固版 PowerShell 长驻监控（按 exe 路径识别，不反复起子进程）。注意它有个
  已知误报点：导出完成后的"GUI 空闲"会被误标 `STALL?`（"活跃后变平"≠"卡死"，需人工分辨）。

---

## 5. 仍在生效的铁约束（沿用 HANDOFF_STATE_20260827 §6）

不直接 push upstream；不用 `git reset --hard`/`checkout --`/`clean`；owner 说停立即停且清后台进程；
不重跑四个完整 frame oracle 长扫描；不动 10ms 内容阈值；禁止 frame/fps 时间戳回退造正式 EDL；
CvEngine/VideoIOThread 保留；解码后端优先 A_PT；导出与帧 oracle 留 ffmpeg.exe。
**等价性是硬门槛**：导出产物与基准逐帧比对（帧数/PTS/内容同源），不达标不合入。

---

## 6. 下一步（按优先级）

1. **阶段三**：分批寻址整合进 `export_pts_schedule`（含音频分批 + 并行优化 + 回归）。让 4 号能导出。
2. **OpenCV 5 对齐验证**（可选）：venv 装 5.0.0.93，复跑一致性探针 + golden 比对，过了再换锁。
3. 收尾：`arknight-preview-pack.zip` 仍未审计，不要运行。

## 7. 关键脚本索引（`.cache/`，gitignored）

| 脚本 | 用途 |
|---|---|
| `exp_batched_seek.py` | 分批寻址导出原型（视频-only） |
| `validate_no_baseline.py` | 无基准验证：产物 PTS 对照认证表 |
| `exp_trim_concat_vfr.py` | 轻方案 trim/concat 对照实验（已证 2 号失败） |
| `probe_seek_pts.py` | 验证 -ss+-copyts 保原始 PTS |
| `bench_upstream_speed.py` / `run_upstream_export.py` | 上游导出速度对比 / 手动跑上游 |
| `monitor_export.ps1` | 加固版导出监控 |
