# 交接文档 2026-09-03（暂停静帧改版回退 + B 台架收官 + v3-ASS 待开工）

> 上一棒：HANDOFF_UI_REVIEW_20260902.md（UI 审查与精简）。本棒：静帧改版事故→回退→方案重定；时间戳修复台架（B）实测收官。
> **新会话从 §0 + §4 + §5 读起即可。**

## §0 待决事项（等 owner 拍板）

1. **v3-ASS 开工与否**——方案已完整（§4），口令「开工」。
2. **预览声音**：mpv `"audio":"no"` 故意静音；开=删一行+人耳复验音画同步。
3. **暂停处理深改**：owner 提过"暂停处理其实有问题"，症状未描述。
4. **PR 草案**：UPSTREAM_FIX_PR_DRAFT.md 待审。
5. **推送**：分支 `fix/preview-pacing-metrics` 共 **16 个**未推送提交（b088b37 起） + tag `archive/pace-ab-20260901`（GitHub 网络中断滞留；上次探测 github.com 000 / api.github.com 200）。
6. **清理**：`D:\qq下载\920\4_clipped.mp4`(417MB)、`.cache/enc_compare/`(~250MB)、`.cache/bench_b/mkvtoolnix/`(~250MB)。
7. UI 审查持续中（owner 驱动）。

**评审覆盖状态**：b088b37→b81e232 经两轮完整 zreview 通过（0 达到门槛的问题）；静帧改版诸提交（e3757af→c9744a6）已被回退覆盖，当前树的静帧代码=已评审状态+1 行标定修复（efd1f4f，P3 评审发现）。v3-ASS 实施后按新规走小审查+像素验证，除非 owner 明说"zreview"。

## §1 本次会话改动链（含回退，净效果）

- `b81e232`：底栏提示恢复上游原文 + 删除复制帧率（按钮/右键菜单/`_copy_osd_text`/`_osd_texts`）；mpv_engine.get_osd_texts 保留（probe 脚本在用）
- `efd1f4f`：休眠标定路径死引用修复（zreview P3 发现）
- `d298138`：**静帧改版整体回退**到 b81e232 状态（仅保留标定修复）
- 文档：`BENCH_B_20260903.md`（台架全记录）、本文件
- **当前暂停静帧 = 原始行为：拉伸但可靠**。GUI 运行中 pid 39108（d298138 代码）
- **GUI 运行中**（pid 39108，d298138 代码——进程可能已死，用时重查）。
- **A3 方案（纯 mpv 暂停显示）要点**（深改首选架构，已搁置）：预览副本双胞胎撑开（gap 待阶段 0 实测 mpv 最小可分辨间隔，候选 8ms）+ EDL 时刻表换源 + 暂停走 mpv seek + 静帧层退为回退；工期 ~3 天；触发器=暂停深改立项。
- **成熟预览方案调研结论**（2026-09-03，主会话亲查）：四流派——播放器（时间寻址，帧精确非目标）/ 网页剪辑器（LosslessCut：HTML5 预览+低质副本）/ NLE（代理媒体+预览缩放+CFR conform）/ 帧服务器（索引数帧）。我们帧号权威+零重编码变速的组合业界无同款；差距在舒适性工程（无代理→4x 天花板、无帧缓存）。派生媒体是行业惯例——ctts 修复副本即其无损变体。
- 已作废提交（仍在历史里，被回退覆盖）：e3757af/f01310b/fdd36c8/16f134a/7d0b440/c9744a6

## §2 静帧改版事故复盘（三个 Tk 暗坑，下不为例）

1. **几何传播**：子组件图片过大 → Label/Frame 请求大尺寸 → 窗口被撑爆。
2. **pack 空间瓜分**：覆盖层与 mpv 原生窗口同 pack expand=True → 视频区被平分（断成两节）。覆盖就该用 place。
3. **未映射组件 winfo=1**：新建 Canvas 在映射前读尺寸得 1 → 居中坐标算成 (0,0) → 图片只剩四分之一。
4. **隐藏坑 0**：`MediaInfo` 本体没有 `.width`——尺寸在 `media_info.video_streams[0].width/height`；getattr+默认值把错误吞成静默回退（letterbox 从未生效却被我"截图验证"误判为生效——游戏画面的黑边被当成 letterbox 黑边）。
5. **教训（新规矩）**：UI 布局类改动 = 小审查 + **运行时像素测量自验**（三场景：导入即暂停/播放→暂停/暂停中拖窗）；静态评审看不见布局涌现行为。

## §3 B 台架结论摘要（全文见 BENCH_B_20260903.md）

