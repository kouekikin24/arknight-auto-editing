# 交接文档 2026-09-04（v3-ASS 收官 + 两修复 + 残余症状诊断与方案 C/B）

> 上一棒：HANDOFF_PAUSE_STILL_20260903.md（§7 有 v3-ASS 实施记录与新坑录）。
> 本棒：v3-ASS 全量验证收官；owner 验收发现两残余症状（暗一点 / 默认窗口下移 1px），诊断完成、方案就绪、**未开工**。
> **新会话读 §0 + §3 + §4 即可。**

## §0 待决事项（等 owner 拍板）

1. **方案 C + B 开工与否**——owner 原话"开工之前先分析"，分析已交付（§3/§4）。选项：「开工」=C+B 一起；「先排雷」=只验 C 可行性；「只做 B」=只修 1px。
2. **提交**：v3-ASS + 色彩/几何修复全部未提交（三文件，见 §1），等验收。
3. **推送**：16 个未推送提交（b088b37 起）+ tag `archive/pace-ab-20260901`（GitHub 网络）。
4. 预览声音开关（mpv `audio:no` 故意静音）；暂停处理深改（症状未描述）；PR 草案审阅；清理 ~900MB 缓存（详见上一棒 §0）。
5. **观察项（未决异常）**：pid 11652 实例曾出现"暂停但静帧未覆盖（FPS 计数器仍可见）、帧号定格、合成输入无法播放"，重启后消失，**未定位根因**。当时该实例经历过多开/最小化恢复；新实例（16940）全程正常。若再出现"暂停了但 FPS 计数器还在"——即静帧没盖上——优先查 `_show_paused_still` 的早期返回分支与渲染循环边沿检测。另有观察：全新载入（未跑分析）时右下时间显示 00:00/00:00，两个实例都如此，大概率是"无 EDL 虚拟时间轴"的正常表现，但未有代码级确认。

## §1 代码状态

- HEAD = `455d22d`；工作区未提交：`preview_player.py`（+316/-29）、`mpv_engine.py`（+3）、`HANDOFF_PAUSE_STILL_20260903.md`（§7）。
- preview_player.py 未提交内容 = v3-ASS 全套（解码/化妆分离、place 化、开关回调、自愈）+ 色彩修复（`in_color_matrix`，PyAV 喂 yuv420p）+ 几何修复（`_video_dims` + floor 取整）。
- GUI 运行中：pid 16940（含全部修复；pid 随时过期，以任务栏实例为准）。
- libmpv 版本实测：mpv v0.41.0-926-ge034d612c / FFmpeg N-126175（LUT 类校正与该版本绑定）。
- pytest 430+90 全绿（修复后复跑）。

## §2 v3-ASS 已验证结论（已通过，勿重复验证）

- 帧号双态同源：libass 两侧 + msyh 钉死；标定常数 PlayResY=720、PlayResX=round(W*720/H)、Fontsize=30、MarginL/V=15、Outline=1.65（三尺寸 bbox 零差）。
- letterbox 与 mpv 逐点对齐（floor 取整后 1000x483 → left=71 与 mpv 实测一致）。
- 帧正确性：5409 对金样 mean diff 1.137/p99 3（5407-5411 最小）。
- 启动竞态自愈、开关回调（含播放中取消勾选定格的现存 bug 修复）、缩放防抖重贴均在。
- 详细记录：上一棒 §7；台架资产 `.cache/v3ass/`（calibrate.py / verify_gui2.py 等）。

## §3 残余症状诊断（本轮新进展，全部实测）

### 症状 1：暂停比播放暗（owner 无损截图对实锤）
- 结构：暗部暂停暗 10%、中暗 5%、亮部 3%——**伽马/缩放管线差异的指纹**，非亮度偏移。
- 分层真相：①平坦区残余只差 **+2 灰阶恒定**（小窗口/下采样时）；②大窗口差异放大到 10% 的主因是**上采样路径分歧**：mpv `sigmoid_upscaling=True`（线性光，暗部保亮度）vs 我们 swscale area（伽马空间）。
- 关键方法学发现：**mpv 的 video 模式截图 ≈ 我们输出（恒等）！差异只存在于上屏（window）路径**。之前台架用 video 截图所以测不出来。
- mpv 自证（`video_params` 实测）：未标记流被当作 bt.709 + **bt.1886**。

