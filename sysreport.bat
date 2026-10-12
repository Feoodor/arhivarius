@echo off
rem Arkhivarius: computer + Ollama report (ASCII only - Russian text is printed by Python)
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.11+ from python.org and check "Add to PATH".
  pause
  exit /b 1
)
python sysreport.py --bench %*
pause
