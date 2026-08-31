# HANDOFF — 样本4导出性能专项 + 版本排查（2026-08-30/31）

> 本文件是**本次会话**的交接。宏观现状仍以 `HANDOFF_STATE_20260827.md` 为准（ffprobe 退役、
> PyAV 整合、证书 v2 等）。本文件聚焦这次新做的：**样本 4 导出性能（阶段三已完成）** +
> v26.7.23 导出失败根因实锤（§3.1）。
> 分支 `fix/preview-pacing-metrics`，HEAD 见 git log（阶段三提交链：`042b53c` 调研 →
> `95b2c30` 实现 → `acbaf0b` GUI 降级提示 → 本文档），工作区干净（仅 `.zcode/` 未跟踪）。
> 配套调研报告：`RESEARCH_PHASE3_20260831.md`（实测数据全在里面）。

---

## 0. 一句话现状

**阶段三已完成（`95b2c30` + `acbaf0b`）：样本 4（2623 段 × 42 万帧、带音频）从"3 小时超时
失败"变为 12.7 分钟成功导出，与单趟基准逐帧一致（107434/107434、内容差 0.0000）；
样本 2/3 回归通过，pytest 415+90 全绿。** 方案经调研修订为"视频不动 + 音频分批 +
容器时长地雷修复"，详见 §2.4。

---

## 1. 四套代码/产物在哪（别搞混）

| 目录 | 是什么 |
|---|---|
| `arknight-auto-editing-main` | **你的工作区**（fork，带 PTS 认证导出 `export_pts_schedule`，分批寻址要进这里） |
| `arknight-auto-editing-upstream` | 干净上游克隆（liemark，main @ ce10360 = v26.7.23），带独立 `.venv`（opencv 5.0.0.93、imageio-ffmpeg 供 ffmpeg） |
| `arknight-auto-editing-v26.7.23-win` | 官方打包成品（上游 build），`剪暂停260723.exe` |
| `arknight-auto-editing-v26.7.15-win-fix` | 官方打包成品（更早带 fix 版），`剪暂停.exe` |

样本在 `D:\qq下载\920\{1,2,3,4}.mp4`。4 号最长（117.8 分钟、424176 帧、2623 个保留段）。

**代码血统**：owner 即 fork 作者（kouekikin24）。A_PT 是其贡献，已被上游 liemark 合并（PR #9，
merge commit `ce10360` = 上游 v26.7.23）。主项目 remote：`origin`=kouekikin24（你的 fork）、
`upstream`=liemark。上游克隆（`arknight-auto-editing-upstream`）的 remote：`origin`=liemark。
**注意**：打包成品是**上游** build（预览走 cv2 VideoIOThread，无 mpv/PTS 认证）；你的工作区是 fork
（有 CvEngine/MpvEngine/PTS 认证导出）。分批寻址要进的是 fork 的 `export_pts_schedule`。

**待清理**：`D:\qq下载\920\4_clipped.mp4` 是打包版逐帧导出留下的 5.6 分钟半成品（无音轨），可删。

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

### 2.2.5 为什么是"自己做"而不是用现成工具 + 与上游速度对比

**成熟方案调研**（结论：没有开箱即用的）：LosslessCut 默认只对齐关键帧（不帧精确）、其 smart-cut
是实验性；mkvmerge 拆分只能在关键帧；MoviePy 逐帧 Python 解码（更慢）；MLT/GStreamer 是重型 C 框架且
会把 VFR 归一化成匀速（破坏我们的 PTS 精确）。所以**分批寻址不是重复造轮子**——它就是转码行业标准的
"分块编码 + 无损拼接"模式，积木全是 ffmpeg 现成的（`-ss` 寻址、concat demuxer `-c copy`）。

**速度对比（实测）**：上游的 trim/concat 滤镜路径在 2 号（683 段）就 **30 分钟超时报废**；我们分批
8 分钟完成。3 号（108 段）两家都快（~45s vs ~25s）。**段数越多我们越占优**——4 号上游根本出不来。

**ffprobe 删除后的生产导出回归**：1/2/3 号用当前项目（fork）的认证导出冒烟全部 PASS（帧数精确、
证书命中 v2、无 ffprobe 残留调用）。4 号因段数超时（正是本专项要修的）。

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

### 2.4 阶段三（2026-08-31 已实施，方案经调研修订为"视频不动 + 音频分批"）

