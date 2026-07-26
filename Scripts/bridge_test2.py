"""
bridge_test2.py - Test COM with fresh Motor-CAD launch
Kill running Motor-CAD, then let COM start it fresh via 32-bit cscript
"""
import subprocess
import json
import os
import sys
import time
import ctypes

def kill_motorcad():
    """Kill all Motor-CAD processes"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "Motor-CAD_11_1_5.exe"],
            capture_output=True, timeout=10
        )
        print(f"[{time.strftime('%H:%M:%S')}] Motor-CAD killed")
    except Exception:
        pass
    time.sleep(3)

def run_bridge(action="test", **kwargs):
    """Run COM command via 32-bit bridge"""
    scripts_dir = r"D:\MotorDesign\Scripts"

    cmd = {"action": action}
    cmd.update(kwargs)
    cmd_text = json.dumps(cmd, ensure_ascii=False)

    cmd_file = os.path.join(scripts_dir, "_bridge_cmd.json")
    result_file = os.path.join(scripts_dir, "_bridge_result.json")

    with open(cmd_file, "w", encoding="utf-8") as f:
        f.write(cmd_text)

    if os.path.exists(result_file):
        os.remove(result_file)

    wow64 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SysWOW64")
    host32 = os.path.join(wow64, "cscript.exe")
    vbs_path = os.path.join(scripts_dir, "motorcad_bridge.vbs")

    print(f"[{time.strftime('%H:%M:%S')}] Running 32-bit bridge: action={action}")
    sys.stdout.flush()

    result = subprocess.run(
        [host32, "//nologo", vbs_path, cmd_file, result_file],
        capture_output=True, timeout=300
    )

    stdout_text = result.stdout.decode("gbk", errors="replace") if result.stdout else ""
    stderr_text = result.stderr.decode("gbk", errors="replace") if result.stderr else ""

    if stdout_text.strip():
        print(f"  stdout: {stdout_text.strip()}")
    if stderr_text.strip():
        print(f"  stderr: {stderr_text.strip()}")
    print(f"  return code: {result.returncode}")
    sys.stdout.flush()

    if os.path.exists(result_file):
        with open(result_file, "rb") as f:
            raw = f.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except:
            try:
                return json.loads(raw.decode("gbk"))
            except:
                print(f"  Raw result: {raw}")
                return {"raw": raw.decode("gbk", errors="replace")}
    else:
        print("  No result file!")
        return None


if __name__ == "__main__":
    print("=== Motor-CAD COM Bridge Test (Fresh Launch) ===")

    # Step 1: Kill running Motor-CAD
    print("\n--- Step 1: Kill Motor-CAD ---")
    kill_motorcad()
    time.sleep(2)

    # Verify it's killed
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Motor-CAD*", "/FO", "CSV", "/NH"],
        capture_output=True, timeout=5, encoding="gbk", errors="replace"
    )
    mc_running = "Motor-CAD" in (r.stdout or "")
    print(f"  Motor-CAD running: {mc_running}")

    if mc_running:
        print("  WARNING: Motor-CAD still running, trying again...")
        kill_motorcad()
        time.sleep(3)

    # Step 2: Run COM bridge - COM will start Motor-CAD fresh
    print("\n--- Step 2: Run 32-bit COM bridge (fresh launch) ---")
    print("  (COM will start Motor-CAD via LocalServer32)")
    result = run_bridge("test")

    if result:
        print(f"\n  Result: {json.dumps(result, indent=2, ensure_ascii=False)}")
        if result.get("success"):
            print("\n  >>> COM BRIDGE WORKS! <<<")
        else:
            err = result.get("error", "unknown")
            print(f"\n  >>> COM failed: {err} <<<")

            # Check if Motor-CAD was launched by COM
            r2 = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Motor-CAD*", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=5, encoding="gbk", errors="replace"
            )
            mc_launched = "Motor-CAD" in (r2.stdout or "")
            print(f"  Motor-CAD was launched by COM: {mc_launched}")
            if mc_launched:
                print("  >> COM DID start Motor-CAD, but class factory didn't register")
                print("  >> Try waiting longer and retrying...")
            else:
                print("  >> COM did NOT start Motor-CAD process")
    else:
        print("\n  >>> No result from bridge <<<")
