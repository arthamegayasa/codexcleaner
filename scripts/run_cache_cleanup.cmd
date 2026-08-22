@echo off
setlocal
chcp 65001 >nul
title CodexCleaner 性能缓存清理

tasklist.exe /FI "IMAGENAME eq ChatGPT.exe" /FO CSV /NH 2>nul | findstr.exe /I /C:"ChatGPT.exe" >nul
if not errorlevel 1 (
  echo Codex / ChatGPT 仍在运行。
  echo 请先从系统托盘彻底退出应用，再重新双击本工具。
  pause
  exit /b 3
)

where.exe py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%~dp0cache_maintenance.py" clean --apply --relaunch
) else (
  where.exe python.exe >nul 2>nul
  if errorlevel 1 (
    echo 找不到 Python 3。请在 Codex 中运行缓存清理工作流。
    pause
    exit /b 2
  )
  python.exe "%~dp0cache_maintenance.py" clean --apply --relaunch
)

set "CLEAN_EXIT=%ERRORLEVEL%"
if not "%CLEAN_EXIT%"=="0" (
  echo.
  echo 清理没有完整完成，错误码：%CLEAN_EXIT%。请保留窗口内容并交给 Codex 检查。
) else (
  echo.
  echo 可重建缓存已备份并清理，Codex 正在重新启动。
)
pause
exit /b %CLEAN_EXIT%