**重要修订**：调研（`RESEARCH_PHASE3_20260831.md`）证明视频平铺 select 在 2623 段
**不爆炸**（求值近乎免费，单趟 11 分钟），爆炸的是音频 atrim/concat 图（400 段 120s、
800 段 >600s）。因此阶段三**没有**做视频分批寻址整合，而是：

1. **音频分批（路线 B）**：`export_pts_schedule` 段数 >400（`_MAX_PTS_SINGLE_GRAPH_AUDIO_RANGES`）
   且有音轨时，视频单趟照常（`-an`），音频走 `_export_audio_pts_batched()`——每 100 段一批
   `-ss` 寻址 + 批内 atrim/concat → 无损 PCM（.nut）→ concat 清单写精确 duration →
   混流时一遍 AAC。音频失败**降级为无声成片**（audio_mode=failed_video_only），不删视频；
   取消（TaskCancelled）仍传播并清理。
2. **容器时长地雷修复（既有生产 bug，新发现）**：tpad 克隆哨兵继承源帧病态 duration →
   幻影包 + 容器声明时长垃圾值（2_baseline 声明 383s 实际 283s）。修复 = 编码命令加
   `-bsf:v setts` 把克隆帧钉到分析坐标（`_pts_sentinel_fix_args`，按 PTS 阈值识别，
   不按包序号，健康源上为 no-op）；导出后跑 `_verify_pts_export_container` 容器后检
   （克隆位/声明时长/尾部单调为硬检查；包数在坏时间戳源上允许 ±2 软差异——muxer 对
   非单调 pts 会去重，既有行为）。
   ⚠️ **必须用 BSF 形态**：essentials 构建的 ffmpeg 没有 AVFilter `setts`（只有 BSF 版），
   改成滤镜形态会直接报 `Unknown filter`。
3. **等价性验证**：样本 2 全量（683 段）带音频端到端——视频与基准逐帧一致（16973/16973，
   内容差 0.0000）；音频分批 vs 老单图样本级 A/B：长度完全一致、最大差 -96dB（0.3% 样本，
   seek 解码噪声，AAC 重编码后不可闻）；pytest 全量 **415 passed + 90 subtests**（基线
   408 + 新增 7）。**样本 4 端到端 PASS：12.7 分钟带音频导出，107434/107434 帧与 A2a
   单趟基准逐帧一致、内容差 0.0000**（`.cache/sentinel_fix/4_full_audio.mp4`）。
4. **样本 3 回归 PASS**（108 段单图路径行为不变）；样本 1 有 2312 段，同样受益于音频分批。

**实施后发现的两个坑（已在代码注释/后检中处理）**：
- 音频批命令必须加**输入侧** `-t {批跨度}` 限制解码（否则每批扫到 EOF，楼梯形浪费）；
- 坏时间戳源上 muxer 会对非单调 pts 去重/钳位 → 包数与"scheduled+1"可差 1（软告警，
  不是 bug）；**切片导出（非全表）会因末窗口之外逃帧而少帧**——后检的包数硬差异报警
  只在全表导出下保证为零。

**未做/可选**：视频分批寻址 + 并行编码（编码 2-4 倍加速，现总耗时已被"视频单趟 11 分钟"
覆盖，优先级降）；CFR 分支千段同样有病（调研 B），留给未来 CFR 源出现时再处理；
真实 5.6 分钟剪法重建（对齐 v3）放弃（内容对齐被 imageio 0.74% 拉伸破坏）。

---

## 3. 版本排查结论（都已定论，别再纠结）

- **预览流畅度**：v26.7.15-fix 与 v26.7.23 的预览代码（`_render_loop`/`toggle_play`/`video_io.py`）
  **逐行相同**，打包库也相同（OpenCV 4.12.0.88 + Python 3.12）。**1x 预览客观上无差异**，体感差异是误判。
  两版 2x/4x 预览卡顿都真实存在（高倍速跟不上，正常）。
- **导出失败（v26.7.23 在 4 号上）**：已实锤归因，详见 §3.1。一句话：不是 A_PT 的锅，是
  "几千段滤镜图"架构在真实剪辑规模下建图+逐帧双重爆炸；而 A_PT PR 引入的 `resolve_ffmpeg_path`
  让打包版第一次走上了这条从未被测过的路。
