#requires -Version 5.1
<#
.SYNOPSIS
    一键在 Windows 任务计划程序中注册 Obsidian Paper Radar 每日论文任务。

.DESCRIPTION
    创建一个每天定时运行的计划任务，调用 scripts/run_daily.ps1。
    支持错过补跑（开机后若已过点会立即执行）、休眠唤醒，运行时长上限 2 小时。
    重复执行本脚本会用 -Force 覆盖同名任务，便于改时间。

    ── 功能 / 参数 / 是否需要管理员（经实测确认）─────────────────────
      功能                              参数                         需管理员
      每日定时运行（核心，必选）        -Time HH:mm                  否
      错过补跑 / 休眠唤醒               内置，无需开关               否
      失败自动重试                      -RetryIntervalMinutes
                                        -RetryCount                  否
      无论是否登录都运行（存密码登录）  -RunWhetherLoggedOnOrNot     否(*)
      指定运行用户                      -UserId                      否
      解锁兜底触发器（工作站解锁时跑）  -AddUnlockTrigger:$true      是 ★
      解锁延迟秒数                      -UnlockDelaySeconds N        —
    ──────────────────────────────────────────────────────────────────
    (*) 给"自己的账户"注册存密码任务，标准用户即可，不弹 UAC。

    ★ 唯一需要管理员的功能是"解锁兜底触发器"（会话状态变化触发器，
      系统限制必须管理员才能注册）。只有当你显式传 -AddUnlockTrigger:$true
      且当前非管理员时，脚本才会自动请求一次 UAC 提权。其余功能都不提权。

    其它设计要点（修复历史 bug）：
      - 所有触发器（每日 + 可选解锁）在同一次 Register-ScheduledTask 调用里注册，
        不再用脆弱、且需要管理员权限的 COM 二次更新。
      - 注册完成后读回真实任务状态进行验证，按真实结果汇报，绝不谎报。

.PARAMETER Time
    每日运行时间，24 小时制 HH:mm，默认 08:30。不需要管理员。

.PARAMETER TaskName
    计划任务名称，默认 obsidian-paper-radar-daily。

.PARAMETER ExtraArgs
    转发给 run_daily_papers.py 的附加参数（如 --no-images）。

.PARAMETER RetryIntervalMinutes
    任务失败后的重试间隔，默认 15 分钟。不需要管理员。

.PARAMETER RetryCount
    任务失败后的最多重试次数，默认 4 次。不需要管理员。

.PARAMETER RunWhetherLoggedOnOrNot
    注册为"无论用户是否登录都运行"（存密码登录）。会提示输入当前 Windows
    账户密码，适合锁屏/未登录时也要运行。给自己账户注册不需要管理员，不弹 UAC。

.PARAMETER UserId
    运行任务的 Windows 用户，默认当前用户。

.PARAMETER AddUnlockTrigger
    默认 $false。传 -AddUnlockTrigger:$true 时附加"工作站解锁时"触发器作为兜底，
    解锁后延迟 UnlockDelaySeconds 秒运行。★该功能需要管理员权限★，脚本会在
    非管理员时自动请求 UAC 提权。提示：用了 -RunWhetherLoggedOnOrNot 后任务
    本就锁屏/未登录都能跑，解锁兜底通常多余，按需开启即可。

.PARAMETER UnlockDelaySeconds
    解锁后延迟多少秒再触发，默认 120（2 分钟），给网络恢复留出时间。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RunWhetherLoggedOnOrNot -RetryIntervalMinutes 15 -RetryCount 4

.EXAMPLE
    # 额外开启解锁兜底（会弹一次 UAC，因为该功能需要管理员）
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -RunWhetherLoggedOnOrNot -AddUnlockTrigger:$true
#>
[CmdletBinding()]
param(
    [string] $Time = '08:30',
    [string] $TaskName = 'obsidian-paper-radar-daily',
    [string[]] $ExtraArgs = @(),
    [int] $RetryIntervalMinutes = 15,
    [int] $RetryCount = 4,
    [switch] $RunWhetherLoggedOnOrNot,
    [string] $UserId = '',
    [bool] $AddUnlockTrigger = $false,
    [int] $UnlockDelaySeconds = 120
)

$ErrorActionPreference = 'Stop'

