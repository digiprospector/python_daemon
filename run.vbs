' run.vbs
' This script calls the gui.py script.
' It runs the command in the background without opening a command window.

Set WshShell = CreateObject("WScript.Shell")
command = "pythonw.exe gui.py"
WshShell.Run command
Set WshShell = Nothing