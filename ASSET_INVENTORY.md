# 资产与能力清单（ASSET_INVENTORY）— 任何任务先翻这里

> **持久化约定**：本文件应提交进 git，且每份新交接的 §0 必须写
> "新会话先读 ASSET_INVENTORY.md"。否则它会像它要替代的叙述一样被压缩丢掉。
> 路径相对仓库根；`../` 指 `D:\ArknightsPathFinding\`。
> "故意不跟踪" = .gitignore 有明确注释（证据留盘），非遗漏。

## 一、外部参照

| 资产 | 路径 | 身份 | 用途 |
|---|---|---|---|
| 真·源码上游 | `../arknight-auto-editing-upstream` | liemark git(HEAD含PR#9) | parity 唯一基准 |
| 发布包 26.7.15/23 | `../arknight-*-win{-fix,}` | 非git,编译exe | 成品行为参照 |

## 二、运行时依赖

| 资产 | 路径 | 跟踪 |
|---|---|---|
| libmpv-2.dll | `tools/libmpv/dll/` | 故意不跟踪 |
| ffmpeg 7.1.0 | `tools/ffmpeg-7.1.0/` | 故意不跟踪 |
| PyAV 关键帧表 | `.cache/pyav_kf/*.npz` | gitignore,按size+mtime失效 |

## 三、源码模块（未提交??，等验收）

`preview_player.py`(改) `mpv_engine.py`(改) `media_info.py` `pts_timeline.py`
`frame_pts_certifier.py` `timeline_plan.py` `project_state.py` `task_manager.py`
`media_exporter.py` `edit_commands.py`；`tests/` **大部分已跟踪**+新增未跟踪。

## 四、台架探针（.cache/v3ass/，gitignore）

标定calibrate / 端到端verify_gui2 / 色彩transfer_curve+mpv_color_params2 /
几何probe_b_geometry+default_size_probe / C排雷spike_c*+c4/c5/c6+duo_instance /
验收verify_c4_prod+step_latency_probe+c4_region_diag。结论见§六。

## 五、决策与交接

- 交接链(已跟踪): HANDOFF_{STATE_0827,EXPORT_PERF_0830,PR_STRATEGY_0901,
  UI_REVIEW_0902,PAUSE_STILL_0903,V3ASS_0904}.md 等~11份；
  未跟踪: HANDOFF_C4STILL_0904.md、本文件。
- 决策(故意不跟踪): MPV_GATE_DECISION_*_v{1,2}.json、MPV_PHASE0_{SPEC,RESULTS}.md、
  PRODUCTION_{PTS_GOLDEN,VFR}*、arknight-preview-pack.zip、_wf_plan.json。
- 已跟踪: UPSTREAM_FIX_PR_DRAFT.md、AV_REVIEW_AUTOVERIFICATION.md。
- 用户证据(仓外): `C:\Users\AAA\Pictures\Screenshots\屏幕截图 2026-09-04 00*.png`。
- **文档架构(分工防 proliferation)**：AGENTS.md=规矩(自动读)；
  checklist.md=当前任务工单(本地不提交,已入.git/info/exclude)；
  HANDOFF_*.md=会话状态链；本文件=资产/能力/决策/铁律。新文档先登记分工。

## 六、已证明能力表（防漏组合；证据为gitignore探针，结论亦见交接）

| 能力 | 证明 | 可组合 |
|---|---|---|
| wid 嵌 mpv 到 Tk 子窗 | C6/mpv_host | G 双窗 |
| 双 vo=gpu 同进程稳定 | duo_instance | G 双窗 |
| 离屏 Toplevel 无任务栏+运行时改尺寸 | C6 | G/隐藏 |
| 单帧 Y4M 与播放位级一致(mad0.0001) | C4 | G/C4 |
| window截图=用户所见; video≠ | spike_c | 测量法 |
| 落帧:无修正=边界陈旧帧;+5/120=过冲+1 | landing/4 | 不可mpv定帧 |
| C3 互换:reload视频67ms/恢复51ms | spike_c | 2B/G 取舍 |
| osd_duration=1 杀闪且 overlay 活; osd_level=0 杀overlay(禁) | c4 | OSD |
| 内嵌暂停保留 fps 角标(延迟清) | c4 | OSD 复刻 |
| PyAV容器非线程安全 / resize与vo重配竞态 | 段错误 | 单worker+settle |
| 跨双屏被DPI重采样污染 | c4右条 | 测量须单屏 |
| (提案未证) G双窗原生呈现≈80-100ms且不碰内嵌状态 | 本轮分析 | 待开工 |

## 七、铁律（唯一权威源；交接只引用不复制）

- **R1 parity强制**：引擎移植/改动先出 parity 表再写码。模板三列：
  `功能 | 上游(liemark)有无+行为 | mpv有无+行为`，性质标"砍/差异/新增/持平"。
- **R2 先盘再造**：实现前 grep 上游+本表§六+已有探针；没盘完不许造轮子。
- **R3 数据先行**：先找可直读定谳信号写一行测量计划；卫生=单屏/无损PNG/DPI感知。
  探针首行必须 `from probe_guard import assert_measurement_hygiene` 并调用；
  方案/计划模板强制带 **parity表+ACH矩阵** 两栏，缺栏视为不合规（opt-in→默认）。
- **R4 清单维护(带牙齿)**：每次更新交接必须同一次提交触碰本文件；
  新能力/资产当场登记§五/§六，否则视为不存在。
- **R5 ACH强制(治确认偏误)**：任何症状/停滞诊断，先列≥2个竞争假说，
  建§九证据矩阵，用"不一致证据"淘汰，禁止单假说找支持。
- **R6 事实/判断分离+结论分级(ICD203)**：结论必须分开"观测事实"与"我的推断"，
  置信度给依据，并定级：**T0**=位级实测/逐位一致，**T1**=多源验证可复现，
  **T2**=单源推断，**T3**=猜测。**门禁：生产代码只准嵌入 T0/T1；T2/T3 必须
  显式标注且不得进生产，须 owner 确认后才可升格**（对齐 1Ark 铁律⑧）。
- **R7 制度封版**：本轮规范化后**只维护、不新增**制度件；确需新增须先登记
  §八 并标"防膨胀"理由。治文档陈旧/膨胀（借鉴 1Ark D-12）。

## 八、决策登记（ADR-lite，唯一权威；防"静默默认"）

| 日期 | 决策 | 上下文 | 选择 | 后果 | 状态 |
|---|---|---|---|---|---|
| 09-04 | mpv剥离业务变速 | 引擎拒播速度策略,日用保护 | 忽略业务参数按原速+提示 | 业务加速静默失效(重大失误) | 待owner确认(拟补第1步) |
| 09-04 | 静帧=C4隐藏截图回贴 | 需帧精确+同管线 | 隐藏mpv+Y4M+截图+Tk贴 | 步进~660ms偏慢 | 拟被G取代(待开工) |
| 09-04 | 制度封版 | 规范化完成,防膨胀 | 只维护不新增(R7)+T0-T3门禁(R6) | 制度件冻结 | 已定 |

## 九、ACH 矩阵（R5 的工具；症状诊断必用）

模板：行=证据，列=竞争假说，格=一致/不一致/?；选"不一致最少"者。
实例(c4右条,2026-09-04)：见正文分析；H2跨屏DPI重采样以0不一致胜出，
H1帧偏移/H3色彩差各3不一致被淘汰。若当时强制ACH,误判第一步即死。

## 十、硬机制工具（R1-R6 的机械版；先跑再动手）

| 工具 | 机制化条款 | 行为 |
|---|---|---|
| `tools/probe_guard.py` | R3 | 探针开头断言单屏+DPI+无损，不达标拒跑 |
| `tools/preflight.py` | R1/R2 | 打印§六能力+上游parity提醒+R1-R6勾单 |
| `tools/inventory_audit.py` | R4 | 比对 asset_index.json 报新增/消失；`--write` 重建基线 |
| `asset_index.json` | R4 | 机读资产索引(Backstage 目录的 json 版)，audit 的登记基线 |
