# -*- coding: utf-8 -*-
"""
深度 COM 连接测试 - 使用底层 CoGetClassObject 方法
之前测试都用 Dispatch (高层封装), 这次用 ctypes 直接调用 COM 底层 API

理论: Motor-CAD exe 包含 CoRegisterClassObject, 可能在完全加载后(90秒+)
才注册 class factory。之前的 120 秒等待可能不够, 或者 Dispatch 的内部
超时太短。用 CoGetClassObject 可以直接检查 class factory 是否已注册。
"""
import ctypes
import ctypes.wintypes
import os
import subprocess
import time
import sys

# Motor-CAD COM CLSID
CLSID_MotorCAD = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
IID_IDispatch = "{00020400-0000-0000-C000-000000000046}"
IID_IClassFactory = "{00000001-0000-0000-C000-000000000046}"

# COM 初始化
ole32 = ctypes.OleDLL("ole32")
ole32.CoInitialize(None)

# CLSID/IID 结构体
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.wintypes.DWORD),
        ("Data2", ctypes.wintypes.WORD),
        ("Data3", ctypes.wintypes.WORD),
        ("Data4", ctypes.wintypes.BYTE * 8),
    ]

def string_to_guid(s):
    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g

clsid = string_to_guid(CLSID_MotorCAD)
iid_icf = string_to_guid(IID_IClassFactory)
iid_idispatch = string_to_guid(IID_IDispatch)

# CLSCTX_LOCAL_SERVER = 0x4
CLSCTX_LOCAL_SERVER = 0x4
CLSCTX_INPROC_SERVER = 0x1
CLSCTX_ALL = 0x17

# === Step 1: 检查当前是否有 Motor-CAD 在运行 ===
print("=" * 60)
print("[1] 检查当前 Motor-CAD 进程")
print("=" * 60)

import psutil
motor_pids = []
for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
    if 'motor' in proc.info['name'].lower():
        motor_pids.append(proc.info['pid'])
        print(f"  PID {proc.info['pid']}: {proc.info['name']}")
        try:
            print(f"    cmdline: {proc.info['cmdline']}")
        except:
            pass

if not motor_pids:
    print("  Motor-CAD 未运行, 用 -Embedding 启动...")
    exe = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
    mot = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
    # 用 -Embedding 启动 (COM 服务器模式)
    proc = subprocess.Popen(
        [exe, "-Embedding", mot],
        cwd=os.path.dirname(exe),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
    )
    print(f"  启动 PID: {proc.pid}")
    motor_pids.append(proc.pid)

# === Step 2: 等待并定期检查 class factory ===
print("\n" + "=" * 60)
print("[2] 等待 Motor-CAD 注册 COM class factory")
print("=" * 60)

# 定义 IClassFactory 的 vtable (简化)
# IClassFactory::CreateInstance(pUnkOuter, riid, ppvObject)
# IClassFactory::LockServer(fLock)

class IClassFactory(ctypes.Structure):
    pass  # 我们只需要指针, 不需要调用方法

# CoGetClassObject 函数签名
# HRESULT CoGetClassObject(REFCLSID rclsid, DWORD dwClsContext, 
#                           COSERVERINFO *pServerInfo, REFIID riid, void **ppv)
try:
    CoGetClassObject = ole32.CoGetClassObject
except AttributeError:
    # 某些系统可能需要从 ole32.dll 获取
    CoGetClassObject = ole32.CoGetClassObject

# 尝试连接的时间点 (秒)
check_points = [60, 90, 120, 150, 180]
start_time = time.time()
connected = False
class_factory_ptr = ctypes.c_void_p()

