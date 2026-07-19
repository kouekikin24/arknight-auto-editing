# CLI ↔ Desktop 快速切换

工作目录（两端必须相同）  
包含这些脚本的**仓库根目录**（即 `Open-Claude-CLI.cmd` / `Prepare-Claude-Desktop.cmd` 所在目录）。

本机示例（仅供参考，不是通用要求）：  
`D:\ArknightsPathFinding\arknight-auto-editing-main`

两个 `.cmd` 入口会自动把工作目录设为脚本自身所在目录，一般无需再手写绝对路径。

## 铁律
- 同一时间只允许 **CLI 或 Desktop 一端** 修改项目文件
- 不靠聊天记录同步；靠 Git 状态 + `CLAUDE_HANDOFF.md`
- 不在交接文件写密钥 / Token / 账号
- `CLAUDE_HANDOFF.md` 为本地草稿（已 gitignore）；仓库内模板见 `CLAUDE_HANDOFF.example.md`

## 离开当前端之前
1. 停写，保存已改文件  
2. 更新 `CLAUDE_HANDOFF.md`（若本地尚无该文件，可先复制 `CLAUDE_HANDOFF.example.md`）  
3. 可选：查看 `git status`

### A. 离开时（复制给当前端 Claude）
```text
请根据当前对话与 git status 更新 CLAUDE_HANDOFF.md：填写当前目标、已完成、修改过的文件、测试结果、下一步、阻塞与风险。不要写入密钥或敏感信息，不要提交 Git，更新后用 5 行以内总结。
```

## 进入另一端之后
1. CLI：双击仓库根目录下的 `Open-Claude-CLI.cmd`（或 `cd` 到仓库根目录后 `claude --continue`）  
2. Desktop：双击仓库根目录下的 `Prepare-Claude-Desktop.cmd`，再手动打开同一项目目录并粘贴接入指令

### B. 进入时（复制给另一端 Claude）
```text
读取 CLAUDE.md（若存在）、CLAUDE_HANDOFF.md 和 git status，简要复述当前目标、已有进度、未提交修改、下一步和风险。暂时不要修改文件。
```

## 快捷入口
| 方向 | 操作 |
|---|---|
| → CLI | `Open-Claude-CLI.cmd` |
| → Desktop | `Prepare-Claude-Desktop.cmd`（仅准备，不自动启动 Desktop） |
