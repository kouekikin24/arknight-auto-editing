# 交接文档 2026-09-04（B：1px 下移修复 + C4：隐藏 mpv 自渲染静帧）

> 上一棒：HANDOFF_V3ASS_20260904.md（v3-ASS 收官 + 方案 C/B 设计）。
> 本棒：owner 口令「开工」→ B + C 一起实施、验证完毕。**未提交，等验收。**
> **新会话先读 ASSET_INVENTORY.md（资产/能力/铁律唯一权威源），再读本文件 §0+§1+§3。**

## §0 待决事项（等 owner 拍板）

1. **B+C4 验收**：口令「验收」→ 提交 `preview_player.py` + `mpv_engine.py`
   （现全部在工作区）。owner 视觉复验方法：默认窗口暂停/播放切换，
   画面应不再下移、不再变暗（残余 ±2 灰阶随机噪声，肉眼不可见）。
2. 沿用上一棒 §0 其余项：推送（16 提交+tag）、预览声音、暂停处理深改、
   PR 草案、缓存清理、pid-11652 观察项。
3. 首帧暂停延迟 ~0.7s（含隐藏实例一次性创建 ~0.4s），后续 ~0.3-0.4s；
   旧实现 ~0.1s。若 owner 觉得暂停变慢，可调（见 §4 优化方向）。

## §1 本次改动（全部未提交）

### preview_player.py
- **B 修复**：`_blit_still` 的 Label 钉 `bd=0, highlightthickness=0`；
  居中改显式整数 floor（`_recenter_still_label`，覆盖层 Configure 重居中）。
  根因：Tk `place(relx=.5,rely=.5,anchor=center)` 对奇数高度取整不对称
  （round(m/2)-c//2=+1）→ 位图 y=+1（默认窗口 703 高=奇数；4K 偶数不发作）。
- **C4 后端**（主路，失败自动回退旧 ffmpeg 化妆链）：
  - `_ensure_still_mpv(w,h)`：主线程建/复用隐藏实例——离屏
    `+20000+20000` 的 `overrideredirect` Toplevel（无任务栏按钮）+
    `mpv.MPV(wid=…)`；运行时改 geometry 免重建；resize 记 settle 截止点。
  - `_still_render_mpv(frame,w,h,osd_text)`：工作线程——PyAV 计数帧 →
    yuv420p 平面 → 单帧 Y4M（C420mpeg2）→ loadfile → osd-overlay 帧号
    （an7，与播放同 libass）→ `screenshot-to-file window` → PIL。
    帧精确由构造保证（**无时间寻址**，免疫落帧/刻度碰撞问题）；Y4M=未标记
    YUV → mpv 走与真实视频相同色彩启发式；window 截图含上屏转换。
  - `_decode_still_work`：PyAV 帧先走 C4，None 才回退 `_burn_av_frame`。
  - `_teardown_still_mpv()`：close() 调用；等 worker 离窗 ≤1s 再 terminate。
  - 竞态保护：resize 后 settle 0.4s；命令前 identity 检查（teardown/重建）。

### mpv_engine.py
- `osd-font: Microsoft YaHei`（上一棒已加，仍未提交）。

## §2 验证结论（实测，勿重复验证）

- **B**：探针 `probe_b_geometry.py`——label (0,0)、edges top 暂停=播放=0、
  平移扫描 (0,0)。修复前 dy=+1 复现。
- **C 排雷**（spike_c/landing/c4/c5/c6）：
  - 离屏自建窗 & wid-Toplevel 均可渲染+window 截图；wid 形态运行时改尺寸免重建。
  - 落帧规律：无修正 exact seek 在 PTS 边界显示**前一帧**；+5/120 修正
    **过冲 +1**（60fps 区）。→ C4 用 Y4M 彻底绕开寻址。
  - C1（视频+寻址）window 截图 ≈ 内嵌上屏（静态区 mad 0.004）；
    C2（PNG 输入）有 sRGB/bt1886 分歧 mad 1.18 → 弃；
    **C4（Y4M）内容区位级一致 mad 0.0001/max 7**。
