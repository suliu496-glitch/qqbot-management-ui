@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "stop_qqbot_process.ps1" (
  echo Missing stop helper. Close the bot window manually.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_qqbot_process.ps1"
if errorlevel 1 (
  echo Could not stop the bot. Close its window manually.
  pause
  exit /b 1
)
echo Stopped this bot process. QQ and NapCat were left running.
pause
