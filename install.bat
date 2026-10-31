@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] Checking Python 3.11 or later...
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or later with the py launcher is required.
  echo Download it from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
echo [2/3] Creating a local virtual environment...
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 (
  echo Failed to create virtual environment.
  pause
  exit /b 1
)
echo [3/3] Installing Python dependencies from PyPI...
".venv\Scripts\python.exe" -m pip install "httpx>=0.28.1" "mcp[cli]>=1.28.1,<2" "python-docx>=1.1.2" "websockets>=16.0" "python-dotenv>=1.0" "playwright>=1.61.0" "pydantic-settings>=2"
if errorlevel 1 (
  echo Dependency installation failed. Check network connectivity and try again.
  pause
  exit /b 1
)
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Installation complete. Edit .env, then set up NapCat and read README.md.
pause
