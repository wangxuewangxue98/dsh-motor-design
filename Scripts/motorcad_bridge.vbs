' motorcad_bridge.vbs - Motor-CAD COM bridge (runs in 32-bit cscript)
' Connects to Motor-CAD via COM and executes commands from JSON file
' Usage: cscript //nologo motorcad_bridge.vbs <command_json> <result_json>

Option Explicit

Dim fso, WshShell, cmdFile, resultFile
Dim mc, cmdText, action

Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 2 Then
    WScript.StdErr.WriteLine "Usage: motorcad_bridge.vbs <cmd_json> <result_json>"
    WScript.Quit(1)
End If

cmdFile = WScript.Arguments(0)
resultFile = WScript.Arguments(1)

' Read command JSON
If Not fso.FileExists(cmdFile) Then
    WriteResult resultFile, "{""success"": false, ""error"": ""Command file not found: " & cmdFile & """}"
    WScript.Quit(1)
End If

Dim cmdStream
Set cmdStream = fso.OpenTextFile(cmdFile, 1)
cmdText = cmdStream.ReadAll()
cmdStream.Close

action = ExtractJsonField(cmdText, "action")

' ===== COM Connection with multiple strategies =====
On Error Resume Next

' Strategy 1: GetObject with correct syntax (connect to running instance)
' GetObject(, "class") = get existing instance via ROT
Set mc = GetObject(, "MotorCAD.AppAutomation")
If Err.Number = 0 Then
    On Error GoTo 0
    ExecuteAction mc, action, cmdText, resultFile
    WScript.Quit(0)
End If
Dim getObjErr
getObjErr = Err.Number & ": " & Err.Description
Err.Clear

' Strategy 2: CreateObject (start new instance via COM SCM)
Set mc = CreateObject("MotorCAD.AppAutomation")
If Err.Number = 0 Then
    On Error GoTo 0
    ExecuteAction mc, action, cmdText, resultFile
    WScript.Quit(0)
End If
Dim createObjErr
createObjErr = Err.Number & ": " & Err.Description
Err.Clear

' Strategy 3: Try with CLSID directly
Dim clsid
clsid = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
Set mc = CreateObject("new:" & clsid)
If Err.Number = 0 Then
    On Error GoTo 0
    ExecuteAction mc, action, cmdText, resultFile
    WScript.Quit(0)
End If
Dim clsidErr
clsidErr = Err.Number & ": " & Err.Description
Err.Clear

On Error GoTo 0

' All strategies failed
WriteResult resultFile, "{""success"": false, ""error"": ""COM connection failed. GetObject: " & CleanJson(getObjErr) & "; CreateObject: " & CleanJson(createObjErr) & "; CLSID: " & CleanJson(clsidErr) & """}"
WScript.Quit(1)