### 症状 2：默认窗口暂停画面下移 1px（4K 窗口无）
- 静态场景互相关复现：默认 1380x920 窗口下 dy=+1（暂停低 1px）；4K 尺寸 dy=0。
- 已排除：pad 黑边奇数余数分配（mpv 实测 75/76 = 我们一致）。
- 嫌疑：Tk 贴图/mpv 子窗口放置层的 1px 差（位图 1380x703 == 表面尺寸，排除尺寸不符）。
- **剩最后一步**：对比同一表面内 mpv_host 的 winfo_y vs 覆盖层 place 的实际 y（各加 1px 打印探针即可定位）。

## §4 方案 C/B/A（设计完毕，待开工）

### 方案 C（推荐）：静帧让 mpv 自己渲染
PyAV 出精确帧 → 存 PNG（原生分辨率，颜色 709 已对齐）→ **隐藏 mpv 实例**以表面尺寸渲染（同缩放器、同传递转换、同 OSD）→ `screenshot-to-file window` → 贴 Tk。
- 效果：逐像素一致由构造保证（单一渲染器），色彩/缩放/黑边/OSD/下移全消。
- 代价：静帧延迟 ~100-200ms（现 ~50-100ms）；多一个常驻隐藏 mpv 实例。
- **排雷点**（先做这个）：①离屏/隐藏窗口 vo=gpu 渲染可行性（窗口移到屏幕外或 1x1 + keep_open）；②window 截图是否含 target 转换（本轮实测：含）；③PNG 路径的矩阵一致性（PNG 是 RGB，mpv 对 RGB 输入不做矩阵转换——我们的 PNG 必须用 709 转换好，已具备）。
- 注意：mpv 对 **PNG 输入 vs 视频输入**的初始处理链不同（rgb 直通 vs yuv 矩阵），残余差异可能性存在，排雷时对照实测。

### 方案 B：1px 下移定位修复
探针打印 mpv_host/覆盖层的实际 y 坐标差，修 place/pack 的 1px。1-2 小时。

### 方案 A（C 失败时的兜底）：LUT 校正
测 mpv 上屏路径的 256 级 1D 映射（多帧聚合、平坦区采样），生成 `.cube`，静帧链挂 `lut3d`。平坦区可压到 <1 灰阶；边缘纹理差异不可消。

### 反向备选（不做但记录在案）
把 mpv 播放输出简化（`target-trc=bt709` 等）让播放向静帧看齐——会改变播放观感，owner 未表态，不主动做。

## §5 调研结论：没有同类项目解决过这个问题

### 同类项目形态（为什么没有可比作业）
- Tk+mpv 生态全是音乐/油管小播放器（0-6 星），不碰帧。
- 最邻近者 **WebmGenerator**（⭐592，Python+Tkinter+mpv 剪辑工具）：逐帧定位直接用 mpv seek，**不做静帧覆盖**，接受 VFR 不精确（导出时 ffmpeg 重新精确）。其 issue 区无 VFR/帧精确讨论。
- 大嵌入方（Celluloid/mpv.net/SMPlayer）= 单渲染器架构，无此问题。
- mpv 官方手册：VFR framestep "probably not work correctly"（除 -1）。
- 结论：**"帧精确暂停 + mpv 播放"组合我们是孤例**，观感对齐无作业可抄。