function Test-IsAdmin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    $principal.IsInRole([System.Security.Principal.WindowsBuiltinRole]::Administrator)
}

# ---- 自动提权：唯一需要管理员的功能是"解锁兜底触发器" ----
# 实测：存密码登录(-RunWhetherLoggedOnOrNot)给自己账户注册不需要管理员；
# 而"会话状态变化(解锁)触发器"必须管理员才能注册，否则 0x80070005 拒绝访问。
# 因此只在显式开启解锁触发器且当前非管理员时才提权，其余情况一律不弹 UAC。
if ($AddUnlockTrigger -and -not (Test-IsAdmin)) {
    Write-Host "解锁兜底触发器需要管理员权限，正在请求 UAC 提权..." -ForegroundColor Yellow

    $fwd = [System.Collections.Generic.List[string]]::new()
    foreach ($kv in $PSBoundParameters.GetEnumerator()) {
        $name = $kv.Key
        $val  = $kv.Value
        if ($val -is [System.Management.Automation.SwitchParameter]) {
            if ($val.IsPresent) { $fwd.Add("-$name") }
        } elseif ($val -is [bool]) {
            $fwd.Add(("-{0}:`${1}" -f $name, $val.ToString().ToLower()))
        } elseif ($val -is [System.Array]) {
            if ($val.Count -gt 0) {
                $fwd.Add("-$name")
                foreach ($v in $val) { $fwd.Add('"' + ($v -replace '"', '`"') + '"') }
            }
        } else {
            $fwd.Add("-$name")
            $fwd.Add('"' + ([string]$val -replace '"', '`"') + '"')
        }
    }

    # 密码在提权后的窗口里现场输入，绝不放到命令行参数。
    # -NoExit 让提权窗口停留，便于查看注册与验证结果。
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', "`"$PSCommandPath`"") + $fwd
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList | Out-Null
        Write-Host "已弹出管理员窗口，请在新窗口里输入密码并查看结果。" -ForegroundColor Green
    } catch {
        Write-Host "提权被取消或失败：$_" -ForegroundColor Red
        Write-Host "请右键'以管理员身份运行 PowerShell'后重试，或去掉 -AddUnlockTrigger:`$true。" -ForegroundColor Yellow
    }
    return
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Wrapper = Join-Path $RepoRoot 'scripts\run_daily_hidden.vbs'
if (-not (Test-Path -LiteralPath $Wrapper)) {
    throw "找不到隐藏启动脚本: $Wrapper"
}

function Quote-TaskArg([string] $Value) {
    '"' + ($Value -replace '"', '""') + '"'
}

function ConvertTo-PlainText([securestring] $SecureValue) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        if ($ptr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        }
    }
}

