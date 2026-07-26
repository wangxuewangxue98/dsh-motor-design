Write-Host "=== 32-bit PowerShell COM Test ==="
Write-Host "Process bits: $([System.IntPtr]::Size * 8)"

# Kill existing Motor-CAD
Get-Process -Name 'Motor-CAD*' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Trying New-Object -ComObject MotorCAD.AppAutomation..."
Write-Host "(Motor-CAD will start, please wait 60-120s...)"

try {
    $mc = New-Object -ComObject MotorCAD.AppAutomation
    Write-Host "SUCCESS! COM connection established!"

    $mc.Visible = $true
    Write-Host "Visible = True: OK"

    $template = 'D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot'
    if (Test-Path $template) {
        $mc.LoadFromFile($template)
        Write-Host "LoadFromFile: OK"
    }

    $mc.SetVariable('Speed', 3000)
    $val = $mc.GetVariable('Speed')
    Write-Host "Speed = $val"

    Write-Host ""
    Write-Host "Motor-CAD COM automation WORKS from 32-bit!"

} catch {
    Write-Host "FAILED: $_"
    Write-Host "Error details: $($_.Exception.Message)"
    if ($_.Exception.HResult) {
        Write-Host "HRESULT: 0x$($_.Exception.HResult.ToString('X8'))"
    }
}
