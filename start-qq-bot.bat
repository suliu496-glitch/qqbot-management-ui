@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
if not exist ".env" (
  echo Missing .env. Run install.bat and edit .env first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "from dotenv import load_dotenv; import os,sys; load_dotenv(); missing=[k for k in ('MY_QQ','MASTER_QQ','SELF_NICK','DEEPSEEK_API_KEY') if not os.getenv(k,'').strip()]; print('Missing configuration: '+', '.join(missing) if missing else 'Configuration present. Starting bot...'); sys.exit(1 if missing else 0)"
if errorlevel 1 (
  echo Edit .env and fill in the required fields. Do not share it.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "%CD%\bot.py"
echo Bot stopped. Read the message above if startup failed.
pause
