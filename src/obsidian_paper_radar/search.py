from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .config import ROOT
from .conference_sources import fetch_conference_candidates
from .feedback import adjustment_for_text, load_adjustments
from .models import Paper
from .net_cache import JsonDiskCache, request_with_retry
from .utils import normalize_title, parse_date

logger = logging.getLogger(__name__)

ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = "title,abstract,publicationDate,citationCount,influentialCitationCount,url,authors,externalIds"


def fetch_candidates(profile: dict[str, Any], daily: dict[str, Any], run_date: date, limit: int | None = None) -> list[Paper]:
    sources = daily.get("sources", {})
    arxiv_cfg = sources.get("arxiv", {})
    s2_cfg = sources.get("semantic_scholar", {})
    papers: list[Paper] = []

    if arxiv_cfg.get("enabled", True):
        papers.extend(_fetch_arxiv(arxiv_cfg, run_date, limit))
    if s2_cfg.get("enabled", True):
        papers.extend(_fetch_semantic_scholar(s2_cfg, profile, run_date, limit))
    papers.extend(fetch_conference_candidates(daily, run_date, limit, profile))

    unique = dedupe_papers(papers)
    feedback_adjustments, feedback_max_adjust = load_adjustments(daily.get("feedback", {}) if isinstance(daily.get("feedback"), dict) else {})
    scored = rough_score(unique, profile, run_date, daily, feedback_adjustments, feedback_max_adjust)
    scored.sort(key=lambda p: p.rough_score, reverse=True)
    candidate_limit = limit or profile.get("daily_quota", {}).get("candidate_limit", 300)
    return scored[:candidate_limit]


def _fetch_arxiv(cfg: dict[str, Any], run_date: date, limit: int | None) -> list[Paper]:
    lookback_days = int(cfg.get("lookback_days", 7))
    max_results = min(int(cfg.get("max_results", 300)), limit or int(cfg.get("max_results", 300)))
    categories = cfg.get("categories", ["cs.AI", "cs.CL", "cs.LG"])
    start = run_date - timedelta(days=lookback_days)
    query = "+OR+".join(f"cat:{cat}" for cat in categories)
    date_query = f"submittedDate:[{start.strftime('%Y%m%d')}0000+TO+{run_date.strftime('%Y%m%d')}2359]"
    url = (
        "https://export.arxiv.org/api/query?"
        f"search_query=({query})+AND+{date_query}&max_results={max_results}"
        "&sortBy=submittedDate&sortOrder=descending"
    )
    logger.info("Fetching arXiv candidates: %s", url[:180])
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return _parse_arxiv(response.text)
    except Exception as exc:
        logger.warning("arXiv fetch failed: %s", exc)
        return []


