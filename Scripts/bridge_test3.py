"""
bridge_test3.py - Start Motor-CAD with -Embedding, then test 32-bit COM
-Embedding flag tells COM server to register its class factory
"""
import subprocess
import json
import os
import sys
import time

def kill_motorcad():
    subprocess.run(["taskkill", "/F", "/IM", "Motor-CAD_11_1_5.exe"],
                   capture_output=True, timeout=10)
    time.sleep(3)

def start_motorcad_embedding():
    """Start Motor-CAD with -Embedding flag (COM registration mode)"""
    exe = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
    cwd = r"D:\Motor-CAD\11_1_5"
    proc = subprocess.Popen([exe, "-Embedding"], cwd=cwd)
    print(f"[{time.strftime('%H:%M:%S')}] Motor-CAD started with -Embedding, PID={proc.pid}")
    return proc

def run_bridge(action="test", **kwargs):
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
        for enc in ["utf-8", "gbk"]:
            try:
                return json.loads(raw.decode(enc))
            except:
                continue
        print(f"  Raw: {raw}")
        return {"raw": raw.decode("gbk", errors="replace")}
    return None


if __name__ == "__main__":
    print("=== Motor-CAD COM Bridge Test (with -Embedding) ===")

    # Step 1: Kill existing Motor-CAD
    print("\n--- Step 1: Kill Motor-CAD ---")
    kill_motorcad()

    # Step 2: Start Motor-CAD with -Embedding flag
    print("\n--- Step 2: Start Motor-CAD with -Embedding ---")
    proc = start_motorcad_embedding()

    # Step 3: Wait for full load (90 seconds)
    print(f"\n--- Step 3: Wait 90s for Motor-CAD to load + register COM ---")
    for i in range(90):
        if (i + 1) % 15 == 0:
            # Check if process is alive
            check = subprocess.run(
                ["tasklist", "/FI", "PID eq " + str(proc.pid), "/FO", "CSV", "/NH"],
                capture_output=True, timeout=5, encoding="gbk", errors="replace"
            )
            alive = str(proc.pid) in (check.stdout or "")
            print(f"  [{i+1}s] Process alive: {alive}")
        time.sleep(1)

    # Step 4: Test COM connection
    print(f"\n--- Step 4: Test 32-bit COM bridge ---")
    result = run_bridge("test")

    if result:
        print(f"\n  Result: {json.dumps(result, indent=2, ensure_ascii=False)}")
        if result.get("success"):
            print("\n  >>> COM BRIDGE WORKS! <<<")
        else:
            print(f"\n  >>> COM failed: {result.get('error', 'unknown')} <<<")
    else:
        print("\n  >>> No result <<<")