- **"无可用编码器"**：误报。机器有 RTX 4060，nvenc 实测可用。是当时 16 线程分析占满系统，
  上游 5 秒 GPU 探测超时所致（代码注释里自己承认这个假阴性）。系统空闲时检测正常（认得出 h264_nvenc）。
- **下载慢**：是到 GitHub 主站/成品 CDN 的链路慢且不稳（git clone、release 资产），但 codeload 和
  清华镜像很快。以后拉源码用"下载 zip"别用 `git clone`。

### 3.1 v26.7.23 导出失败根因（2026-08-31 实锤：分阶段复现 + 段数缩放实测）

**失败链**：v26.7.23 导出是三段式（v26.7.15 只有逐帧 pipe 一条路）：
① 快速滤镜图（trim/concat + 音频 atrim，超时 30min）→ ② 回退逐帧 pipe（能成）→
③ `_mux_audio_for_ranges` 音频 atrim/concat 混流（超时/报错 → RuntimeError → 判失败并**删产物**）。

**实测证据**（样本 4，2623 段旧清单，tools/ffmpeg 7.1.0；脚本 `.cache/repro_v26723.py` /
`.cache/scaling_test.py` / `.cache/stage1_mem.py`）：
- 阶段① 完整图跑满 13 分钟输出 **0 字节**；阶段③ 音频图 5 分钟只产出 44 字节（一个文件头）。
- 段数缩放（`-f null` 隔离编码）：视频图 50段 4s → 200段 42s → 800段 >600s；
  音频图 50段 2s → 200段 14s → 800段 >600s。200→800 跳崖，远超线性。
- **建图本身就爆炸**：2623 段图纸只喂 60 秒源片也 180s 跑不完（帧还没开始处理）。
  机制 = 建图随段数超线性 + 逐帧 O(段数×帧数)，双重爆炸。
- 内存爆炸假设**已否**：阶段① RSS 平坦 0.25GB。不是撑死，是磨死。
- 存疑：用户那次约 8 分钟即无动静的确切死法未完全复现（复现里可沉默磨 13min+），
  与真实段数/系统状态有关；不影响"两阶段都走不通"的结论。

**归因链（git 实证）**：
- 埋雷：`0a08c2b`（引入两段式音频导出）+ `7ad6b7d`（加固）。二者门控在 `shutil.which("ffmpeg")` 上，
  打包版 PATH 无 ffmpeg → **新管线在成品里从未执行过（死代码）**，发版前因此没人发现。
- 激活：`e9f4de1`（**A_PT PR**）引入 `resolve_ffmpeg_path`（imageio_ffmpeg 兜底）；上游打包恰好捆了
  `imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe` → 打包版第一次能解析到 ffmpeg → 死代码首次激活。
  即：A_PT 没碰导出逻辑，但换了导出走的路。
- 若给上游提修复，最小三刀：段数超阈值直接回退逐帧；混流失败降级为无声输出而不是删产物；
  音频图同样分批。

**为什么 v26.7.15-fix 能成**：它解析不到 ffmpeg（只有 `shutil.which`）→ imageio 兜底逐帧写
（视频-only、CFR）。物证：`4_clipped.mp4` 是 1920×**1088**（源片 1920×1080）——imageio_ffmpeg
（`_io.py:549-561`）对不被 16 整除的尺寸加 `-vf scale` **拉伸**（不是补边）。
⚠️ 副作用：用内容对齐反推真实剪法已失败两版——成片每帧被纵向拉伸 0.74%，与源片像素对不上，
静态区误配会让扫描指针一路跑到源片末尾。要重建真实剪法需锚点法 + 对拉伸鲁棒的特征。

---

## 4. 环境/工具现状（本次有变化）

### 4.0 ⚠️ 现在有三个不同的 OpenCV 版本在环境里，别搞混

| 在哪 | OpenCV 版本 | 说明 |
|---|---|---|
| 本机当前项目实际运行（pyproject 锁定） | **4.13.0.92** | 所有一致性证据（指纹链/golden/单测）都在它下面产出 |
| 两个打包成品 exe 捆绑 | **4.12.0.88** | 上游构建时打包的，跟运行它的机器无关 |
| 上游克隆 `.venv`（我建的）+ 上游 requirements.txt | **5.0.0.93** | 上游锁定值 |

含义：测"上游/打包版"用的是 4.12 或 5.0，测"当前项目"用 4.13——比较行为前先确认用的是哪个 cv2。
OpenCV 5 对齐任务的目标版本是 **5.0.0.93**。

