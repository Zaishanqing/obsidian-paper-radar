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

## 2.1 参数总览（功能 / 参数 / 是否需要管理员）

下表经在 Windows 11 上实测确认。**绝大多数功能都不需要管理员，注册时不会弹 UAC**；唯一需要管理员的是「解锁兜底触发器」。

| 功能 | 参数 | 默认 | 需要管理员 |
| --- | --- | --- | --- |
| 每日定时运行（核心，必选） | `-Time HH:mm` | `08:30` | 否 |
| 任务名 | `-TaskName <名字>` | `obsidian-paper-radar-daily` | 否 |
| 转发给 Python 的附加参数 | `-ExtraArgs '--no-images'` | 无 | 否 |
| 错过补跑 / 休眠唤醒 | 内置，无开关 | 启用 | 否 |
| 失败自动重试 | `-RetryIntervalMinutes` `-RetryCount` | 15 分钟 / 4 次 | 否 |
| 无论是否登录都运行（存密码登录） | `-RunWhetherLoggedOnOrNot` | 关 | **否** |
| 指定运行用户 | `-UserId '<电脑名\用户名>'` | 当前用户 | 否 |
| 解锁兜底触发器（工作站解锁时跑一次） | `-AddUnlockTrigger:$true` | 关 | **是 ★** |
| 解锁后延迟秒数 | `-UnlockDelaySeconds N` | 120 | —（随上一项） |

要点：

- **`-RunWhetherLoggedOnOrNot`（存密码登录）不需要管理员**：给「自己的账户」注册存密码任务，标准用户即有权限，不弹 UAC。脚本会提示输入账户密码（不是 PIN）。
- **只有 `-AddUnlockTrigger:$true` 需要管理员**：它注册的是「会话状态变化（解锁）触发器」，系统硬性要求管理员才能写入；非管理员注册会报 `0x80070005 拒绝访问`。脚本检测到这种情况会**自动请求一次 UAC 提权**，在提权后的新窗口里输入密码并查看结果。
- 注册脚本会在注册后**读回真实任务状态进行验证**，按实际 `LogonType` 和触发器如实汇报。
- 完整参数说明也可在 PowerShell 里查看：`Get-Help .\scripts\register_task.ps1 -Full`。

## 2.2 指令汇总（按功能组合复制即用）

所有命令在仓库根目录的 **PowerShell** 中运行。只有标注「需管理员」的会弹一次 UAC，其余都不会。

```powershell
# A. 基础版：每天 08:30，仅登录后运行（最常用）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30
```

```powershell
# B. 基础版 + 失败自动重试（每 15 分钟重试，最多 4 次）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RetryIntervalMinutes 15 -RetryCount 4
```

```powershell
# C. 基础版 + 无论是否登录都运行（存密码登录，锁屏/未登录也跑）。不需要管理员
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RunWhetherLoggedOnOrNot
```

```powershell
# D. 基础版 + 解锁兜底触发器（工作站解锁时跑一次）。★需管理员，会弹一次 UAC
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -AddUnlockTrigger:$true
```

```powershell
# E. 基础版 + 附加参数（如 --no-images，并用单独任务名避免覆盖默认任务）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -TaskName obsidian-paper-radar-no-images -ExtraArgs '--no-images'
```

```powershell
# F. 除「解锁触发」外全部功能：无论是否登录 + 自定义重试 + 附加参数。不需要管理员
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RunWhetherLoggedOnOrNot -RetryIntervalMinutes 15 -RetryCount 4 -ExtraArgs '--no-images'
```

```powershell
# G. 全部功能：在 F 基础上再加解锁兜底触发器。★需管理员，会弹一次 UAC
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RunWhetherLoggedOnOrNot -RetryIntervalMinutes 15 -RetryCount 4 -ExtraArgs '--no-images' -AddUnlockTrigger:$true -UnlockDelaySeconds 120
```

> 重复运行任意一条都会用 `-Force` 覆盖同名任务（默认任务名 `obsidian-paper-radar-daily`），所以改配置直接重跑对应命令即可。

## 3. 手动触发和检查

```powershell
Start-ScheduledTask -TaskName 'obsidian-paper-radar-daily'
Get-ScheduledTaskInfo -TaskName 'obsidian-paper-radar-daily'
```