### 精确定帧轮子审计（owner 两次质疑"重复造轮子"的完整答案）
- **BestSource**（VapourSynth 生态，最活跃）：R17 起有 PyPI 包（`vapoursynth-bestsource`，Windows wheel 12.3MB），但三把锁——`requires_python>=3.12`（我们 3.11）、强依赖 `VapourSynth>=74` 整个宿主、R21 已用 FFmpeg 9 编译（我们钉 7.1 代，证据链逐位一致性的根基）。
- **FFMS2**（2009-）：重复时间戳正是其著名弱项（#77）。
- **L-SMASH Works**（2011-）：MP4 专精但纯 Avisynth/VapourSynth 宿主插件。
- **DGDecNV**（2008-）：付费闭源 + NVIDIA 锁定 + 宿主插件。
- **decord**（pip 原生，ML 社区）：帧号索引访问思路对路，但最后发布 2021-06（0.6.0 Alpha）、最后提交 2024-07，事实死亡；捆绑 FFmpeg 4 代。
- **NVIDIA DALI / PyNvVideoCodec**：工业级但 CUDA 锁定 + 为训练批量抽帧设计。
- **MLT（Shotcut/Kdenlive）**：源码实证（producer_avformat.c `seek_video`）——帧号→时间戳按**恒定帧率**换算再 seek，VFR 源上换算即失真；Shotcut 源码原话 "This file is variable frame rate, which is not reliable for editing"（劝用户转码）。其暂停机制 "We're paused - use last image"（同一帧重端上桌）是单渲染器一致性的范本。
- 总结论：解码器（libavcodec）和容器定位（libavformat）是商品轮子（PyAV 已封装），"不信任时间戳的最后一百米"（关键帧表+计数+指纹 ≈150 行）无人售卖——所有需要它的项目（MLT/BestSource/FFMS2）都各自本地实现。

### 卡顿战争考证修正
见上一棒 §7 末节（四真实敌人：monotonic 时钟 15.625ms 量化说谎、OpenCV seek 一刀切 60ms、Tk 占拍、缺 vsync 主时钟）。**启示**：未来若复活单管线，要补的只有"呈现时钟"一层，且必须长在渲染栈深处。

## §6 新坑录增量（本轮实测）

1. **mpv window 模式截图才是用户所见**；video 模式绕过显示器目标转换。
2. **大窗口截图被约束**：mpv 自建 1920x1080 窗口实际渲染 1514x852（WM/工作区限制）；1280x720 不缩。标定/测量用小窗口。
3. **python-mpv 的 get_property(字符串) 在本绑定全挂**（-8 不存在）；属性风格 `p.video_params` 才通。`keepaspect_window` 默认会偷改窗口尺寸（标定必须 keepaspect_window=False）。
4. **坐标驱动不可靠**：owner 用电脑时窗口移动 → 合成点击全落空还静默。测量资产优先无损文件（Win+PrintScreen → Pictures\Screenshots）或进程内驱动（verify_gui2 模式）。
5. 聊天贴图是 JPEG 压缩，±2-3 灰阶噪声会掩埋色彩差异——测量必须无损。

## §7 关键位置

- 交接链：本文件 ← HANDOFF_PAUSE_STILL_20260903.md ← HANDOFF_UI_REVIEW_20260902.md
- 台架：`.cache/v3ass/`（calibrate.py 标定、verify_gui2.py 端到端、window_curve2.py 传递曲线、default_size_probe.py / static_shift_probe.py 下移复现、mpv_color_params2.py mpv 自证、analyze_user_pair.py 用户截图分析）
- 用户无损证据对：`C:\Users\AAA\Pictures\Screenshots\屏幕截图 2026-09-04 001850.png`（暂停）/ `001901.png`（播放）
- 静帧链路：`_show_paused_still` / `_decode_still_work` / `_still_burn_ffmpeg` / `_still_filter_chain` / `_build_still_ass` / `_still_color_matrix`（均 preview_player.py）
- mpv 侧：mpv_engine.py options（含 `osd-font` 钉死）

## §8 铁约束（沿用）+ 新规

沿用上一棒 §5 全部。**新增**：
- 进程内驱动探针优先于桌面坐标驱动（owner 明确否决主动采集式电脑控制）；测量只用无损 PNG；owner 证据以 Win+PrintScreen 存盘文件为准（聊天贴图有 JPEG 噪声）。
- 静帧覆盖层**禁止 pack/grid，只能 place**（几何回压已发作两次）。
