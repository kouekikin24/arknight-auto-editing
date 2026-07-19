@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Use this script's directory as the project root (works on any machine/path).
set "PROJECT_DIR=%~dp0"


echo.
echo === Open Claude CLI ===
echo Project: "%PROJECT_DIR%"
echo.

cd /d "%PROJECT_DIR%"
if errorlevel 1 (
  echo [错误] 无法进入项目目录：
  echo        "%PROJECT_DIR%"
  echo 请确认路径存在后重试。
  echo.
  pause
  exit /b 1
)

where claude >nul 2>&1
if errorlevel 1 (
  echo [错误] 未在 PATH 中找到 claude 命令。
  echo 请先确认 Claude Code CLI 已安装，并可在本机终端直接运行 claude。
  echo.
  echo 备用：在本目录手动执行
  echo   claude
  echo 或
  echo   claude --resume
  echo.
  pause
  exit /b 1
)

echo 正在优先恢复本目录最近一次 CLI 会话...
echo 命令: claude --continue
echo.

call claude --continue
set "EC=%ERRORLEVEL%"

if not "%EC%"=="0" (
  echo.
  echo [提示] claude --continue 未成功结束，退出码: %EC%
  echo 可能原因：本目录尚无历史会话，或恢复过程被中断。
  echo.
  echo 你现在仍位于项目目录。可手动尝试：
  echo   1^) claude
  echo   2^) claude --resume
  echo   3^) 先阅读 CLAUDE_HANDOFF.md 与 CLAUDE_SWITCH_GUIDE.md，再开新会话
  echo.
  echo 注意：本脚本不会跳过权限检查，也不会修改项目文件。
  echo.
  pause
  exit /b %EC%
)

endlocal
exit /b 0
