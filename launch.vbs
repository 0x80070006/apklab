' Lanceur d'APK Lab : démarre le serveur local sans fenêtre puis ouvre le navigateur.
Option Explicit
Dim sh, fso, base, toolsData, toolsLocal, port
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
port = "7341"

' Premier lancement : installer jadx / apktool (fenêtre visible pour suivre le téléchargement).
toolsData = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\APK Lab\tools\apktool.jar"
toolsLocal = base & "\tools\apktool.jar"
If (Not fso.FileExists(toolsData)) And (Not fso.FileExists(toolsLocal)) Then
  On Error Resume Next
  sh.Run "cmd /c title Installation des outils APK Lab & python """ & base & "\setup.py"" & timeout /t 4", 1, True
  On Error GoTo 0
End If

' Démarrer le serveur sans fenêtre (pythonw), avec repli sur python.
Dim started
started = False
On Error Resume Next
sh.Run "pythonw """ & base & "\server.py"" --no-browser", 0, False
If Err.Number = 0 Then started = True Else Err.Clear
If Not started Then
  sh.Run "python """ & base & "\server.py"" --no-browser", 0, False
  If Err.Number = 0 Then started = True Else Err.Clear
End If
On Error GoTo 0

If Not started Then
  MsgBox "Python 3 est requis pour lancer APK Lab." & vbCrLf & _
         "Installe-le depuis python.org (en cochant « Add to PATH »), puis relance APK Lab.", _
         48, "APK Lab"
  sh.Run "https://www.python.org/downloads/windows/", 1, False
  WScript.Quit 1
End If

WScript.Sleep 1500
sh.Run "http://127.0.0.1:" & port & "/", 1, False
