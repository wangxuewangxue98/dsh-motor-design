@echo off
title Motor-AI Environment

echo ============================================================
echo   Motor-AI Design Assistant - Pure Windows
echo ============================================================
echo.

REM ---- Check Python ----
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    echo         https://www.python.org/downloads/
    echo         Check "Add Python to PATH" during install
    pause
    exit /b 1
)

REM ---- Check Ollama ----
where ollama >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Ollama not found. Please install it.
    echo         https://ollama.com/download/windows
    pause
    exit /b 1
)

REM ---- Check Ollama service ----
ollama list >nul 2>&1
if errorlevel 1 (
    echo [INFO] Starting Ollama service...
    start "" ollama serve
    timeout /t 3 /nobreak >nul
)

REM ---- Check virtual environment ----
if not exist "D:\MotorDesign\venv\Scripts\activate.bat" (
    echo [SETUP] Creating Python venv...
    python -m venv D:\MotorDesign\venv
    echo [SETUP] Installing dependencies...
    call D:\MotorDesign\venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r D:\MotorDesign\Scripts\requirements.txt
    echo.
    echo [DONE] Environment initialized.
) else (
    call D:\MotorDesign\venv\Scripts\activate.bat
)

echo.
echo ============================================================
echo   Environment Ready
echo ============================================================
echo   Project:   D:\MotorDesign\Scripts
echo   Database:  D:\MotorDesign\KnowledgeBase\designs.db
echo   Reports:   D:\MotorDesign\Reports
echo ============================================================
echo.
echo   Commands:
echo     python workflow.py --test   (test mode, 2 params)
echo     python workflow.py --demo   (5 examples)
echo     python workflow.py          (interactive mode)
echo.
echo   Available models:
ollama list
echo.

cd /d D:\MotorDesign\Scripts
cmd /k
