
# -*- coding: mbcs -*-
import sys
import os

print("Python version:", sys.version)
print("Platform:", sys.platform)
print("Executable:", sys.executable)

# Try to import win32com
try:
    import win32com.client
    print("win32com available: YES")
except ImportError as e:
    print("win32com available: NO -", e)
    sys.exit(1)

# Try to connect to Motor-CAD COM
print("")
print("=== COM Connection Test ===")

# Strategy 1: GetActiveObject (connect to running instance)
try:
    mc = win32com.client.GetActiveObject("MotorCAD.AppAutomation")
    print("GetActiveObject: SUCCESS!")
    try:
        ver = mc.GetVariable("Version")
        print("  Version:", ver)
    except Exception as e:
        print("  GetVariable failed:", e)
    sys.exit(0)
except Exception as e:
    print("GetActiveObject failed:", e)

# Strategy 2: Dispatch (start new or connect)
try:
    mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
    print("Dispatch: SUCCESS!")
    try:
        ver = mc.GetVariable("Version")
        print("  Version:", ver)
    except Exception as e:
        print("  GetVariable failed:", e)
    sys.exit(0)
except Exception as e:
    err = str(e)
    print("Dispatch failed:", err)
    if "0x800700c1" in err.lower():
        print("  >> bad exe format (LocalServer32 points to .bat)")
    elif "0x80080005" in err.lower():
        print("  >> server execution failed")
    elif "0x800401f3" in err.lower():
        print("  >> invalid class string (ProgID not registered)")
    elif "demo" in err.lower():
        print("  >> demo restriction")

# Strategy 3: EnsureDispatch (generates type library wrapper)
try:
    mc = win32com.client.gencache.EnsureDispatch("MotorCAD.AppAutomation")
    print("EnsureDispatch: SUCCESS!")
    sys.exit(0)
except Exception as e:
    print("EnsureDispatch failed:", e)

print("")
print("RESULT: ALL COM METHODS FAILED")
sys.exit(1)
