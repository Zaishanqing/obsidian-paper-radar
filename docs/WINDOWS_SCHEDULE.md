# Windows 定时任务

本项目推荐用 Windows 任务计划程序每天静默运行论文日报流程。注册脚本会让任务调用 `wscript.exe`，由 `scripts/run_daily_hidden.vbs` 隐藏启动 `scripts/run_daily.ps1`；后者会自动切到仓库根目录，优先使用 `.venv`，并把调度日志写入 `logs/scheduler_YYYY-MM-DD.log`。

> 下列命令在 **PowerShell** 中运行。

## 1. 先手动验证

在仓库根目录运行：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe --dry-run --limit 10 --no-images
```

确认能正常输出日报预览后，再注册任务。

## 2. 一键注册

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30
```

常用变体：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 21:00
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -TaskName obsidian-paper-radar-no-images -ExtraArgs '--no-images'
```

脚本会创建或覆盖同名任务。默认任务名是 `obsidian-paper-radar-daily`。

## 3. 手动触发和检查

```powershell
Start-ScheduledTask -TaskName 'obsidian-paper-radar-daily'
Get-ScheduledTaskInfo -TaskName 'obsidian-paper-radar-daily'
```

`LastTaskResult` 为 `0` 通常表示任务执行成功。

## 4. 图形界面创建

如果要在任务计划程序里手动创建：

1. 打开“任务计划程序”。
2. 选择“创建基本任务”。
3. 触发器选择“每天”，设置运行时间。
4. 操作选择“启动程序”。
5. 程序填写：

```text
wscript.exe
```

参数填写，把 `<repo-root>` 换成仓库根目录：

```text
//B //Nologo "<repo-root>\scripts\run_daily_hidden.vbs"
```

“起始于”填写仓库根目录：

```text
<repo-root>
```

## 5. 查看日志

调度日志：

```powershell
Get-Content (Join-Path logs ("scheduler_{0}.log" -f (Get-Date -Format yyyy-MM-dd))) -Tail 30
```

应用日志：

```text
logs/daily_papers_YYYY-MM-DD.log
logs/report_YYYY-MM-DD.md
```

## 6. 什么时候会运行

- 到注册时间且电脑开着、当前用户已登录时会运行。
- 注册脚本启用了 `StartWhenAvailable`，如果错过时间，电脑恢复可用后会尝试补跑。
- 注册脚本启用了 `WakeToRun`，电脑睡眠时是否会被唤醒取决于 Windows 电源计划的“允许使用唤醒定时器”。可用下面命令查看：

```powershell
powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE
```

常见结果含义：

- 交流电源为 `启用`：插电睡眠时允许被计划任务唤醒。
- 直流电源为 `禁用`：电池供电睡眠时不会为了该任务唤醒。
- 电脑关机、当前用户未登录、任务计划程序服务不可用，任务通常不会运行。

## 7. 常见问题

- 任务没运行：先用 `Start-ScheduledTask` 手动触发，再看 scheduler 日志。
- 手动触发时弹出窗口：重新运行注册命令覆盖旧任务；当前注册脚本使用 `wscript.exe` 静默启动，不应再弹出 PowerShell 窗口。
- 找不到 Python：确认 `.venv` 已创建，或系统 Python 可用。
- 找不到命令：重新运行 `.\.venv\Scripts\python.exe -m pip install -e .`。
- Vault 没有写入：确认 `.env` 中的 `OBSIDIAN_VAULT_PATH` 指向 Vault 根目录。
- ExecutionPolicy 报错：注册命令已经带 `-ExecutionPolicy Bypass`，通常无需改全局策略。
- 删除任务：

```powershell
Unregister-ScheduledTask -TaskName 'obsidian-paper-radar-daily' -Confirm:$false
```