def _parse_arxiv(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        id_text = _entry_text(entry, "atom:id")
        arxiv_id = re.sub(r"v\d+$", "", id_text.rstrip("/").split("/")[-1])
        title = " ".join(_entry_text(entry, "atom:title").split())
        abstract = " ".join(_entry_text(entry, "atom:summary").split())
        authors = []
        for author in entry.findall("atom:author", ARXIV_NS):
            name = author.find("atom:name", ARXIV_NS)
            if name is not None and name.text:
                authors.append(name.text.strip())
        categories = [cat.attrib.get("term", "") for cat in entry.findall("atom:category", ARXIV_NS)]
        published = _entry_text(entry, "atom:published")[:10]
        pdf_url = ""
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
        papers.append(
            Paper(
                paper_id=f"arxiv:{arxiv_id}",
                title=title,
                authors=authors,
                abstract=abstract,
                published=published,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=pdf_url or f"https://arxiv.org/pdf/{arxiv_id}",
                source="arxiv",
                categories=[c for c in categories if c],
            )
        )
    return papers


def _entry_text(entry: ET.Element, path: str) -> str:
    elem = entry.find(path, ARXIV_NS)
    return elem.text.strip() if elem is not None and elem.text else ""


def _fetch_semantic_scholar(cfg: dict[str, Any], profile: dict[str, Any], run_date: date, limit: int | None) -> list[Paper]:
    lookback_days = int(cfg.get("lookback_days", 30))
    max_results = min(int(cfg.get("max_results", 100)), limit or int(cfg.get("max_results", 100)))
    start = run_date - timedelta(days=lookback_days)
    queries = _profile_queries(profile)[:4]
    papers: list[Paper] = []
    headers = {"User-Agent": "obsidian-paper-radar/0.2"}
    api_key = cfg.get("api_key") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = str(api_key)

    cache = _build_s2_cache(cfg)
    stop_on_rate_limit = bool(cfg.get("stop_on_rate_limit", True))
    max_retries = int(cfg.get("max_retries", 3))
    backoff = float(cfg.get("backoff_seconds", 2))
    respect_retry_after = bool(cfg.get("respect_retry_after", True))
    timeout = int(cfg.get("request_timeout_seconds", 30))
    page_limit = min(100, max_results)
    date_range = f"{start.isoformat()}:{run_date.isoformat()}"

    for query in queries:
        cache_key = f"{query}|{date_range}|{page_limit}"
        cached = cache.get(cache_key) if cache else None
        if isinstance(cached, list):
            logger.info("Using Semantic Scholar cache for %r", query)
            papers.extend(_parse_s2(cached))
            continue
        params = {
            "query": query,
            "publicationDateOrYear": date_range,
            "limit": page_limit,
            "fields": S2_FIELDS,
        }
        try:
            response = request_with_retry(
                "GET",
                S2_URL,
                headers=headers,
                params=params,
                timeout=timeout,
                max_retries=max_retries,
                backoff_base=backoff,
                respect_retry_after=respect_retry_after,
            )
        except Exception as exc:
            logger.warning("Semantic Scholar fetch failed for %r: %s", query, exc)
            continue
        if response.status_code == 429:
            logger.warning(
                "Semantic Scholar 多次重试后仍被限流；%s剩余查询。建议配置 api_key 或依赖本地缓存。",
                "跳过" if stop_on_rate_limit else "继续尝试",
            )
            if stop_on_rate_limit:
                break
            continue
        try:
            response.raise_for_status()
            data = response.json().get("data", [])
        except Exception as exc:
            logger.warning("Semantic Scholar response error for %r: %s", query, exc)
            continue
        if cache:
            cache.set(cache_key, data)
        papers.extend(_parse_s2(data))
        time.sleep(float(cfg.get("sleep_between_requests_seconds", 1)))
    return papers[:max_results]


def _build_s2_cache(cfg: dict[str, Any]) -> JsonDiskCache | None:
    cache_cfg = cfg.get("cache", {}) if isinstance(cfg.get("cache"), dict) else {}
    if not cache_cfg.get("enabled", True):
        return None
    root_raw = str(cache_cfg.get("dir") or "data/cache/semantic_scholar").strip()
    root = Path(root_raw)
    if not root.is_absolute():
        root = ROOT / root
    return JsonDiskCache(root, ttl_days=int(cache_cfg.get("ttl_days", 3)))


def _parse_s2(items: list[dict[str, Any]]) -> list[Paper]:
    papers: list[Paper] = []
    for item in items:
        title = item.get("title") or ""
        abstract = item.get("abstract") or ""
        if not title or not abstract:
            continue
        external = item.get("externalIds") or {}
        arxiv_id = external.get("ArXiv")
        paper_id = f"arxiv:{arxiv_id}" if arxiv_id else f"s2:{item.get('paperId') or normalize_title(title)[:40]}"
        authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
        papers.append(
            Paper(
                paper_id=paper_id,
                title=title,
                authors=authors,
                abstract=abstract,
                published=item.get("publicationDate") or "",
                url=item.get("url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
                source="semantic_scholar",
                citation_count=item.get("citationCount") or 0,
                influential_citation_count=item.get("influentialCitationCount") or 0,
            )
        )
    return papers


def _profile_queries(profile: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    for item in profile.get("long_term_interests", []):
        if isinstance(item, str):
            queries.append(item)
    for project in profile.get("current_projects", []):
        for key in ("prefer",):
            for item in project.get(key, []):
                if isinstance(item, str):
                    queries.append(item)
    return list(dict.fromkeys(queries)) or ["large language model", "retrieval augmented generation"]


def rough_score(
    papers: list[Paper],
    profile: dict[str, Any],
    run_date: date,
    daily: dict[str, Any] | None = None,
    feedback_adjustments: dict[str, float] | None = None,
    feedback_max_adjust: float = 3.0,
) -> list[Paper]:
    excluded = [s.lower() for s in profile.get("excluded_keywords", [])]
    interests = [s for s in profile.get("long_term_interests", []) if isinstance(s, str)]
    project_terms: list[str] = []
    for project in profile.get("current_projects", []):
        project_terms.extend([s for s in project.get("prefer", []) if isinstance(s, str)])

    for paper in papers:
        text = f"{paper.title}\n{paper.abstract}".lower()
        if any(term in text for term in excluded):
            paper.rough_score = -1
            continue
        matches = []
        score = 0.0
        for term in interests:
            if term.lower() in text:
                score += 2.0
                matches.append(term)
        for term in project_terms:
            if term.lower() in text:
                score += 2.5
                matches.append(term)
        if paper.categories:
            score += 0.5
        score += min(paper.influential_citation_count / 20, 2.0)
        score += _hotness_score(paper, daily or {}, run_date)
        if feedback_adjustments:
            score += adjustment_for_text(text, feedback_adjustments, max_adjust=feedback_max_adjust)
        parsed = parse_date(paper.published)
        if parsed:
            score += max(0, 2.0 - (datetime.combine(run_date, datetime.min.time()) - parsed).days / 14)
        paper.matched_interests = list(dict.fromkeys(matches))
        paper.rough_score = round(score, 2)
    return [p for p in papers if p.rough_score >= 0]


def _hotness_score(paper: Paper, daily: dict[str, Any], run_date: date) -> float:
    cfg = daily.get("hotness", {}) if isinstance(daily.get("hotness"), dict) else {}
    if not cfg.get("enabled", True):
        return 0.0
    score = 0.0
    source = paper.source.lower()
    venue = str(paper.extra_context.get("venue") or "").lower()
    top_sources = ("openreview:", "cvf:", "pmlr:", "acl:", "dblp:")
    if source.startswith(top_sources):
        score += float(cfg.get("top_venue_bonus", 1.5))
    top_venues = {str(item).lower() for item in cfg.get("top_venues", []) if str(item).strip()}
    if top_venues and venue in top_venues:
        score += float(cfg.get("named_venue_bonus", 0.7))
    if paper.extra_context.get("has_software_link"):
        score += float(cfg.get("software_signal_bonus", 0.8))
    parsed = parse_date(paper.published)
    if parsed:
        recent_years = max(int(cfg.get("recent_years", 2)), 0)
        if recent_years and run_date.year - parsed.year <= recent_years:
            score += float(cfg.get("recent_publication_bonus", 0.8))
    return round(score, 2)


def dedupe_papers(papers: list[Paper]) -> list[Paper]:
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Paper] = []
    for paper in papers:
        key = paper.paper_id.lower()
        title_key = normalize_title(paper.title)
        if key in seen_ids or title_key in seen_titles:
            continue
        seen_ids.add(key)
        seen_titles.add(title_key)
        unique.append(paper)
    return unique
