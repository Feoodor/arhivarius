@echo off
rem Arkhivarius launcher (ASCII only - Russian text is printed by Python)
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.11+ from python.org and check "Add to PATH".
  echo Or use Arkhivarius.exe - it needs no Python.
  pause
  exit /b 1
)

rem Make sure a local Ollama server answers; if not, try to start one (quietly, no wait).
python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:11434/api/version',timeout=2)" >nul 2>nul
if errorlevel 1 (
  echo Ollama is not answering on 127.0.0.1 - starting local server in background...
  if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    set "OLLAMA_HOST=127.0.0.1:11434"
    start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
  ) else (
    echo Ollama executable not found. The window will still open; checks live inside the app.
  )
)

rem Fast start: the window opens immediately and checks everything itself.
start "" pythonw app.py
exit /b 0
