@echo off
setlocal
where pwsh.exe >nul 2>&1
if errorlevel 1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0demo.ps1" -Interactive
) else (
    pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0demo.ps1" -Interactive
)
if errorlevel 1 echo Demo exited with an error.
pause
