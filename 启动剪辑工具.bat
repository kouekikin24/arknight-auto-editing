@echo off
rem 明日方舟剪辑工具 - 双击启动（mpv 预览引擎）
rem 关键：带上 ARKNIGHT_PREVIEW_ENGINE=mpv，否则走老的 OpenCV 引擎，看不到新功能
cd /d "%~dp0"
set ARKNIGHT_PREVIEW_ENGINE=mpv
python main.py
pause
