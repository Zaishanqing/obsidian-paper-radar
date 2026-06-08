# DeepSeek 与 Obsidian 配置

> 下列命令默认在 PowerShell 中运行。

## 环境变量

复制示例：

```powershell
Copy-Item .env.example .env
```

至少填写：

```env
DEEPSEEK_API_KEY=<你的 DeepSeek API Key>
OBSIDIAN_VAULT_PATH=<你的 Obsidian Vault 根目录>
```

代码会读取的常用环境变量：

| 变量 | 是否必需 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是 | DeepSeek API Key |
| `OBSIDIAN_VAULT_PATH` | 是 | Obsidian Vault 根目录 |
| `DEEPSEEK_BASE_URL` | 否 | 默认 `https://api.deepseek.com` |
| `DEEPSEEK_MODEL_FAST` / `DEEPSEEK_MODEL_PRO` | 否 | fast/pro 模型名 |
| `NETWORK_PROXY` | 否 | 一次性设置 HTTP、HTTPS、ALL 代理 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` | 否 | requests 使用的代理变量 |
| `SEMANTIC_SCHOLAR_API_KEY` | 否 | Semantic Scholar API Key |
| `GITHUB_TOKEN` | 否 | GitHub API Token，用于开源检查 |

`OBSIDIAN_DAILY_DIR`、`OBSIDIAN_PAPER_DIR` 等旧式目录变量不作为主要配置入口。输出目录请在 `config/daily_papers.yaml` 的 `output` 段里配置。

## 代理

如果 Python 请求无法使用系统代理，在 `.env` 中显式写：

```env
NETWORK_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,api.deepseek.com
```

`NETWORK_PROXY` 会同步为 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY`。`NO_PROXY` 可让 DeepSeek 直连，同时让 arXiv、OpenReview、ACL Anthology 等学术站点继续走代理。

## 本地 YAML 配置

复制示例：

```powershell
Copy-Item config\paper_profile.example.yaml config\paper_profile.yaml
Copy-Item config\daily_papers.example.yaml config\daily_papers.yaml
```

`config/paper_profile.yaml`：

| 字段 | 说明 |
| --- | --- |
| `profile_name` | 研究画像名称 |
| `language.output` | 输出语言 |
| `long_term_interests` | 长期兴趣，作为软偏好 |
| `selection_policy` | 是否允许兴趣外热门论文、可迁移模块等 |
| `current_projects` | 当前项目、优先方向和避开方向 |
| `excluded_keywords` | 排除关键词 |
| `daily_quota` | 候选、LLM、日报、精读数量 |
| `scoring_weights` | 粗排权重 |

`config/daily_papers.yaml`：

| 字段 | 说明 |
| --- | --- |
| `sources.arxiv` | arXiv 分类、回溯天数、最大结果数 |
| `sources.semantic_scholar` | Semantic Scholar 查询、缓存、限流参数 |
| `sources.openreview` / `cvf` / `pmlr` / `acl_anthology` / `dblp` | 可选会议来源 |
| `hotness` | 顶会、近期、开源等热度加分 |
| `open_source_check` | 代码、数据集、项目页检查 |
| `citation_network` | 参考文献和被引补充 |
| `fulltext_notes` | 单篇精读笔记生成 |
| `feedback` | 反馈加权 |
| `vault_links` | 自动链接 Vault 里已有笔记 |
| `moc` | 主题索引页 |
| `output` | Vault 输出目录、图片提取、单篇笔记 |
| `runtime` | 超时、重试、批量大小、日志、去重 |

`config/*.yaml` 一旦存在就优先于 `config/*.example.yaml`。修改示例文件不会影响本机运行，除非同步到本地 YAML。

## 运行

验证配置：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe --dry-run --limit 10 --no-images
```

正式运行：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe
```

指定配置文件：

```powershell
.\.venv\Scripts\obsidian-paper-radar.exe --profile path\to\paper_profile.yaml --config path\to\daily_papers.yaml
```

## Obsidian 输出

默认输出目录：

```text
每日科研论文/
  日报/
  笔记/
  周报/
  MOC/
```

日报图片会使用 Vault 相对路径：

```markdown
![[每日科研论文/笔记/06-08/中文短名/assets/fig1.png|600]]
```

单篇笔记图片使用当前笔记目录下的相对路径：

```markdown
![[assets/fig1.png]]
```

## 常见问题

| 问题 | 检查项 |
| --- | --- |
| 缺少 `.env` | 复制 `.env.example` 并填写必需字段 |
| Vault 没有写入 | 检查 `OBSIDIAN_VAULT_PATH` 是否指向 Vault 根目录 |
| DeepSeek 调用失败 | 检查 API Key、网络、余额、代理和 `NO_PROXY` |
| Semantic Scholar 429 | 配置 `SEMANTIC_SCHOLAR_API_KEY`，或调大 `sleep_between_requests_seconds` |
| 图片提取失败 | 先用 `--no-images` 排查主流程；图片提取是非致命步骤 |
| 找不到命令 | 重新运行 `.\.venv\Scripts\python.exe -m pip install -e .` |
