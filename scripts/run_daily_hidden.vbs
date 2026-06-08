Option Explicit

Dim shell, fso, scriptDir, psScript, args, i, arg, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(scriptDir, "run_daily.ps1")

args = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    args = args & " " & QuoteArg(arg)
Next

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & QuoteArg(psScript) & args

Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run(cmd, 0, True)

Function QuoteArg(value)
    QuoteArg = Chr(34) & Replace(CStr(value), Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
