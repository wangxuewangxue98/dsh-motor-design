"""
bridge_test.py - Test 32-bit COM bridge for Motor-CAD
This script runs 32-bit WSH to connect to 32-bit Motor-CAD COM,
bypassing the 64-bit/32-bit COM incompatibility.
"""
import subprocess
import json
import os
import sys
import time

def run_bridge(action="test", **kwargs):
    """Run COM command via 32-bit bridge"""
    scripts_dir = r"D:\MotorDesign\Scripts"

    # Build command JSON
    cmd = {"action": action}
    cmd.update(kwargs)
    cmd_text = json.dumps(cmd, ensure_ascii=False)

    # Write command file
    cmd_file = os.path.join(scripts_dir, "_bridge_cmd.json")
    result_file = os.path.join(scripts_dir, "_bridge_result.json")

    with open(cmd_file, "w", encoding="utf-8") as f:
        f.write(cmd_text)

    # Clean old result
    if os.path.exists(result_file):
        os.remove(result_file)

    # Build the 32-bit scripting host path
    # SysWOW64 contains 32-bit system tools on 64-bit Windows
    wow64 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SysWOW64")
    host32 = os.path.join(wow64, "cscript.exe")
    vbs_path = os.path.join(scripts_dir, "motorcad_bridge.vbs")

    if not os.path.exists(host32):
        print(f"ERROR: 32-bit host not found at {host32}")
        return None

    print(f"[{time.strftime('%H:%M:%S')}] Running 32-bit bridge: action={action}")
    sys.stdout.flush()

    result = subprocess.run(
        [host32, "//nologo", vbs_path, cmd_file, result_file],
        capture_output=True, timeout=180
    )

    stdout_text = result.stdout.decode("gbk", errors="replace") if result.stdout else ""
    stderr_text = result.stderr.decode("gbk", errors="replace") if result.stderr else ""

    if stdout_text.strip():
        print(f"Bridge stdout: {stdout_text.strip()}")
    if stderr_text.strip():
        print(f"Bridge stderr: {stderr_text.strip()}")
    print(f"Return code: {result.returncode}")
    sys.stdout.flush()

    # Read result
    if os.path.exists(result_file):
        with open(result_file, "r", encoding="utf-8") as f:
            result_text = f.read()
        try:
            return json.loads(result_text)
        except json.JSONDecodeError:
            print(f"Raw result: {result_text}")
            return {"success": False, "raw": result_text}
    else:
        print("No result file generated!")
        return None


if __name__ == "__main__":
    print("=== Motor-CAD 32-bit COM Bridge Test ===")
    result = run_bridge("test")
    if result:
        print(f"\nResult: {json.dumps(result, indent=2, ensure_ascii=False)}")
        if result.get("success"):
            print("\n>>> COM BRIDGE WORKS! <<<")
        else:
            print(f"\n>>> Bridge failed: {result.get('error', 'unknown')} <<<")
    else:
        print("\n>>> Bridge did not produce output <<<")
