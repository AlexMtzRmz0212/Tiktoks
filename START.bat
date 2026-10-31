@echo off
rem Starts the TikTok archive page at http://127.0.0.1:8765 and keeps this window open while it runs.
rem Close the window (or press Ctrl+C) to stop it. Extra options are passed on, e.g.:  start-ui.bat --no-browser
cd /d "%~dp0"
title TikTok archive - local page

set "DOCKER=docker"
where docker >nul 2>nul || set "DOCKER=C:\Program Files\Docker\Docker\resources\bin\docker.exe"

echo Starting the database container (needs Docker Desktop to be running)...
"%DOCKER%" compose up -d
if errorlevel 1 (
    echo.
    echo [!] Could not start the database. Open Docker Desktop, wait until it says it is running,
    echo     then start this again. The page will still open, but it will say the database is not reachable.
    echo.
)

echo.
echo Starting the page. It opens in your browser; keep this window open while you use it.
echo.
".venv\Scripts\python.exe" -m tiktok_archive.ui %*

echo.
echo The page has stopped.
pause
