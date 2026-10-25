' Run a .bat in a hidden console (window style 0) without waiting, so no cmd
' window appears. The .bat is responsible for redirecting its own output to a
' log file (see start-searxng.bat / start-server.bat).
' Usage: wscript //nologo run-hidden.vbs "C:\path\to\script.bat"
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c """ & WScript.Arguments(0) & """", 0, False
