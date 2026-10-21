$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
$pythonExe = (Get-Command python.exe).Source
$pythonwExe = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwExe)) { throw 'Python 3 with tkinter is required.' }
$dataDir = Join-Path $env:LOCALAPPDATA 'DiskWatch'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$configPath = Join-Path $dataDir 'settings.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    '{"threshold_mb":100,"rescan_hours":6,"retention_days":0}' | Set-Content -LiteralPath $configPath -Encoding utf8
}
$shellObject = New-Object -ComObject WScript.Shell
$desktopDir = [Environment]::GetFolderPath('Desktop')
$shortcutName = ([char]0x78c1).ToString()+([char]0x76d8)+([char]0x54e8)+([char]0x5175)+'.lnk'
$shortcut = $shellObject.CreateShortcut((Join-Path $desktopDir $shortcutName))
$shortcut.TargetPath = $pythonwExe
$shortcut.Arguments = '"' + (Join-Path $appDir 'dashboard.py') + '"'
$shortcut.WorkingDirectory = $appDir
$shortcut.Description = 'DiskWatch - daily large file changes'
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,8"
$shortcut.Save()
$localShortcut = $shellObject.CreateShortcut((Join-Path $appDir 'DiskWatch.lnk'))
$localShortcut.TargetPath = $pythonwExe
$localShortcut.Arguments = $shortcut.Arguments
$localShortcut.WorkingDirectory = $appDir
$localShortcut.Description = $shortcut.Description
$localShortcut.IconLocation = $shortcut.IconLocation
$localShortcut.Save()
$runPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-Item -Path $runPath -Force | Out-Null
$startCommand = '"' + $pythonwExe + '" "' + (Join-Path $appDir 'monitor.py') + '" --start'
New-ItemProperty -Path $runPath -Name 'DiskWatch' -Value $startCommand -PropertyType String -Force | Out-Null
Start-Process -FilePath $pythonwExe -ArgumentList ('"' + (Join-Path $appDir 'monitor.py') + '" --start') -WindowStyle Hidden
Write-Output "Installed DiskWatch. Data: $dataDir"
Write-Output "Desktop shortcut: $(Join-Path $desktopDir $shortcutName)"
