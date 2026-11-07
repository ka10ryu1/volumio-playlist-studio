@echo off
rem Volumio Playlist Studio launcher. The PowerShell implementation is kept under scripts\windows.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start.ps1" %*
if errorlevel 1 (
  pause
  exit /b 1
)
