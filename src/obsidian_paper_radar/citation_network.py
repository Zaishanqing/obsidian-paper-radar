"""引用网络顺藤摸瓜：用 Semantic Scholar 给精读论文拉关键参考文献与被引论文。

只对 Top N 精读论文做（控制网络开销），结果写入 ``paper.extra_context['citation_network']``，
由 ObsidianExporter 渲染进精读笔记，帮你顺着「它站在哪些工作上」「谁在引用它」追脉络。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import ROOT
from .models import Paper
from .net_cache import JsonDiskCache, request_with_retry

logger = logging.getLogger(__name__)

S2_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper/"
S2_FIELDS = (
    "references.title,references.year,references.externalIds,references.citationCount,"
    "citations.title,citations.year,citations.externalIds,citations.citationCount"
)


def enrich_citation_network(papers: list[Paper], cfg: dict[str, Any]) -> None:
    if not cfg.get("enabled", True):
        return
    max_papers = int(cfg.get("max_papers", 3))
    max_refs = int(cfg.get("max_references", 6))
    max_cites = int(cfg.get("max_citations", 6))
    cache = _build_cache(cfg)
    timeout = int(cfg.get("request_timeout_seconds", 20))
    max_retries = int(cfg.get("max_retries", 2))
    backoff = float(cfg.get("backoff_seconds", 2))

    for paper in papers[:max_papers]:
        s2_id = _s2_id_for(paper)
        if not s2_id:
            continue
        try:
            data = _fetch_paper(s2_id, cache, timeout, max_retries, backoff)
        except Exception as exc:
            logger.debug("引用网络拉取失败 %s: %s", paper.paper_id, exc)
            continue
        references = _format_related(data.get("references"), max_refs, sort_by_citations=True)
        citations = _format_related(data.get("citations"), max_cites, sort_by_citations=True)
        if references or citations:
            paper.extra_context["citation_network"] = {"references": references, "citations": citations}


def _s2_id_for(paper: Paper) -> str:
    paper_id = paper.paper_id or ""
    if paper_id.startswith("arxiv:"):
        return f"ARXIV:{paper_id.split(':', 1)[1]}"
    arxiv_id = str(paper.extra_context.get("arxiv_id") or "").strip()
    if arxiv_id:
        return f"ARXIV:{arxiv_id}"
    doi = str(paper.extra_context.get("doi") or "").strip()
    if doi:
        return f"DOI:{doi}"
    return ""


def _fetch_paper(s2_id: str, cache: JsonDiskCache | None, timeout: int, max_retries: int, backoff: float) -> dict[str, Any]:
    cache_key = f"graph:{s2_id}"
    if cache:
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return cached
    response = request_with_retry(
        "GET",
        f"{S2_PAPER_URL}{s2_id}",
        headers={"User-Agent": "obsidian-paper-radar/0.2"},
        params={"fields": S2_FIELDS},
        timeout=timeout,
        max_retries=max_retries,
        backoff_base=backoff,
    )
    if response.status_code == 429:
        logger.warning("Semantic Scholar 引用网络被限流，跳过 %s", s2_id)
        return {}
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return {}
    if cache:
        cache.set(cache_key, data)
    return data


def _format_related(items: Any, limit: int, *, sort_by_citations: bool = False) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        arxiv = external.get("ArXiv")
        doi = external.get("DOI")
        url = f"https://arxiv.org/abs/{arxiv}" if arxiv else (f"https://doi.org/{doi}" if doi else "")
        rows.append({"title": title, "year": item.get("year"), "url": url, "citations": int(item.get("citationCount") or 0)})
    if sort_by_citations:
        rows.sort(key=lambda row: row["citations"], reverse=True)
    return rows[:limit]


def _build_cache(cfg: dict[str, Any]) -> JsonDiskCache | None:
    cache_cfg = cfg.get("cache", {}) if isinstance(cfg.get("cache"), dict) else {}
    if not cache_cfg.get("enabled", True):
        return None
    root = Path(str(cache_cfg.get("dir") or "data/cache/semantic_scholar").strip())
    if not root.is_absolute():
        root = ROOT / root
    return JsonDiskCache(root, ttl_days=int(cache_cfg.get("ttl_days", 14)))
