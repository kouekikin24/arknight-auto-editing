# 预览流畅度优化 — 交接文档

> 写给下一位接手者（Codex / 人）。目标读者不需要读之前的对话。
> 更新时间：2026-08-03
> 分支：`fix/preview-pacing-metrics`　HEAD：`52b7aef`　基线：`b706823`

---

## 0. 一句话现状

预览「跳过裁剪区连续播放」的流畅度等级已稳定在 **良**；
想在**业务加速开启**的配置下达到 **优**，**当前「单线程同步 VideoCapture」架构基本做不到**——
不是参数没调好，是每拍 16.67ms 的预算装不下一次跳裁剪（47–78ms seek 或 ~53ms grab）。

---

## 1. 先读这段：三条容易被误导的事实

1. **A1/A2（追帧止损 + 追帧限额）已经在代码里**，不需要重做。
   见 `video_io._pace_after_slot`：追帧循环内每丢一拍重测 lag、中途若自己又 seek/大跳立刻软锚退出、`_MAX_CATCHUP_SLOTS` 已由 12 降到 4。
2. **`.cache/preview_fluency/` 下的 `1_histogram.log` / `2c_histogram.log` / `3_histogram.log` / `4_histogram.log` / `6_histogram.log` 是无效产物**（5 个文件字节数完全相同，实际都是对 `2.mp4` 的同一份报告）。
   **只有 `2.mp4` 的 kept-gap 结论是真数据。** 不要采信「所有片子保留缝都是 37.5%」这类跨片结论。建议删除这 5 个 log。
3. **UI 类改动（时间轴、标签、PhotoImage）在无界面 harness 里测不出来**，因为 harness 不起 Tk。
   必须用 GUI 实跑观察流畅度行里的 `UI xx/xxms 顿xxxx q丢xx`。

---

## 2. 评级门槛（权威定义）

`preview_player._grade_smoothness`：

| 等级 | 条件（全部满足） |
|---|---|
| **优** | late1 ≤ **3%** 且 drop ≤ **1%** 且 解码均 ≤ 25ms 且 实时比 ≤ 1.05 |
| 良 | late1 ≤ 10% 且 drop ≤ 5% 且 解码均 ≤ 40ms 且 实时比 ≤ 1.15 |
| 中 | late1 ≤ 25% 且 drop ≤ 15% |

- `late1` = 本拍迟到 > 1 个 frame_dur 的占比
- `drop`  = discarded / (presented + discarded)
- 60fps 源 ⇒ `frame_dur ≈ 16.67ms`

---

## 3. 代码现状（全部未提交，5 文件 +1773/−176）

| 文件 | 已做的改动 |
|---|---|
| `video_io.py` | 墙钟 slot 计时 + 追帧 + 软/硬重锚；seek 阈值 30→**100**（追帧路径 **120**）；S1 步进封顶 **3**；追帧上限 **4**；**seek 债务豁免**（4 个触发条件）；**A1 追帧中途止损**；`opt`/`base` 双模便于 A/B；完整 perf 统计 + spikes 归因环 |
| `preview_player.py` | PhotoImage 复用（`paste`，尺寸变了才重建）；流畅度面板与评级；自动倍率标定改用 play-steps 计数（不含跳裁剪帧） |
| `timeline_widget.py` | 红线**增量绘制**：`coords` 移动而非 `delete("all")`；像素未变直接 return；刻度签名 (w, zoom, scroll, total) 变化才全量重建 |
| `analyzer.py` | A_PT 解码后端（`ffmpeg_sw_passthrough`）+ 分析 context 复用（跳过第二次边界扫描） |
| `settings_panel.py` | 分析解码后端下拉（默认 OpenCV，可选 FFmpeg A_PT） |

未跟踪文件：`scripts/`（新工具）、`_wf_plan.json`、`_tmp_v261715_analyzer.py`、`arknight-preview-pack.zip`。

---

## 4. 工具链

| 脚本 | 作用 | 注意 |
|---|---|---|
| `scripts/cache_apt_analysis.py` | **强制 A_PT** 分析并把 states/diffs/pause_segs/speed_segs 缓存到 `.cache/preview_fluency/` | 已缓存 `1_` `2_` `3_` `4_` 的 meta |
| `scripts/bench_preview_fluency.py` | **无界面** harness，直接驱动 `VideoIOThread`，输出与 GUI 同格式的流畅度行 + 完整 json | 不起 Tk ⇒ UI 改动无效 |
| `scripts/kept_gap_histogram.py` | 只读统计保留缝分布 + 合并模拟（不改播放） | 只有 `2.mp4` 的输出可信 |

复现命令：

