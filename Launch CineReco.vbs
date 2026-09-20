Option Explicit
Dim sh, fso, root, py, pyw, cmd, log, i, ok
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
py = root & "\.venv\Scripts\python.exe"
pyw = root & "\.venv\Scripts\pythonw.exe"
log = root & "\logs\launcher.log"
If Not fso.FolderExists(root & "\logs") Then fso.CreateFolder(root & "\logs")

If Not fso.FileExists(py) Then
  cmd = "cmd /c py -3 -m venv " & Q(root & "\.venv")
  RunHidden cmd, True
End If

If Not fso.FileExists(py) Then
  cmd = "cmd /c python -m venv " & Q(root & "\.venv")
  RunHidden cmd, True
End If

If Not fso.FileExists(py) Then
  LogLine "Python could not create .venv"
  MsgBox "CineReco could not create its local environment. Check logs\launcher.log.",16,"CineReco"
  WScript.Quit 1
End If

ok = RunHidden(Q(py) & " -c ""import flask""", True)
If ok <> 0 Then
  If RunHidden(Q(py) & " -m pip install --disable-pip-version-check --no-input --prefer-binary --only-binary=:all: --timeout 30 --retries 1 -r " & Q(root & "\requirements.txt"), True) <> 0 Then
    LogLine "Flask installation failed."
    MsgBox "CineReco could not install its lightweight Flask runtime. Check logs\launcher.log.",16,"CineReco"
    WScript.Quit 1
  End If
End If

If Not fso.FileExists(pyw) Then pyw = py
RunHidden Q(pyw) & " " & Q(root & "\server.pyw"), False

For i = 1 To 30
  If HealthOK() Then
    sh.Run "http://127.0.0.1:5083/", 1, False
    WScript.Quit 0
  End If
  WScript.Sleep 1000
Next
LogLine "Server did not become healthy on port 5083."
MsgBox "CineReco could not start. Check logs\server.log.",16,"CineReco"
WScript.Quit 1

Function Q(s): Q = """" & Replace(s,"""","""""") & """": End Function
Sub LogLine(s)
  Dim t:Set t=fso.OpenTextFile(log,8,True):t.WriteLine Now & " " & s:t.Close
End Sub
Function RunHidden(c,wait)
  RunHidden = sh.Run(c,0,wait)
End Function
Function HealthOK()
  Dim x
  On Error Resume Next
  Set x=CreateObject("MSXML2.XMLHTTP")
  x.Open "GET","http://127.0.0.1:5083/health",False
  x.Send
  HealthOK=(Err.Number=0 And x.Status=200)
  Err.Clear
End Function
