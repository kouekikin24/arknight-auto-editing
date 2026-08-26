# HANDOFF — 预览帧精确 + 12 帧误报溯源（2026-08-25）

分支 `fix/preview-pacing-metrics`。本文档交接本轮全部工作、未提交改动、验证状态与遗留事项。
前序交接：HANDOFF_PREVIEW.md、HANDOFF_TIMING.md、HANDOFF_CURRENT.md。

---

## 0. 一句话现状

暂停预览静帧 = 覆盖层（MLT 模式）+ **PyAV 计数定帧（已接线为主选，§5.6）**；
12 个用户报的"误判暂停"帧全部溯源清楚（§1），真根因 = 容器刻度碰撞（§5.7）；
时间戳修复全景已两轮调研+实测（§5.8）；**四维深度调研已于 2026-08-26 完成**
（子代理限额后用户批准改主会话直调），成果 = `RESEARCH_TIMESTAMP_PANORAMA.md`
（mkvmerge 时间轴注入 = 待实测第一候选；GPAC patch_dts 源码级判无效；
BestSource 哈希校验为同思路收官级零件——详见 §5.9）；全部代码改动未提交（§5），等用户验收。

---

## 1. 用户报案的溯源（已闭环）

用户报帧号 `5202, 5257, 5409, 5410, 5474, 5531, 5613, 5711, 5793, 5794, 5892, 5957`
"被划分为一倍速但实际是暂停"。最终结论分两层：

### 1.1 检测层：不是漏检，是"0.2X 状态下的暂停"概念缺口
- 12 帧全部判为 **0.2X**（分 0.30~0.42 << 0.7 阈值；明暗 61~63，真暂停 ~41）。
- 这些 0.2X 段**不在删除集合**：策略是抽帧保留（每 10 帧保 1，analyzer.py `_speedup_mask`），
  残留观感来自抽帧后保留的近静止帧。
- 用户概念校正：这是"暂停后拖干员的 0.2x 尾巴"（大字消失、左侧挂干员面板、CD 慢走）。
- **上游 liemark 没有此划分**：`_classify_gray` 与本地逐字节相同，pause=满屏大字模板
  （templates_pause 就是 PAUSE 字样的 S/U/E 三个字母），单值互斥分类。
- **夹心统计**：全片 573/573 个长度≥5 的 0.2X 游程被 PAUSE 游程前后紧贴（≤1 帧）。
  → 未来修法：分段层把"被 PAUSE 夹心的 0.2X 段"并入暂停 episode。**用户明确推迟：现在不做。**

### 1.2 预览层：真凶是 mpv 暂停寻址停早（已修，见 §2/§3）
- 用户 app 截图（帧 5957）显示 PAUSE 大字+▶；直接解码同号帧是亮屏+||。
- 4 条独立解码路径（ffmpeg select / ffmpeg -ss / OpenCV 随机 / OpenCV 顺序）一致 → 读帧脚本无错。
- 用户截图指纹匹配 PAUSE 段 [5921,5956]（差≈30.7 vs 亮屏段≈42.7）→ app 显示的是上一段。
- 12 个帧号全是**保留段起点=边界**——只有边界会翻错页。
- PTS 表 CFR 对齐（恒定 16.7ms 间隔、恒定 -0.034s 偏移）；帧↔秒换算 `sec*60+2`（头部异常）。

---

## 2. mpv 暂停寻址缺陷（根因，调研结论）

实测 + mpv 源码（player/video.c、playloop.c）+ GitHub 调研：

- **普通 exact seek 不承诺帧精确**：hr-seek 容差 5ms + 寻址期丢帧；实测本机停早 2~3 帧
  （源模式与 EDL 模式都中；最小配置裸 mpv 也中 → libmpv 构建行为，非我方配置）。
- **frame-step 走 VERY_EXACT 路径**（多退 0.5s 解码前进 + 不丢帧 + 零容差）→ 逐帧精确（实测）。
- **没有"当前呈现帧"读回属性**（time-pos 报目标值；estimated-frame-number 官方承认 off-by-one #9206）。
- 旋钮 `hr-seek-framedrop=no` / `hr-seek-demuxer-offset=0.5` 在本机构**无效**（实测仍停早）。
- 业界无任何 NLE 用 libmpv exact seek 做帧精确定位：mpv-cut 靠 frame-step；
  LosslessCut 用 HTML5+ffmpeg（自身精度问题 #1216）；**Shotcut/Kdenlive 的 MLT：
  整数帧号 + AVSEEK_FLAG_BACKWARD 退关键帧 + 解码到目标（预留 2 帧余量）——静帧独立解码**。
