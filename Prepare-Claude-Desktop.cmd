@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Use this script's directory as the project root (works on any machine/path).
set "PROJECT_DIR=%~dp0"


echo(
echo(=== Prepare Claude Desktop - safe fallback ===
echo(本脚本不会启动 Claude Desktop，也不会自动载入项目。
echo(Project: "%PROJECT_DIR%"
echo(

cd /d "%PROJECT_DIR%"
if errorlevel 1 (
  echo([错误] 无法进入项目目录：
  echo(       "%PROJECT_DIR%"
  echo(请确认路径存在后重试。
  echo(
  pause
  exit /b 1
)

echo([1/3] 打开项目目录...
start "" explorer "%PROJECT_DIR%"

echo([2/3] 打开交接与说明文件...
if exist "%PROJECT_DIR%\CLAUDE_HANDOFF.md" (
  start "" notepad "%PROJECT_DIR%\CLAUDE_HANDOFF.md"
) else (
  echo([警告] 未找到 CLAUDE_HANDOFF.md
)

if exist "%PROJECT_DIR%\CLAUDE_SWITCH_GUIDE.md" (
  start "" notepad "%PROJECT_DIR%\CLAUDE_SWITCH_GUIDE.md"
) else (
  echo([警告] 未找到 CLAUDE_SWITCH_GUIDE.md
)

echo(
echo([3/3] 请手动打开 Claude Desktop。
echo(请在 Desktop 中打开同一项目目录。
echo(复制下面这段接入指令并发送：
echo(
echo(------------------------------------------------------------
echo(读取 CLAUDE.md（若存在）、CLAUDE_HANDOFF.md 和 git status，简要复述当前目标、已有进度、未提交修改、下一步和风险。暂时不要修改文件。
echo(------------------------------------------------------------
echo(
echo(规则提醒：同一时间只允许 CLI 或 Desktop 其中一端修改项目文件。
echo(完成后按任意键关闭窗口。
echo(
pause
endlocal
exit /b 0
