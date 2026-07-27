@echo off
title Motor-AI DEV (Debug)
set MOTOR_ENV=dev

echo ============================================================
echo   Motor-AI - Development Environment (DEV)
echo   Version: 2.03.02-dev  -  Port: 5000
echo ============================================================
echo.

rem Activate virtual environment
call "D:\MotorDesign\venv\Scripts\activate.bat"

rem Open browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul ^& start http://127.0.0.1:5000"

echo   Web UI: http://127.0.0.1:5000
echo   Motor-CAD: D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe
echo   Press Ctrl+C to stop the server
echo ============================================================
echo.

rem Start Flask server (debug mode via app.py)
python "D:\MotorDesign\app\app.py"

pause