- 相关 issue：#9169（exact seek 不精确的社区确认）、#12047/#13013（手机 MP4 seek 错位，
  ffmpeg demuxer 回归，7.1 修复）、#6942（Windows frame-step 连按）。

### 本机 libmpv 身份（tools/libmpv/build-record.txt）
zhongfly/mpv-winbuild **LGPL 配置**、mpv **dev 快照** v0.41.0-926-ge034d612c。
LGPL 砍的是 GPL 组件（x264/x265 编码器、rubberband、vidstab、DVD、SMB 等）——我们全用不到。
咬我们的两条（seek 不精确、无读回）是**全 mpv 共有语义**，非本构建缺失。

---

## 3. 最终架构（已实现并验证）

**播放归 mpv；暂停静帧由独立解码贴覆盖层。** 补丁（补偿闭环）已拆除。

### preview_player.py
- `_show_paused_still(N)`：暂停+原生引擎时启动后台线程解码帧 N。**主线程取好全部
  参数（认证 PTS 时刻 t、ffmpeg_path、画布尺寸）再传线程**；入口节流
  （`_still_decoding` 在飞即跳过）。
- `_decode_still_work`：**FFmpeg 优先**（A_PT 工具链）：**两段式精确取帧**
  `-copyts -ss (pts-0.5) -i … -vf select=gte(t\,pts)`（输入 -ss 单独用在本仓源上
  会停早，5409 实测落进前一段 PAUSE）；OpenCV 仅回退。解码后写
  `self._still_pending` 槽（**工作线程绝不调 Tcl/settings**）。
- 渲染循环 `_drain_still_pending()` 主线程排空 → `_blit_still`：Tk Frame+Label 覆盖层
  盖住 mpv 子窗口，左上角 Tk 自绘帧号（替代被盖住的 mpv OSD），`_still_last_gray`
  留作探针比对；随后追帧收敛（`_still_frame_idx != current` 时补解码）。
- 生命周期：渲染循环边缘检测（播放上升沿 `_drop_still_overlay`；下降沿含片尾自动停播
  贴当前帧静帧）；`load_video` 撤覆盖层。
- **已删除**：landing check 闭环全家（_schedule_landing_check/_verify_landing/
  _landing_check_work/_apply_landing_correction/_source_gray_ref）。
  教训：旧闭环在工作线程调 Tk `after`，异常被静默吞——**在真实播放器里从未工作过**。

### mpv_engine.py
- `_command_seek_locked`：暂停 exact seek **+2.5 tick（Fraction(5,120)）前补偿**——
  保留，但只影响"恢复播放时 mpv 从哪帧接着跑"（≈N），暂停画面与它无关。
- 新增 `source_time_for_frame(frame) -> float|None`（认证 PTS 秒）。
- 已删除 `screenshot_to_file` / `corrective_seek_relative`。
- 落地曲线存档：+1.5 tick 部分边界仍错页；+2.5 tick 全边界落 [N,N+1]；
  曾犯 Fraction(5,240)=1.25 tick 的算术错，扫描全红后修正。

### settings_panel.py（A_PT 前提，owner 大前提："解码后端优先 A_PT"）
- 分析默认解码后端改为 **"FFmpeg软件 A_PT（默认）"**（=ffmpeg_sw_passthrough），
  OpenCV 降为"回退"。探针确认 `默认分析后端: ffmpeg_sw_passthrough`。
- **约束**：analyzer.py:416 的 A_PT 命令图是验证过的，**不得增删时间戳/同步 flag**；
  覆盖层静帧的 ffmpeg 命令是独立命令，不受此约束，但分析管线不许动。
- 前一轮已提交的相关机制（3e672bb）：暂停态 UI 帧号权威（`_apply_native_perf` 不回写、
  `_paused_seek_guard_until` 保护窗）——覆盖层的"帧号立即更新"依赖它，细节见
  HANDOFF_PREVIEW.md。

### 测试
- tests/test_mpv_engine.py：3 处寻址断言更新为带 2.5 tick 补偿值
  （`_fraction_seconds(Fraction(12,100)+Fraction(5,120))` 等；其中 1 处确认播放态不带补偿）。
- tests/test_preview_fps_osd.py：LandingLoopTests 已随闭环删除（文件回到 HEAD 状态）。
- **409 passed + 90 subtests 全绿。**

---

## 4. 验证记录（全部通过）

