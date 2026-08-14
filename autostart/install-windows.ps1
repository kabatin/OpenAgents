# ログイン時に OpenAgents を自動起動する（Windows / タスクスケジューラ）。
#
#   powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1
#   powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1 -Remove
#
# OSに登録するのは run.py 1本だけです。個々のBOTの起動・再起動は
# run.py（スーパーバイザ）が面倒を見るので、BOTを増やしてもここは変わりません。

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "OpenAgents"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "OK 自動起動を解除しました"
    exit 0
}

# 使う Python を決める（venv があれば優先）
# pythonw.exe を使うとコンソール窓が出ない
$VenvPythonw = Join-Path $Root "venv\Scripts\pythonw.exe"
$VenvPython  = Join-Path $Root "venv\Scripts\python.exe"
if (Test-Path $VenvPythonw)      { $Python = $VenvPythonw }
elseif (Test-Path $VenvPython)   { $Python = $VenvPython }
else {
    $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
}
if (-not $Python) {
    Write-Error "python が見つかりません。先に python start.py を実行してください"
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "state\logs") | Out-Null

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ('"{0}"' -f (Join-Path $Root "run.py")) `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# 落ちたら起こし直す。ノートPCでの利用を想定し、電源条件では止めない
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "OpenAgents の常駐プロセス" -Force | Out-Null

Write-Host "OK 自動起動を登録しました"
Write-Host "   タスク名 : $TaskName"
Write-Host "   Python   : $Python"
Write-Host "   ログ     : $Root\state\logs"
Write-Host ""
Write-Host "   今すぐ起動する : Start-ScheduledTask -TaskName $TaskName"
Write-Host "   状態を見る     : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "   解除する       : powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1 -Remove"
