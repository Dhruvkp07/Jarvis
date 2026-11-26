[CmdletBinding()]
param([switch]$Test, [switch]$Build, [switch]$NativeSmoke)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Invoke-Checked {
    param([string]$Program, [string[]]$ProgramArgs)
    & $Program @ProgramArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw "Install 64-bit Python 3.12 with the Windows Python Launcher, then run this script again."
    }
    Invoke-Checked py @("-3.12", "-m", "venv", ".venv")
}

$JarvisPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Invoke-Checked $JarvisPython @("-m", "pip", "install", ".")
if ($Test) {
    Invoke-Checked $JarvisPython @("-m", "unittest", "discover", "-v")
}
if ($NativeSmoke) {
    Invoke-Checked $JarvisPython @("-m", "tests.windows_smoke")
}
if ($Build) {
    Invoke-Checked $JarvisPython @("-m", "pip", "install", "pyinstaller>=6.13,<7")
    Invoke-Checked $JarvisPython @("-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--onedir", "--name", "Jarvis",
        "--collect-all", "playwright", "--collect-submodules", "pywinauto",
        "--hidden-import", "win32cred", "--hidden-import", "comtypes.stream",
        "jarvis\__main__.py")
    Write-Host "Packaged executable: dist\Jarvis\Jarvis.exe"
}
Write-Host "Launch Jarvis with .\launch.cmd"