for wait_sec in check_points:
    elapsed = time.time() - start_time
    if elapsed < wait_sec:
        sleep_time = wait_sec - elapsed
        print(f"  等待 {sleep_time:.0f}s 到 {wait_sec}s 检查点...")
        time.sleep(sleep_time)
    
    print(f"\n  [{wait_sec}s] 尝试 CoGetClassObject...")
    
    # 方法1: CoGetClassObject (直接获取 class factory)
    try:
        ptr = ctypes.c_void_p()
        hr = CoGetClassObject(
            ctypes.byref(clsid),
            CLSCTX_LOCAL_SERVER,
            None,
            ctypes.byref(iid_icf),
            ctypes.byref(ptr)
        )
        if hr == 0:  # S_OK
            print(f"    CoGetClassObject 成功! class factory ptr = {ptr.value}")
            connected = True
            class_factory_ptr = ptr
            break
        else:
            # HRESULT 转十六进制
            hr_hex = f"0x{hr & 0xFFFFFFFF:08X}"
            hr_desc = {
                0x80080005: "CO_E_SERVER_EXEC_FAILURE (服务器执行失败)",
                0x800401E3: "MK_E_UNAVAILABLE (对象不可用)",
                0x80040154: "REGDB_E_CLASSNOTREG (类未注册)",
                0x80070005: "E_ACCESSDENIED (访问被拒绝)",
                0x80004005: "E_FAIL (未指定错误)",
            }
            desc = hr_desc.get(hr & 0xFFFFFFFF, f"未知错误")
            print(f"    CoGetClassObject 失败: {hr_hex} = {desc}")
    except Exception as e:
        print(f"    CoGetClassObject 异常: {e}")
    
    # 方法2: 尝试 GetActiveObject (ROT 查找)
    try:
        import win32com.client
        import pythoncom
        obj = pythoncom.GetActiveObject("MotorCAD.AppAutomation")
        print(f"    GetActiveObject 成功!")
        connected = True
        break
    except Exception as e:
        print(f"    GetActiveObject 失败: {e}")
    
    # 方法3: 尝试 Dispatch (创建实例)
    try:
        import win32com.client
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"    Dispatch 成功!")
        print(f"    版本: {mc.GetVariable('MotorCADVERSION')}")
        connected = True
        break
    except Exception as e:
        err_str = str(e)
        if "SERVER_EXEC_FAILURE" in err_str or "80080005" in err_str:
            print(f"    Dispatch 失败: CO_E_SERVER_EXEC_FAILURE (class factory 未注册)")
        elif "UNAVAILABLE" in err_str or "800401E3" in err_str:
            print(f"    Dispatch 失败: MK_E_UNAVAILABLE (不在 ROT 中)")
        else:
            print(f"    Dispatch 失败: {err_str[:100]}")
    
    # 检查进程是否还在运行
    import psutil
    still_running = False
    for proc in psutil.process_iter(['pid', 'name']):
        if 'motor' in proc.info['name'].lower():
            still_running = True
            # 检查端口
            try:
                conns = proc.net_connections() if hasattr(proc, 'net_connections') else proc.connections()
                if conns:
                    print(f"    进程 {proc.info['pid']} 有 {len(conns)} 个连接")
            except:
                pass
    if not still_running:
        print(f"    *** Motor-CAD 进程已退出! ***")
        break

# === Step 3: 如果连接成功, 测试基本操作 ===
if connected:
    print("\n" + "=" * 60)
    print("[3] COM 连接成功! 测试基本操作")
    print("=" * 60)
    try:
        import win32com.client
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        
        # 获取版本
        ver = mc.GetVariable("MotorCADVERSION")
        print(f"  Motor-CAD 版本: {ver}")
        
        # 获取参数
        dia = mc.GetVariable("StatorOuterDiameter")
        print(f"  定子外径: {dia}")
        
        # 设置参数
        mc.SetVariable("StatorOuterDiameter", 0.15)
        print(f"  设置定子外径为 0.15")
        
        # 读取回来
        dia2 = mc.GetVariable("StatorOuterDiameter")
        print(f"  读取定子外径: {dia2}")
        
        print("\n  *** COM 自动化完全可用! ***")
        print("  可以进行电磁和热分析自动化!")
    except Exception as e:
        print(f"  操作失败: {e}")
        traceback.print_exc()
else:
    print("\n" + "=" * 60)
    print("[3] COM class factory 在 180 秒内未注册")
    print("=" * 60)
    print("  结论: Motor-CAD v11.1.5 不通过 COM 注册 class factory")
    print("  exe 中包含 CoRegisterClassObject 但可能:")
    print("  1. 仅在特定条件下调用 (如特定许可证状态)")
    print("  2. 调用但因 32/64 位 marshaling 失败")
    print("  3. 需要不同的启动参数")

# 清理
ole32.CoUninitialize()
print("\n测试完成")
