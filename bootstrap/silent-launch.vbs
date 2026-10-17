' silent-launch.vbs - start Drum AI silently (no console windows)
' py -3 / python -> app\launcher.py -> pythonw service + browser/window
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)   ' bootstrap\
root = fso.GetParentFolderName(base)                      ' project root
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