- 12 帧覆盖层验收：覆盖层帧号=目标，与源同号差 0.4~1.1（像素级），可见、播放即撤。
- 34 点边界+段中指纹扫描（2.5 tick 时代）：0 错页。
- 合成截图 overlay_5957.png：整窗即 5957 亮屏画面，PAUSE 大字消失。
- FFmpeg 静帧判别：d[本帧]≈3.8（跨管线灰度基差，肉眼不可见）<< d[上段]≈23.6，
  且 d[本帧] < d[相邻帧] → 帧号正确。
- 412→409：闭环 3 单测随代码退休。
- 指纹验戳升级验收（2026-08-26）：27 点 ALL PASS（含 2 处验戳校正命中）；
  旧亮度挑选分歧率实测 16/20（慢放撞号位）、11/12（普通位）——见 §5.6.1。

---

## 5. 未提交改动清单（工作区，基线 HEAD=3e672bb，等用户重启验收后一次提交）

```
M mpv_engine.py        # +2.5tick 暂停补偿；+source_time_for_frame；-截图/修正接口
M preview_player.py    # 覆盖层(MLT模式)+PyAV计数定帧+指纹验戳(三级回退)+OSD自绘+节流追帧
M pyproject.toml       # 依赖 +av>=14,<19
M settings_panel.py    # 分析默认后端 A_PT
M tests/test_mpv_engine.py        # 补偿感知的寻址断言（Fraction(5,120)）
M tests/test_preview_fps_osd.py   # 仅末尾 1 行空行差异（LandingLoopTests 加了又删的残留）
?? HANDOFF_PREVIEW_ACCURACY.md    # 本文档，随本次一起提交
?? RESEARCH_TIMESTAMP_PANORAMA.md # 四维深度调研成果（2026-08-26），随本次一起提交
```
（此前 fbaca7f/3e672bb 已提交。）

