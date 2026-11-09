# 净室重构 Prompt（盲测版）

> 用途：交给一个**独立 agent**，让它在不知道团队历史方案的前提下，重新构想
> 预览流畅方案。本文档是可直接复制粘贴的完整 prompt。
>
> 生成日期：2026-10-24。修正依据：10-24 对净室可行性的实测审计
> （发现 4 处必须修正、2 处建议修正）。
>
> **重要**：本文档本身属于"答案"，**不要放进净室**。

---

## 使用说明（给 owner，不给接手 agent）

1. 复制下面「═══ PROMPT 开始 ═══」到「═══ PROMPT 结束 ═══」之间的内容，发给接手 agent。
2. 净室路径 `D:\ArknightsPathFinding\arknight-cleanroom`，与主工作区完全隔离。
3. 净室会缺运行时依赖（gitignore 域），prompt 第 0 步用 junction 挂载。
4. 本 prompt 已按实测修正以下问题：
   - 补删 4 份会泄题的文档（`RESEARCH_PHASE3_20260831.md` 等）
   - 删除 5 个测试文件名会泄露方向的测试
   - 显式声明 `skim_cache.py` 无法删除（HEAD 中被 import）
   - 显式声明 dsvc.exe 与 HEAD 源码不同代
   - 加入 graphify 过滤版的使用边界（explain 可信 / path 须核对）
5. 阶段 5 之前，agent 不得读主工作区、不得看 git 历史。

---

═══ PROMPT 开始 ═══

你是性能架构师。任务：为一个视频剪辑工具重新设计"预览流畅"方案。这是一次
独立设计：你需要自己提出假设、自己做测量、自己下结论。不要预设任何结论。

═══════════════════════════════════════════
第 0 步：搭建净室（必须严格照做）
═══════════════════════════════════════════

主工作区含未提交的"别人的答案"，禁止读取、禁止 ls、禁止 grep。
所有工作在净室进行。以下命令在 **cmd** 中执行（不是 Git Bash，mklink 只在 cmd 可用）。

  cd /d D:\ArknightsPathFinding\arknight-auto-editing-main
  git worktree add -b cleanroom/redesign D:\ArknightsPathFinding\arknight-cleanroom HEAD

  cd /d D:\ArknightsPathFinding\arknight-cleanroom

  REM 1) 挂载运行时依赖（gitignore 域，用 junction 零拷贝）
  mkdir .cache
  mkdir .cache\v3ass
  mklink /J .cache\v3ass\dsvc        D:\ArknightsPathFinding\arknight-auto-editing-main\.cache\v3ass\dsvc
  mklink /J .cache\v3ass\ffmpeg-dev  D:\ArknightsPathFinding\arknight-auto-editing-main\.cache\v3ass\ffmpeg-dev
  mklink /J .cache\analysis          D:\ArknightsPathFinding\arknight-auto-editing-main\.cache\analysis
  mklink /J .cache\media_info        D:\ArknightsPathFinding\arknight-auto-editing-main\.cache\media_info
  mklink /J .cache\pyav_kf           D:\ArknightsPathFinding\arknight-auto-editing-main\.cache\pyav_kf
  mklink /J tools\libmpv             D:\ArknightsPathFinding\arknight-auto-editing-main\tools\libmpv
  mklink /J tools\ffmpeg-7.1.0       D:\ArknightsPathFinding\arknight-auto-editing-main\tools\ffmpeg-7.1.0

  REM 2) 删除泄题文档（净室内删，不影响主工作区）
  del ASSET_INVENTORY.md
  del HANDOFF_*.md
  del asset_index.json
  del RESEARCH_PHASE3_20260831.md
  del RESEARCH_TIMESTAMP_PANORAMA.md
  del BENCH_B_20260903.md
  del ARCHIVE_PREVIEW_PACE.md
  del DESIGN_EXPORT_BATCHED_SEEK.md
  del UPSTREAM_FIX_PR_DRAFT.md
  del AV_REVIEW_AUTOVERIFICATION.md
  del CLAUDE_SWITCH_GUIDE.md
  del CLAUDE_HANDOFF.example.md
  del CLAUDE_HANDOFF.md 2>nul
  REM ↑ CLAUDE_HANDOFF.md 在 .gitignore 域，worktree 里通常不存在，2>nul 容错
  rmdir /s /q agents\research_proxy_materialize
  rmdir /s /q agents\research_analysis_cache

  REM 3) 删除会泄露方向的测试（测试名本身就是提示）
  del tests\test_preview_dthrough_tier.py
  del tests\test_skim_cache.py
  del tests\test_dthrough_ledger.py
  del tests\test_dthrough_service.py
  del tests\test_preview_skim_rebuild.py
  del tests\test_analysis_cache.py
  del tests\test_proxy_cache.py 2>nul
  REM ↑ 该测试属于未提交代码，HEAD 中不存在，2>nul 容错
  del tests\test_pts_normalized_proxy.py
  del tests\test_verify_proxy_audio_av.py
  REM ↑ 实测补删：虽不含方案线索，但文件名带 "proxy" 会误导方向

  REM 4) 自证净室干净（以下命令必须全部无输出）
  git grep -l "物化代理" -- .
  git grep -l "proxy_cache" -- .
  git grep -l "40x" -- .
  dir /b HANDOFF_*.md
  dir /b tests\test_dthrough* tests\test_skim*