```bash
python scripts/cache_apt_analysis.py --video "D:\qq下载\920\2.mp4"
python -u scripts/bench_preview_fluency.py --meta .cache/preview_fluency/2_meta.json --pace opt --biz on
python -u scripts/bench_preview_fluency.py --meta .cache/preview_fluency/2_meta.json --pace opt --biz off
python -u scripts/bench_preview_fluency.py --meta .cache/preview_fluency/2_meta.json --pace base --biz on   # 基线对照
```

---

## 5. 实测数据（2.mp4，184293 帧 @60fps，A_PT 段表）

| 运行 | late1 | drop | seek | discarded | 软锚 | 解码均 | 实时比 | 墙钟 |
|---|---|---|---|---|---|---|---|---|
| harness biz **off** | 1.89% | 1.36% | 101 | 231 | 104 | 2.7ms | 1.011 | 286s |
| harness biz on（A1A2 前） | 4.85% | 4.42% | 97 | 305 | 74 | 5.0ms | 1.025 | 118s |
| harness biz on（A1A2 后） | 4.53% | 3.81% | 97 | 263 | 91 | 4.6ms | 1.023 | 118s |
| harness biz on（时间轴改动后） | 5.05% | 4.22% | 97 | 291 | 94 | 4.5ms | 1.023 | 118s |
| **GUI 全片实跑** | 4.05% | 4.55% | 97 | 314 | 28 | 4ms | 1.01 | 116s |

### 噪声底（重要）

最后两行 harness 相差 **0.52pp late1 / 0.41pp drop**，但时间轴改动**在 harness 里不可能生效** ⇒
**这个差值就是单次运行的噪声。**

> **任何小于约 0.5pp 的结论，必须同配置重复 3–5 次取中位数**，否则是自欺。
> 当前 `.cache/preview_fluency/bench_*.json` 里有两个是不可比的单次样本。

---

## 6. 为什么卡在「良」：算术诊断

**60fps ⇒ 每拍预算 16.67ms。这是所有问题的根。**

### 6.1 单次跳裁剪一定超预算，两条路都超

- `cap.set(POS_FRAMES)` 精准 seek：固定 **47–78ms ≈ 3–5 拍**（与跨度几乎无关）
- 阈值 100 帧内改 grab：0.53ms/帧 × 100 ≈ **53ms ≈ 3 拍**

把阈值从 30 调到 100 只是把「seek 尖峰」换成「grab 尖峰」——
spikes 里的 `skip_trim:46`、`skip_trim:49`（15–31ms）就是 grab 造成的。

**结论：同步单 cap 在 present 关键路径里做跳转，参数怎么调都装不进 16.67ms。**

### 6.2 drop ≤ 1% 的数值要求极苛刻

presented ≈ 6600 ⇒ discarded 必须 ≤ **约 67**。现在 263–305。
而 seek 97 次、每次天然欠 3–5 拍 ⇒ 等价于要求「几乎每次跳都零 discard」。
豁免已把部分 catchup 转成软锚（软锚 74→91，discarded 305→263），剩余来自非 trim 的迟到。

### 6.3 就算 seek 完全免费，late1 也过不了线

剔掉所有带 `seek` 标签的 late1 后剩 223 次 ≈ **3.38%**，仍 > 3%。
原因：present 耗时是**双峰分布**（便宜拍 2–5ms / 贵拍 31ms+），p95 = 31ms ≈ 1.86 拍。
贵拍来自 grab 跳裁剪与 0.2x 区 `step:3`。

### 6.4 业务加速是「让密度翻倍」，不是「让单次变贵」

| | biz off | biz on |
|---|---|---|
| 墙钟 | 286s | 118s |
| seek 密度 | 0.35/s | 0.82/s |
| discarded | 231 | 305 |
| presented | ~16700 | ~6600 |
| drop% | 1.36% | 4.42% |

discarded 绝对值差不多，**drop% 主要是被分母（presented）缩小 2.5 倍放大的**。

### 6.5 直接结论

- **biz off（1x + 忽略业务加速）**：已非常接近优，只差 drop **0.36pp**。
- **biz on（1x + 业务加速）**：当前架构**基本达不到优**。

---

## 7. 已被数据否证 / 已评估的方案

| 方案 | 结论 | 依据 |
|---|---|---|
| 碎缝合并（kept-gap merge，K=12） | **不作为主线** | 2.mp4 真数据：总跳转 683→426（−38%），但 **seek-like 仅 101→90（−11%）**；且预览会藏掉约 **42s** 该保留画面 |
| 碎缝合并 K=30 | 更不可接受 | 藏掉约 147s 保留画面 |
| 继续拧 seek 阈值 / 追帧上限 | 只能在 4%±0.5 徘徊 | 见 §6.1、§6.3 |
| 强设解码线程数 | **有害** | 本机默认已 16 线程；强设 4 让 decode max 由 3.4ms 抬到 11.9ms |
| 时间轴增量绘制 | 对**体感/UI**有效，对 harness 分数**无效** | harness 不起 Tk（§1.3） |

