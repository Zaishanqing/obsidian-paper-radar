#requires -Version 5.1
<#
.SYNOPSIS
    Obsidian Paper Radar 每日论文任务的包装脚本，供 Windows 任务计划程序调用。

.DESCRIPTION
    自动定位仓库根目录、优先使用项目内的 .venv，再运行 scripts/run_daily_papers.py，
    并把本次调度的标准输出/错误追加到 logs/scheduler_YYYY-MM-DD.log。
    任意附加参数会原样转发给 run_daily_papers.py。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily.ps1

.EXAMPLE
    # 传给底层脚本的参数（例如先跳过图片做一次验证）
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily.ps1 --no-images
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ExtraArgs = @()
)

$ErrorActionPreference = 'Continue'

# 仓库根目录 = 本脚本所在目录（scripts/）的上一级
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

# 优先使用项目内的虚拟环境，否则回退到 PATH 中的 python
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = 'python'
}

# 确保日志目录存在；包装日志按天分文件
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$WrapperLog = Join-Path $LogDir ('scheduler_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

$startLine = '[{0}] start python={1} args=[{2}]' -f (Get-Date -Format 's'), $Python, ($ExtraArgs -join ' ')
Add-Content -LiteralPath $WrapperLog -Value $startLine -Encoding UTF8
Write-Host $startLine
Write-Host "详细日志写入: $WrapperLog"
Write-Host "日报流程自身日志写入: $LogDir\daily_papers_YYYY-MM-DD.log"

# 合并 stderr 到 stdout 后追加到包装日志；run_daily_papers.py 自身仍会另写 daily_papers_*.log
& $Python 'scripts\run_daily_papers.py' @ExtraArgs 2>&1 | Add-Content -LiteralPath $WrapperLog -Encoding UTF8
$Code = $LASTEXITCODE

$endLine = '[{0}] end exit={1}' -f (Get-Date -Format 's'), $Code
Add-Content -LiteralPath $WrapperLog -Value $endLine -Encoding UTF8
Write-Host $endLine

exit $Code
