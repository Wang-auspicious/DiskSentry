$ErrorActionPreference = 'Stop'
$dataDir = Join-Path $env:LOCALAPPDATA 'DiskWatch'
if (Test-Path -LiteralPath $dataDir) {
    New-Item -ItemType File -Force -Path (Join-Path $dataDir 'stop.request') | Out-Null
}
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'DiskWatch' -ErrorAction SilentlyContinue
$shortcutName = ([char]0x78c1).ToString()+([char]0x76d8)+([char]0x54e8)+([char]0x5175)+'.lnk'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) $shortcutName
if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
$dashboardInfoPath = Join-Path $dataDir 'dashboard.json'
if (Test-Path -LiteralPath $dashboardInfoPath) {
    $dashboardInfo = Get-Content -LiteralPath $dashboardInfoPath -Raw | ConvertFrom-Json
    $dashboardProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($dashboardInfo.pid)" -ErrorAction SilentlyContinue
    $dashboardScript = Join-Path $PSScriptRoot 'dashboard.py'
    if ($dashboardProcess -and $dashboardProcess.CommandLine -like ('*' + $dashboardScript + '*--serve*')) {
        Stop-Process -Id $dashboardInfo.pid -ErrorAction SilentlyContinue
    }
}
Write-Output 'Monitoring stopped; startup entry and desktop shortcut removed. App and history files are preserved.'
