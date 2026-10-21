$ErrorActionPreference='Stop'
$pythonExe=(Get-Command python.exe).Source
$pythonwExe=Join-Path (Split-Path $pythonExe) 'pythonw.exe'
$appDir=$PSScriptRoot
& $pythonExe -c 'import psutil'
if ($LASTEXITCODE -ne 0) { throw 'Install psutil first: python -m pip install -r requirements.txt' }
$oldStop=Join-Path $env:LOCALAPPDATA 'DiskWatch\stop.request'
if (Test-Path -LiteralPath (Split-Path $oldStop)) { New-Item -ItemType File -Force -Path $oldStop | Out-Null }
& $pythonExe (Join-Path $appDir 'launcher.py') --install --start
if ($LASTEXITCODE -ne 0) { throw 'DiskSentry startup failed; check the local collector log.' }
$shellObject=New-Object -ComObject WScript.Shell
$desktopDir=[Environment]::GetFolderPath('Desktop')
foreach($shortcutPath in @((Join-Path $desktopDir 'DiskSentry.lnk'),(Join-Path $appDir 'DiskSentry.lnk'),(Join-Path $desktopDir '磁盘哨兵.lnk'))) {
 $link=$shellObject.CreateShortcut($shortcutPath)
 $link.TargetPath=$pythonwExe
 $link.Arguments='"'+(Join-Path $appDir 'launcher.py')+'"'
 $link.WorkingDirectory=$appDir
 $link.IconLocation="$env:SystemRoot\System32\shell32.dll,8"
 $link.Description='DiskSentry - readonly storage change observatory'
 $link.Save()
}
$xml=@"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Keep the current-user DiskSentry readonly monitor running.</Description></RegistrationInfo><Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)</UserId></LogonTrigger><TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$((Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss'))</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers><Principals><Principal id="Author"><UserId>$([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT2M</ExecutionTimeLimit><Enabled>true</Enabled></Settings><Actions Context="Author"><Exec><Command>$pythonwExe</Command><Arguments>&quot;$(Join-Path $appDir 'launcher.py')&quot; --start</Arguments><WorkingDirectory>$appDir</WorkingDirectory></Exec></Actions></Task>
"@
try { Register-ScheduledTask -TaskName 'DiskSentry Watchdog' -Xml $xml -Force | Out-Null; Write-Output 'Per-user watchdog registered.' }
catch { Write-Output 'Task registration unavailable; logon entry and in-app recovery remain active.' }
Write-Output "DiskSentry installed: $appDir"
