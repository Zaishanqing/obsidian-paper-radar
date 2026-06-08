# 架构说明

Obsidian Paper Radar 是一个本地论文发现和 Obsidian 输出流程。它把“抓取候选、筛选排序、补充上下文、生成笔记、写入 Vault”拆成一组独立模块，并通过 `daily.py` 串联。

## 主流程

```text
CLI
  -> 加载 .env、paper_profile.yaml、daily_papers.yaml
  -> 抓取候选论文
  -> 本地缓存和去重
  -> 规则粗排
  -> DeepSeek rerank
  -> 补充全文、引用、开源和图片信息
  -> 生成日报、单篇笔记、周报、MOC
  -> 写入 Obsidian Vault
```

入口：

| 入口 | 说明 |
| --- | --- |
| `obsidian-paper-radar` | 安装后的命令行入口 |
| `scripts/run_daily_papers.py` | 兼容包装脚本，委托给包内 CLI |
| `scripts/run_daily.ps1` | Windows 任务计划包装脚本 |
| `scripts/register_task.ps1` | 注册 Windows 每日任务 |

## 数据流

1. `config.py` 读取 `.env`、`config/paper_profile.yaml`、`config/daily_papers.yaml`，并设置代理环境变量。
2. `search.py` 和 `conference_sources.py` 从 arXiv、Semantic Scholar、OpenReview、CVF、PMLR、ACL Anthology、DBLP 等来源获取候选论文。
3. `net_cache.py` 为外部请求提供磁盘缓存、重试和限流退避。
4. `search.py` 结合主题匹配、热度信号、引用和反馈权重做本地粗排。
5. `rerank.py` 调用 DeepSeek 对候选论文做语义筛选，并校验结构化字段。
6. `paper_fulltext.py`、`citation_network.py`、`open_source_check.py`、`image_utils.py` 为进入精读范围的论文补充上下文。
7. `note_generator.py` 生成单篇精读笔记字段。
8. `obsidian_exporter.py` 输出 Obsidian Markdown，`weekly_digest.py` 输出周报，`moc.py` 更新主题索引。
9. `feedback.py` 和 `dedup.py` 更新反馈、去重和运行历史。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 命令行参数解析，分发日报、周报、反馈子命令 |
| `daily.py` | 日报主流程编排，记录日志和运行摘要 |
| `config.py` | 配置加载、路径解析、代理设置 |
| `models.py` | 论文、rerank 结果、图片等核心数据结构 |
| `search.py` | arXiv / Semantic Scholar 获取、候选合并、粗排 |
| `conference_sources.py` | 会议论文来源抓取和缓存 |
| `net_cache.py` | HTTP 请求缓存、重试、退避 |
| `rerank.py` | DeepSeek rerank、结果校验、fallback |
| `deepseek_client.py` | DeepSeek API 客户端 |
| `paper_fulltext.py` | arXiv source 和 PDF 文本片段提取 |
| `paper_context.py` | 精读上下文组织 |
| `citation_network.py` | 引用和被引信息查询 |
| `open_source_check.py` | 代码、数据集、项目页可用性检查 |
| `image_utils.py` | PDF 图片提取、过滤、架构/机制/流程图筛选 |
| `note_generator.py` | 单篇精读笔记内容生成 |
| `obsidian_exporter.py` | 日报、单篇笔记和附件写入 |
| `weekly_digest.py` | 周报内容生成和写入 |
| `moc.py` | 主题索引页生成 |
| `feedback.py` | 反馈导入、记录和权重调整 |
| `dedup.py` | 已见论文、推荐历史和去重 |
| `vault_links.py` | 已有 Obsidian 笔记索引和 wikilink 插入 |
| `health.py` | 运行健康报告 |

## 配置入口

```text
.env
config/paper_profile.yaml
config/daily_papers.yaml
```

`.env` 负责运行环境：

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `OBSIDIAN_VAULT_PATH` | Obsidian Vault 根目录 |
| `DEEPSEEK_BASE_URL` | 可选，DeepSeek API 地址 |
| `DEEPSEEK_MODEL_FAST` / `DEEPSEEK_MODEL_PRO` | 可选，fast/pro 模型名 |
| `NETWORK_PROXY` / `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` | 可选，代理 |
| `SEMANTIC_SCHOLAR_API_KEY` | 可选，降低 Semantic Scholar 限流 |
| `GITHUB_TOKEN` | 可选，开源检查使用 GitHub API 时提额 |

`paper_profile.yaml` 负责你的研究画像：

| 字段 | 说明 |
| --- | --- |
| `long_term_interests` | 长期研究兴趣 |
| `current_projects` | 当前项目、偏好和避开的主题 |
| `excluded_keywords` | 排除关键词 |
| `daily_quota` | 候选数量、LLM 数量、日报和精读篇数 |
| `scoring_weights` | 本地粗排权重 |

`daily_papers.yaml` 负责运行开关：

| 字段 | 说明 |
| --- | --- |
| `sources` | arXiv、Semantic Scholar、会议来源开关和抓取参数 |
| `hotness` | 顶会、近期、开源等热度加权 |
| `open_source_check` | 代码和数据集检查 |
| `citation_network` | 引用网络补充 |
| `fulltext_notes` | 精读笔记生成 |
| `feedback` | 反馈闭环 |
| `vault_links` | 自动链接已有笔记 |
| `moc` | 主题索引页 |
| `output` | Vault 输出目录、图片和单篇笔记开关 |
| `runtime` | 超时、重试、批量大小、日志等级、去重开关 |

## 本地状态

```text
data/cache/   网络请求缓存
data/state/   seen_papers.json、feedback.json、run_history.jsonl
logs/         daily_papers_YYYY-MM-DD.log、report_YYYY-MM-DD.md
```

这些文件只保存在项目本地，不会写入 Obsidian Vault。

## Obsidian 输出

默认输出结构：

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

图片附件放在单篇笔记目录下的 `assets/`，并通过 Obsidian wikilink 嵌入：

```markdown
![[assets/fig1.png]]
```
