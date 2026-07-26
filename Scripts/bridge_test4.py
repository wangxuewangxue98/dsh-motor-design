"""
bridge_test4.py - Use MotorLABruntime.exe (32-bit Python 2.7) as COM bridge
Motor-LAB has its own 32-bit Python 2.7 with pythoncom27.dll
This should be able to connect to 32-bit Motor-CAD COM natively
"""
import subprocess
import json
import os
import sys
import time

def run_motorlab_script(script_path):
    """Run a Python script using MotorLABruntime.exe (32-bit Python 2.7)"""
    runtime = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib\MotorLABruntime.exe"
    lib_dir = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib"

    if not os.path.exists(runtime):
        print(f"ERROR: MotorLABruntime.exe not found at {runtime}")
        return None

    print(f"[{time.strftime('%H:%M:%S')}] Running MotorLABruntime with script: {os.path.basename(script_path)}")
    sys.stdout.flush()

    # MotorLABruntime.exe might accept script path as argument
    # Also set PYTHONPATH to include the lib directory
    env = os.environ.copy()
    env["PYTHONPATH"] = lib_dir + os.pathsep + os.path.join(lib_dir, "support")
    env["PATH"] = lib_dir + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [runtime, script_path],
        capture_output=True, timeout=180,
        env=env,
        cwd=lib_dir
    )

    stdout_text = result.stdout.decode("gbk", errors="replace") if result.stdout else ""
    stderr_text = result.stderr.decode("gbk", errors="replace") if result.stderr else ""

    if stdout_text.strip():
        print(f"  stdout: {stdout_text.strip()[:2000]}")
    if stderr_text.strip():
        print(f"  stderr: {stderr_text.strip()[:2000]}")
    print(f"  return code: {result.returncode}")
    sys.stdout.flush()

    return result


# Python 2.7 compatible script that tests COM connection
MOTORLAB_TEST_SCRIPT = r"""
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
"""

if __name__ == "__main__":
    print("=== Motor-LAB (32-bit Python 2.7) COM Bridge Test ===")

    # Write the test script
    script_path = r"D:\MotorDesign\Scripts\motorlab_test.py"
    with open(script_path, "w", encoding="mbcs") as f:
        f.write(MOTORLAB_TEST_SCRIPT)
    print(f"Test script written to: {script_path}")

    # Run it
    result = run_motorlab_script(script_path)

    if result and result.returncode == 0:
        print("\n>>> Motor-LAB COM bridge WORKS! <<<")
    else:
        print("\n>>> Motor-LAB COM bridge failed <<<")

        # Fallback: also try just running MotorLABruntime.exe with --version or --help
        print("\n--- Checking MotorLABruntime.exe usage ---")
        runtime = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib\MotorLABruntime.exe"
        lib_dir = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib"

        r2 = subprocess.run(
            [runtime, "--version"],
            capture_output=True, timeout=10,
            cwd=lib_dir
        )
        print(f"  --version: {r2.stdout.decode('gbk', errors='replace')}")
        print(f"  stderr: {r2.stderr.decode('gbk', errors='replace')}")
