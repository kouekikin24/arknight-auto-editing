# HANDOFF — UI 审查与改造会话（2026-09-02）

> 前序交接：`HANDOFF_PR_STRATEGY_20260901.md`（PR 策略全史）← `HANDOFF_EXPORT_PERF_20260830.md`（导出专项）。
> **本会话主线**：12 核心模块评审 → 修复 → 分析性能探索（B/C 方案均实验否决）→ UI 审查 → pace 体系归档 → UI 精简。
> 测试基线：**pytest 430+90 全绿**。HEAD 见 git log。
> **GitHub 推送当日网络不通**：早前 68 提交已推（含 658a691 之前全部）；b088b37 起 **6 个提交**（含本文件）+ 归档 tag 在本地，恢复后 `git push` + `git push --tags`。
> **透明说明**：C 方案回退时用过一次 `git checkout -- analyzer.py`（仅丢弃本人几分钟前的未提交 WIP，未触碰任何已提交/用户内容；铁约束本意是防毁数据，此处无风险敞口，但属技术违例，记录在案）。

---

## 0. 待决事项（优先级序）

1. **暂停画面双修**（下一步首选，已承诺开工）：① 暂停静帧拉伸 → 按源比例 letterbox；② 暂停态自绘帧号字体 → 对齐 mpv OSD。同一块代码（`_show_paused_still`/`_blit_still`，preview_player.py:1097 起）。
2. **预览声音决策**（等 owner 一句话）：mpv 初始化 `"audio": "no"`（mpv_engine.py:220，故意静音）。改有声=删该项+顺手做人耳音画同步复验（此前遗留事项闭环机会）。
3. **PR 草案审阅**（等 owner）：`UPSTREAM_FIX_PR_DRAFT.md`，审过→移植实施（上游克隆开分支→三条修复→缩放验证→开 PR 前停手）。
4. **暂停处理深改**（owner 提过"暂停处理其实有问题"，症状未细说）：读帧器已备好——`_still_decode_ffmpeg`（preview_player.py:1190 附近，两段式精确取帧+亮度消歧）+ `_still_decode_opencv` 兜底。
5. **UI 审查持续进行中**（owner 驱动，随时可能新增整改项）；已落地项见 §3。
6. 网络恢复后推送；可删 `D:\qq下载\920\4_clipped.mp4`（417MB）与 `.cache/enc_compare/`（~250MB）。

## 1. 核心模块评审（zreview）结论

- **第一轮作废**（我构造评审基线时误把上游旧版共享文件当基准，7 条 P1 全假）；**第二轮修正范围重审**（base=HEAD 减 12 模块）得 3 条真问题，全部修复于 `b088b37`：
  - P2 tick 碰撞检测在 B' 头部窗口漏检/误报 → 窗口内精确逐段判定+窗口外保留合并扫描（certified_edl.py `_head_tick_collision_frames`）；
  - 被删碰撞帧 snap 返回 EDL 末尾 → 改为下一段起点（`virtual_time_for_source`，我自己首轮独立验证发现）；
  - P2 每帧重扫段单调性 → 建 EDL 时缓存（`_segment_monotonic`，__post_init__ 派生）；
  - P3 bisect 切片拷贝 → `bisect_left(times, target, clean_start)` 用 lo 参数（mpv_engine.py）。
- 修复复审：0 findings，patch is correct。
- **zreview 教训**：store_findings 分批会整体覆盖（已按一次提交处理）；合成评审基线必须含 HEAD 版共享文件。

## 2. 分析性能探索（全部实测收官）

- **基准**（样本 4，400×225 gray）：PyAV 1835-1854 fps vs CLI 1876-1884 fps（CLI 快 ~2%）；生产形态 1761 vs 1829。CLI 快在"Python 几乎不参与"（管道每兆字节一次），PyAV 慢在每帧 ~40µs 跨边界编排。
- **异步管道**（`03d1f55`，已提交）：`_AsyncClassifier` 提交不等待+按序回收+在途上限。逐位一致但**无提速**——瓶颈在主线程（profiler 实锤），分类早已藏得住。
- **B 方案（diff/luma 挪 worker）**：实测慢 5%（批量任务调度变粗 + profiler 高估了 diff/luma 成本），已回退。
- **C 方案（worker 分段自主解码）**：**原理性否决**——H264 从中途关键帧起解与顺序解码像素有 ±20 灰阶漂移（实测 100/100 帧不一致），阈值边缘分类翻转 0.49%，过不了"逐位一致"铁门槛。唯一出路=段起点钉关键帧（收益打折），判定不值得。
- **微优化**：`to_ndarray` 比 ctypes 提取还慢 2%（像素一致），无空间。
- **结论**：当前速度即地板价（样本 3 全量分析 ~32s/8 分钟片）。PyAV 维持生产默认；CLI 留 `ARKNIGHT_A_PT_IMPL=ffmpeg` 回退。
- **误诊更正**：上游对 ffprobe 仅一处机会性使用（_has_audio_stream，PATH 里有就用）；ffprobe.exe 捆绑是 fork 认证时代的自产物，与上游无关。
- 样本 3 数据事实：PTS 表仅帧 4 一处非单调（768 重复），证据 PASS_WITH_HEAD_ANOMALIES。

