@echo off
title Motor-AI Control Panel

echo ============================================================
echo   Motor-AI - Motor Design AI Assistant
echo   Version: 1.02.01  |  Updated: 2026-07-27
echo ============================================================
echo.

:: Activate virtual environment
call "D:\MotorDesign\venv\Scripts\activate.bat"

:: Open browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000"

echo   Web UI: http://127.0.0.1:5000
echo   Motor-CAD: D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe
echo   Press Ctrl+C to stop the server
echo ============================================================
echo.

:: Start Flask server
python "D:\MotorDesign\app\app.py"

pause