- **生产验收** `verify_c4_prod.py`（不 patch 任何生产逻辑）：
  - 暂停 vs 播放：mad **0.19-0.38 / p99 ≤4**（旧实现 1.26 / p99 20 = 暗症状）；
    平移 (0,0)。残余=Tk 合成层随机噪声，非系统偏差。
  - 暂停中改尺寸、暂停中步进（帧号正确、画面非黑）均正常，无崩溃。
  - pytest **430+90 全绿**。

## §3 新坑录

1. **探针窗口跨屏=测量污染**：owner 双屏（主 3840@150% / 副@175%，边界
   x=3840）。跨边界窗口被 DWM 混合 DPI 重采样，右侧 ~140px 抓取失真
   （曾误判为帧偏移）。探针窗口必须完整落在单屏（主屏用 +2400）。
2. **PyAV 容器非线程安全**：探针主线程与生产静帧 worker 并发
   `_still_decode_pyav` = 段错误。生产自身串行（_still_decoding 节流）安全。
3. **mpv loadfile 闪暂停/播放指示图标 ~1s** → 隐藏实例 `osd_duration=1`；
   注意 `osd_level=0` 会连 osd-overlay 一起杀，禁用。
4. 内嵌实例暂停后 **FPS 角标延迟清除**（短暂仍在）；复刻 OSD 时以
   「暂停稳态」为准：只复刻帧号 an7。
5. **resize 与 mpv vo 重配竞态**：改 wid 窗口尺寸时 mpv 内部线程异步重配
   d3d11 交换链，同期命令=段错误。settle 0.4s + identity 检查 + teardown
   grace 三保险（已实现）。
6. 双 vo=gpu 实例同进程稳定（duo_instance 三连验证）；崩溃皆为上述竞态。

## §4 关键位置与资产

- 代码：preview_player.py `_ensure_still_mpv` / `_still_render_mpv` /
  `_write_still_y4m` / `_teardown_still_mpv` / `_recenter_still_label`。
- 台架 `.cache/v3ass/`：probe_b_geometry.py、spike_c.py、
  spike_c_landing{,4}.py、spike_c4.py、spike_c5.py、spike_c6.py、
  verify_c4_prod.py、duo_instance.py、c4_region_diag.py、
  step_latency_probe.py + 各 results json。
- 优化方向（未做）：隐藏实例预热到 load_video（消首次 0.4s）；Y4M 写内存盘。

## §4b owner 复验反馈（2026-09-04 晚）与待拍板优化

1. **业务变速段按原速播放**——设计现状，非 bug：mpv 引擎对带业务变速
   （speedup_1x/speedup_02/speed_segments）的请求直接拒播
   （mpv_engine.py MPV_SPEED_POLICY_UNSUPPORTED）；日用路径 preview_player
   主动剥离业务参数按原速播并提示（preview_player.py:1902-1915）。
   mpv 真支持=新特性（源帧边界→运行时 speed 切换），owner 未拍板不做。
2. **步进延迟账单**（step_latency_probe.py，深区 ±1 步进实测 ~660ms/步）：
   PyAV seek 1ms + 计数解码 ~28ms/3帧（首调冷启动 1.1s 离群）+ C4 固定开销
   ~360ms（250ms 保守睡眠 + 66ms 截图 + 11ms 写盘 + blit）。旧化妆链同量级
   （解码 + ffmpeg 子进程），非 C4 回归。优化杠杆（待拍板）：
   ① 砍 250ms 睡眠改条件等待（-200ms）；② 步进先撤覆盖层露出内嵌 mpv 的
   即时寻址帧（感知 ~100ms），精确静帧落地后替换（±1 边界旧帧窗口 <0.5s）。

## §5 铁约束 + 新规（沿用 V3ASS §8，新增）

- 探针窗口完整落单屏；测量只用无损 PNG；进程内驱动。
- 隐藏实例禁止 osd_level=0；OSD 只复刻帧号 an7。
- 其余（不 push upstream、禁 reset --hard/clean、停口令即停、不重跑四 oracle、
  覆盖层只 place、zreview 明说才启用等）全部沿用。
