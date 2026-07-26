# -*- coding: utf-8 -*-
"""
检查 Motor-CAD ActiveX Registration 设置
找到 Defaults → ActiveX Registration 菜单项并打开
"""
import win32gui
import win32process
import win32con
import ctypes
import time
import sys

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 找到 Motor-CAD 主窗口
main_hwnd = None
def find_main(hwnd, p):
    global main_hwnd
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if "TMainForm" in cls and "Motor-CAD" in title:
            main_hwnd = hwnd
            print(f"主窗口: hwnd={hwnd}, title='{title[:60]}'")
    except:
        pass
    return True

win32gui.EnumWindows(find_main, None)

if not main_hwnd:
    print("未找到 Motor-CAD 主窗口!")
    sys.exit(1)

# 获取菜单结构
menu_hwnd = win32gui.GetMenu(main_hwnd)
print(f"菜单句柄: {menu_hwnd}")

# ctypes 菜单函数
GetMenuItemCount = user32.GetMenuItemCount
GetMenuItemID = user32.GetMenuItemID
GetSubMenu = user32.GetSubMenu
GetMenuStringA = user32.GetMenuStringA
GetMenuStringW = user32.GetMenuStringW
MENU_ITEM_INFO_SIZE = 48  # sizeof(MENUITEMINFOW) on 32-bit

class MENUITEMINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("fMask", ctypes.wintypes.UINT),
        ("fType", ctypes.wintypes.UINT),
        ("fState", ctypes.wintypes.UINT),
        ("wID", ctypes.wintypes.UINT),
        ("hSubMenu", ctypes.wintypes.HMENU),
        ("hbmpChecked", ctypes.wintypes.HBITMAP),
        ("hbmpUnchecked", ctypes.wintypes.HBITMAP),
        ("dwItemData", ctypes.wintypes.LPVOID),
        ("dwTypeData", ctypes.c_wchar_p),
        ("cch", ctypes.wintypes.UINT),
        ("hbmpItem", ctypes.wintypes.HBITMAP),
    ]

MIIM_STRING = 0x00000040
MIIM_SUBMENU = 0x00000004
MIIM_ID = 0x00000002
MIIM_STATE = 0x00000001

def get_menu_text(hmenu, pos):
    mii = MENUITEMINFO()
    mii.cbSize = MENU_ITEM_INFO_SIZE
    mii.fMask = MIIM_STRING | MIIM_SUBMENU | MIIM_ID | MIIM_STATE
    mii.dwTypeData = None
    mii.cch = 0
    
    # 第一次调用获取字符串长度
    user32.GetMenuItemInfoW(hmenu, pos, True, ctypes.byref(mii))
    
    if mii.cch > 0:
        mii.cch += 1
        buf = ctypes.create_unicode_buffer(mii.cch)
        mii.dwTypeData = buf
        user32.GetMenuItemInfoW(hmenu, pos, True, ctypes.byref(mii))
        text = buf.value
    else:
        text = ""
    
    return text, mii.wID, mii.hSubMenu, mii.fState

# 遍历菜单
print("\n=== 菜单结构 ===")
target_ids = []

top_count = GetMenuItemCount(menu_hwnd)
for i in range(top_count):
    text, wid, submenu, state = get_menu_text(menu_hwnd, i)
    print(f"[{i}] '{text}' (id={wid}, submenu={submenu})")
    
    if submenu:
        sub_count = GetMenuItemCount(submenu)
        for j in range(sub_count):
            stext, swid, ssubmenu, sstate = get_menu_text(submenu, j)
            indent = "  "
            print(f"{indent}[{j}] '{stext}' (id={swid}, submenu={ssubmenu})")
            
            # 标记 ActiveX 相关菜单项
            if 'activex' in stext.lower() or 'registration' in stext.lower():
                print(f"  *** 发现 ActiveX 菜单项! id={swid} ***")
                target_ids.append(swid)
            
            if ssubmenu:
                ss_count = GetMenuItemCount(ssubmenu)
                for k in range(ss_count):
                    sstext, sswid, sssubmenu, ssstate = get_menu_text(ssubmenu, k)
                    print(f"    [{k}] '{sstext}' (id={sswid})")
                    if 'activex' in sstext.lower() or 'registration' in sstext.lower():
                        print(f"    *** 发现 ActiveX 菜单项! id={sswid} ***")
                        target_ids.append(sswid)

# 也搜索 Defaults 菜单下的所有项
print("\n=== 搜索 Defaults 菜单 ===")
for i in range(top_count):
    text, wid, submenu, state = get_menu_text(menu_hwnd, i)
    if 'default' in text.lower():
        print(f"找到 Defaults 菜单: [{i}] '{text}' (submenu={submenu})")
        if submenu:
            sub_count = GetMenuItemCount(submenu)
            for j in range(sub_count):
                stext, swid, ssubmenu, sstate = get_menu_text(submenu, j)
                print(f"  [{j}] '{stext}' (id={swid}, submenu={ssubmenu})")
                if 'activex' in stext.lower() or 'registration' in stext.lower() or 'automation' in stext.lower():
                    print(f"  *** 发现目标! id={swid} ***")
                    target_ids.append(swid)
                if ssubmenu:
                    ss_count = GetMenuItemCount(ssubmenu)
                    for k in range(ss_count):
                        sstext, sswid, _, _ = get_menu_text(ssubmenu, k)
                        print(f"    [{k}] '{sstext}' (id={sswid})")
                        if 'activex' in sstext.lower() or 'registration' in sstext.lower():
                            print(f"    *** 发现目标! id={sswid} ***")
                            target_ids.append(sswid)

# 如果找到目标菜单项，点击它
if target_ids:
    print(f"\n=== 打开 ActiveX Registration 对话框 (id={target_ids[0]}) ===")
    win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, target_ids[0], 0)
    time.sleep(2)
    
    # 枚举新出现的窗口
    print("新窗口:")
    def enum_new(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == 18928 and vis and title:
                print(f"  hwnd={hwnd}, cls={cls}, title='{title[:60]}'")
        except:
            pass
        return True
    win32gui.EnumWindows(enum_new, None)
    
    # 枚举所有子控件
    print("\n所有可见窗口的子控件:")
    def enum_all_children(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == 18928 and vis:
                text = f"'{title[:40]}'" if title else "(no text)"
                print(f"  hwnd={hwnd}, cls={cls}, text={text}")
        except:
            pass
        return True
    win32gui.EnumWindows(enum_all_children, None)
else:
    print("\n未找到 ActiveX Registration 菜单项")
    print("尝试搜索所有包含 'ActiveX' 或 'Registration' 的窗口...")
    
    def find_activex_windows(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == 18928:
                lower = (cls + title).lower()
                if 'activex' in lower or 'registration' in lower:
                    print(f"  hwnd={hwnd}, cls={cls}, title='{title}', vis={win32gui.IsWindowVisible(hwnd)}")
        except:
            pass
        return True
    win32gui.EnumWindows(find_activex_windows, None)