探针与证据（.cache，不入库）：
- `.cache/probe_overlay_still.py` —— **覆盖层 12 帧验收（可复跑）**：`python .cache/probe_overlay_still.py`
- `.cache/probe_pyav_proto3.py` —— **PyAV 计数定帧原型（G2''' 27/27 通过，可复跑）**；
  首跑自动建关键帧索引表 `.cache/preview_fluency/2_pyav_index.npz`（84.8s）；
  应用内正式表缓存在 `.cache/pyav_kf/{stem}_{size}_{mtime}.npz`
- `.cache/probe_pyav_proto2.py` / `probe_pyav_proto.py` —— 两轮否决记录（pts 匹配不可行）
- `.cache/probe_boundary_sweep.py` —— 34 点边界/段中指纹扫描（2.5 tick 时代 0 错页）
- `.cache/probe_hash_verify.py` —— **指纹验戳 27 点验收 + 新旧机制对照实验（可复跑）**；
  首跑自动建 v2 表（含逐帧指纹，~85s）
- `.cache/probe_hash_cost.py` —— 哈希管线开销对比（planes 直读定案依据）
- `.cache/probe_boundary_display.py` / `probe_recipe.py` —— 复现与落地曲线测量
- `.cache/norm_test/*` —— 时间戳修复 8 项实测（§5.8 表格的原始产物，含失败候选）
- `.cache/frame_shots/*` —— 证据截图（grid_12_full.png、topright_12frames.png、overlay_5957.png 等）

---

## 5.5 审计发现的问题（本轮已修 3 处 + 遗留观察项）

**已修：**
1. ~~暂停时"屏显帧号"OSD 被覆盖层遮住~~ → 覆盖层上用 Tk Label 自绘同样的左上角
   帧号（`_blit_still` 内 `_still_osd`，跟随 屏显帧号 开关）。
2. ~~暂停态拖时间轴连续起 ffmpeg 子进程~~ → `_show_paused_still` 入口节流
   （`_still_decoding` 在飞标志）+ 渲染循环追帧收敛（落地后 `_still_frame_idx !=
   current_frame_idx` 时自动补齐最新目标）。
3. ~~worker 线程静默失败、FFmpeg 从未真正生效~~ → **worker 里调
   `settings.get_params()`（读 Tk 变量）是非主线程调 Tcl，异常被吞还搭 1 秒阻塞，
   一直在走 OpenCV 兜底**。修法：全部 Tk/settings 读取移到主线程
   （`_show_paused_still` 里取好 t 与 ffmpeg_path 作参数传入线程）。
   **教训：后台线程只拿纯值，任何 Tk/settings 访问都留在主线程。**
4. ~~ffmpeg 输入 `-ss` 在本仓源上不精确~~ → 实测帧 5409 的 `-ss pts` 落进前一段
   PAUSE（解封装 seek 偏差，同命令 5202 却精确）。修法：**两段式取帧**——
   `-copyts -ss (pts-0.5) -i … -vf select=gte(t\,pts)`（退 0.5s 粗寻址 + 保留原始
   时间戳 + select 按 PTS 截取）。注意：不加 `-copyts` 时 select 的 t 会被输入
   `-ss` 重置，必须成对使用。实测 4 帧全部精确、耗时 ~0.3s。

**遗留观察项（未修）：**
- 覆盖层图像直接 resize 到 surface 尺寸：窗口非 16:9 时静帧拉伸（mpv 本体 letterbox），
  观感级。
- 认证未就绪时 `source_time_for_frame` 返回 None → 静帧退回 n/fps 定位（VFR 理论
  风险；当前 load_video 已等认证）。
- 暂停态右键菜单行为未验证（覆盖层是 Tk，右键可能已能弹出）。
- 换 libmpv 版本须重测 +2.5 tick 补偿曲线（本机经验值）。
- **PyAV 表首建后台跑（2.mp4 约 75~85s），未完成前 CLI 顶替**——暂停步进在
  "加载后前 ~80s"内走 CLI（同样精确、只是稍慢），表就绪后自动换 PyAV。接手人
  若见到加载初期静帧延迟略高，属预期行为，不是 bug。

**接手人必知（审计补记，踩坑预警）：**
1. **CvEngine（cv 引擎）路径未受影响**：覆盖层有 `native_rendering` 守卫，cv 引擎
   下 `video_canvas` 仍是唯一 RGB 目标（preview_player.py:305,529-533）——静帧体系
   只在 mpv 引擎生效，CvEngine/VideoIOThread 原样保留（长期约束）。
2. **PyAV 表的失效判据是 size+mtime**：同路径同大小同 mtime 但内容不同的文件
   会复用旧表（共享缓存目录 `tools` 下副本、二进制雷同的极端情形）。日常使用
   无风险；做回归测试用多份"同名同长"文件时注意。
3. **线程模型是"单容器串行复用"**：节流保证同一时刻只有一个静帧解码在飞，
   PyAV 容器只被静帧工作线程访问——这是设计依赖（顺序使用无并发）。接手人
   若在别的工作线程也开 PyAV 解码，必须用自己的容器，不得共享
   `_pyav_container`。
4. **`_fps_samples` 等 deque 无视频失效**：换视频时（load_video）这些缓存不清，
   新视频前 1 秒的 FPS 读数可能混入旧样本（瞬时、无积累危害）。已记录，
   未修（观感级）。

---

## 5.6 PyAV 升级（已接线；2026-08-25 三轮原型迭代定案）

owner 批准推进 PyAV（FFmpeg API 层）。经历两轮门禁否决后第三轮定案**已接线**：

- **否决史**：v1 按 pts 匹配认证表 → 碰撞 tick 歧义，全红；v2 碰撞感知 pts 匹配 →
  同样全红。深挖（§5.7）发现真因是容器刻度碰撞 + **PyAV(FFmpeg 8) 的 pts 标签随
  解码上下文变**（从 0 顺序解 vs seek 后解，同一帧标签不同；与认证账本差 0~2 帧
  浮动）——**任何按 pts 匹配的方案都不可行**。
- **最终形态（v3，MLT 计数式）**：一次全片顺序解码建**PyAV 自己的关键帧索引表**
  （`_pyav_table_build_work`，npz 缓存于 `.cache/pyav_kf/{stem}_{size}_{mtime}.npz`，
  按 大小+mtime 失效；2.mp4 首建 74~85s，之后即载）；定帧 = seek 最近关键帧
  （seek 落点有内部错位：越过 K 时退一个 GOP 重试）→ **纯计数**到第 N 帧 →
  碰撞处 ±1 由邻域 3 帧按暗/亮语境挑（同 CLI 路径）。计数对标签偏移/碰撞全部免疫。
- **接线**：`_decode_still_work` 三级链 **PyAV（表就绪时）→ ffmpeg CLI 两段式 →
  OpenCV**；表后台构建不阻塞（未就绪时 CLI 顶替）；`import av` 惰性，未安装
  PyAV 应用照常；容器随播放器生命周期复用（load_video/close 重置）。
- **验证**：原型 27/27（12 用户帧+10 碰撞岛起点+头部+EOF 附近）内容精确
  （d≈0.0）；应用内 11 位（含全部碰撞位）精确、PyAV 调用 11/11；延迟中位
  ~0.12s（CLI ~0.3s 的 1/3~1/2）；单测 409 绿。
- 依赖：pyproject `av>=14,<19`。探针：`.cache/probe_pyav_proto3.py`（最终形态）、
  `probe_pyav_proto2.py`（否决记录）。

### 5.6.1 指纹验戳升级（2026-08-26，方案 A 已落地）

owner 质询"邻域亮度挑选是否存在人眼可分而亮度不可分的选错风险"，读码确认：
旧实现把 ±1 邻域亮度挑选**无条件套用在所有暂停位**（不止碰撞处），结构性成立。
升级为 **计数定帧 + 指纹验戳**：

- **建表**：全量解码时逐帧记指纹（Y 平面 `[::16,::16]` 子采样 blake2b-64，
  ctypes 零拷贝直读平面，实测单帧 ~0.02ms、全片仅 +3s），存入 npz 第三数组
  `frame_hashes`；`_pyav_kf` 变三元组 `(kf_indices, kf_pts, kf_hashes)`。
- **查帧**：`_still_decode_pyav` 直接取计数位第 N 帧 → 与账本 `hash[N]` 对账；
  不等（关键帧踩碰撞刻度致计数基准 ±1）则在 ±2 邻域找指纹相等者校正；
  指纹缺失才退回旧亮度法（过渡兜底）。**"挑图"消失，变为"定位+验戳"。**
- **兼容**：旧 v1 缓存缺 `frame_hashes` key → 加载失败自动重建一次
  （~85s 后台，期间 CLI 顶替）；v2 表已被探针预先建好，应用首启即秒载。
- **验收（`.cache/probe_hash_verify.py`，可复跑）**：
  - 27 标准点 **ALL PASS**：25 处计数位指纹直接命中；11548 与 184290 两处
    **验戳抓到计数基准偏一格并自动校正**（内容对 ffmpeg 参照仍 d=0.4）——
    校正路径实战命中；延迟中位 0.10s。
  - **对照实验（回答 owner 质询的量化证据）**：旧"邻域亮度挑选"相对计数
    真值的分歧率——0.2X 慢放内部撞号位抽样 **16/20**；普通位（非撞号）
    抽样 **11/12**。即旧机制在大多数位置返回的是 N±1，此前未暴露是因为
    冻结画面 N 与 N±1 内容相同/高度相似 + 验证点集中在明暗对比强的边界位。
    新机制下此类任意性不复存在。
- 哈希开销实测探针：`.cache/probe_hash_cost.py`（planes 直读 vs 其余管线）。

## 5.7 真根因：容器刻度碰撞（2026-08-25 深挖定案）

**原始容器的时间戳本身就是乱的**（PyAV demux 全量统计）：
- 184293 包中 **14644 处重复 pts**（两帧共用同一刻度，涉及 49173 帧≈27%）；
- 89363 处非单调/跳跃（B 帧重排 + 录制抖动）；
- 共享刻度共 24576 个。

**认证表是理想化重建**：0 重复、仅头部 4 处异常（认证器把碰撞"裁平"了——这就是
它文档里 tick collision / "cannot independently address source frame(s)" 的含义）。

**后果**：在碰撞 tick 上，**任何按时间寻址的手段（mpv seek、CLI select gte(t)、
PyAV pts>=）都天然歧义**——两帧共用该刻度，取到的第一帧可能是前一段的画面。
这统一解释了本轮全部现象：
- mpv"停早 2~3 帧"= 在碰撞 tick 上呈现了前一段的帧（非 mpv 缺陷，是刻度歧义）；
- +2.5 tick 补偿有效 = 跳过了碰撞 tick 落到下一刻度；
- 认证表头注记 "preview may lag at content transitions" = 认证器早就知道。

**量化暴露**：683 个保留岛起点中 **83 个（12%）踩碰撞刻度**；用户 12 帧中 3 个
（5202/5257/5474）。本源 mpv 在这些位置必然显示前一段。

**碰撞成因与严重性重估（按分析器状态分类 14644 处碰撞）**：
- `PAUSE→PAUSE` **13379（91%）**：静止画面段内部的重复时间戳——采集/编码管线在
  画面冻结时对同一帧盖了重复时间戳（**录制/编码工具不明，不作断言**；本源 91.6%
  是暂停段，故占绝对多数）；两帧内容逐像素相同，取错无观感；
- `0.2X→0.2X` 1177（8%）：慢放段画面近乎静止，同机制；
- **跨切换碰撞仅 87 处**（PAUSE→0.2X 46 + 反向 41）——与 83 个踩碰岛起点吻合，
  这才是唯一有害的子集，已由消歧修复。
- 结论：严重性比数字观感低得多——有害碰撞仅 ~87 处且已消歧；其余 14557 处是
  "同画面双页码"，无害。

**已修（碰撞消歧）**：覆盖层 FFmpeg 取帧改抓 **2 帧**（`-frames:v 2`），按分析器
段落语境挑帧——N 在暂停段内=暗帧(目标亮度~40)、否则亮帧(~62)，取亮度最接近者
（`_expected_bright` + `_still_decode_ffmpeg`）。验证：12 用户帧 + 6 个碰撞岛起点
（5710/5792/11548/12409/13136/14967）**全部精确**；单测 409 绿。

**仍开放的语义边界（记录，不影响预览）**：碰撞 tick 上的两帧在时间上不可区分，
这是容器事实；导出切点若落在碰撞 tick，同样只能靠认证表的裁平约定（既有设计，
frame oracle 体系已在此前提下验证过，不在本轮范围）。

## 5.8 时间戳修复方案全面评估（2026-08-25，调研+实测）

**结论：本文件不存在"无损修时间戳"的路线；成熟方案是 CFR 重编码 conform
（业界标准，有损）或索引定帧绕过（已实现，零损）。**

无损重写候选实测（`.cache/norm_test/`，全部失败）：
| 方案 | 结果 |
|---|---|
| genpts / 纯重封装 / timescale / avoid_negative_ts | 重复 14644 原样（流复制不碰包内 pts） |
| setts BSF `dts=N*256:pts=N*256+PTS-DTS` | 重复反涨至 24600（ctts 偏移在 B 帧处自造重号） |
| 输入 `-r 60` + `-c copy` | 重复 24597（同上，pts=dts+ctts 重建碰撞） |
| 裸 h264 流往返（-f h264 → -r 60 重封装） | **丢 2 帧 + 呈现序倒退 36858**（B 帧顺序破坏，灾难性） |

有效方案实测：
| 方案 | 结果 | 代价 |
|---|---|---|
| **CFR 重编码 conform**（`-fps_mode cfr -r 60`，110s 段验证） | 帧数 6600 精确保持、重复 0、倒退 0、内容对齐（d=0.8） | 一次重编码代际损失 + 全片约 20-40min |
| **索引定帧绕过**（现状：读帧器/分析器/预览覆盖层） | 零损、已验证 | 无 |

调研要点（全面搜罗，两轮：1 轮大报告 + 3 个并行专项）：
- genpts 只补缺失不去重；fps_mode 与 -c copy 不兼容且 vfr 模式靠丢帧；**没有专门
  修重复 pts 的成熟开源项目**（两轮检索一致确认）；
- NLE 厂商官方文档均无 VFR 明文背书（Apple FCP 全手册核验无 VFR 条目；
  Premiere/Resolve/Vegas 未找到官方页）——"转 CFR 再剪"是社区公认经验而非厂商
  承诺（Shotcut 官方论坛原话背书，forum.shotcut.org/t/editing-variable-frame-rate-
  source-clips/43414）；OBS #13396、LosslessCut #1216/#2921 同源问题；
- **setts 实战用例全部是"N 计数器重建"**（yt-dlp FFmpegFixupTimestampPP 平移归零、
  LosslessCut 丢非关键帧后 ts=N/fps/TB 重建）——与本实测一致：保偏移的公式必留
  重复，N 计数公式只在无 B 帧时安全；
- **GPAC/Mp4Box 是唯一有源码级证据的工具**：mp4 muxer 检测 DTS 非递增即补丁成
  严格递增（mux_isom.c L4959，相等亦触发），但只补 DTS、ctts 造成的 pts 重复未
  覆盖——若走此路需实测；
- **TorchCodec**（PyTorch 官方，活跃）：按帧号随机访问的一等 API（get_frames_at），
  decord 的官方认证继任者（torchvision read_video 已 deprecated 并指路它）——将来
  换"按帧号取帧"底层零件时的首选评估对象；
- FFmpeg 官方无帧号寻址，标准优化 = 关键帧预滚 + select 计数（即本项目预览的
  做法，两轮调研双重印证）；工业界（数据集管线）默认无视容器时间戳：顺序解码 +
  均匀采样，或 CFR conform / JPEG 帧序列；
- OpenCV CAP_PROP_POS_FRAMES 公认不可靠（opencv#9053、#27819 负 DTS seek 出错
  直接命中本案）；decord 停滞且不处理重复 pts。

---

## 5.9 四维深度调研（2026-08-26 已完成，成果在 `RESEARCH_TIMESTAMP_PANORAMA.md`）

**历程**：2026-08-25 发 4 个并行子代理全部失败于每日限额；2026-08-26 用户批准
改在主会话直接调研，四个维度全部完成。完整证据、出处与判定见
`RESEARCH_TIMESTAMP_PANORAMA.md`（与本文档同级）。此处只留结论骨架：

- **维度 1 帧服务器**：BestSource（VS 官方、活跃）= 全解码索引 + 帧哈希校验 +
  坏点回退，对重复时间戳免疫——与本项目"计数 + 亮度语境消歧"**思路同源**，
  是未来若重构取帧底层的评估对象（与 TorchCodec 并列）；FFMS2 对重复时间戳
  **直接拒载**（#77），不适用；L-SMASH Works 机制同构无增益。
- **维度 2 容器修复**：`mkvmerge --timestamps/--default-duration`（流复制 +
  外部呈现序时间轴注入；MKV 无 MP4 式 DTS+ctts 双结构）**= 半无损修复第一候选，
  待实测**；GPAC `patch_dts`（mux_isom.c L4959 区）源码级确认**只补 DTS、不碰
  ctts 造成的 pts 重复**→ 对本案无效；bento4 无时间戳重写能力；setts 变量全集
  核对后确认呈现序不可表达 → 理论关闭；IMF/ST 2067-21 结构上无 VFR 母版。
- **维度 3 NLE/广播**：本机网络受限最重（Adobe/Resolve/Netflix Portal/BBC 不可达，
  已在报告 §3.2 列明待补证清单）；已取得的一手旁证 = OBS #13396 记录的
  FCPX/Premiere 对 VFR 的实际失败模式。
- **维度 4 录制端**：OBS #13396 = 本案成因机制的最佳公开证据（硬件编码器
  固定帧率+CBR 仍产 VFR 时间戳，OBS 管线无 CFR 规范化；x264 软件编码不受影响；
  其 workaround 与本仓 CFR conform 命令同款，独立印证）；MediaInfo 的 VFR 判定
  有已知误报（#576/#293）且**无重复 pts 检测**→ 验收必须用解封装级统计（本仓已有）。

**本机网络可达性约束**（报告 §0.2 有表）：github/ffmpeg/mkvtoolnix/OBS/Apple/
微软/MediaInfo/Doom9 可达；Adobe helpx 内容页、Google 系、Wayback、Wikipedia、
Blackmagic、Android issue 库不可达——维度 3/4 的相应条目已如实标注为"待补证"。

---

## 6. 遗留与后续方向

1. **四维调研已完成**（2026-08-26）→ 成果 `RESEARCH_TIMESTAMP_PANORAMA.md`。
   调研总方案已回归，**实测与否等 owner 拍板**。待实测清单（报告 §7）：
   1.1 **mkvmerge 时间轴注入**（半无损第一候选）：认证表 → v2 时间码 →
       `mkvmerge -c copy --timestamps 0:tc.txt 2.mp4 -o 2.mkv`，复用
       `.cache/norm_test` 检查脚本做 重复/倒退/内容对齐 三项验收；
   1.2 BestSource / TorchCodec 作为未来取帧底层零件的评估（非紧急）；
   1.3 GPAC patch_dts 命令行语法（预期收益低，可不做）。
2. **用户重启验收**（当前运行的 app 是旧代码）：暂停跳帧应指哪帧是哪帧；首次
   打开视频后台建 PyAV 表约 75~85s（只此一次，缓存秒载）；暂停后画面延迟
   PyAV≈0.12s / CLI≈0.3s；连按 ←/→ 每下精确一帧。
3. **提交未提交改动**（验收通过后，按 §5 清单一次提交，含
   `RESEARCH_TIMESTAMP_PANORAMA.md` 与本文档）。
4. **夹心规则**（检测侧）：被 PAUSE 夹心的 0.2X 段并入暂停 episode——方案已定，用户推迟。
5. **0.2X 裁剪策略**（抽帧倍率/整段删）：后续方向，用户明确"不是现在"。
6. 旧队列：hwdec 实验（~30min）、P1 导出测量、mpv 真实变速段支持、小清理。
7. 若将来换 libmpv 版本：重测暂停寻址落地曲线（2.5 tick 是本机构经验值）。

---

## 7. 长期约束（逐字保留）

- 不要直接 push upstream；不使用 git reset --hard / git checkout -- / git clean。
- 用户说"主动停止"时立即停止，且不留 FFmpeg/mpv/Python 媒体后台进程。
- 不重跑四个完整 frame oracle 长扫描；不重复已关闭的 sample-3 [316,326) 候选扫描；
  不放宽 10ms 内容阈值。
- 禁止 frame/fps 时间戳回退造正式 EDL/媒体时间。
- CvEngine/VideoIOThread 保留；不运行未审计的 arknight-preview-pack.zip。
- 生产 MpvEngine 仅限 preview-only 范围（owner 已批准预览路线）。
- **解码后端优先 A_PT**（owner 大前提，本轮新增）；OpenCV 仅回退。
- **0.2X 裁剪/夹心并入 = 后续方向，现在不做**（owner 指示）。
- **调研先行、实测后置**：时间戳方案的实测评估等四维调研回归后再定（owner 指示）。
- **子代理工作方式**：不限时间；慢就多开并行分任务（owner 指示）。

---

## 8. 2026-08-27 工具链与导出新结论（本轮新增，防上下文压缩丢失）

### 8.1 工具链版本矩阵（实测）
同一个视频可能被**三个不同年代的 FFmpeg** 解码：
| 通路 | FFmpeg 版本 | 用途 |
|---|---|---|
| ffmpeg.exe | 7.1 / 7.1.1 | 分析 A_PT、导出、取证 |
| PyAV 18.1 | libavcodec 62 = **FFmpeg 8.x** | 暂停定帧、建指纹表 |
| OpenCV 4.13 | libavcodec 58 = FFmpeg 4.4 | 模板匹配、成像、CvEngine |
**关键**：身份关键路径（指纹建表+验戳）**已全走 PyAV 自洽**，三版本不一致的隐患
被天然挡住；CLI/OpenCV 只在静帧显示回退链上、非身份关键。统一原则=职责清晰，
不为统一而砍工具。

### 8.2 OpenCV 不可替代（实证）
`cv2.matchTemplate`（analyzer.py:86,125）就是**暂停/变速检测的算法本体**——
认画面，不是读视频。ffmpeg/PyAV 只会解码、不会识别，**换不掉**。唯一撞车的
"读视频"已被压成回退/帧数权威（`CAP_PROP_FRAME_COUNT`），且 CvEngine 受保留约束。

### 8.3 PyAV GPU 编码——已实测跑通（纠正"不能"的误判）
- 正确入口：`output.add_stream("h264_nvenc", rate=fps, hwaccel=HWAccel(device_type="cuda"))`
  + `stream.pix_fmt="cuda"`，喂普通 yuv420p 帧即可（PyAV 自动上传 GPU）。
  **不是**给 `CodecContext.hwaccel` 赋值（该属性不可写，之前走错入口）。
- 已在 RTX 4060 验证：产出合法 h264、30 帧、回读内容渐变正确；
  `allow_software_fallback=False` 下成功 = 确走显卡。
- 证据：`.cache/research/pyav_nvenc_test.mp4`。N 卡 cuda / A 卡 amf / Intel qsv。

### 8.4 导出三模式 + PyAV 化真实差距（纠正"滤镜图很复杂"的含糊说法）
导出有三条路，**全部用 FFmpeg 编码**：
1. 快速滤镜：`trim=帧区间+setpts` 每段再 `concat`（段数适中）；
2. 刻度精确：扁平 `select`+分段 `setpts`（`_pts_select_setpts_video_filter`，
   避开 O(n²) 与表达式解析深度 ~100 段上限）；
3. 逐帧兜底：**OpenCV 逐帧解码→按掩码过滤→管道喂 ffmpeg 编码**（analyzer.py:2138）。
**"挑选/拼接要手写"是错的——模式 3 已经是 Python 版（~30 行循环）。**
PyAV 化的真实差距只剩三条：
- **音频混音**：滤镜图 `atrim/asetpts` 一把梭 → PyAV 需新写 ~100 行（解码音频→
  按时间切→重计时→aac→对齐画面），最易埋 sync bug；
- **性能倒退**：逐帧 Python 循环比 C 滤镜图慢（模式 3 只当兜底就是这原因）；
- **验证重来**：现有路径已验证，PyAV 版需重证。
**结论**：导出继续用 ffmpeg.exe——不是能力问题，是音频/性能/验证三条务实理由。

### 8.5 待办快照
- 指纹批次已提交（`bf621f2`）；重启肉眼验收可选（自动化已全绿：409+90→415+90、27 点探针）。
- **【主线已完成】PyAV 整合任务**（ffprobe/ffmpeg→PyAV，导出除外）三阶段全部落地并通过
  验收：元数据探测与分析解码默认走 PyAV（各带 env 回退开关），av 锁 13.1 与 ffmpeg.exe 7.1
  同代、全片 184293 帧 states/diffs 逐位一致。详见 **`HANDOFF_PYAV_CONSOLIDATION.md`**。
- 决策队列：B（mkvmerge 实测，外部需求触发）/ 夹心 / 0.2X 裁剪 / 可选加固
  （指纹报警器、新视频建表提示）。
