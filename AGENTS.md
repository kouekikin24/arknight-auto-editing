# AGENTS.md — 本项目给 AI 的常驻规则（每次会话先读）

> 本文件=**规矩**(how)，稳定少变；状态看 HANDOFF，工单看 checklist，
> 资产/能力/决策看 ASSET_INVENTORY。铁律权威源在 ASSET_INVENTORY §七，
> 本文件只引用不复制，防漂移。

## 读序
1. ASSET_INVENTORY.md（资产/能力/铁律/决策）
2. 最新 HANDOFF_*.md（会话状态）
3. checklist.md（当前任务工单，本地不提交）

## 运行约定
- GUI 必须 `ARKNIGHT_PREVIEW_ENGINE=mpv` 启动。
- 测量卫生(R3)：单屏窗口(主屏x<3840)、无损PNG、DPI感知；进程内驱动优先，
  禁主动桌面采集。新探针首行 import probe_guard 并断言。
- 开工前跑 `tools/preflight.py`；定期跑 `tools/inventory_audit.py` 防漂移。
- 调研：owner 说禁子代理则主会话亲查。

## 授权门（不可越）
- 不 commit/push/PR/tag，除非 owner 明说「验收/提交/推送」；
  功能实现需「开工」。
- 不用 `git reset --hard`/`checkout --`/`clean`（恢复用 `git show ref:file`）。
- 不重跑四个 frame-oracle 长扫描；不动 10ms 内容阈值。
- 停止口令立即停并清后台进程。

## 文档分工（防 proliferation）
- AGENTS.md=规矩；checklist.md=当前任务工单(本地不提交)；
  HANDOFF=会话状态链；ASSET_INVENTORY=资产/能力/决策/铁律。
- 新文档必须先在此登记分工，否则不建。
