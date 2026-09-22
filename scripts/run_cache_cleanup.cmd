@echo off
setlocal
chcp 65001 >nul
title CodexCleaner cache maintenance

rem Default to read-only audit. Python owns the fail-closed process checks.
set "CACHE_COMMAND=audit"
if "%~1"=="--apply" (
  if not "%~2"=="" goto usage
  set "CACHE_COMMAND=clean --apply --relaunch"
) else (
  if not "%~1"=="" goto usage
)

where.exe py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%~dp0cache_maintenance.py" %CACHE_COMMAND%
) else (
  where.exe python.exe >nul 2>nul
  if errorlevel 1 (
    echo Python 3 was not found. Install Python before using this helper.
    pause
    exit /b 2
  )
  python.exe "%~dp0cache_maintenance.py" %CACHE_COMMAND%
)

set "CLEAN_EXIT=%ERRORLEVEL%"
echo.
if not "%CLEAN_EXIT%"=="0" (
  echo Maintenance stopped with code %CLEAN_EXIT%. Keep the report for review.
) else (
  if "%~1"=="--apply" (
    echo Cache directories were moved to a backup on the same volume.
    echo This does not reclaim disk space. No backups are automatically deleted.
  ) else (
    echo Read-only audit completed. Nothing was moved or deleted.
    echo To apply: fully exit Codex desktop and CLI sessions, then run this file with --apply.
  )
)
pause
exit /b %CLEAN_EXIT%

:usage
echo Usage: run_cache_cleanup.cmd [--apply]
echo Without --apply, this helper only audits cache sizes.
exit /b 2