【禁止事项】
- 禁止读主工作区（D:\...\arknight-auto-editing-main）里的任何文件。
- 禁止在阶段 5 之前看 git 历史：不要 git log / git show / git branch -a
  （提交信息会泄露既往方案）。
- 禁止 commit / push / tag / PR。

【必须知道的既有现状（不是答案，但会影响你的判断）】
- `skim_cache.py` 是 HEAD 里已有的休眠旧模块，被 `preview_player.py` 导入，
  因此**无法删除**。它代表一条已退役的旧思路：你可以读它、测量它、否定它，
  但不要把它当作"正确答案"。
- `.cache/v3ass/dsvc/target/release/dsvc.exe` 是现成可用的解码服务二进制，
  与 HEAD 里的 `tools/dsvc/src/main.rs` **不完全同代**。优先直接使用该 exe，
  不要重建 Rust 服务（重建会引入差异，干扰测量）。
- HEAD 中已有 `dthrough_*`（一个既有的解码直通实现）。它是"现状"，
  不是"答案"：你可以测量、质疑、替换它。

═══════════════════════════════════════════
产品背景
═══════════════════════════════════════════

《明日方舟》录播自动剪辑工具：导入录屏 → 自动识别"暂停/无操作"段并删除 →
识别 0.2x/1x/2x 变速段 → 用户在时间轴上微调 → 导出成片。
技术栈：Python 3.11 + Tkinter + 现有播放/解码模块（含一个 Rust 解码服务）。

【代码地图（只是导航，不代表它是对的）】
- 预览：preview_player.py、mpv_engine.py、video_io.py
- 分析与导出：analyzer.py、media_exporter.py、timeline_plan.py
- 解码服务：dthrough_service.py + tools/dsvc/（Rust，源码可改）
- 时间轴/编辑：timeline_widget.py、project_state.py、edit_commands.py

【真实素材（必须用，禁止合成替代）】
- D:\qq下载\920\4.mp4：2.27GB，7069.9s，1920x1080@60fps，H.264 长 GOP
- D:\qq下载\920\2.mp4：750MB，3071.8s，含系统性重复 PTS 与 VFR 抖动
- D:\qq下载\920\3.mp4：118MB，497s（快速迭代用）
注意：中文路径可能让某些库打不开，可用 NTFS 硬链接绕开
（PowerShell: New-Item -ItemType HardLink）。
若路径不存在，停下来问，不要假设。

═══════════════════════════════════════════
目标体验（这是需求，不是方案）
═══════════════════════════════════════════

1. 预览支持 UI 倍速 1x/2x/4x；0.2x 慢动作段在预览时按 10x 业务速度呈现，
   即 UI 4x 时该段达到 40x 源帧速（2400 源帧/秒）。
2. 播放稳定 60fps，无可感知停顿（你自己定义量化判据，如 >300ms 停顿数=0）。
3. 暂停态定位与逐帧步进必须帧精确（落点误差 0 帧）。
4. 预览节奏必须与导出成片一致（同样的片段、同样的变速）。
5. 编辑后能快速继续播放（你自己定义"快速"阈值并测量）。
6. 预览优化不得降低导出质量。
7. 保留段非常碎：真实项目中位段长约 11 帧（0.18 秒）。
   **必须用产品自带分析在真实素材上跑出真实段表来测试；
   禁止用人工合成的均匀长段表代替**——合成段表会系统性掩盖问题，
   得出的"没问题"结论无效。

【环境事实】
Windows 10/11 x64；约 16 核 CPU；RTX 4060 Laptop。
可用：Python 3.11、ffmpeg 7.1、PyAV、libmpv、Rust/cargo。
外部库可以调研，但需记录许可证与集成成本。

【可选辅助工具：graphify（代码知识图）】
净室可用一个已装好的代码图工具，帮助快速理解 7 万行代码：
  python tools/graphify_build.py                    # 构建过滤版图（约 1-2s）
  python tools/graphify_build.py --explain _send_play
  python tools/graphify_build.py --path preview_player proxy_cache
**能力边界（实测）**：
- `--explain`（函数被谁调用/调用了谁，带行号）：可信，建议用。
- `--path`（两个模块间最短路径）：**不可直接采信**。根因=函数内局部
  import 不被 AST 建模（如 `def f(): import x`），且"两模块本无直接依赖"
  时会返回真实但反直觉的最短路径。**path 结果必须回源码人工核对。**
