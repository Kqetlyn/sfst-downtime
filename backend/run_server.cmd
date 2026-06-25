@echo off
cd /d "%~dp0"
rem Free port 5005 first so stale/zombie backends can't pile up or block startup
rem (closing the console window does not always kill the old python process).
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5005" ^| findstr "LISTENING"') do taskkill /f /pid %%P >nul 2>&1
rem Local dev launcher: enable Flask auto-reload so backend code changes take
rem effect without a manual restart (prevents "backend unavailable" after edits).
rem Deployment uses its own entrypoint and is unaffected by this file.
if not defined FLASK_DEBUG set "FLASK_DEBUG=1"

rem ── Ensure local Ollama is running (MIRA AI wording layer) ──────────────────
rem The backend is only an Ollama CLIENT; it does not launch the server. If the
rem Ollama background service isn't up, every MIRA AI call silently falls back to
rem rule-based wording. Start "ollama serve" here if port 11434 isn't listening.
set "OLLAMA_EXE=%LocalAppData%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA_EXE%" set "OLLAMA_EXE=ollama"
netstat -ano | findstr ":11434" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo Starting local Ollama server...
    start "Ollama" /min "%OLLAMA_EXE%" serve
    rem Give the server a moment to bind the port, then pre-load the model in the
    rem background so the first MIRA AI request isn't slow ^(model load ~10-30s^).
    timeout /t 3 /nobreak >nul
    start "Ollama warm-up" /min "%OLLAMA_EXE%" run qwen2.5:7b "warming up"
) else (
    echo Local Ollama already running.
)

set "PYTHON_EXE="
for /f "delims=" %%F in ('dir /b /s "%LocalAppData%\Python\pythoncore-*\python.exe" 2^>nul') do (
    set "PYTHON_EXE=%%F"
    goto :run
)
set "PYTHON_EXE=python"

:run
"%PYTHON_EXE%" app.py
pause
