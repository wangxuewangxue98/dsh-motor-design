On Error Resume Next
Set mc = CreateObject("MotorCAD.AppAutomation")
If Err.Number = 0 Then
    WScript.Echo "COM_SUCCESS"
    WScript.Echo "TypeName: " & TypeName(mc)
    ' Try a simple method
    mc.SetVariable "test", 1
    If Err.Number = 0 Then
        WScript.Echo "SetVariable: OK"
    Else
        WScript.Echo "SetVariable: " & Err.Description
        Err.Clear
    End If
Else
    WScript.Echo "COM_FAILED"
    WScript.Echo "Error: " & Err.Number & " - " & Err.Description
End If
On Error GoTo 0
