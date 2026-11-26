@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run setup.ps1 in PowerShell first.
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m jarvis