---

## 8. 要在 biz on 下进「优」：四条候选路（必须选一）

跳转成本必须**离开 present 关键路径**：

| 路 | 机制 | 主要代价 |
|---|---|---|
| **深队列 + 呈现时间戳** | 用 100ms+ 缓冲吸收跳转尖峰 | 双时钟、控制路径需 flush、指标要重基线 |
| **第二 cap 独立线程预取** | 后台准备下一落点 | FFmpeg 线程安全（async_lock）、错帧风险；必须开关 + 同步回退 |
| **短 GOP / all-intra 预览代理** | 让 seek 与 grab 都变便宜 | 一次长转码 + 磁盘占用（**当前用户已否决默认开启**） |
| **换引擎（libmpv / PyAV）** | 硬解 + vsync + 关键帧索引全部交出去 | `preview_player` 渲染核心重写 |

---

## 9. 建议执行顺序

### 第 0 步：修好验收跑道（必做，否则后续全是噪声）
- harness 支持同配置重复 **N=3–5** 次并输出中位数
- 文档化「UI 类改动只能用 GUI 验收」
- 清理 §1.2 提到的 5 个无效 histogram log

### 第 1 步：把目标定死（需产品决策）
「优」是要在 **biz off** 还是 **biz on** 下达成？
- biz off：只差 drop 0.36pp，成本极低
- biz on：需要 §8 的架构改动

**这个决定改变后面全部工作量，先问清再动手。**

### 第 2 步：低风险清尾（若目标为 biz off 进优）
两项各自单独 A/B、N≥3：
1. 豁免条件放宽为「任何 `skip_trim` 拍都不 discard」，观察 discarded 是否掉到 ~67 以下，同时盯 **实时比 ≤ 1.05**
2. `_GRAB_SEEK_THRESHOLD` 由写死 100 改为按 frame_dur 推导（16.67ms / 0.53ms ≈ **30 帧**）

### 第 3 步：UI 侧补齐（改体感，不改 harness 分数）
- `_update_labels` 仍每帧调用 `settings.get_params()`（约 20+ 次 Tcl 往返）→ 缓存 + 降频（~4Hz），保留 `_step_frame` 等强制刷新路径
- `_rebuild_static` 仍是 Python 逐段循环 → 向量化（run-length + 一次 pfill），播放中改 `after_idle` 延迟重建
- 依据：GUI 实跑 `UI 32/282ms 顿3255 q丢13`

### 第 4 步：架构选型（仅当目标是 biz on 进优）
在 §8 四条路里选一条，先做**可开关的最小骨架 + 同步回退**，再谈默认。

---

## 10. 禁止事项（均有事故记录）

1. **不要**用 `capped/raw` 缩 `frame_dur`（S2）。raw=10/cap=3 时间隔约 10ms，而单拍常 30ms+ ⇒ 每拍迟到 ⇒ 追帧雪崩。`video_io.py` 内相关注释为权威说明。
2. **不要**禁掉中途 seek 改纯 grab 大跳。7214 帧 grab 走过一次耗时 **3.8 秒**。
3. **不要**强设解码线程数（见 §7）。
4. **代理/预览路径不得泄漏到导出与分析**：`self.video_path` 必须始终指向源片。
5. **分析一律用 A_PT**，不要用默认 OpenCV——否则段表与预览跳表对不齐，所有 seek 指标失真。
6. **一次只改一项**，否则归因作废。
7. 不要为了让分数进「优」而改门槛或改 drop 口径，除非同时说明体感变化。

---

## 11. 附：spikes 归因示例（biz on，A1A2 后）

```
f163896 present=47.0ms lag=47.0ms  [seek, skip_trim:7214, present]
f165815 present=47.0ms lag=49.3ms  [seek, skip_trim:1905, present]
f165899 present=15.0ms lag=18.0ms  [skip_trim:46, present]        ← grab 造成
f166242 present=47.0ms lag=50.0ms  [seek, skip_trim:309, present]
f172816 present=78.0ms lag=63.7ms  [seek, skip_trim:3878, present] ← 最贵单拍
f173856 present=31.0ms lag=19.7ms  [skip_trim:49, present]        ← grab 造成
```

残留尖峰**全部**是跳裁剪（seek 或大跨度 grab），没有 `step:9` 类雪崩 —— S1 步进封顶已生效。
