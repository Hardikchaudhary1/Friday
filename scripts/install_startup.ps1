# Adds a shortcut to your Windows Startup folder so Friday briefs you each time you log in.
# No admin rights needed. Remove it with uninstall_startup.ps1.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found at $python. Run setup first (see README)." }

$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "Friday Briefing.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $python
# The always-on assistant: briefing once a day, reminders, voice and typed commands.
$lnk.Arguments = "-m friday run --startup"
$lnk.WorkingDirectory = $root
$lnk.Description = "Friday assistant"
$lnk.WindowStyle = 7   # start minimized; open the window from the taskbar to type commands
$lnk.Save()
Write-Host "Installed: $lnkPath"
Write-Host "Friday will start at your next login: briefing once per day, then reminders and voice commands."