' ===== Execute action =====
Sub ExecuteAction(mc, action, cmdText, resultFile)
    Dim result
    Select Case action
        Case "test"
            result = DoTest(mc)
        Case "load_template"
            result = DoLoadTemplate(mc, cmdText)
        Case "set_variable"
            result = DoSetVariable(mc, cmdText)
        Case "get_variable"
            result = DoGetVariable(mc, cmdText)
        Case "run_emag"
            result = DoRunEmag(mc, cmdText)
        Case "run_thermal"
            result = DoRunThermal(mc, cmdText)
        Case "get_results"
            result = DoGetResults(mc, cmdText)
        Case "batch_solve"
            result = DoBatchSolve(mc, cmdText)
        Case Else
            result = "{""success"": false, ""error"": ""Unknown action: " & action & """}"
    End Select
    WriteResult resultFile, result
End Sub

' ===== Action implementations =====

Function DoTest(mc)
    On Error Resume Next
    Dim ver, output
    output = "{""success"": true, ""action"": ""test"", ""data"": {"

    ver = mc.GetVariable("Version")
    If Err.Number = 0 Then
        output = output & """version"": """ & CleanJson(ver) & """"
    Else
        Err.Clear
        output = output & """version"": ""unknown"""
    End If

    output = output & ", ""variables"": {"
    output = output & """motor_type"": """ & SafeGetVar(mc, "motor_type") & """"
    output = output & ", ""no_poles"": """ & SafeGetVar(mc, "no_poles") & """"
    output = output & ", ""rated_speed"": """ & SafeGetVar(mc, "rated_speed") & """"
    output = output & "}}"

    DoTest = output
End Function

Function DoLoadTemplate(mc, cmdText)
    Dim templatePath
    templatePath = ExtractJsonField(cmdText, "template_path")

    On Error Resume Next
    mc.LoadTemplate templatePath
    If Err.Number <> 0 Then
        DoLoadTemplate = "{""success"": false, ""error"": ""LoadTemplate failed: " & CleanJson(Err.Description) & """}"
        Err.Clear
        Exit Function
    End If

    DoLoadTemplate = "{""success"": true, ""action"": ""load_template"", ""template"": """ & CleanJson(templatePath) & """}"
End Function

Function DoSetVariable(mc, cmdText)
    Dim varName, varValue
    varName = ExtractJsonField(cmdText, "variable")
    varValue = ExtractJsonField(cmdText, "value")

    On Error Resume Next
    mc.SetVariable varName, varValue
    If Err.Number <> 0 Then
        DoSetVariable = "{""success"": false, ""error"": ""SetVariable failed: " & CleanJson(Err.Description) & """}"
        Err.Clear
        Exit Function
    End If

    DoSetVariable = "{""success"": true, ""action"": ""set_variable"", ""variable"": """ & CleanJson(varName) & """, ""value"": """ & CleanJson(varValue) & """}"
End Function

Function DoGetVariable(mc, cmdText)
    Dim varName, varValue
    varName = ExtractJsonField(cmdText, "variable")

    On Error Resume Next
    varValue = mc.GetVariable(varName)
    If Err.Number <> 0 Then
        DoGetVariable = "{""success"": false, ""error"": ""GetVariable failed: " & CleanJson(Err.Description) & """}"
        Err.Clear
        Exit Function
    End If

    DoGetVariable = "{""success"": true, ""action"": ""get_variable"", ""variable"": """ & CleanJson(varName) & """, ""value"": """ & CleanJson(CStr(varValue)) & """}"
End Function

Function DoRunEmag(mc, cmdText)
    On Error Resume Next
    mc.DoStaticEmagSim
    If Err.Number <> 0 Then
        Err.Clear
        mc.RunSimulation "EMag"
        If Err.Number <> 0 Then
            Err.Clear
            mc.CalculateEMag
            If Err.Number <> 0 Then
                DoRunEmag = "{""success"": false, ""error"": ""EMag simulation failed: " & CleanJson(Err.Description) & """}"
                Err.Clear
                Exit Function
            End If
        End If
    End If

    DoRunEmag = "{""success"": true, ""action"": ""run_emag""}"
End Function

Function DoRunThermal(mc, cmdText)
    On Error Resume Next
    mc.DoThermalSimulation
    If Err.Number <> 0 Then
        Err.Clear
        mc.RunSimulation "Thermal"
        If Err.Number <> 0 Then
            Err.Clear
            mc.CalculateThermal
            If Err.Number <> 0 Then
                DoRunThermal = "{""success"": false, ""error"": ""Thermal simulation failed: " & CleanJson(Err.Description) & """}"
                Err.Clear
                Exit Function
            End If
        End If
    End If

    DoRunThermal = "{""success"": true, ""action"": ""run_thermal""}"
End Function

Function DoGetResults(mc, cmdText)
    On Error Resume Next
    Dim output, i, varName, varValue

    Dim resultVars(19)
    resultVars(0) = "Torque"
    resultVars(1) = "rms_phase_current"
    resultVars(2) = "max_phase_current"
    resultVars(3) = "no_poles"
    resultVars(4) = "no_slots"
    resultVars(5) = "winding_factor"
    resultVars(6) = "airgap_flux_density"
    resultVars(7) = "iron_loss"
    resultVars(8) = "copper_loss"
    resultVars(9) = "friction_loss"
    resultVars(10) = "total_loss"
    resultVars(11) = "efficiency"
    resultVars(12) = "power_factor"
    resultVars(13) = "output_power"
    resultVars(14) = "input_power"
    resultVars(15) = "rated_torque"
    resultVars(16) = "rated_speed"
    resultVars(17) = "motor_type"
    resultVars(18) = "stack_length"
    resultVars(19) = "stator_outer_diameter"

    output = "{""success"": true, ""action"": ""get_results"", ""data"": {"
    Dim first
    first = True
    For i = 0 To 19
        varName = resultVars(i)
        varValue = ""
        Err.Clear
        varValue = mc.GetVariable(varName)
        If Err.Number = 0 And Len(CStr(varValue)) > 0 Then
            If Not first Then
                output = output & ", "
            End If
            output = output & """" & varName & """: """ & CleanJson(CStr(varValue)) & """"
            first = False
        Else
            Err.Clear
        End If
    Next
    output = output & "}}"

    DoGetResults = output
