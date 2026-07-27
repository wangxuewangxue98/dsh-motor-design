@echo off
title Motor-AI Firewall Setup

echo ============================================================
echo   Motor-AI Firewall Rule Configuration
echo   Configure inbound rule for port 5000
echo ============================================================
echo.

rem Check admin privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges required!
    echo Right-click this file -^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

:MENU
echo Select an option:
echo.
echo   1. Allow LAN only (192.168.0.0/16)
echo   2. Allow LAN + Tailscale (100.64.0.0/10)
echo   3. Allow all sources (NOT recommended on public WiFi)
echo   4. Delete existing Motor-AI firewall rule
echo   5. View current Motor-AI firewall rule
echo   6. [FIX] Set current WiFi network to Private (recommended)
echo   7. [FIX] Disable python.exe Block rules on Public profile
echo   8. [DIAG] Show network profile + python.exe firewall rules
echo   9. Exit
echo.
set /p choice="Enter option (1-9): "

if "%choice%"=="1" goto RULE_LAN
if "%choice%"=="2" goto RULE_LAN_TS
if "%choice%"=="3" goto RULE_ALL
if "%choice%"=="4" goto DELETE
if "%choice%"=="5" goto VIEW
if "%choice%"=="6" goto FIX_PRIVATE
if "%choice%"=="7" goto FIX_PYTHON
if "%choice%"=="8" goto DIAG
if "%choice%"=="9" exit /b 0
echo Invalid option!
goto MENU

:RULE_LAN
echo.
echo [Setup] Allowing LAN (192.168.0.0/16) on port 5000...
rem Delete old rule first
netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)" >nul 2>&1
rem Add new rule with IP restriction
netsh advfirewall firewall add rule ^
    name="Motor-AI Web UI (Port 5000)" ^
    dir=in action=allow protocol=TCP localport=5000 ^
    remoteip=192.168.0.0/16,LocalSubnet ^
    description="Motor-AI Flask Web UI - LAN only"
echo.
if %errorLevel% equ 0 (
    echo [OK] Firewall rule set: LAN only
    echo      Source IP range: 192.168.0.0/16 + LocalSubnet
) else (
    echo [FAIL] Firewall rule setup failed
)
echo.
goto DONE

:RULE_LAN_TS
echo.
echo [Setup] Allowing LAN + Tailscale on port 5000...
netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)" >nul 2>&1
netsh advfirewall firewall add rule ^
    name="Motor-AI Web UI (Port 5000)" ^
    dir=in action=allow protocol=TCP localport=5000 ^
    remoteip=192.168.0.0/16,LocalSubnet,100.64.0.0/10 ^
    description="Motor-AI Flask Web UI - LAN + Tailscale"
echo.
if %errorLevel% equ 0 (
    echo [OK] Firewall rule set: LAN + Tailscale
    echo      Source IP range: 192.168.0.0/16 + LocalSubnet + 100.64.0.0/10
) else (
    echo [FAIL] Firewall rule setup failed
)
echo.
goto DONE

:RULE_ALL
echo.
echo [WARNING] This allows ALL IPs on port 5000 - unsafe on public WiFi!
set /p confirm="Continue? (y/N): "
if /i not "%confirm%"=="y" goto MENU
echo.
netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)" >nul 2>&1
netsh advfirewall firewall add rule ^
    name="Motor-AI Web UI (Port 5000)" ^
    dir=in action=allow protocol=TCP localport=5000 ^
    description="Motor-AI Flask Web UI - All sources (WARNING)"
echo.
if %errorLevel% equ 0 (
    echo [OK] Firewall rule set: All sources
    echo [WARNING] Do NOT use on public WiFi!
) else (
    echo [FAIL] Firewall rule setup failed
)
echo.
goto DONE

:DELETE
echo.
echo [Delete] Removing Motor-AI firewall rule...
netsh advfirewall firewall delete rule name="Motor-AI Web UI (Port 5000)"
echo.
goto DONE

:VIEW
echo.
echo [View] Current Motor-AI firewall rule:
echo ----------------------------------------
netsh advfirewall firewall show rule name="Motor-AI Web UI (Port 5000)"
echo ----------------------------------------
echo.
goto DONE

:FIX_PRIVATE
echo.
echo [Fix] Setting current WiFi network to Private...
echo      (Public profile triggers python.exe Block rules)
echo.
powershell -Command "Get-NetConnectionProfile | Where-Object {$_.NetworkCategory -eq 'Public'} | Set-NetConnectionProfile -NetworkCategory Private"
echo.
echo Current network profiles:
powershell -Command "Get-NetConnectionProfile | Select-Object Name,NetworkCategory | Format-Table -AutoSize"
echo.
echo [OK] If any network was Public, it is now Private.
echo      Phone should now be able to access port 5000.
echo.
goto DONE

:FIX_PYTHON
echo.
echo [Fix] Disabling python.exe Block rules on Public profile...
echo      (These rules block Flask on Public WiFi networks)
echo.
powershell -Command "Get-NetFirewallRule -DisplayName 'Python*' -Direction Inbound -Action Block -ErrorAction SilentlyContinue | Set-NetFirewallRule -Enabled False"
powershell -Command "Get-NetFirewallRule -DisplayName 'python.exe*' -Direction Inbound -Action Block -ErrorAction SilentlyContinue | Set-NetFirewallRule -Enabled False"
echo.
echo Disabled Block rules for python.exe on Public profile.
echo Alternatively, use option 6 to set network to Private.
echo.
goto DONE

:DIAG
echo.
echo [Diag] Network profile:
echo ----------------------------------------
powershell -Command "Get-NetConnectionProfile | Select-Object Name,NetworkCategory,IPv4Connectivity | Format-Table -AutoSize"
echo.
echo [Diag] python.exe Block rules:
echo ----------------------------------------
powershell -Command "Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True | Where-Object {$_.DisplayName -match 'python|Python'} | Select-Object DisplayName,Profile,Enabled | Format-Table -AutoSize"
echo.
echo [Diag] Motor-AI Allow rule:
echo ----------------------------------------
netsh advfirewall firewall show rule name="Motor-AI Web UI (Port 5000)" 2>nul
echo.
goto DONE

:DONE
echo.
echo Tip: Restart Web UI (start_ui.bat) after changing bind address
echo.
pause
goto MENU
