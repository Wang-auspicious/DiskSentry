$ErrorActionPreference='Stop'
$dataDir=Join-Path $env:LOCALAPPDATA 'DiskSentry'
if(Test-Path -LiteralPath $dataDir) { New-Item -ItemType File -Force -Path (Join-Path $dataDir 'stop.request') | Out-Null }
Unregister-ScheduledTask -TaskName 'DiskSentry Watchdog' -Confirm:$false -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name DiskSentry -ErrorAction SilentlyContinue
$infoPath=Join-Path $dataDir 'dashboard.json'
if(Test-Path -LiteralPath $infoPath) {
 $info=Get-Content -LiteralPath $infoPath -Raw | ConvertFrom-Json
 $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($info.pid)" -ErrorAction SilentlyContinue
 if($process.CommandLine -like ('*'+$PSScriptRoot+'\launcher.py*--serve*')) { Stop-Process -Id $info.pid }
}
Write-Output 'Stopped DiskSentry; source, file inventory and history preserved. No monitored file was modified or deleted.'
