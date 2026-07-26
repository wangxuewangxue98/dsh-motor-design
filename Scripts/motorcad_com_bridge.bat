@echo off
REM motorcad_com_bridge.bat - 32-bit COM bridge for Motor-CAD
REM Called by Python (64-bit) to execute COM commands via 32-bit cscript
REM Usage: motorcad_com_bridge.bat <command_json_path> <result_json_path>

setlocal
set CMD_FILE=%1
set RESULT_FILE=%2
set VBS_PATH=%~dp0motorcad_bridge.vbs

REM Use 32-bit cscript to connect to 32-bit Motor-CAD COM
C:\Windows\SysWOW64\cscript.exe //nologo "%VBS_PATH%" "%CMD_FILE%" "%RESULT_FILE%"
exit /b %ERRORLEVEL%
