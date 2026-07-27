"""Fix all .bat files: ensure CRLF line endings + ASCII-only content."""
import os

base_dir = os.path.dirname(os.path.dirname(__file__))

# ── start_ui.bat ──────────────────────────────────────────────
start_ui = (
    "@echo off\r\n"
    "title Motor-AI Control Panel\r\n"
    "\r\n"
    "echo ============================================================\r\n"
    "echo   Motor-AI - Motor Design AI Assistant\r\n"
    'echo   Version: 1.02.01  -  Updated: 2026-07-27\r\n'
    "echo ============================================================\r\n"
    "echo.\r\n"
    "\r\n"
    "rem Activate virtual environment\r\n"
    'call "D:\\MotorDesign\\venv\\Scripts\\activate.bat"\r\n'
    "\r\n"
    "rem Read BIND_HOST from .env\r\n"
    "set BIND_HOST=127.0.0.1\r\n"
    'for /f "tokens=2 delims==" %%a in (\'findstr /B "BIND_HOST" "D:\\MotorDesign\\.env" 2^>nul\') do set BIND_HOST=%%a\r\n'
    "\r\n"
    "rem Open browser after 3 seconds\r\n"
    'start "" cmd /c "timeout /t 3 /nobreak >nul ^& start http://127.0.0.1:5000"\r\n'
    "\r\n"
    "echo   Web UI: http://%BIND_HOST%:5000\r\n"
    'if not "%BIND_HOST%"=="127.0.0.1" (\r\n'
    "    echo   [Network access enabled] Run Scripts\\setup_firewall.bat first\r\n"
    ")\r\n"
    "echo   Motor-CAD: D:\\Motor-CAD\\11_1_5\\Motor-CAD_11_1_5.exe\r\n"
    "echo   Press Ctrl+C to stop the server\r\n"
    "echo ============================================================\r\n"
    "echo.\r\n"
    "\r\n"
    "rem Start Flask server\r\n"
    'python "D:\\MotorDesign\\app\\app.py"\r\n'
    "\r\n"
    "pause\r\n"
)

