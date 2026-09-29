Option Explicit

Const BOT_SCRIPT_NAME = "bothistory.py"
Const BOT_NAME = "Discord Status & History Logger"
Const VENV_DIR = ".venv"
Const REQ_FILE = "requirements.txt"
Const ENV_FILE = ".env"
Const ENV_EXAMPLE = ".env.example"
Const HIDDEN_MODE = 0

Dim WshShell, FSO, objWMIService, colProcesses
Dim ScriptPath, BotDir, BotScript, VenvPython, PythonCmd, VenvDir

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

ScriptPath = FSO.GetParentFolderName(WScript.ScriptFullName)
BotDir = ScriptPath
BotScript = BotDir & "\" & BOT_SCRIPT_NAME
VenvDir = BotDir & "\" & VENV_DIR
VenvPython = VenvDir & "\Scripts\python.exe"

Sub ShowWindowsToast(Title, Message)
    Dim psCmd
    psCmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command " & _
            """Add-Type -AssemblyName System.Windows.Forms; " & _
            "$notify = New-Object System.Windows.Forms.NotifyIcon; " & _
            "$notify.Icon = [System.Drawing.SystemIcons]::Information; " & _
            "$notify.Visible = $True; " & _
            "$notify.ShowBalloonTip(3500, '" & Replace(Title, "'", "''") & "', '" & Replace(Message, "'", "''") & "', [System.Windows.Forms.ToolTipIcon]::Info); " & _
            "Start-Sleep -Seconds 4; " & _
            "$notify.Dispose()"""
    On Error Resume Next
    WshShell.Run psCmd, HIDDEN_MODE, False
    On Error GoTo 0
End Sub

Function DetectPython()
    Dim exitCode
    On Error Resume Next
    exitCode = WshShell.Run("cmd /c python --version", HIDDEN_MODE, True)
    If exitCode = 0 Then
        DetectPython = "python"
        Exit Function
    End If
    
    exitCode = WshShell.Run("cmd /c py --version", HIDDEN_MODE, True)
    If exitCode = 0 Then
        DetectPython = "py"
        Exit Function
    End If
    
    DetectPython = ""
    On Error GoTo 0
End Function

Function IsBotRunning()
    Dim process, cmdLine, isFound
    isFound = False
    On Error Resume Next
    Set objWMIService = GetObject("winmgmts://./root/cimv2")
    Set colProcesses = objWMIService.ExecQuery("SELECT * FROM Win32_Process WHERE Name='python.exe' OR Name='pythonw.exe'")
    For Each process In colProcesses
        cmdLine = process.CommandLine
        If InStr(1, cmdLine, BOT_SCRIPT_NAME, vbTextCompare) > 0 Then
            isFound = True
            Exit For
        End If
    Next
    On Error GoTo 0
    IsBotRunning = isFound
End Function

Sub CheckEnvFile()
    If Not FSO.FileExists(BotDir & "\" & ENV_FILE) Then
        If FSO.FileExists(BotDir & "\" & ENV_EXAMPLE) Then
            FSO.CopyFile BotDir & "\" & ENV_EXAMPLE, BotDir & "\" & ENV_FILE
        Else
            Dim ts
            Set ts = FSO.CreateTextFile(BotDir & "\" & ENV_FILE, True)
            ts.WriteLine "DISCORD_TOKEN="
            ts.Close
        End If
    End If
End Sub

Sub SetupEnvironment()
    Dim pipCmd
    If Not FSO.FolderExists(VenvDir) Then
        On Error Resume Next
        WshShell.Run "cmd /c cd /d """ & BotDir & """ && " & PythonCmd & " -m venv """ & VENV_DIR & """", HIDDEN_MODE, True
        On Error GoTo 0
    End If
    
    If FSO.FileExists(VenvPython) Then
        If FSO.FileExists(BotDir & "\" & REQ_FILE) Then
            pipCmd = "cmd /c cd /d """ & BotDir & """ && """ & VenvPython & """ -m pip install -q -r """ & REQ_FILE & """"
            On Error Resume Next
            WshShell.Run pipCmd, HIDDEN_MODE, True
            On Error GoTo 0
        End If
    End If
End Sub

Sub LaunchBot()
    Dim runCmd
    If FSO.FileExists(VenvPython) Then
        runCmd = "cmd /c cd /d """ & BotDir & """ && """ & VenvPython & """ """ & BotScript & """"
    Else
        runCmd = "cmd /c cd /d """ & BotDir & """ && " & PythonCmd & " """ & BotScript & """"
    End If
    
    On Error Resume Next
    WshShell.Run runCmd, HIDDEN_MODE, False
    On Error GoTo 0
    ShowWindowsToast BOT_NAME, "บอทเริ่มทำงานในเบื้องหลังเรียบร้อยแล้ว (Active in Background)"
End Sub

' === Main Execution ===
If Not FSO.FileExists(BotScript) Then
    MsgBox "ไม่พบไฟล์สคริปต์: " & BOT_SCRIPT_NAME, vbCritical, "Discord Bot Error"
    WScript.Quit 1
End If

If IsBotRunning() Then
    ShowWindowsToast BOT_NAME, "บอทกำลังทำงานอยู่แล้วในระบบ (Already running)"
    WScript.Quit 0
End If

PythonCmd = DetectPython()
If PythonCmd = "" Then
    Dim ans
    ans = MsgBox("ไม่พบการติดตั้ง Python บนระบบนี้ กรุณาติดตั้ง Python 3.10 ขึ้นไปก่อนใช้งาน (อย่าลืมติ๊ก Add python.exe to PATH)" & vbCrLf & vbCrLf & "ต้องการเปิดหน้าเว็บดาวน์โหลด Python หรือไม่?", vbYesNo + vbCritical, "Discord Bot - ไม่พบ Python")
    If ans = vbYes Then
        WshShell.Run "https://www.python.org/downloads/"
    End If
    WScript.Quit 1
End If

CheckEnvFile
SetupEnvironment
LaunchBot

Set WshShell = Nothing
Set FSO = Nothing
Set objWMIService = Nothing
