#requires -Version 5.1
<#
.SYNOPSIS
    一键在 Windows 任务计划程序中注册 Obsidian Paper Radar 每日论文任务。

.DESCRIPTION
    创建一个每天定时运行的计划任务，调用 scripts/run_daily.ps1。
    支持错过补跑（开机后若已过点会立即执行）、休眠唤醒，运行时长上限 2 小时。
    重复执行本脚本会用 -Force 覆盖同名任务，便于改时间。

.PARAMETER Time
    每日运行时间，24 小时制 HH:mm，默认 08:30。

.PARAMETER TaskName
    计划任务名称，默认 obsidian-paper-radar-daily。

.PARAMETER ExtraArgs
    转发给 run_daily_papers.py 的附加参数（如 --no-images）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 21:00 -ExtraArgs '--no-images'
#>
[CmdletBinding()]
param(
    [string] $Time = '08:30',
    [string] $TaskName = 'obsidian-paper-radar-daily',
    [string[]] $ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Wrapper = Join-Path $RepoRoot 'scripts\run_daily_hidden.vbs'
if (-not (Test-Path -LiteralPath $Wrapper)) {
    throw "找不到隐藏启动脚本: $Wrapper"
}

function Quote-TaskArg([string] $Value) {
    '"' + ($Value -replace '"', '""') + '"'
}

# 用 wscript.exe 静默启动 VBS，再由 VBS 以隐藏窗口运行 run_daily.ps1。
# 计划任务会把详细输出写到 logs/scheduler_YYYY-MM-DD.log，不依赖可见终端。
$taskArgs = '//B //Nologo {0}' -f (Quote-TaskArg $Wrapper)
if ($ExtraArgs.Count -gt 0) {
    $taskArgs += ' ' + (($ExtraArgs | ForEach-Object { Quote-TaskArg $_ }) -join ' ')
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $taskArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
# 当前用户、登录后运行；无需管理员，也不必存储密码。
# Register-ScheduledTask 在部分 Windows 环境不接受短用户名（如 "12294"），
# 使用 WindowsIdentity.Name 可得到 "电脑名\用户名" / "域\用户名" 形式。
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Obsidian Paper Radar 每日论文筛选并写入 Obsidian' `
    -Force | Out-Null

Write-Host "已注册计划任务 '$TaskName'，每天 $Time 运行。" -ForegroundColor Green
Write-Host "运行用户： $CurrentUser"
Write-Host "立即手动测试： Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "查看上次结果： Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "删除任务：     Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