`LastTaskResult` 为 `0` 通常表示任务执行成功。

## 3.1 一天只跑一次 & 当天强制重跑

主流程内置「一天只跑一次」守卫：成功跑完后会在 `data/state/daily_done_<日期>.stamp` 写一个完成标记。

- 当天再次被触发（解锁兜底触发器、手动 `Start-ScheduledTask`、失败重试时其实已成功过）会**检测到标记并直接跳过**，不会重复生成报告，且返回成功（退出码 0）。
- **失败的运行不会写标记**（例如等满网络超时就退出），所以失败重试时会真正重跑，直到成功才写标记锁定当天。
- `--dry-run` 预览既不检查也不写标记。

如果你确实想在当天**强制再跑一次**（比如白天补充了兴趣配置、想立刻重出报告），先删掉当天的完成标记，再触发：

```powershell
# 在仓库根目录运行：删除当天完成标记
Remove-Item ("data\state\daily_done_{0}.stamp" -f (Get-Date -Format yyyy-MM-dd)) -ErrorAction SilentlyContinue

# 方式一：手动触发已注册的计划任务
Start-ScheduledTask -TaskName 'obsidian-paper-radar-daily'

# 方式二：直接前台运行（能看到完整输出）
.\.venv\Scripts\obsidian-paper-radar.exe
```

> 要重跑「历史某一天」的报告，删除对应日期的标记即可，例如 `data\state\daily_done_2026-06-08.stamp`，再用 `--date 2026-06-08` 运行。

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

### 睡眠唤醒后暂时没网

普通睡眠唤醒只保证任务有机会启动，不保证 Wi-Fi/网卡已经恢复。项目默认建议两层兜底：

- `config/daily_papers.yaml` 中设置 `runtime.connectivity_max_wait_seconds: 900`，让主流程启动前最多等 15 分钟。
- 设置 `runtime.connectivity_fail_on_timeout: true`，如果等到超时仍没网，就以失败退出。
- 重新运行 `scripts/register_task.ps1` 注册任务；脚本会设置失败后每 15 分钟重试、最多 4 次。

可以按需调整重试：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RetryIntervalMinutes 15 -RetryCount 4
```

这样如果电脑在计划时间被唤醒但网络还没恢复，第一次运行会等待；仍失败时，任务计划程序会继续重试。

### 锁屏或未登录时也要运行

如果只是“已经登录但锁屏”，Windows 计划任务通常仍能运行；如果电脑重启后停在登录界面，或睡眠唤醒后登录会话不可用，默认的“仅用户已登录时运行”就不够。

需要更稳时，用下面命令注册为“无论用户是否登录都运行”：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RunWhetherLoggedOnOrNot
```

脚本会提示输入 Windows 账户密码。这里要输入账户密码，不是 Windows Hello PIN。之后任务计划程序会保存凭据；如果你修改了 Windows 密码，需要重新运行注册脚本。

> **不需要管理员**：给自己的账户注册这种「存密码登录」任务，标准用户就有权限，不会弹 UAC。

这个模式适合自动任务，但有两个注意点：

- 不要把 Vault 放在登录后才挂载的网络盘或加密盘里；未登录运行时这些路径可能不可用。
- 如果你的账户是 Microsoft 账户或域账户，`UserId` 可能需要写成对应形式，例如 `MicrosoftAccount\your@email.com` 或 `DOMAIN\name`。

### 解锁兜底触发器（可选，需要管理员）

如果你想在「每天定时」之外，再加一个「工作站解锁时也跑一次」的兜底（例如担心定时点正好错过），可以开启解锁触发器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -RunWhetherLoggedOnOrNot -AddUnlockTrigger:$true
```

- 这是**唯一需要管理员**的功能，运行时会弹一次 UAC；在提权后的新窗口里输入密码即可。
- 默认关闭。用 `-UnlockDelaySeconds N` 调整解锁后延迟多少秒触发（默认 120，给网络恢复留时间）。
- 提示：**用了 `-RunWhetherLoggedOnOrNot` 后，任务本就锁屏/未登录都能按时跑，解锁兜底通常是多余的**，按需开启即可。

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
