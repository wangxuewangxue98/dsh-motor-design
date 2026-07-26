@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  Motor-CAD COM 注册修复脚本 v2
REM  解决: 64位 Python 无法连接 32位 Motor-CAD COM 的问题
REM
REM  根本原因:
REM    注册表 LocalServer32 = %MOTORCAD_ACTIVEX% (REG_EXPAND_SZ)
REM    系统环境变量 MOTORCAD_ACTIVEX = "C:\...\activex.bat"
REM    .bat 不是有效的 Win32 可执行文件 -> 0x800700c1 错误
REM
REM  修复方案 (双保险):
REM    [1] 修改系统环境变量 MOTORCAD_ACTIVEX -> 直接指向 .exe (根因修复)
REM    [2] 修改注册表 LocalServer32 -> 硬编码 .exe 路径 (双保险)
REM ============================================================
echo.
echo  ============================================
echo   Motor-CAD COM 注册修复 v2
echo  ============================================
echo.

REM 检查管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo  [错误] 需要管理员权限!
    echo  请右键此文件, 选择"以管理员身份运行"
    pause
    exit /b 1
)

set "MC_EXE=D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
set "MC_CLSID={4ACD06EB-5486-42AF-91DB-FB752E64C465}"

REM 检查 .exe 是否存在
if not exist "%MC_EXE%" (
    echo  [错误] Motor-CAD 可执行文件不存在: %MC_EXE%
    echo  请检查安装路径或修改脚本中的 MC_EXE 变量
    pause
    exit /b 1
)
echo  [确认] Motor-CAD .exe 存在: %MC_EXE%
echo.

REM ============================================================
REM [1/3] 修改系统环境变量 MOTORCAD_ACTIVEX (根因修复)
REM ============================================================
echo  [1/3] 修改系统环境变量 MOTORCAD_ACTIVEX ...
echo    旧值: %MOTORCAD_ACTIVEX%
setx MOTORCAD_ACTIVEX "%MC_EXE%" /M >nul 2>&1
if %errorLevel% equ 0 (
    echo    [OK] 环境变量已更新为: %MC_EXE%
) else (
    echo    [警告] setx 失败, 尝试注册表方式...
    REG ADD "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v MOTORCAD_ACTIVEX /t REG_SZ /d "%MC_EXE%" /f >nul 2>&1
    if %errorLevel% equ 0 (
        echo    [OK] 注册表方式更新成功
    ) else (
        echo    [错误] 环境变量更新失败
    )
)
echo.

REM ============================================================
REM [2/3] 修改注册表 LocalServer32 (双保险 - 硬编码路径)
REM ============================================================
echo  [2/3] 修改注册表 LocalServer32 ...

REM 32位 COM 注册 (WOW6432Node) - 这是 Python 实际查询的路径
echo    - 32位 COM (WOW6432Node)...
REG ADD "HKLM\SOFTWARE\Classes\WOW6432Node\CLSID\%MC_CLSID%\LocalServer32" /ve /t REG_SZ /d "%MC_EXE%" /f >nul 2>&1
if %errorLevel% equ 0 (
    echo      [OK] 32位注册表已更新
) else (
    echo      [警告] 32位注册表更新失败
)

REM 64位 COM 注册 (如果存在)
echo    - 64位 COM (Native)...
REG ADD "HKLM\SOFTWARE\Classes\CLSID\%MC_CLSID%\LocalServer32" /ve /t REG_SZ /d "%MC_EXE%" /f >nul 2>&1
if %errorLevel% equ 0 (
    echo      [OK] 64位注册表已更新
) else (
    echo      [跳过] 64位 COM 节点不存在 (正常, Motor-CAD 是32位)
)
echo.

REM ============================================================
REM [3/3] 广播 WM_SETTINGCHANGE 让环境变量立即生效
REM ============================================================
echo  [3/3] 刷新环境变量 ...
powershell -Command "[void][Win32API.NativeMethods]::SendMessageTimeout(0xFFFF, 0x001A, 0, $null, 0x2, 5000, [ref]0)" >nul 2>&1
if %errorLevel% neq 0 (
    REM 降级: 用 WMIC 触发环境刷新
    echo    [提示] 环境变量将在下次打开命令行窗口时生效
)
echo.

REM ============================================================
REM 验证修复结果
REM ============================================================
echo  ============================================
echo   验证修复结果
echo  ============================================

echo.
echo  [环境变量] MOTORCAD_ACTIVEX:
REG QUERY "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v MOTORCAD_ACTIVEX 2>nul | findstr MOTORCAD_ACTIVEX

echo.
echo  [注册表] LocalServer32 (32位):
REG QUERY "HKLM\SOFTWARE\Classes\WOW6432Node\CLSID\%MC_CLSID%\LocalServer32" /ve 2>nul

echo.
echo  ============================================
echo   修复完成!
echo  ============================================
echo.
echo  请执行以下步骤:
echo    1. 关闭所有 Motor-CAD 窗口
echo    2. 关闭当前的命令行/终端 (让环境变量生效)
echo    3. 重新运行 start_ui.bat 启动系统
echo    4. 在 Web 界面点击"批量求解"
echo.
echo  如果仍然失败, 请检查:
echo    - Motor-CAD 是否已手动启动并完全加载
echo    - 加密狗/授权是否正常
echo.
pause
