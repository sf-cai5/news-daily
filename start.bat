@echo off
rem ==============================================================
rem  Daily News Aggregator - double-click launcher
rem
rem  ASCII only on purpose: cmd.exe reads a .bat file using the
rem  console code page, which is 936 on some machines and 65001 on
rem  others. Non-ASCII text here breaks on the 65001 ones.
rem
rem  Spec section 10 gives these lines:
rem      python app.py
rem      start http://localhost:5000
rem  python app.py blocks, so the browser would never open; a helper
rem  window opens it after 2 seconds instead. Everything else is the
rem  same.
rem ==============================================================

cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo         Install Python 3.10+ first, then run:
    echo         pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting daily news aggregator ...
echo Browser will open http://localhost:5000 in 2 seconds.
echo Keep this window open; closing it stops the server (Ctrl+C too).
echo.

start "" /min cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:5000"
python app.py

echo.
echo Server stopped.
pause