# 构造"工作站解锁时"触发器（CIM 实例），用于和每日触发器一次性注册。
function New-UnlockTrigger {
    param([int] $DelaySeconds)
    $class = Get-CimClass -ClassName MSFT_TaskSessionStateChangeTrigger `
        -Namespace Root/Microsoft/Windows/TaskScheduler
    $t = New-CimInstance -CimClass $class -ClientOnly
    $t.Enabled = $true
    $t.StateChange = [uint16]8           # TASK_SESSION_UNLOCK
    if ($DelaySeconds -gt 0) {
        $t.Delay = ('PT{0}S' -f $DelaySeconds)
    }
    $t
}

# 用 wscript.exe 静默启动 VBS，再由 VBS 以隐藏窗口运行 run_daily.ps1。
# 计划任务会把详细输出写到 logs/scheduler_YYYY-MM-DD.log，不依赖可见终端。
$taskArgs = '//B //Nologo {0}' -f (Quote-TaskArg $Wrapper)
if ($ExtraArgs.Count -gt 0) {
    $taskArgs += ' ' + (($ExtraArgs | ForEach-Object { Quote-TaskArg $_ }) -join ' ')
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $taskArgs -WorkingDirectory $RepoRoot

# 所有触发器一次性准备好（每日 + 可选解锁兜底），单次注册全部生效。
$triggers = @(New-ScheduledTaskTrigger -Daily -At $Time)
if ($AddUnlockTrigger) {
    try {
        $triggers += (New-UnlockTrigger -DelaySeconds $UnlockDelaySeconds)
    } catch {
        Write-Warning "构造解锁触发器失败，本次将不含解锁兜底：$_"
        $AddUnlockTrigger = $false
    }
}

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances 'IgnoreNew' `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -RestartInterval (New-TimeSpan -Minutes $RetryIntervalMinutes) `
    -RestartCount $RetryCount `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Register-ScheduledTask 在部分 Windows 环境不接受短用户名（如 "12294"），
# 使用 WindowsIdentity.Name 可得到 "电脑名\用户名" / "域\用户名" 形式。
$CurrentUser = if ($UserId.Trim()) { $UserId.Trim() } else { [System.Security.Principal.WindowsIdentity]::GetCurrent().Name }

$Description = 'Obsidian Paper Radar 每日论文筛选并写入 Obsidian'

if ($RunWhetherLoggedOnOrNot) {
    $securePassword = Read-Host -AsSecureString -Prompt "请输入 Windows 账户 '$CurrentUser' 的密码（不是 PIN）"
    $plainPassword = ConvertTo-PlainText $securePassword
    try {
        # -User + -Password => Password 登录，"无论用户是否登录都运行"。
        # 解锁触发器已在 $triggers 内，随本次一并注册，无需后续 COM 更新。
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $triggers `
            -Settings $settings `
            -User $CurrentUser `
            -Password $plainPassword `
            -RunLevel Limited `
            -Description $Description `
            -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "存密码登录注册失败：$_" -ForegroundColor Red
        Write-Host "可能原因：该账户/策略不支持存密码登录，或密码错误。" -ForegroundColor Yellow
        Write-Host "回退到 Interactive 模式（仅登录后运行）重新注册..." -ForegroundColor Yellow
        $principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $triggers `
            -Settings $settings `
            -Principal $principal `
            -Description $Description `
            -Force -ErrorAction Stop | Out-Null
    } finally {
        $plainPassword = $null
    }
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force -ErrorAction Stop | Out-Null
}

# ---- 验证：读回真实任务状态，按真实结果汇报，不靠假设 ----
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

$logonTypeMap = @{
    'Password'    = '无论用户是否登录都运行（存密码登录）'
    'S4U'         = '无论用户是否登录都运行（S4U）'
    'Interactive' = '仅用户已登录时运行'
    'InteractiveOrPassword' = '登录时用交互令牌，否则用存密码'
    'None'        = '无登录信息'
}
$actualLogonRaw = [string]$task.Principal.LogonType
$LogonMode = if ($logonTypeMap.ContainsKey($actualLogonRaw)) { $logonTypeMap[$actualLogonRaw] } else { $actualLogonRaw }

$triggerClasses = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName })
$hasUnlock = $triggerClasses -contains 'MSFT_TaskSessionStateChangeTrigger'
$hasDaily  = ($triggerClasses | Where-Object { $_ -ne 'MSFT_TaskSessionStateChangeTrigger' }).Count -gt 0

if (-not $hasDaily) {
    throw "验证失败：任务 '$TaskName' 缺少每日触发器，请检查注册过程。"
}
if ($RunWhetherLoggedOnOrNot -and $actualLogonRaw -eq 'Interactive') {
    Write-Host "提示：你请求了'无论是否登录都运行'，但实际回退为 Interactive（仅登录后运行）。" -ForegroundColor Yellow
}
if ($AddUnlockTrigger) {
    $unlockInfo = if ($hasUnlock) { "解锁后 ${UnlockDelaySeconds} 秒（已验证）" } else { "请求添加但未生效，请检查" }
    if (-not $hasUnlock) { Write-Warning "解锁触发器未出现在已注册任务中。" }
} else {
    $unlockInfo = "未启用"
}

Write-Host ""
Write-Host "已注册并验证计划任务 '$TaskName'，每天 $Time 运行。" -ForegroundColor Green
Write-Host "运行用户： $CurrentUser"
Write-Host "登录模式： $LogonMode （实际 LogonType=$actualLogonRaw）"
Write-Host "解锁兜底： $unlockInfo"
Write-Host "失败重试： 每 $RetryIntervalMinutes 分钟重试，最多 $RetryCount 次"
Write-Host "触发器数： $($task.Triggers.Count)"
Write-Host "立即手动测试： Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "查看上次结果： Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "删除任务：     Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
