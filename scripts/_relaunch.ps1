# 临时包装：以真正的 PowerShell $true 调用注册脚本，恢复解锁兜底触发器
& "$PSScriptRoot\register_task.ps1" -Time 08:30 -RunWhetherLoggedOnOrNot -AddUnlockTrigger:$true