这是辅助工具，只读代码结构，不提供任何方案提示。

═══════════════════════════════════════════
工作流程（按顺序，阶段间向我汇报）
═══════════════════════════════════════════

阶段 1｜理解与建模（不写生产代码）
- 读代码，画出当前预览路径：解码在哪、渲染在哪、时间轴如何映射。
- 用真实素材测出当前实现的实际表现（帧率、停顿、seek 延迟、
  CPU/内存/磁盘占用）。先有基线数字，再谈方案。
- 列出至少 3 种候选架构。可以是完全不同的思路，由你判断，不要预设方向。

阶段 2｜假设与判据
- 每个候选写出：预期收益、预期代价、可能失败的方式。
- 定义可判定的验收判据（数字）+ 测量方法（工具、样本、重复次数）。
- 把基线数字与候选清单发给 owner 确认方向，再进入原型。

阶段 3｜原型与测量
- 对至少 2 个最有希望的候选做可运行原型（脚本级即可，不必接 UI）。
- 测：吞吐、停顿分布、seek 精度、编辑后恢复时间、磁盘/内存占用。
- 每次测量记录口径（同机、同源、同计时方式），保证数字可比。
- 测量优先在进程内读取真实对象状态，不要靠截屏估算。
- 失败要如实记录，不要只报成功的部分。

阶段 4｜裁决
- 推荐方案 + 为什么其他候选被排除（用实测数字，不用直觉）。
- 明确列出"我没有测什么、为什么没测、会不会改变结论"。
- 接入现有产品的成本估计（改哪些模块、风险点）。

阶段 5｜盲对照（完成阶段 4 后才可做）
- 现在才可以读主工作区的：
  - `ASSET_INVENTORY.md` §六/§八、`HANDOFF_*.md`、`checklist.md`
  - `agents/research_proxy_materialize/`（六份行业调研）
  - `agents/research_player_kernels/`（内核实测、跨源冒烟、cq 扫描）
  - `agents/research_player_kernels/HANDOFF_DECISION_PACKAGE.md`（决策简报）
  - git 历史
- 对比：你的结论与团队历史结论是否一致？不一致时指出分歧点与你的证据。
- 不要因为"文档这么说"就改自己的结论；以你的实测为准。

═══════════════════════════════════════════
硬约束
═══════════════════════════════════════════
- 不修改主工作区；不 commit/push/tag/PR。
- 不删除任何文件；需清理先列清单等 owner 确认。
- D:\qq下载\920\ 下素材只读。
- 结论区分"观测事实"与"我的推断"，标注置信度
  （T0=位级/可复现实测，T1=多源验证，T2=单源推断，T3=猜测）。
- 若需求本身物理不可达，直接说，并给出你能达到的最好结果，不要编造数字。

═══════════════════════════════════════════
交付物
═══════════════════════════════════════════
写到 D:\ArknightsPathFinding\arknight-cleanroom\REDESIGN_REPORT.md（不要提交）：

0. 你读过的文件清单（用于审计净室是否干净）
1. 当前实现基线（实测数字）
2. 候选架构清单（≥3）及各自先验分析
3. 原型与测量结果（口径、原始数据、失败记录）
4. 推荐方案 + 成本/风险 + 接入计划
5. 未测项清单
6. 盲对照结论（与团队历史结论的一致/分歧）
7. 一句话总结

【时间盒】
阶段 1-2 控制在 2-3 小时内并向我汇报；原型阶段预算 1-2 天，超出先汇报。

═══ PROMPT 结束 ═══

---

## 附录：净室完整性自查（owner 用）

净室搭好后，在主工作区执行以下检查确认没有泄题：

```bash
cd D:/ArknightsPathFinding/arknight-cleanroom
# 1) 文档已清空
ls *.md 2>/dev/null            # 期望只剩 README.md / LICENSE
# 2) 关键方案词无命中
git grep -l "物化代理" -- .    # 期望无输出
git grep -l "proxy_cache" -- . # 期望无输出
# 3) 方向性测试已删
ls tests/ | grep -E "dthrough|skim|proxy"   # 期望无输出
# 4) 运行时依赖可用
.cache/v3ass/dsvc/target/release/dsvc.exe --version 2>&1 | head -2
ls tools/libmpv/dll | head -3
```

**已知残余泄题风险（诚实登记）**：
1. `skim_cache.py` 无法删除（被 preview_player 导入）——已在 prompt 中显式声明。
2. `dthrough_*.py` 是既有实现，构成"现状提示"——已在 prompt 中声明为"现状非答案"。
3. git 历史仍含方案线索——只能靠"阶段 5 前禁止 git log"约束，无法物理阻断。
   若要更严格，可在独立 Windows 用户账户下运行净室。
