@echo off
title Motor-AI Control Panel

echo ============================================================
echo   Motor-AI - Motor Design AI Assistant
echo   Version: 1.03.01  -  Updated: 2026-07-27
echo   (Deprecated: use start_dev.bat or start_trial.bat)
echo ============================================================
echo.

rem Activate virtual environment
call "D:\MotorDesign\venv\Scripts\activate.bat"

rem Read BIND_HOST from .env
set BIND_HOST=127.0.0.1
for /f "tokens=2 delims==" %%a in ('findstr /B "BIND_HOST" "D:\MotorDesign\.env" 2^>nul') do set BIND_HOST=%%a

rem Open browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul ^& start http://127.0.0.1:5000"

echo   Web UI: http://%BIND_HOST%:5000
if not "%BIND_HOST%"=="127.0.0.1" (
    echo   [Network access enabled] Run Scripts\setup_firewall.bat first
)
echo   Motor-CAD: D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe
echo   Press Ctrl+C to stop the server
echo ============================================================
echo.

rem Start Flask server
python "D:\MotorDesign\app\app.py"

pause