# ── setup_firewall.bat ────────────────────────────────────────
setup_fw = (
    "@echo off\r\n"
    "title Motor-AI Firewall Setup\r\n"
    "\r\n"
    "echo ============================================================\r\n"
    "echo   Motor-AI Firewall Rule Configuration\r\n"
    "echo   Configure inbound rule for port 5000\r\n"
    "echo ============================================================\r\n"
    "echo.\r\n"
    "\r\n"
    "rem Check admin privileges\r\n"
    "net session >nul 2>&1\r\n"
    "if %errorLevel% neq 0 (\r\n"
    "    echo [ERROR] Administrator privileges required!\r\n"
    '    echo Right-click this file -^> "Run as administrator"\r\n'
    "    echo.\r\n"
    "    pause\r\n"
    "    exit /b 1\r\n"
    ")\r\n"
    "\r\n"
    ":MENU\r\n"
    "echo Select an option:\r\n"
    "echo.\r\n"
    "echo   1. Allow LAN only (192.168.0.0/16)\r\n"
    "echo   2. Allow LAN + Tailscale (100.64.0.0/10)\r\n"
    "echo   3. Allow all sources (NOT recommended on public WiFi)\r\n"
    "echo   4. Delete existing Motor-AI firewall rule\r\n"
    "echo   5. View current Motor-AI firewall rule\r\n"
    "echo   6. [FIX] Set current WiFi network to Private (recommended)\r\n"
    "echo   7. [FIX] Disable python.exe Block rules on Public profile\r\n"
    "echo   8. [DIAG] Show network profile + python.exe firewall rules\r\n"
    "echo   9. Exit\r\n"
    "echo.\r\n"
    'set /p choice="Enter option (1-9): "\r\n'
    "\r\n"
    'if "%choice%"=="1" goto RULE_LAN\r\n'
    'if "%choice%"=="2" goto RULE_LAN_TS\r\n'
    'if "%choice%"=="3" goto RULE_ALL\r\n'
    'if "%choice%"=="4" goto DELETE\r\n'
    'if "%choice%"=="5" goto VIEW\r\n'
    'if "%choice%"=="6" goto FIX_PRIVATE\r\n'
    'if "%choice%"=="7" goto FIX_PYTHON\r\n'
    'if "%choice%"=="8" goto DIAG\r\n'
    'if "%choice%"=="9" exit /b 0\r\n'
    "echo Invalid option!\r\n"
    "goto MENU\r\n"
    "\r\n"
    ":RULE_LAN\r\n"
    "echo.\r\n"
    "echo [Setup] Allowing LAN (192.168.0.0/16) on port 5000...\r\n"
    "rem Delete old rule first\r\n"
    'netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)" >nul 2>&1\r\n'
    "rem Add new rule with IP restriction\r\n"
    "netsh advfirewall firewall add rule ^\r\n"
    '    name="Motor-AI Web UI (Port 5000)" ^\r\n'
    "    dir=in action=allow protocol=TCP localport=5000 ^\r\n"
    "    remoteip=192.168.0.0/16,LocalSubnet ^\r\n"
    '    description="Motor-AI Flask Web UI - LAN only"\r\n'
    "echo.\r\n"
    "if %errorLevel% equ 0 (\r\n"
    "    echo [OK] Firewall rule set: LAN only\r\n"
    "    echo      Source IP range: 192.168.0.0/16 + LocalSubnet\r\n"
    ") else (\r\n"
    "    echo [FAIL] Firewall rule setup failed\r\n"
    ")\r\n"
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":RULE_LAN_TS\r\n"
    "echo.\r\n"
    "echo [Setup] Allowing LAN + Tailscale on port 5000...\r\n"
    'netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)" >nul 2>&1\r\n'
    "netsh advfirewall firewall add rule ^\r\n"
    '    name="Motor-AI Web UI (Port 5000)" ^\r\n'
    "    dir=in action=allow protocol=TCP localport=5000 ^\r\n"
    "    remoteip=192.168.0.0/16,LocalSubnet,100.64.0.0/10 ^\r\n"
    '    description="Motor-AI Flask Web UI - LAN + Tailscale"\r\n'
    "echo.\r\n"
    "if %errorLevel% equ 0 (\r\n"
    "    echo [OK] Firewall rule set: LAN + Tailscale\r\n"
    "    echo      Source IP range: 192.168.0.0/16 + LocalSubnet + 100.64.0.0/10\r\n"
    ") else (\r\n"
    "    echo [FAIL] Firewall rule setup failed\r\n"
    ")\r\n"
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":RULE_ALL\r\n"
    "echo.\r\n"
    "echo [WARNING] This allows ALL IPs on port 5000 - unsafe on public WiFi!\r\n"
    'set /p confirm="Continue? (y/N): "\r\n'
    'if /i not "%confirm%"=="y" goto MENU\r\n'
    "echo.\r\n"
    'netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)" >nul 2>&1\r\n'
    "netsh advfirewall firewall add rule ^\r\n"
    '    name="Motor-AI Web UI (Port 5000)" ^\r\n'
    "    dir=in action=allow protocol=TCP localport=5000 ^\r\n"
    '    description="Motor-AI Flask Web UI - All sources (WARNING)"\r\n'
    "echo.\r\n"
    "if %errorLevel% equ 0 (\r\n"
    "    echo [OK] Firewall rule set: All sources\r\n"
    "    echo [WARNING] Do NOT use on public WiFi!\r\n"
    ") else (\r\n"
    "    echo [FAIL] Firewall rule setup failed\r\n"
    ")\r\n"
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":DELETE\r\n"
    "echo.\r\n"
    "echo [Delete] Removing Motor-AI firewall rule...\r\n"
    'netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)"\r\n'
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":VIEW\r\n"
    "echo.\r\n"
    "echo [View] Current Motor-AI firewall rule:\r\n"
    "echo ----------------------------------------\r\n"
    'netsh advfirewall firewall show rule name="Motor-AI Web UI (Port 5000)"\r\n'
    "echo ----------------------------------------\r\n"
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":FIX_PRIVATE\r\n"
    "echo.\r\n"
    "echo [Fix] Setting current WiFi network to Private...\r\n"
    "echo      (Public profile triggers python.exe Block rules)\r\n"
    "echo.\r\n"
    "powershell -Command \"Get-NetConnectionProfile | Where-Object {$_.NetworkCategory -eq 'Public'} | Set-NetConnectionProfile -NetworkCategory Private\"\r\n"
    "echo.\r\n"
    "echo Current network profiles:\r\n"
    "powershell -Command \"Get-NetConnectionProfile | Select-Object Name,NetworkCategory | Format-Table -AutoSize\"\r\n"
    "echo.\r\n"
    "echo [OK] If any network was Public, it is now Private.\r\n"
    "echo      Phone should now be able to access port 5000.\r\n"
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":FIX_PYTHON\r\n"
    "echo.\r\n"
    "echo [Fix] Disabling python.exe Block rules on Public profile...\r\n"
    "echo      (These rules block Flask on Public WiFi networks)\r\n"
    "echo.\r\n"
    "powershell -Command \"Get-NetFirewallRule -DisplayName 'Python*' -Direction Inbound -Action Block -ErrorAction SilentlyContinue | Set-NetFirewallRule -Enabled False\"\r\n"
    "powershell -Command \"Get-NetFirewallRule -DisplayName 'python.exe*' -Direction Inbound -Action Block -ErrorAction SilentlyContinue | Set-NetFirewallRule -Enabled False\"\r\n"
    "echo.\r\n"
    "echo Disabled Block rules for python.exe on Public profile.\r\n"
    "echo Alternatively, use option 6 to set network to Private.\r\n"
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":DIAG\r\n"
    "echo.\r\n"
    "echo [Diag] Network profile:\r\n"
    "echo ----------------------------------------\r\n"
    "powershell -Command \"Get-NetConnectionProfile | Select-Object Name,NetworkCategory,IPv4Connectivity | Format-Table -AutoSize\"\r\n"
    "echo.\r\n"
    "echo [Diag] python.exe Block rules:\r\n"
    "echo ----------------------------------------\r\n"
    "powershell -Command \"Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True | Where-Object {$_.DisplayName -match 'python|Python'} | Select-Object DisplayName,Profile,Enabled | Format-Table -AutoSize\"\r\n"
    "echo.\r\n"
    "echo [Diag] Motor-AI Allow rule:\r\n"
    "echo ----------------------------------------\r\n"
    'netsh advfirewall firewall show rule name="Motor-AI Web UI (Port 5000)" 2>nul\r\n'
    "echo.\r\n"
    "goto DONE\r\n"
    "\r\n"
    ":DONE\r\n"
    "echo.\r\n"
    "echo Tip: Restart Web UI (start_ui.bat) after changing bind address\r\n"
    "echo.\r\n"
    "pause\r\n"
    "goto MENU\r\n"
)

files = {
    os.path.join(base_dir, "start_ui.bat"): start_ui,
    os.path.join(base_dir, "Scripts", "setup_firewall.bat"): setup_fw,
}

for path, content in files.items():
    with open(path, "w", newline="", encoding="ascii") as f:
        f.write(content)
    with open(path, "rb") as f:
        data = f.read()
    crlf = data.count(b"\r\n")
    lf_only = data.count(b"\n") - crlf
    non_ascii = sum(1 for b in data if b > 127)
    status = "OK" if lf_only == 0 and non_ascii == 0 else "FAIL"
    name = os.path.basename(path)
    print(f"  {name:30s}  CRLF={crlf:3d}  LF={lf_only}  NonASCII={non_ascii:4d}  [{status}]")
