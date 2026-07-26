#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD UI 深度检测 (win32gui 直接枚举)
适合 Delphi VCL 应用 (TApplication 类)
"""
import sys
import os
import time
import subprocess
import ctypes
import win32gui
import win32con
import win32process

EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
EXE_DIR = r"D:\Motor-CAD\11_1_5"
TEMPLATE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

print("=" * 60)
print("Motor-CAD UI 深度检测 (win32gui)")
print("=" * 60)

# Step 1: Start Motor-CAD
print("\n[1/3] 启动 Motor-CAD...")
proc = subprocess.Popen([EXE_PATH, TEMPLATE], cwd=EXE_DIR)
pid = proc.pid
print(f"  PID = {pid}")

# Step 2: Wait and find Motor-CAD windows
print("\n[2/3] 等待 Motor-CAD 完全启动 (60秒)...")
time.sleep(60)

# Check if process is alive
if proc.poll() is not None:
    print(f"  Motor-CAD 退出! code={proc.returncode}")
    sys.exit(1)

# Step 3: Enumerate all windows for this process
print(f"\n[3/3] 枚举 PID={pid} 的所有窗口...\n")

windows_found = []

def enum_windows_callback(hwnd, lparam):
    """Find all windows belonging to the Motor-CAD process"""
    _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
    if window_pid == pid:
        title = win32gui.GetWindowText(hwnd)
        cls_name = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        
        windows_found.append({
            'hwnd': hwnd,
            'title': title,
            'class': cls_name,
            'visible': visible,
            'enabled': enabled,
            'rect': rect,
            'size': (w, h),
        })
    return True

win32gui.EnumWindows(enum_windows_callback, None)

print(f"找到 {len(windows_found)} 个顶级窗口:")
for i, w in enumerate(windows_found):
    print(f"  [{i}] hwnd={w['hwnd']}, class='{w['class']}', title='{w['title'][:50]}'")
    print(f"      visible={w['visible']}, enabled={w['enabled']}, size={w['size']}, rect={w['rect']}")

# For each top-level window, enumerate child windows
print("\n=== 子窗口枚举 ===")
all_children = []

def enum_child_callback(hwnd, lparam):
    """Enumerate child windows"""
    title = win32gui.GetWindowText(hwnd)
    cls_name = win32gui.GetClassName(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    
    all_children.append({
        'hwnd': hwnd,
        'title': title,
        'class': cls_name,
        'visible': visible,
        'size': (w, h),
        'rect': rect,
    })
    return True

for parent in windows_found:
    if parent['visible'] and parent['size'] != (0, 0):
        print(f"\n  父窗口: '{parent['title'][:30]}' ({parent['class']})")
        all_children.clear()
        try:
            win32gui.EnumChildWindows(parent['hwnd'], enum_child_callback, None)
            print(f"  子窗口数量: {len(all_children)}")
            
            # Show first 30 children
            for j, c in enumerate(all_children[:30]):
                size_str = f"{c['size'][0]}x{c['size'][1]}" if c['visible'] else "hidden"
                title_str = f"'{c['title'][:40]}'" if c['title'] else "(no title)"
                print(f"    [{j}] {c['class']:20s} {title_str:42s} {size_str}")
            
            if len(all_children) > 30:
                print(f"    ... 还有 {len(all_children)-30} 个子窗口")
                
        except Exception as e:
            print(f"  枚举子窗口失败: {e}")

# Try to find the main Motor-CAD form (not TApplication, but the actual form)
print("\n=== 查找 Motor-CAD 主窗体 ===")
main_form = None
for w in windows_found:
    # Delphi TApplication is a hidden window; the actual form has a different class
    if w['class'] != 'TApplication' and w['visible']:
        main_form = w
        print(f"  主窗体: hwnd={w['hwnd']}, class='{w['class']}', title='{w['title'][:50]}'")
        print(f"  大小: {w['size']}, 位置: ({w['rect'][0]}, {w['rect'][1]})")
        break

if main_form is None:
    print("  未找到可见的主窗体")
    print("  可能原因: Motor-CAD 仍在启动中, 或窗口被最小化")
    
    # Try to bring TApplication window to foreground
    for w in windows_found:
        if w['class'] == 'TApplication':
            print(f"\n  尝试激活 TApplication 窗口 (hwnd={w['hwnd']})...")
            try:
                win32gui.ShowWindow(w['hwnd'], win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(w['hwnd'])
                time.sleep(3)
                
                # Re-enumerate after activation
                windows_found.clear()
                win32gui.EnumWindows(enum_windows_callback, None)
                print(f"  激活后找到 {len(windows_found)} 个窗口:")
                for w2 in windows_found:
                    print(f"    class='{w2['class']}', title='{w2['title'][:50]}', visible={w2['visible']}, size={w2['size']}")
                
                # Look for main form again
                for w2 in windows_found:
                    if w2['class'] != 'TApplication' and w2['visible'] and w2['size'] != (0, 0):
                        main_form = w2
                        print(f"\n  找到主窗体: class='{w2['class']}', title='{w2['title'][:50]}'")
                        break
            except Exception as e:
                print(f"  激活失败: {e}")

# If we found the main form, enumerate its children
if main_form:
    print(f"\n=== 主窗体子控件 ({main_form['class']}) ===")
    all_children.clear()
    try:
        win32gui.EnumChildWindows(main_form['hwnd'], enum_child_callback, None)
        print(f"子控件数量: {len(all_children)}")
        
        # Categorize
        buttons = [c for c in all_children if 'button' in c['class'].lower() or 'btn' in c['class'].lower()]
        edits = [c for c in all_children if 'edit' in c['class'].lower() or 'text' in c['class'].lower()]
        tabs = [c for c in all_children if 'tab' in c['class'].lower()]
        menus = [c for c in all_children if 'menu' in c['class'].lower()]
        
        if buttons:
            print(f"\n按钮 ({len(buttons)} 个):")
            for c in buttons[:15]:
                print(f"  '{c['title'][:40]}' ({c['class']})")
        if edits:
            print(f"\n输入框 ({len(edits)} 个):")
            for c in edits[:10]:
                print(f"  '{c['title'][:40]}' ({c['class']})")
        if tabs:
            print(f"\n标签页 ({len(tabs)} 个):")
            for c in tabs[:10]:
                print(f"  '{c['title'][:40]}' ({c['class']})")
        
        # Show all visible children
        visible_children = [c for c in all_children if c['visible'] and c['title']]
        if visible_children:
            print(f"\n有标题的可见控件 ({len(visible_children)} 个):")
            for c in visible_children[:30]:
                print(f"  [{c['class']:20s}] '{c['title'][:50]}'")
                
    except Exception as e:
        print(f"枚举失败: {e}")

# Also try to get the menu
print("\n=== 菜单检测 ===")
for w in windows_found:
    if w['visible']:
        try:
            menu = win32gui.GetMenu(w['hwnd'])
            if menu:
                print(f"  窗口 '{w['title'][:30]}' 有菜单 (handle={menu})")
                # Try to get menu items
                count = win32gui.GetMenuItemCount(menu)
                print(f"  菜单项数量: {count}")
                for i in range(min(count, 20)):
                    try:
                        item_text = win32gui.GetMenuString(menu, i, win32con.MF_BYPOSITION)
                        if item_text:
                            print(f"    [{i}] '{item_text}'")
                    except:
                        pass
        except Exception as e:
            pass

print("\n" + "=" * 60)
print("检测完成")
print("=" * 60)