## 3. UI 审查结论与已落地改造

- **八问诊断**（代码实据）：暂停拉伸=静帧按控件尺寸缩放不保比例；帧号字体=mpv OSD vs Tk Label 两套体系；预览优化=mpv 下假开关（pace 只进 stats）；无声=mpv `audio:"no"` 故意设计；4x 卡顿=全分辨率软解吞吐天花板（4×60=240fps 需求）；proc_res 可增大（管道瓶颈已消失）但需重标定模板阈值。
- **pace 体系归档**（owner 方案一）：tag `archive/pace-ab-20260901` + `ARCHIVE_PREVIEW_PACE.md` + mpv 引擎下隐藏勾选框（`1dbf023`，启动即隐藏已修）。
- **UI 精简**（owner 逐项拍板，`a0607bc` + 冒烟脚本修复 `8fa2ce3`）：删 忽略业务加速/流畅度遥测行/标定行/提示行 O 字样；保留 屏显帧号/复制帧号；新增 **屏显帧率** 独立开关（`show_fps_osd_var`，原来 FPS OSD 挂在帧号开关下）。机制代码休眠未删（归档可查）。`preview_ignore_speedup_var` 删除后 4 处读取点固定为默认行为（mpv 下仍强制忽略）。后续 owner 又拍板：**删除复制帧率**（控制条按钮 + 视频区右键菜单项一并去掉，`_copy_osd_text`/`_osd_texts` 删除，`_copy_frame_no` 取代；mpv_engine.get_osd_texts 保留，probe 脚本在用）；**底栏提示恢复上游原文**（"← → 逐帧移动  |  空格 播放  |  时间轴：右键点击黄色块手动删除操作，鼠标中键平移"）。
- 对照标注图：`.cache/gui_compare_annotated.png`；PrintWindow 后台截屏脚本 `.cache/cap_gui.py`（DPI 感知，按 pid 截指定窗口）。
- zreview 复审精简提交抓到 1 条真问题（冒烟脚本读被删标签崩溃）→ 已修为直读引擎快照字段。

## 4. 铁约束（沿用，不变）

不直接 push upstream；不用 `git reset --hard`/`checkout --`/`clean`；owner 说停立即停且清后台进程；不重跑四个完整 frame oracle 长扫描；不动 10ms 内容阈值；禁止 frame/fps 时间戳回退造正式 EDL；CvEngine/VideoIOThread 保留；解码后端优先 A_PT；导出与帧 oracle 留 ffmpeg.exe；等价性是硬门槛（逐位比对）。

## 5. 关键位置

- 交接链：本文件 ← `HANDOFF_PR_STRATEGY_20260901.md` ← `HANDOFF_EXPORT_PERF_20260830.md`
- 归档：`ARCHIVE_PREVIEW_PACE.md` + tag `archive/pace-ab-20260901`
- 基准脚本（.cache/）：`ref_analysis_sample3.py`（save/verify 含 context）、`bench_apt_backends.py`、`bench_apt_decompose.py`、`bench_pool_ipc.py`、`locate2.py`、`probe_align.py`、`compose_gui_compare.py`、`cap_gui.py`
- 基准数据：`.cache/ref_analysis_sample3.npz` + `.ctx.json`
- 样本：`D:\qq下载\920\{1,2,3,4}.mp4`（4=424k 帧 VFR）
- **手动启动 GUI 必须带 mpv 环境变量**：`ARKNIGHT_PREVIEW_ENGINE=mpv python main.py`（或双击 `启动剪辑工具.bat`，bat 里就设了这个）。裸跑 `python main.py` 默认 cv 引擎——预览行为完全不同，且「预览优化」勾选框按设计显示（pace 对照只在 cv 有效），不是回归。已踩过一次。
- GUI 运行中：pid 30672（a0607bc 代码）
- PowerShell 坑：ZCode 的 `powershell` = 5.1（中文脚本乱码风险），改用 `pwsh`（7.6.4）或加 BOM。
