' test_com.vbs - Test COM connection to Motor-CAD via 32-bit cscript
' Run with: C:\Windows\SysWOW64\cscript.exe //nologo test_com.vbs

On Error Resume Next

WScript.Echo "=== Motor-CAD COM Bridge Test ==="
WScript.Echo "Process: 32-bit cscript"

' Try to connect to running Motor-CAD instance
WScript.Echo "Attempting COM connection: MotorCAD.AppAutomation..."

Set mc = GetObject("MotorCAD.AppAutomation")

If Err.Number <> 0 Then
    WScript.Echo "GetObject failed: " & Err.Number & " - " & Err.Description
    Err.Clear
    
    ' Try CreateObject (start new instance)
    WScript.Echo "Trying CreateObject..."
    Set mc = CreateObject("MotorCAD.AppAutomation")
    
    If Err.Number <> 0 Then
        WScript.Echo "CreateObject failed: " & Err.Number & " - " & Err.Description
        WScript.Echo "RESULT: FAILED"
        WScript.Quit(1)
    End If
End If

WScript.Echo "COM CONNECTED SUCCESSFULLY!"

' Try to read version
On Error Resume Next
ver = mc.GetVariable("Version")
If Err.Number = 0 Then
    WScript.Echo "Motor-CAD Version: " & ver
Else
    WScript.Echo "GetVariable(Version) failed: " & Err.Description
    Err.Clear
End If

' Try to read a few common variables
vars = Array("motor_type", "no_magnet_segments", "pole_pairs", "rated_speed")
For Each vname In vars
    On Error Resume Next
    val = mc.GetVariable(vname)
    If Err.Number = 0 Then
        WScript.Echo "  " & vname & " = " & val
    Else
        WScript.Echo "  " & vname & " = (not available)"
        Err.Clear
    End If
Next

WScript.Echo "RESULT: SUCCESS"
WScript.Quit(0)
