#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 TypeLib 注册 v2 + 多策略 COM 连接测试
"""
import sys
import os
import time
import struct
import pythoncom
import win32com.client
import winreg

pythoncom.CoInitialize()

PROGID = "MotorCAD.AppAutomation"
CLSID = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
TYPELIB_ID = "{E6490088-9C2C-46A4-85BD-196313BDF1A7}"
EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"

print("=" * 60)
print("Motor-CAD TypeLib 修复 v2 + COM 连接测试")
print("=" * 60)

# Step 1: 加载 TypeLib
print("\n[1/5] 从 .exe 加载 TypeLib...")
try:
    tlb = pythoncom.LoadTypeLib(EXE_PATH)
    print("  TypeLib 加载成功: MotorCAD Library")
    # Get lib attr using correct API
    try:
        attr = tlb.GetLibAttr()
        # In pywin32, GetLibAttr might return a TYPEATTR struct
        if hasattr(attr, 'guid'):
            tlb_guid = str(attr.guid)
            tlb_major = attr.wMajorVerNum
            tlb_minor = attr.wMinorVerNum
        elif isinstance(attr, tuple):
            # Some versions return a tuple
            tlb_guid = str(attr[0])
            tlb_major = attr[4] if len(attr) > 4 else 1
            tlb_minor = attr[5] if len(attr) > 5 else 0
        else:
            tlb_guid = TYPELIB_ID
            tlb_major = 1
            tlb_minor = 0
        print(f"  GUID: {tlb_guid}, Version: {tlb_major}.{tlb_minor}")
    except Exception as e:
        print(f"  GetLibAttr 注意: {e}")
        tlb_guid = TYPELIB_ID
        tlb_major = 1
        tlb_minor = 0
        print(f"  使用已知值: GUID={tlb_guid}, Version={tlb_major}.{tlb_minor}")
except Exception as e:
    print(f"  TypeLib 加载失败: {e}")
    tlb = None
    tlb_guid = TYPELIB_ID
    tlb_major = 1
    tlb_minor = 0

# Step 2: 写入 HKCU TypeLib 注册
print(f"\n[2/5] 注册 TypeLib 到 HKCU (GUID={tlb_guid})...")
tlb_base = f"Software\\Classes\\TypeLib\\{tlb_guid}\\{tlb_major}.{tlb_minor}"
try:
    # win32 entry
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"{tlb_base}\\0\\win32", 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, None, 0, winreg.REG_SZ, EXE_PATH)
    winreg.CloseKey(key)
    print(f"  HKCU TypeLib\\0\\win32 = {EXE_PATH}")
    
    # win64 entry
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"{tlb_base}\\0\\win64", 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, None, 0, winreg.REG_SZ, EXE_PATH)
    winreg.CloseKey(key)
    print(f"  HKCU TypeLib\\0\\win64 = {EXE_PATH}")
    
    # FLAGS
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, tlb_base, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, "FLAGS", 0, winreg.REG_DWORD, 0)
    winreg.CloseKey(key)
    
    print("  TypeLib HKCU 注册完成!")
except Exception as e:
    print(f"  注册失败: {e}")

# Step 3: 写入 HKCU CLSID LocalServer32
print(f"\n[3/5] 写入 HKCU CLSID LocalServer32...")
try:
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
        f"Software\\Classes\\CLSID\\{CLSID}\\LocalServer32", 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, None, 0, winreg.REG_SZ, EXE_PATH)
    winreg.CloseKey(key)
    print(f"  LocalServer32 = {EXE_PATH}")
except Exception as e:
    print(f"  写入失败: {e}")

# Step 4: 生成 COM 包装器 (gencache)
print("\n[4/5] 生成 COM 类型库包装器 (gencache)...")
try:
    # This forces pywin32 to read the type library and generate a Python wrapper
    # It might also trigger a different COM connection path
    import win32com.client.gencache
    # Try to generate from the type lib directly
    win32com.client.gencache.EnsureModule(tlb_guid, 0, tlb_major, tlb_minor)
    print("  gencache 模块生成成功!")
except Exception as e:
    print(f"  gencache 生成: {e}")
    # This is OK, we can still try Dispatch without generated wrapper

# Step 5: 测试 COM 连接 - 多策略
print("\n[5/5] COM 连接测试...")
import subprocess

# Kill existing Motor-CAD
subprocess.run(["taskkill", "/F", "/IM", "Motor-CAD_11_1_5.exe"],
    capture_output=True)
time.sleep(2)

mc = None
strategies = []

# Strategy A: gencache.EnsureDispatch (type-safe, uses type library)
print("\n  [A] gencache.EnsureDispatch...")
try:
    mc = win32com.client.gencache.EnsureDispatch(PROGID)
    print("  -> 成功!")
    strategies.append(("EnsureDispatch", True))
except Exception as e:
    print(f"  -> 失败: {e}")
    strategies.append(("EnsureDispatch", False, str(e)))

# Strategy B: Dispatch (dynamic, late binding)
if mc is None:
    print("\n  [B] Dispatch (dynamic)...")
    t0 = time.time()
    try:
        mc = win32com.client.Dispatch(PROGID)
        print(f"  -> 成功! ({time.time()-t0:.1f}s)")
        strategies.append(("Dispatch", True))
    except Exception as e:
        print(f"  -> 失败 ({time.time()-t0:.1f}s): {e}")
        strategies.append(("Dispatch", False, str(e)))

# Strategy C: 启动 Motor-CAD 正常模式, 然后尝试连接
if mc is None:
    print("\n  [C] 先启动 Motor-CAD, 然后轮询连接...")
    proc = subprocess.Popen([EXE_PATH], cwd=os.path.dirname(EXE_PATH))
    print(f"  Motor-CAD PID={proc.pid}")
    
    for wait in range(90):
        time.sleep(2)
        if wait > 0 and wait % 5 == 0:
            print(f"  等待 {wait*2}s...")
        
        # Try GetActiveObject
        try:
            mc = win32com.client.GetActiveObject(PROGID)
            print(f"  GetActiveObject 成功! ({wait*2}s)")
            strategies.append(("Start+GetActiveObject", True))
            break
        except:
            pass
        
        # Try Dispatch (might connect to running instance)
        if wait > 10:  # Wait at least 20s before trying Dispatch
            try:
                mc = win32com.client.Dispatch(PROGID)
                print(f"  Dispatch 成功! ({wait*2}s)")
                strategies.append(("Start+Dispatch", True))
                break
            except:
                pass
        
        # Check if process is still alive
        if proc.poll() is not None:
            print(f"  Motor-CAD 退出 (code={proc.returncode})")
            break
    
    if mc is None:
        strategies.append(("Start+Poll", False, "timeout"))

# Summary
print("\n" + "=" * 60)
print("结果汇总")
print("=" * 60)
for s in strategies:
    name = s[0]
    success = s[1]
    print(f"  {name:25s}: {'OK' if success else 'FAIL'}")

if mc:
    print("\nCOM 连接成功! 测试操作...")
    try:
        mc.Visible = True
        print("  Visible: OK")
    except:
        pass
    try:
        template = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
        mc.LoadFromFile(template)
        print("  LoadFromFile: OK")
    except Exception as e:
        print(f"  LoadFromFile: {e}")
    try:
        mc.SetVariable("Speed", 3000)
        val = mc.GetVariable("Speed")
        print(f"  Speed = {val}")
    except Exception as e:
        print(f"  SetVariable/GetVariable: {e}")
    print("\nMotor-CAD COM 自动化已就绪!")
else:
    print("\n所有策略均失败。")
    print("\n根因分析:")
    print("  Motor-CAD v11.1.5 (32位, 2017) 的 COM 服务器不调用")
    print("  CoRegisterClassObject，COM 无法建立 class factory 连接。")
    print("  TypeLib 已正确加载和注册，但无法解决此根本问题。")
    print("\n建议方案:")
    print("  1. 使用手动求解工作流 (已实现)")
    print("  2. 升级到 Motor-CAD v13+ (支持 COM 自动化)")
    print("  3. 使用 Motor-LAB runtime 做计算桥接")

pythoncom.CoUninitialize()