- **四样本双胞胎普查**（解封装级）：1/2/3/4.mp4 全部 13~14% 重复 PTS（每 7 帧 1 帧），录制管线系统性产物；双胞胎 296/300 内容不同（非复制帧）。
- **mkvmerge 路线证伪**（时刻表完美但双胞胎序 44% 被调换，不可控）。
- **自研 ctts 微手术全项满分**：`.cache/bench_b/ctts_patch.py`，产物 `repaired_ctts.mp4`（750MB 金样）；重复清零、映射 184293/184293 零误差、内容逐位一致、ffmpeg 严格解码零错。
- **mpv 实测**：原文件 seek 在双胞胎附近连普通帧都偏一帧；修复文件普通帧指哪打哪；双胞胎 65µs 间距低于 mpv 容差 → 帧号权威永远归索引表。
- **依赖体检**：5 个声明依赖全在干活（共 ~331MB）；全局 Python311 环境有 ~700MB 无关包（scipy/playwright 等，零引用）；本机 8 个 venv 全部 3.11.9，无 3.12。
- 方向 A（TorchCodec）owner 已否（依赖过重）。

## §4 v3-ASS 方案（已获 owner 认可设计，待「开工」）

**目标**：暂停静帧 = letterbox 保比例 + 帧号与 mpv 播放态观感一致。

**设计要点**：
- 静帧走 `_still_decode_ffmpeg`（ffmpeg CLI 路径），滤镜链：
  `select=gte` → `scale=W:H:force_original_aspect_ratio=decrease` → `pad=显示区:黑` → `ass=临时.ass`
- ASS 样式对齐 mpv OSD 默认值：Alignment=7、MarginL=25、MarginV=22、白字黑描边 Outline=1.65、Fontname 两侧钉死（mpv 侧加 `osd_font="Microsoft YaHei"`）
- 字号：mpv=30 缩放像素随窗高；实测播放态 31px @ 显示区 985px（比例 0.0315），标定一次迭代到位
- **已验证**：本地 tools ffmpeg 编进 libass（ass/drawtext 滤镜都在）；演示渲染与 mpv OSD 对比图 `.cache/bench_b/osd_vs_ass.png`（观感一致，差一档字号）
- **已实测的坑**：PyAV 13.1 捆绑的 ffmpeg **没有** libass（ass/drawtext 均无）→ 烧字只能走 CLI 路径（0.3s/张，可接受；备选优化=常驻渲染进程）
- 重贴触发器：media_info 就绪时（探测 ~3.8s）+ Configure 防抖 180ms；落地钳制作底兜
- **不改布局管理器、不引入 Canvas**——上次三个坑全是动布局动出来的
- 验收协议：三场景像素测量（内容矩形 1.778±0.02、OSD 位置 (25,22)±4px、字高差<15%）+ pytest 全绿，自测通过后交付

## §5 铁约束（沿用）+ 新规

不直接 push upstream；不用 `git reset --hard`/`checkout --`/`clean`（恢复文件用 `git show <ref>:file > file`）；owner 说停立即停且清后台进程；不重跑四个完整 frame oracle 长扫描；不动 10ms 内容阈值；禁止 frame/fps 时间戳回退造正式 EDL；CvEngine/VideoIOThread 保留；解码后端优先 A_PT；导出与帧 oracle 留 ffmpeg.exe；等价性是硬门槛（逐位比对）。
**新增**：
- **zreview 只在 owner 明说"zreview"时启用**；普通改动走小审查+运行时验证
- **手动启动 GUI 必须带 `ARKNIGHT_PREVIEW_ENGINE=mpv`**（或双击 启动剪辑工具.bat），裸跑 `python main.py` 会走 cv 引擎
- 调研禁止子代理时主会话亲查（WebFetch/curl；github.com 不通时用 api.github.com）

## §6 关键位置

- 交接链：本文件 ← HANDOFF_UI_REVIEW_20260902.md ← HANDOFF_PR_STRATEGY_20260901.md
- 台架：`.cache/bench_b/`（demux_stats.py / ctts_patch.py / verify_content.py / twin_identity.py / mpv_seek_test.py / 各 npz + repaired_ctts.mp4）
- 静帧链路：`_show_paused_still`(preview_player.py:~1043) / `_blit_still` / `_decode_still_work` / `_still_decode_ffmpeg` / `_still_decode_pyav`
- mpv OSD：mpv_engine.py `_osd_overlay`(an7=帧号 an9=帧率)
- 基准数据：`.cache/ref_analysis_sample3.npz` + `.ctx.json`
- 样本：`D:\qq下载\920\{1,2,3,4}.mp4`（全部带 ~13.6% 双胞胎）
