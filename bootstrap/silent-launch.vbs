' silent-launch.vbs - start Drum AI silently (no console windows)
' Integrated package: launch dist\DrumQuill\DrumQuill.exe directly when present
' (no system Python needed). Dev machines: py/python -> app\launcher.py chain.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)   ' bootstrap\
root = fso.GetParentFolderName(base)                      ' project root
exe = root & "\dist\DrumQuill\DrumQuill.exe"
If fso.FileExists(exe) Then
    sh.Run """" & exe & """", 1, False
    WScript.Quit 0
End If
L = """" & root & "\app\launcher.py"""

rc = sh.Run("cmd /c py -3 " & L & " 2>nul", 0, True)
if rc <> 0 then
    rc = sh.Run("cmd /c python " & L & " 2>nul", 0, True)
end if
if rc <> 0 then
    rc = sh.Run("cmd /c py " & L & " 2>nul", 0, True)
end if
if rc <> 0 then
    rc = sh.Run("cmd /c pythonw " & L & " 2>nul", 0, False)
end if