- **OpenCV 锁定 `==4.13.0.92`**（pyproject.toml）——本机实际运行版本，所有一致性证据都在它下面产出。
  上游锁 5.0.0.93，但本机 5/29 装的 4.13，uv.lock 是从上游带过来的旧文件，本机从未按它同步。
  **要对齐上游 5.x 必须先复跑一致性探针**（matchTemplate 数值可能漂移）。
- **uv.lock 已重新生成**（cf210f6/670b841）：补入 av 13.1.0、imageio-ffmpeg，opencv 按新锁 4.13.0.92。
- **ffprobe.exe / ffplay.exe 已物理删除**；ffmpeg bundle 只剩 7.1.0（tools/）。
- 磁盘：项目从 25G 清到 5.6G。
- 全量测试基线：**415 passed + 90 subtests**（8/31 阶段三后；原 408）。
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

1. **用户 GUI 真实验证**（唯一未闭环项）：源码启动 `python main.py`（打包 exe 是旧代码），
   加载样本 4，用平时的剪法 + 勾"保留音频"（`export_keep_audio` 默认开，preview_player.py:1816，
   段数 >400 自动走音频分批，无需额外操作）导出。预期：成功、带声音、4-6 分钟；
   混音失败会提示降级为无声视频，不删产物。
   顺带可删 `D:\qq下载\920\4_clipped.mp4`（v26.7.15-fix 留下的无声半成品，1.4GB）。
   ⚠️ 别删 `.cache/research_phase3/A2a_singlepass_v2623.mp4`（1.36GB）——它是样本 4 的
   单趟基准，后续等价性回归要靠它对照。
2. **上游修复 PR 草案**（owner 是 v26.7.23 贡献者，可补救口碑）：①音频超 400 段自动分批
   （PCM 中间件）；②混流失败降级不删产物；③克隆帧幻影修复（setts bsf）。根因证据在 §3.1，
   实现参照 fork 的 `95b2c30`。
3. 可选：并行批编码 / CFR 千段验证 / OpenCV 5 对齐（装 5.0.0.93 复跑一致性探针）。
4. 收尾：`arknight-preview-pack.zip` 仍未审计，不要运行。

## 7. 关键脚本索引（`.cache/`，gitignored）

| 脚本 | 用途 |
|---|---|
| `exp_batched_seek.py` | 分批寻址导出原型（视频-only） |
| `validate_no_baseline.py` | 无基准验证：产物 PTS 对照认证表 |
| `exp_trim_concat_vfr.py` | 轻方案 trim/concat 对照实验（已证 2 号失败） |
| `probe_seek_pts.py` | 验证 -ss+-copyts 保原始 PTS |
| `bench_upstream_speed.py` / `run_upstream_export.py` | 上游导出速度对比 / 手动跑上游 |
| `monitor_export.ps1` | 加固版导出监控（**用这个**） |
| `monitor_analysis.py` | ⚠️ 旧监控（每20s起子进程，会挂死），**别再用** |
| `repro_v26723.py` | v26.7.23 三段式导出分阶段复现（§3.1） |
| `scaling_test.py` | 滤镜图段数缩放曲线（50/200/800/2623 段） |
| `stage1_mem.py` | 阶段① 内存画像（已否内存爆炸假设） |
| `derive_real_cut.py` | ⚠️ 内容对齐反推真实剪法，两版均失败（拉伸+静态区），勿直接用 |
| `test_sentinel_fix.py` | 哨兵修复生产路径验证（A=病态场景 / B=全量回归） |
| `test_audio_batched.py` | 音频分批端到端（样本2 带音频 683 段） |
| `test_audio_ab.py` | 音频 新路分批 vs 老单图 样本级 A/B |
| `test_sample4_e2e.py` | 样本4 带音频端到端 + 对照 A2a 基准 |
| `test_sample3_regress.py` | 样本3 单图路径回归 |
| `check_postverify.py` | 容器后检双向验证（修复版过/病态拒） |
| `diag_audio_drift2.py` | 音频漂移探针定位（真实音频静音段易误配，慎用） |
| `.cache/research_phase3/` | 调研脚本与数据（A/B/C/D/F 各工作流，见 RESEARCH 附录） |
| `.cache/sentinel_fix/` | 阶段三验证产物（A_pathological/B_full_fixed/B_full_audio/4_full_audio） |
