# Obsidian Paper Radar

Obsidian Paper Radar 是一个本地运行的论文发现与 Obsidian 笔记生成工具。它会从 arXiv、Semantic Scholar 以及可选的会议来源抓取候选论文，结合规则、历史反馈和 DeepSeek 进行筛选，然后把日报、单篇论文笔记、图片附件、周报和主题索引写入你的 Obsidian Vault。

## 代码架构

主流程入口是 `obsidian_paper_radar.daily.run_daily()`，CLI 入口是 `obsidian_paper_radar.cli.main()`。

```text
论文来源
  -> 候选合并与缓存
  -> 规则粗排和历史反馈调整
  -> DeepSeek rerank
  -> 全文、引用、开源信息、图片等上下文补充
  -> Obsidian Markdown 输出
```

主要模块：

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 命令行入口，分发日报、周报、反馈命令 |
| `daily.py` | 日报主流程编排 |
| `config.py` | `.env`、研究画像和运行配置加载 |
| `search.py` | arXiv / Semantic Scholar 候选抓取和粗排 |
| `conference_sources.py` | OpenReview、CVF、PMLR、ACL Anthology、DBLP 等会议来源 |
| `net_cache.py` | HTTP 缓存、重试、限流退避 |
| `rerank.py` | DeepSeek 语义筛选、字段校验和规则 fallback |
| `deepseek_client.py` | DeepSeek API 调用、流式读取、超时和重试 |
| `paper_fulltext.py` | arXiv source / PDF 文本片段提取 |
| `citation_network.py` | 引用和被引信息补充 |
| `open_source_check.py` | 代码、数据集、项目页检查 |
| `image_utils.py` | PDF 图片提取、过滤、机制/架构/流程图筛选 |
| `note_generator.py` | 单篇精读笔记字段生成 |
| `obsidian_exporter.py` | 日报和单篇笔记 Markdown 输出 |
| `weekly_digest.py` | 周报生成 |
| `moc.py` | 主题 MOC 索引生成 |
| `feedback.py` | 反馈记录与下一轮粗排加权 |
| `vault_links.py` | 扫描已有 Vault 笔记并自动生成 wikilink |
| `health.py` | 运行健康报告 |

更完整的流程说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 安装

以下命令默认在 PowerShell 中运行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

开发和测试依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 配置

复制示例文件：

```powershell
Copy-Item .env.example .env
Copy-Item config\paper_profile.example.yaml config\paper_profile.yaml
Copy-Item config\daily_papers.example.yaml config\daily_papers.yaml
```

`.env` 至少需要：

```env
DEEPSEEK_API_KEY=<你的 DeepSeek API Key>
OBSIDIAN_VAULT_PATH=<你的 Obsidian Vault 根目录>
```

如果需要代理：

```env
NETWORK_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,api.deepseek.com
```

常用配置文件：

| 文件 | 作用 |
| --- | --- |
| `.env` | API Key、Vault 路径、代理、可选外部服务 Token |
| `config/paper_profile.yaml` | 研究兴趣、当前项目、排除关键词、每日配额、打分权重 |
| `config/daily_papers.yaml` | 来源开关、输出目录、图片、全文笔记、周报、MOC、缓存、运行时参数 |

`config/*.yaml` 是本机配置，存在时优先于 `config/*.example.yaml`。运行状态写入 `data/state/`，网络缓存写入 `data/cache/`，它们不会写进 Obsidian Vault。

更细的配置说明见 [docs/DEEPSEEK_OBSIDIAN_SETUP.md](docs/DEEPSEEK_OBSIDIAN_SETUP.md)。

## 运行

先做一次不写入 Vault 的验证：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe --dry-run --limit 10 --no-images
```

正式运行：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只打印预览，不写入 Vault |
| `--limit 10` | 限制候选和 LLM 处理数量，适合测试 |
| `--no-images` | 跳过 PDF 图片提取 |
| `--date YYYY-MM-DD` | 指定运行日期 |
| `--profile PATH` | 指定研究画像配置 |
| `--config PATH` | 指定运行配置 |

兼容脚本仍可使用：

```powershell
.\.venv\Scripts\python.exe scripts\run_daily_papers.py --dry-run --limit 10 --no-images
```

## 周报和反馈

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe weekly --dry-run
.\.venv\Scripts\obsidian-paper-radar.exe feedback --list
.\.venv\Scripts\obsidian-paper-radar.exe feedback arxiv:2606.12345 useful
```

日报中也会写入可勾选的反馈项。后续运行会从日报和 `data/state/feedback.json` 导入反馈，用于调整粗排分数。

## Obsidian 输出

默认输出目录由 `config/daily_papers.yaml` 的 `output` 段控制：

```text
{Vault}/每日科研论文/
  日报/
    MM-DD 论文日报.md
  笔记/
    MM-DD/
      {中文短名}/
        {中文短名}.md
        assets/
  周报/
    YYYY-Www 周报.md
  MOC/
    {主题}.md
```

图片会写入单篇笔记目录下的 `assets/`，并以 Obsidian wikilink 形式嵌入日报或精读笔记。

## Windows 自动化

先确认手动运行正常：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe --dry-run --limit 10 --no-images
```

注册每日任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30
```

详细说明见 [docs/WINDOWS_SCHEDULE.md](docs/WINDOWS_SCHEDULE.md)。

## 清理本地状态

清理缓存、去重状态和日志后重新运行：

```powershell
Remove-Item -Recurse -Force data\cache -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force data\state -ErrorAction SilentlyContinue
Remove-Item -Force logs\daily_papers_*.log, logs\report_*.md, logs\debug_deepseek_*.json -ErrorAction SilentlyContinue
.\.venv\Scripts\obsidian-paper-radar.exe
```

只删除 `data/cache/` 会重新联网抓取，但不会让已经写过笔记的论文重新进入推荐；要重置去重记录，需要删除 `data/state/seen_papers.json` 或整个 `data/state/`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```
