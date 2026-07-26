#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 Motor-CAD TypeLib 注册 + COM 连接测试
根因: TypeLib 注册指向不存在的 v13 路径, 且缺少 win32 条目
修复: 从 .exe 加载嵌入的 TypeLib, 写入 HKCU 覆盖
"""
import sys
import os
import time
import pythoncom
import win32com.client
import win32api
import win32con
import winreg

pythoncom.CoInitialize()

PROGID = "MotorCAD.AppAutomation"
CLSID = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
TYPELIB_ID = "{E6490088-9C2C-46A4-85BD-196313BDF1A7}"
EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"

print("=" * 60)
print("Motor-CAD TypeLib 修复 + COM 连接测试")
print("=" * 60)

# Step 1: 尝试从 .exe 加载 TypeLib
print("\n[1/4] 从 Motor-CAD .exe 加载 TypeLib...")
try:
    tlb = pythoncom.LoadTypeLib(EXE_PATH)
    print(f"  成功! TypeLib 已从 .exe 加载")
    
    # Get TypeLib info
    info = tlb.GetDocumentation(-1)  # -1 = TLB description
    print(f"  TypeLib 名称: {info[0]}")
    print(f"  TypeLib 描述: {info[1]}")
    
    # Count type descriptions
    count = tlb.GetTypeInfoCount()
    print(f"  TypeInfo 数量: {count}")
    
    # List all interfaces/coclasses
    for i in range(count):
        try:
            ti = tlb.GetTypeInfo(i)
            doc = tlb.GetDocumentation(i)
            type_kind = ti.GetTypeAttr().typekind
            kind_names = {0: "Enum", 1: "Record", 2: "Module", 3: "Interface", 
                         4: "Dispatch", 5: "CoClass", 6: "Alias", 7: "Union"}
            kind_str = kind_names.get(type_kind, f"Unknown({type_kind})")
            print(f"    [{i}] {doc[0]} ({kind_str})")
        except Exception as e:
            print(f"    [{i}] Error: {e}")
            
except Exception as e:
    print(f"  失败: {e}")
    print("  .exe 可能不包含 TypeLib 资源")
    print("  尝试其他方法...")
    
    # Try loading from activex.bat path or other locations
    alt_paths = [
        r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe",
        r"C:\ANSYS_Motor-CAD\Shared Files\activex.bat",
    ]
    for alt in alt_paths:
        try:
            tlb = pythoncom.LoadTypeLib(alt)
            print(f"  从 {alt} 加载成功!")
            break
        except:
            pass
    
    if 'tlb' not in dir():
        print("  所有路径均无法加载 TypeLib")
        # Try registering the TypeLib from the .exe using LoadTypeLibEx
        print("\n  尝试 LoadTypeLibEx...")
        try:
            tlb = pythoncom.LoadTypeLibEx(EXE_PATH, pythoncom.REGKIND_NONE)
            print(f"  LoadTypeLibEx 成功!")
        except Exception as e2:
            print(f"  LoadTypeLibEx 也失败: {e2}")
            tlb = None

# Step 2: 注册 TypeLib (写入 HKCU)
if tlb:
    print("\n[2/4] 注册 TypeLib 到 HKCU...")
    try:
        # Get TypeLib attributes
        tlb_attr = tlb.GetLibAttr()
        guid = str(tlb_attr.guid)
        major = tlb_attr.wMajorVerNum
        minor = tlb_attr.wMinorVerNum
        print(f"  GUID: {guid}")
        print(f"  Version: {major}.{minor}")
        
        # Write HKCU TypeLib registration
        # Key: HKCU\Software\Classes\TypeLib\{guid}\{major}.{minor}\0\win32
        tlb_key_path = f"Software\\Classes\\TypeLib\\{guid}\\{major}.{minor}\\0"
        
        # win32 entry (for 32-bit COM clients)
        win32_path = os.path.join(EXE_PATH)  # .exe contains the type library
        key_win32 = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, 
            f"{tlb_key_path}\\win32", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key_win32, None, 0, winreg.REG_SZ, win32_path)
        winreg.CloseKey(key_win32)
        print(f"  HKCU TypeLib\\win32 = {win32_path}")
        
        # win64 entry (for 64-bit COM clients)  
        key_win64 = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
            f"{tlb_key_path}\\win64", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key_win64, None, 0, winreg.REG_SZ, win32_path)
        winreg.CloseKey(key_win64)
        print(f"  HKCU TypeLib\\win64 = {win32_path}")
        
        # FLAGS entry
        key_flags = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
            f"Software\\Classes\\TypeLib\\{guid}\\{major}.{minor}", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key_flags, "FLAGS", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key_flags)
        
        print("  TypeLib HKCU 注册完成!")
        
    except Exception as e:
        print(f"  TypeLib 注册失败: {e}")
        import traceback
        traceback.print_exc()

# Step 3: 写入 HKCU CLSID LocalServer32 覆盖
print("\n[3/4] 写入 HKCU CLSID LocalServer32 覆盖...")
clsid_key_path = f"Software\\Classes\\CLSID\\{CLSID}\\LocalServer32"
try:
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, clsid_key_path, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, None, 0, winreg.REG_SZ, EXE_PATH)
    winreg.CloseKey(key)
    print(f"  HKCU\\...\\LocalServer32 = {EXE_PATH}")
except Exception as e:
    print(f"  写入失败: {e}")

# Step 4: 测试 COM 连接
print("\n[4/4] 测试 COM Dispatch 连接...")
print("  (等待 Motor-CAD 启动, 可能需要 60-120 秒...)")

# Kill existing Motor-CAD
import subprocess
subprocess.run(["taskkill", "/F", "/IM", "Motor-CAD_11_1_5.exe"], 
    capture_output=True, text=True)
time.sleep(2)

mc = None
dispatch_start = time.time()

try:
    mc = win32com.client.Dispatch(PROGID)
    dispatch_time = time.time() - dispatch_start
    print(f"  成功! Dispatch 连接成功 (耗时 {dispatch_time:.1f}s)")
except Exception as e:
    dispatch_time = time.time() - dispatch_start
    print(f"  失败 ({dispatch_time:.1f}s): {e}")
    hresult = e.hresult & 0xFFFFFFFF if hasattr(e, 'hresult') else 0
    print(f"  HRESULT: 0x{hresult:08X}")
    
    # Try GetActiveObject
    print("\n  尝试 GetActiveObject...")
    try:
        mc = win32com.client.GetActiveObject(PROGID)
        print(f"  GetActiveObject 成功!")
    except Exception as e2:
        print(f"  GetActiveObject 失败: {e2}")

if mc:
    print("\n" + "=" * 60)
    print("COM 连接成功! 测试基本操作...")
    print("=" * 60)
    
    try:
        mc.Visible = True
        print("  Visible = True: OK")
    except Exception as e:
        print(f"  Visible: {e}")
    
    # Load template
    template = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
    if os.path.exists(template):
        try:
            print(f"  LoadFromFile({os.path.basename(template)})...")
            mc.LoadFromFile(template)
            print("  -> OK")
        except Exception as e:
            print(f"  -> {e}")
    
    # Set/Get variable
    try:
        mc.SetVariable("Speed", 3000)
        val = mc.GetVariable("Speed")
        print(f"  Speed = {val}")
    except Exception as e:
        print(f"  SetVariable/GetVariable: {e}")
    
    print("\nMotor-CAD COM 自动化可用!")
else:
    print("\n" + "=" * 60)
    print("COM 连接仍然失败")
    print("=" * 60)
    
    # Diagnostic
    print("\n诊断信息:")
    print(f"  TypeLib 加载: {'成功' if tlb else '失败'}")
    print(f"  HKCU LocalServer32: {EXE_PATH}")
    print(f"  Motor-CAD .exe 存在: {os.path.exists(EXE_PATH)}")
    
    # Check if Motor-CAD is running
    import subprocess
    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Motor-CAD_11_1_5.exe"],
        capture_output=True, text=True)
    if "Motor-CAD" in result.stdout:
        print("  Motor-CAD 进程: 运行中")
    else:
        print("  Motor-CAD 进程: 未运行")

pythoncom.CoUninitialize()
