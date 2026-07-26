#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD UI 自动化测试 (pywinauto)
策略: 直接控制 Motor-CAD GUI, 完全绕过 COM
"""
import sys
import os
import time
import subprocess

print("=" * 60)
print("Motor-CAD UI 自动化测试 (pywinauto)")
print("=" * 60)

# Import pywinauto
try:
    from pywinauto import Application, Desktop
    from pywinauto.findwindows import ElementNotFoundError
    print("pywinauto 导入成功")
except ImportError as e:
    print(f"pywinauto 导入失败: {e}")
    sys.exit(1)

EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
EXE_DIR = r"D:\Motor-CAD\11_1_5"
TEMPLATE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

# Step 1: Start Motor-CAD
print("\n[1/4] 启动 Motor-CAD...")
proc = subprocess.Popen([EXE_PATH, TEMPLATE], cwd=EXE_DIR)
print(f"  PID = {proc.pid}")

# Step 2: Wait for Motor-CAD window
print("\n[2/4] 等待 Motor-CAD 窗口...")
app = None
for attempt in range(30):
    time.sleep(2)
    try:
        # Try to connect to the window
        app = Application(backend="win32").connect(process=proc.pid)
        main_window = app.window(title="Motor-CAD")
        if main_window.exists():
            print(f"  找到 Motor-CAD 窗口! (等待 {attempt*2+2}s)")
            break
    except Exception:
        pass
    if attempt % 5 == 0:
        print(f"  等待中... {attempt*2+2}s")
else:
    print("  60秒内未找到 Motor-CAD 窗口")
    # Try with different title
    try:
        app = Application(backend="win32").connect(process=proc.pid)
        windows = app.windows()
        print(f"  进程中的窗口: {[w.window_text() for w in windows]}")
    except Exception as e:
        print(f"  连接失败: {e}")
    sys.exit(1)

# Step 3: Inspect Motor-CAD window
print("\n[3/4] 检查 Motor-CAD 窗口结构...")

# Get window info
print(f"  窗口标题: {main_window.window_text()}")
print(f"  窗口类名: {main_window.class_name()}")
print(f"  窗口矩形: {main_window.rectangle()}")
print(f"  可见: {main_window.is_visible()}")
print(f"  可用: {main_window.is_enabled()}")

# Try to list child controls
print("\n  === 子控件 ===")
try:
    # Try to get all descendant controls
    controls = main_window.descendants()
    print(f"  找到 {len(controls)} 个子控件")
    
    # Categorize controls
    menus = []
    buttons = []
    edits = []
    tabs = []
    others = []
    
    for ctrl in controls[:200]:  # Limit to first 200
        try:
            ctrl_type = ctrl.element_info.control_type
            ctrl_text = ctrl.window_text()
            ctrl_class = ctrl.class_name()
            
            if ctrl_type in ("MenuBar", "MenuItem"):
                menus.append((ctrl_text, ctrl_class, ctrl_type))
            elif ctrl_type == "Button":
                buttons.append((ctrl_text, ctrl_class, ctrl_type))
            elif ctrl_type in ("Edit", "RichEdit"):
                edits.append((ctrl_text, ctrl_class, ctrl_type))
            elif ctrl_type in ("Tab", "TabPage"):
                tabs.append((ctrl_text, ctrl_class, ctrl_type))
            else:
                others.append((ctrl_text[:30] if ctrl_text else "", ctrl_class, ctrl_type))
        except:
            pass
    
    if menus:
        print(f"\n  菜单 ({len(menus)} 个):")
        for text, cls, typ in menus[:20]:
            print(f"    [{typ}] '{text}' ({cls})")
    
    if buttons:
        print(f"\n  按钮 ({len(buttons)} 个):")
        for text, cls, typ in buttons[:20]:
            print(f"    '{text}' ({cls})")
    
    if edits:
        print(f"\n  输入框 ({len(edits)} 个):")
        for text, cls, typ in edits[:10]:
            print(f"    '{text}' ({cls})")
    
    if tabs:
        print(f"\n  标签页 ({len(tabs)} 个):")
        for text, cls, typ in tabs[:10]:
            print(f"    '{text}' ({cls})")
    
    if others:
        print(f"\n  其他控件 (前 20 个, 共 {len(others)} 个):")
        for text, cls, typ in others[:20]:
            print(f"    [{typ}] '{text}' ({cls})")
            
except Exception as e:
    print(f"  遍历控件失败: {e}")

# Try UIA backend for more controls
print("\n  === 尝试 UIA 后端 ===")
try:
    app_uia = Application(backend="uia").connect(process=proc.pid)
    main_uia = app_uia.window(title="Motor-CAD")
    controls_uia = main_uia.descendants()
    print(f"  UIA 找到 {len(controls_uia)} 个控件")
    
    # Look for specific control types
    for ctrl in controls_uia[:300]:
        try:
            ctrl_type = ctrl.element_info.control_type
            ctrl_text = ctrl.window_text()
            if ctrl_text and ctrl_type in ("Button", "MenuItem", "Tab", "TabPage", "Edit"):
                print(f"    [{ctrl_type}] '{ctrl_text[:50]}'")
        except:
            pass
except Exception as e:
    print(f"  UIA 后端失败: {e}")

# Step 4: Try menu operations
print("\n[4/4] 尝试菜单操作...")

# Try to access menu bar
try:
    menu = main_window.menu()
    if menu:
        print("  菜单栏存在")
        menu_items = menu.items()
        print(f"  顶级菜单项: {len(menu_items)}")
        for item in menu_items:
            print(f"    '{item.text()}'")
except Exception as e:
    print(f"  菜单访问失败: {e}")

# Try to find "File" menu
try:
    if main_window.menu_select("File->Open"):
        print("  File->Open 可用!")
except Exception as e:
    print(f"  File->Open: {e}")

# Try to find calculation buttons
try:
    # Look for buttons with "Calculate" or "Run" in the text
    for ctrl in main_window.descendants():
        try:
            text = ctrl.window_text().lower()
            if any(kw in text for kw in ["calc", "run", "emag", "thermal", "solve", "magnetic"]):
                print(f"  找到计算相关控件: '{ctrl.window_text()}' ({ctrl.class_name()})")
        except:
            pass
except Exception as e:
    print(f"  搜索计算按钮失败: {e}")

print("\n" + "=" * 60)
print("UI 自动化测试完成")
print("=" * 60)
print(f"\nMotor-CAD PID={proc.pid} 仍在运行")
print("可以查看 Motor-CAD 界面确认模板已加载")
