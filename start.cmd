@echo off
rem Volumio Playlist Studio launcher. The PowerShell implementation is kept under scripts\windows.
rem PowerShell is started in its own window and this batch ends at once, so that Ctrl+C stops the app
rem without cmd.exe asking "Terminate batch job (Y/N)?". start.ps1 pauses by itself when startup fails.
rem Do not use "start /b": it disables Ctrl+C for PowerShell and the app it starts.
cd /d "%~dp0"
start "Volumio Playlist Studio" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start.ps1" %*
