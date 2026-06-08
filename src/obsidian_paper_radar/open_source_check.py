"""开源情况联网核验模块。

DeepSeek 对“代码/数据集是否开源”的判断只基于摘要和 LaTeX 文本，容易把“作者承诺开源”
误判成“已开源”，也可能漏掉摘要里没提但正文给了链接的仓库。本模块从论文文本中抽取候选
代码/数据集链接，再真正联网访问核验是否存活（GitHub 走 API 拿星标/归档状态），然后用核验
到的事实回填 ``RerankResult`` 的 ``code_availability`` / ``dataset_availability`` 和可复现信号。

核验结果会写入 ``paper.extra_context["open_source"]``，既能喂回 DeepSeek prompt，也能在 rerank
之后纠正结构化字段。所有网络访问都带磁盘缓存与退避重试，单篇失败不影响整体运行。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .config import ROOT
from .models import Paper, RerankResult
from .net_cache import JsonDiskCache, request_with_retry

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]}|]+", re.IGNORECASE)
GITHUB_REPO_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.IGNORECASE)
GITHUB_API = "https://api.github.com/repos/"

CODE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "sourceforge.net")
DATASET_HOSTS = (
    "huggingface.co",
    "zenodo.org",
    "kaggle.com",
    "figshare.com",
    "paperswithcode.com",
    "drive.google.com",
)
# 这些 host 上的链接不是开源代码/数据，直接忽略，避免噪声。
IGNORED_HOSTS = ("arxiv.org", "doi.org", "openreview.net", "aclanthology.org", "creativecommons.org")
DEFAULT_MAX_PER_TYPE = 3


def verify_papers_open_source(papers: list[Paper], cfg: dict[str, Any]) -> None:
    """对候选论文逐篇联网核验开源情况，结果写入 ``paper.extra_context['open_source']``。"""

    if not cfg.get("enabled", True):
        return
    max_papers = int(cfg.get("max_papers", 8))
    cache = _build_cache(cfg)
    for paper in papers[:max_papers]:
        try:
            paper.extra_context["open_source"] = verify_paper_open_source(paper, cfg, cache)
        except Exception as exc:  # 单篇核验失败不应中断整轮运行
            logger.debug("开源核验失败 %s: %s", paper.paper_id, exc)


def verify_paper_open_source(paper: Paper, cfg: dict[str, Any], cache: JsonDiskCache | None) -> dict[str, Any]:
    code_urls, dataset_urls = extract_candidate_urls(paper)
    max_per_type = int(cfg.get("max_links_per_type", DEFAULT_MAX_PER_TYPE))
    use_github_api = bool(cfg.get("github_api", True))

    verified_code: list[dict[str, Any]] = []
    for url in code_urls[:max_per_type]:
        info = _verify_code_url(url, cfg, cache, use_github_api)
        if info.get("alive"):
            verified_code.append(info)

    verified_dataset: list[str] = []
    for url in dataset_urls[:max_per_type]:
        if _verify_plain_url(url, cfg, cache):
            verified_dataset.append(url)

    return {
        "checked": True,
        "code_links": code_urls,
        "dataset_links": dataset_urls,
        "verified_code": verified_code,
        "verified_dataset": verified_dataset,
        "has_verified_code": bool(verified_code),
        "has_verified_dataset": bool(verified_dataset),
        "code_link_unverified": bool(code_urls) and not verified_code,
    }


def extract_candidate_urls(paper: Paper) -> tuple[list[str], list[str]]:
    """从摘要、LaTeX 上下文和 comments 中抽取候选代码/数据集链接。"""

    text = _collect_text(paper)
    code: list[str] = []
    dataset: list[str] = []
    for raw in URL_RE.findall(text):
        url = _normalize_url(raw)
        if not url:
            continue
        host = _host(url)
        if any(host == h or host.endswith("." + h) for h in IGNORED_HOSTS):
            continue
        if any(h in host for h in CODE_HOSTS):
            normalized = _normalize_github_url(url) or url
            if normalized not in code:
                code.append(normalized)
        elif any(h in host for h in DATASET_HOSTS):
            if url not in dataset:
                dataset.append(url)
    return code, dataset


def reconcile_results(results: list[RerankResult], papers: list[Paper]) -> None:
    """用核验到的事实回填 rerank 结果（就地修改）。"""

    paper_map = {paper.paper_id: paper for paper in papers}
    for result in results:
        paper = paper_map.get(result.paper_id)
        if paper is not None:
            reconcile_result(result, paper)


def reconcile_result(result: RerankResult, paper: Paper) -> RerankResult:
    evidence = paper.extra_context.get("open_source") if isinstance(paper.extra_context, dict) else None
    if not isinstance(evidence, dict) or not evidence.get("checked"):
        return result

    signals = list(result.reproducibility_signals)
    if evidence.get("has_verified_code"):
        result.code_availability = "available"
        for repo in evidence.get("verified_code", []):
            url = str(repo.get("url", "")).strip()
            if not url:
                continue
            stars = repo.get("stars")
            star_text = f"（{stars}★）" if isinstance(stars, int) else ""
            archived_text = "，仓库已归档" if repo.get("archived") else ""
            signals.insert(0, f"已联网核验：代码仓库 {url}{star_text}{archived_text}")
    elif evidence.get("code_link_unverified"):
        first = (evidence.get("code_links") or [""])[0]
        signals.append(f"检测到代码链接但联网未访问成功，需手动确认：{first}")

    if evidence.get("has_verified_dataset"):
        result.dataset_availability = "available"
        for url in evidence.get("verified_dataset", []):
            signals.insert(0, f"已联网核验：数据/模型链接 {url}")

    result.reproducibility_signals = list(dict.fromkeys(signals))
    return result


def _verify_code_url(url: str, cfg: dict[str, Any], cache: JsonDiskCache | None, use_github_api: bool) -> dict[str, Any]:
    repo = _github_owner_repo(url)
    if repo and use_github_api:
        info = _verify_github_repo(repo, cfg, cache)
        if info is not None:
            return info
    alive = _verify_plain_url(url, cfg, cache)
    return {"url": url, "alive": alive, "source": "http"}


def _verify_github_repo(owner_repo: str, cfg: dict[str, Any], cache: JsonDiskCache | None) -> dict[str, Any] | None:
    api_url = f"{GITHUB_API}{owner_repo}"
    cache_key = f"github:{owner_repo.lower()}"
    cached = cache.get(cache_key) if cache else None
    if isinstance(cached, dict):
        return cached

    headers = {"User-Agent": "obsidian-paper-radar/0.2", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = request_with_retry(
            "GET",
            api_url,
            headers=headers,
            timeout=int(cfg.get("request_timeout_seconds", 15)),
            max_retries=int(cfg.get("max_retries", 2)),
            backoff_base=float(cfg.get("backoff_seconds", 2)),
        )
    except Exception as exc:
        logger.debug("GitHub API 访问失败 %s: %s", owner_repo, exc)
        return None

    status = response.status_code
    if status == 200:
        data = response.json() if callable(getattr(response, "json", None)) else {}
        info = {
            "url": f"https://github.com/{owner_repo}",
            "alive": True,
            "source": "github",
            "stars": int(data.get("stargazers_count") or 0),
            "archived": bool(data.get("archived")),
            "pushed_at": str(data.get("pushed_at") or ""),
        }
        if cache:
            cache.set(cache_key, info)
        return info
    if status == 404:
        info = {"url": f"https://github.com/{owner_repo}", "alive": False, "source": "github"}
        if cache:
            cache.set(cache_key, info)
        return info
    # 403/限流等：交回上层用普通 HTTP 探测，不写缓存。
    logger.debug("GitHub API 返回 %s（%s），回退普通探测", status, owner_repo)
    return None


def _verify_plain_url(url: str, cfg: dict[str, Any], cache: JsonDiskCache | None) -> bool:
    cache_key = f"alive:{url.lower()}"
    cached = cache.get(cache_key) if cache else None
    if isinstance(cached, bool):
        return cached
    timeout = int(cfg.get("request_timeout_seconds", 15))
    max_retries = int(cfg.get("max_retries", 2))
    backoff = float(cfg.get("backoff_seconds", 2))
    alive = False
    try:
        response = request_with_retry("HEAD", url, timeout=timeout, max_retries=max_retries, backoff_base=backoff)
        if response.status_code in (403, 405) or response.status_code >= 400:
            # 不少站点不支持 HEAD，用 GET 再确认一次。
            response = request_with_retry("GET", url, timeout=timeout, max_retries=max_retries, backoff_base=backoff)
        alive = response.status_code < 400
    except Exception as exc:
        logger.debug("链接探测失败 %s: %s", url, exc)
        alive = False
    if cache:
        cache.set(cache_key, alive)
    return alive


def _collect_text(paper: Paper) -> str:
    parts: list[str] = [paper.abstract or ""]
    ctx = paper.extra_context if isinstance(paper.extra_context, dict) else {}
    for key in ("method_context", "experiment_context", "comment", "comments"):
        value = ctx.get(key)
        if isinstance(value, str):
            parts.append(value)
    for candidate in ctx.get("repo_url_candidates", []) or []:
        parts.append(str(candidate))
    for figure in ctx.get("figure_candidates", []) or []:
        if isinstance(figure, dict):
            parts.append(str(figure.get("caption", "")))
    return "\n".join(parts)


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip().rstrip(".,;:")
    # 去掉常见的结尾包裹字符
    url = url.rstrip(")]}>\"'")
    return url


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _github_owner_repo(url: str) -> str | None:
    match = GITHUB_REPO_RE.search(url)
    if not match:
        return None
    owner = match.group(1)
    repo = re.sub(r"\.git$", "", match.group(2))
    if not owner or not repo or repo.lower() in {"blob", "tree", "raw"}:
        return None
    return f"{owner}/{repo}"


def _normalize_github_url(url: str) -> str | None:
    owner_repo = _github_owner_repo(url)
    return f"https://github.com/{owner_repo}" if owner_repo else None


def _build_cache(cfg: dict[str, Any]) -> JsonDiskCache | None:
    cache_cfg = cfg.get("cache", {}) if isinstance(cfg.get("cache"), dict) else {}
    if not cache_cfg.get("enabled", True):
        return None
    root_raw = str(cache_cfg.get("dir") or "data/cache/open_source").strip()
    root = Path(root_raw)
    if not root.is_absolute():
        root = ROOT / root
    return JsonDiskCache(root, ttl_days=int(cache_cfg.get("ttl_days", 14)))
