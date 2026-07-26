' Motor-CAD COM 自动化测试 (32位 VBScript)
' 使用 32位 cscript.exe 运行: C:\Windows\SysWOW64\cscript.exe test_com_vbs.vbs

Option Explicit
Dim fso, outFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set outFile = fso.CreateTextFile("D:\MotorDesign\Scripts\vbs_com_results.txt", True)

Sub Log(msg)
    WScript.StdOut.WriteLine msg
    outFile.WriteLine msg
End Sub

Log "=== Motor-CAD COM 测试 (32位 VBScript) ==="
Log "时间: " & Now

On Error Resume Next

Log ""
Log "尝试创建 MotorCAD.AppAutomation COM 对象..."
Dim mcad
Set mcad = CreateObject("MotorCAD.AppAutomation")

If Err.Number <> 0 Then
    Log "COM 连接失败!"
    Log "错误号: " & Err.Number
    Log "错误描述: " & Err.Description
    Log "错误来源: " & Err.Source
    
    ' 尝试小写 ProgID
    Err.Clear
    Log ""
    Log "尝试小写 ProgID: motorcad.appautomation..."
    Set mcad = CreateObject("motorcad.appautomation")
    
    If Err.Number <> 0 Then
        Log "小写 ProgID 也失败!"
        Log "错误号: " & Err.Number
        Log "错误描述: " & Err.Description
        
        outFile.Close
        WScript.Quit 1
    End If
End If

Log ""
Log "COM 连接成功!"
Err.Clear

' 获取基本信息
Dim funcResult, val

Log ""
Log "获取 MotorCADprocessID..."
funcResult = mcad.GetVariable("MotorCADprocessID")
If Err.Number = 0 Then
    Log "  MotorCADprocessID = " & funcResult
Else
    Log "  失败: " & Err.Description
    Err.Clear
End If

Log ""
Log "获取 MotorCADexeName..."
funcResult = mcad.GetVariable("MotorCADexeName")
If Err.Number = 0 Then
    Log "  MotorCADexeName = " & funcResult
Else
    Log "  失败: " & Err.Description
    Err.Clear
End If

Log ""
Log "获取 CurrentExeDir_MotorLAB..."
funcResult = mcad.GetVariable("CurrentExeDir_MotorLAB")
If Err.Number = 0 Then
    Log "  CurrentExeDir_MotorLAB = " & funcResult
Else
    Log "  失败: " & Err.Description
    Err.Clear
End If

Log ""
Log "获取 Shaft_Speed_[RPM]..."
funcResult = mcad.GetVariable("Shaft_Speed_[RPM]")
If Err.Number = 0 Then
    Log "  Shaft_Speed = " & funcResult
Else
    Log "  失败: " & Err.Description
    Err.Clear
End If

' 尝试运行稳态热分析
Log ""
Log "=== 尝试 DoSteadyStateAnalysis ==="
mcad.DoSteadyStateAnalysis
If Err.Number = 0 Then
    Log "DoSteadyStateAnalysis 成功!"
    
    ' 获取温度结果
    Log ""
    Log "获取温度结果..."
    
    funcResult = mcad.GetVariable("T_[Winding_Average]")
    If Err.Number = 0 Then
        Log "  T_[Winding_Average] = " & funcResult & " C"
    Else
        Log "  T_[Winding_Average] 失败: " & Err.Description
        Err.Clear
    End If
    
    funcResult = mcad.GetVariable("T_[Housing_Average]")
    If Err.Number = 0 Then
        Log "  T_[Housing_Average] = " & funcResult & " C"
    Else
        Log "  T_[Housing_Average] 失败: " & Err.Description
        Err.Clear
    End If
    
    funcResult = mcad.GetVariable("T_[Magnet_Average]")
    If Err.Number = 0 Then
        Log "  T_[Magnet_Average] = " & funcResult & " C"
    Else
        Log "  T_[Magnet_Average] 失败: " & Err.Description
        Err.Clear
    End If
Else
    Log "DoSteadyStateAnalysis 失败: " & Err.Description
    Err.Clear
End If

' 关闭 COM
Log ""
Log "关闭 COM 连接..."
mcad.Quit
If Err.Number <> 0 Then
    Log "Quit 失败: " & Err.Description
    Err.Clear
End If

Set mcad = Nothing
Log ""
Log "=== 测试完成 ==="

outFile.Close
WScript.Quit 0
