@echo off
title Motor-AI BETA (Trial)
set MOTOR_ENV=trial

echo ============================================================
echo   Motor-AI - Trial Environment (BETA)
echo   Version: 1.03.03-beta.1  -  Port: 5001
echo ============================================================
echo.

rem Activate virtual environment
call "D:\MotorDesign\venv\Scripts\activate.bat"

rem Open browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul ^& start http://127.0.0.1:5001"

echo   Web UI: http://127.0.0.1:5001
echo   Network: http://0.0.0.0:5001 (LAN accessible)
echo   Motor-CAD: Simulation mode only
echo   Cloud API: Disabled (security)
echo   Press Ctrl+C to stop the server
echo ============================================================
echo.

rem Start Flask server (no debug, production-safe)
python "D:\MotorDesign\app\app.py"

pause
