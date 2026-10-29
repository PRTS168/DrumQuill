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
    ' 最后兜底 pythonw 异步拉起：先同步探测它是否存在（退出码 9009 = 命令找不到），
    ' 真不存在则弹一句中文提示，避免「双击完全没反应」用户不知道该干嘛。
    rcw = sh.Run("cmd /c pythonw -c ""pass"" 2>nul", 0, True)
    if rcw = 0 then
        sh.Run "cmd /c pythonw " & L & " 2>nul", 0, False
    else
        MsgBox "未找到 Python 运行时。" & vbCrLf & vbCrLf & _
               "请先安装 Python 3.9 或更高版本（https://www.python.org/downloads/），" & _
               "安装后再双击启动。", 16, "DrumQuill"
    end if
end if
