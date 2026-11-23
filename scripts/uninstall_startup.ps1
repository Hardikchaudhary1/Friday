$lnkPath = Join-Path ([Environment]::GetFolderPath("Startup")) "Friday Briefing.lnk"
if (Test-Path $lnkPath) { Remove-Item $lnkPath; Write-Host "Removed $lnkPath" } else { Write-Host "Not installed." }
