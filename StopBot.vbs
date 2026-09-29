Option Explicit

Const BOT_SCRIPT_NAME = "bothistory.py"
Const BOT_NAME = "Discord Status & History Logger"
Const HIDDEN_MODE = 0

Dim WshShell, FSO, objWMIService, colProcesses
Dim ScriptPath, BotDir

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

ScriptPath = FSO.GetParentFolderName(WScript.ScriptFullName)
BotDir = ScriptPath

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
    WshShell.Run psCmd, HIDDEN_MODE, False
End Sub

Function FindBotProcesses()
    Dim process, cmdLine
    Dim botPIDs
    botPIDs = ""
    
    On Error Resume Next
    Set objWMIService = GetObject("winmgmts://./root/cimv2")
    Set colProcesses = objWMIService.ExecQuery("SELECT * FROM Win32_Process WHERE Name='python.exe' OR Name='pythonw.exe'")
    
    For Each process In colProcesses
        cmdLine = process.CommandLine
        If InStr(1, cmdLine, BOT_SCRIPT_NAME, vbTextCompare) > 0 Then
            If botPIDs = "" Then
                botPIDs = CStr(process.ProcessId)
            Else
                botPIDs = botPIDs & "," & CStr(process.ProcessId)
            End If
        End If
    Next
    On Error GoTo 0
    
    FindBotProcesses = botPIDs
End Function

Function KillBotProcesses()
    Dim pids, pidArray, pid
    Dim killedCount, killCmd
    killedCount = 0
    
    pids = FindBotProcesses()
    
    If pids = "" Then
        KillBotProcesses = 0
        Exit Function
    End If
    
    pidArray = Split(pids, ",")
    
    For Each pid In pidArray
        If Trim(pid) <> "" Then
            killCmd = "taskkill /PID " & Trim(pid) & " /F"
            On Error Resume Next
            WshShell.Run killCmd, HIDDEN_MODE, True
            If Err.Number = 0 Then
                killedCount = killedCount + 1
            End If
            On Error GoTo 0
        End If
    Next
    
    KillBotProcesses = killedCount
End Function

' === Main Execution ===
Dim killedCount
killedCount = KillBotProcesses()

If killedCount > 0 Then
    ShowWindowsToast BOT_NAME, "ปิดการทำงานของบอทเรียบร้อยแล้ว (" & killedCount & " process stopped)"
Else
    ShowWindowsToast BOT_NAME, "ไม่พบบอทที่กำลังทำงานอยู่ในระบบ (Bot is not running)"
End If

Set WshShell = Nothing
Set FSO = Nothing
Set objWMIService = Nothing