End Function

Function DoBatchSolve(mc, cmdText)
    Dim templatePath
    templatePath = ExtractJsonField(cmdText, "template_path")

    If Len(templatePath) > 0 Then
        On Error Resume Next
        mc.LoadTemplate templatePath
        If Err.Number <> 0 Then
            DoBatchSolve = "{""success"": false, ""error"": ""LoadTemplate failed: " & CleanJson(Err.Description) & """}"
            Err.Clear
            Exit Function
        End If
        On Error GoTo 0
    End If

    On Error Resume Next
    mc.DoStaticEmagSim
    If Err.Number <> 0 Then
        Err.Clear
        mc.CalculateEMag
        If Err.Number <> 0 Then
            Err.Clear
        End If
    End If

    Dim done, attempts
    done = False
    For attempts = 1 To 300
        Err.Clear
        done = mc.GetVariable("simulation_complete")
        If Err.Number = 0 And (done = True Or done = "True" Or done = "1" Or done = 1) Then
            Exit For
        End If
        Err.Clear
        WScript.Sleep(1000)
    Next

    DoBatchSolve = DoGetResults(mc, cmdText)
End Function

' ===== Helper functions =====

Function SafeGetVar(mc, varName)
    On Error Resume Next
    Dim val
    val = mc.GetVariable(varName)
    If Err.Number = 0 Then
        SafeGetVar = CleanJson(CStr(val))
    Else
        Err.Clear
        SafeGetVar = "N/A"
    End If
End Function

Function ExtractJsonField(jsonText, fieldName)
    Dim searchStr, startPos, endPos
    searchStr = """" & fieldName & """"
    startPos = InStr(jsonText, searchStr)
    If startPos = 0 Then
        ExtractJsonField = ""
        Exit Function
    End If
    startPos = startPos + Len(searchStr)
    Do While startPos <= Len(jsonText) And (Mid(jsonText, startPos, 1) = ":" Or Mid(jsonText, startPos, 1) = " " Or Mid(jsonText, startPos, 1) = Chr(9))
        startPos = startPos + 1
    Loop
    If startPos <= Len(jsonText) And Mid(jsonText, startPos, 1) = """" Then
        startPos = startPos + 1
        endPos = InStr(startPos, jsonText, """")
        If endPos > 0 Then
            ExtractJsonField = Mid(jsonText, startPos, endPos - startPos)
        Else
            ExtractJsonField = ""
        End If
    Else
        endPos = startPos
        Do While endPos <= Len(jsonText) And Mid(jsonText, endPos, 1) <> "," And Mid(jsonText, endPos, 1) <> "}" And Mid(jsonText, endPos, 1) <> Chr(10) And Mid(jsonText, endPos, 1) <> Chr(13)
            endPos = endPos + 1
        Loop
        ExtractJsonField = Trim(Mid(jsonText, startPos, endPos - startPos))
    End If
End Function

Function CleanJson(text)
    Dim result
    result = CStr(text)
    result = Replace(result, "\", "\\")
    result = Replace(result, """", "\""")
    result = Replace(result, Chr(10), "\n")
    result = Replace(result, Chr(13), "")
    CleanJson = result
End Function

Sub WriteResult(filePath, text)
    Dim stream
    Set stream = fso.CreateTextFile(filePath, True)
    stream.Write text
    stream.Close
End Sub
