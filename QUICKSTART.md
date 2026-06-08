# 快速开始

> 下列命令默认在 PowerShell 中运行。

## 1. 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## 2. 配置

```powershell
Copy-Item .env.example .env
Copy-Item config\paper_profile.example.yaml config\paper_profile.yaml
Copy-Item config\daily_papers.example.yaml config\daily_papers.yaml
```

编辑 `.env`：

```env
DEEPSEEK_API_KEY=<你的 DeepSeek API Key>
OBSIDIAN_VAULT_PATH=<你的 Obsidian Vault 根目录>
```

如需代理：

```env
NETWORK_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,api.deepseek.com
```

编辑本地 YAML：

| 文件 | 重点字段 |
| --- | --- |
| `config/paper_profile.yaml` | `long_term_interests`、`current_projects`、`excluded_keywords`、`daily_quota`、`scoring_weights` |
| `config/daily_papers.yaml` | `sources`、`output`、`fulltext_notes`、`feedback`、`vault_links`、`moc`、`runtime` |

## 3. 测试运行

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe --dry-run --limit 10 --no-images
```

能看到日报预览后，再正式写入 Vault：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe
```

## 4. 常用命令

```powershell
# 指定日期
.\.venv\Scripts\obsidian-paper-radar.exe --date 2026-06-08

# 指定配置文件
.\.venv\Scripts\obsidian-paper-radar.exe --profile path\to\paper_profile.yaml --config path\to\daily_papers.yaml

# 周报
.\.venv\Scripts\obsidian-paper-radar.exe weekly --dry-run

# 查看和记录反馈
.\.venv\Scripts\obsidian-paper-radar.exe feedback --list
.\.venv\Scripts\obsidian-paper-radar.exe feedback arxiv:2606.12345 useful
```

## 5. Windows 定时任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30
```

检查任务：

```powershell
Start-ScheduledTask -TaskName 'obsidian-paper-radar-daily'
Get-ScheduledTaskInfo -TaskName 'obsidian-paper-radar-daily'
```

详见 `docs/WINDOWS_SCHEDULE.md`。

## 6. 写作规范

项目 `config/` 下自带了默认写作规范文件，LLM 生成日报和精读笔记时会自动加载：

```text
config/
  风格-论文日报.md       日报格式与内容要求
  风格-论文精读笔记.md   精读笔记字段与深度要求
```

如果你想自定义规范，只需在自己的 Obsidian Vault 中创建 `Skills/风格-论文日报.md` 或 `Skills/风格-论文精读笔记.md`，程序会优先使用 Vault 版本（Vault `Skills/` > 项目 `config/` > 跳过）。

## 7. 本地数据

```text
data/cache/   网络请求缓存
data/state/   去重、反馈、运行历史
logs/         运行日志和健康报告
```

清理后重新运行：

```powershell
Remove-Item -Recurse -Force data\cache -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force data\state -ErrorAction SilentlyContinue
Remove-Item -Force logs\daily_papers_*.log, logs\report_*.md, logs\debug_deepseek_*.json -ErrorAction SilentlyContinue
.\.venv\Scripts\obsidian-paper-radar.exe
```
