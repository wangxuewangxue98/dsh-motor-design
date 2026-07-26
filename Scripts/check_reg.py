"""check_reg.py - Check COM registry from 32-bit and 64-bit views"""
import winreg
import os

CLSID = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"

print("=== 32-bit COM registry view (what 32-bit cscript sees) ===")
for root_name, root in [("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)]:
    paths = [
        f"Software\\Classes\\CLSID\\{CLSID}\\LocalServer32",
        f"Software\\Classes\\CLSID\\{CLSID}\\InProcServer32",
        f"Software\\Classes\\CLSID\\{CLSID}",
    ]
    for p in paths:
        try:
            key = winreg.OpenKey(root, p, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_32KEY)
            val, typ = winreg.QueryValueEx(key, None)
            typ_name = {0: "REG_NONE", 1: "REG_SZ", 2: "REG_EXPAND_SZ", 3: "REG_BINARY", 4: "REG_DWORD"}.get(typ, f"type{typ}")
            winreg.CloseKey(key)
            print(f"  {root_name} 32bit [{p}]: {val} ({typ_name})")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  {root_name} 32bit [{p}]: ERROR {e}")

print()
print("=== ProgID registration (32-bit view) ===")
for progid_path in ["Software\\Classes\\MotorCAD.AppAutomation", "Software\\Classes\\MotorCAD.AppAutomation\\CLSID"]:
    for root_name, root in [("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER)]:
        try:
            key = winreg.OpenKey(root, progid_path, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_32KEY)
            val, typ = winreg.QueryValueEx(key, None)
            winreg.CloseKey(key)
            print(f"  {root_name} 32bit [{progid_path}]: {val}")
        except FileNotFoundError:
            print(f"  {root_name} 32bit [{progid_path}]: NOT FOUND")

print()
print("=== MOTORCAD_ACTIVEX env var ===")
val = os.environ.get("MOTORCAD_ACTIVEX", "(not set)")
print(f"  Process level: {val}")

# Check system level
try:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment", 0, winreg.KEY_QUERY_VALUE) as key:
        sys_val, _ = winreg.QueryValueEx(key, "MOTORCAD_ACTIVEX")
        print(f"  System level: {sys_val}")
except FileNotFoundError:
    print("  System level: (not set)")
except Exception as e:
    print(f"  System level: ERROR {e}")

# Check if .exe exists
exe_path = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
print(f"\n=== Motor-CAD exe exists: {os.path.exists(exe_path)} ===")
print(f"  Path: {exe_path}")

# Try to register Motor-CAD COM server with /regserver
print("\n=== Checking if Motor-CAD supports /regserver ===")
# Many COM servers support /regserver to register themselves
