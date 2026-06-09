from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from .models import Paper
from .net_cache import JsonDiskCache, request_with_retry
from .utils import normalize_title

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CACHE_VERSION = 2
DEFAULT_CACHE_TTL_DAYS = 14
DEFAULT_CONFERENCE_TIMEOUT = 20

OPENREVIEW_API2 = "https://api2.openreview.net/notes"
CVF_BASE = "https://openaccess.thecvf.com/"
PMLR_BASE = "https://proceedings.mlr.press/"
ACL_BASE = "https://aclanthology.org/"
ACL_GITHUB_XML_BASE = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/"
DBLP_SEARCH_API = "https://dblp.org/search/publ/api"
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_BATCH_FIELDS = "title,abstract,citationCount,influentialCitationCount,externalIds"

# 借鉴上游 conf-papers 技能：DBLP 给结构化的完整会议名录，比抓 HTML 更稳，
# 还覆盖 OpenReview/CVF/PMLR/ACL 抓取链路缺的 AAAI/MICCAI。
DBLP_VENUES = {
    "cvpr": {"toc": "conf/cvpr", "toc_name": "cvpr{year}"},
    "iccv": {"toc": "conf/iccv", "toc_name": "iccv{year}"},
    "eccv": {"toc": "conf/eccv", "toc_name": None, "venue_query": "ECCV"},
    "iclr": {"toc": "conf/iclr", "toc_name": "iclr{year}"},
    "aaai": {"toc": "conf/aaai", "toc_name": "aaai{year}"},
    "neurips": {"toc": "conf/nips", "toc_name": "neurips{year}"},
    "icml": {"toc": "conf/icml", "toc_name": "icml{year}"},
    "miccai": {"toc": "conf/miccai", "toc_name": None, "venue_query": "MICCAI"},
    "acl": {"toc": "conf/acl", "toc_name": "acl{year}"},
    "emnlp": {"toc": "conf/emnlp", "toc_name": None, "venue_query": "EMNLP"},
}
DBLP_VENUE_CATEGORIES = {
    "cvpr": ["cs.CV"],
    "iccv": ["cs.CV"],
    "eccv": ["cs.CV"],
    "iclr": ["cs.LG", "cs.AI"],
    "icml": ["cs.LG"],
    "neurips": ["cs.LG", "cs.AI", "cs.CL"],
    "aaai": ["cs.AI"],
    "miccai": ["cs.CV", "eess.IV"],
    "acl": ["cs.CL"],
    "emnlp": ["cs.CL"],
}

OPENREVIEW_VENUE_PREFIX = {
    "iclr": "ICLR.cc",
    "icml": "ICML.cc",
    "neurips": "NeurIPS.cc",
    "nips": "NeurIPS.cc",
    "aaai": "AAAI.org",
}

CVF_CONFERENCES = {"cvpr", "iccv", "eccv", "wacv"}
PMLR_CONFERENCES = {"icml", "aistats", "colt", "uai"}
PMLR_CONFERENCE_HINTS = {
    "icml": ("international conference on machine learning", "icml"),
    "aistats": ("artificial intelligence and statistics", "aistats"),
    "colt": ("conference on learning theory", "colt"),
    "uai": ("uncertainty in artificial intelligence", "uai"),
}
ACL_VOLUME_SPECS = {
    "acl": (("acl-long", "Long"), ("acl-short", "Short"), ("findings-acl", "Findings")),
    "emnlp": (("emnlp-main", "Main"), ("findings-emnlp", "Findings")),
    "naacl": (("naacl-long", "Long"), ("naacl-short", "Short"), ("findings-naacl", "Findings")),
    "coling": (("coling-main", "Main"),),
}
ACL_DEFAULT_BACKENDS = ("package", "github_xml", "dblp_semantic_scholar", "html")


def fetch_conference_candidates(
    daily: dict[str, Any], run_date: date, limit: int | None = None, profile: dict[str, Any] | None = None
) -> list[Paper]:
    sources = daily.get("sources", {})
    papers: list[Paper] = []

    openreview_cfg = sources.get("openreview", {})
    if openreview_cfg.get("enabled", False):
        logger.info("抓取顶会来源：OpenReview")
        papers.extend(fetch_openreview(openreview_cfg, run_date, limit))

    cvf_cfg = sources.get("cvf", {})
    if cvf_cfg.get("enabled", False):
        logger.info("抓取顶会来源：CVF Open Access")
        papers.extend(fetch_cvf(cvf_cfg, run_date, limit))

    pmlr_cfg = sources.get("pmlr", {})
    if pmlr_cfg.get("enabled", False):
        logger.info("抓取顶会来源：PMLR")
        papers.extend(fetch_pmlr(pmlr_cfg, run_date, limit))

    acl_cfg = sources.get("acl_anthology", {})
    if acl_cfg.get("enabled", False):
        logger.info("抓取顶会来源：ACL Anthology")
        papers.extend(fetch_acl_anthology(acl_cfg, run_date, limit))

    dblp_cfg = sources.get("dblp", {})
    if dblp_cfg.get("enabled", False):
        logger.info("抓取顶会来源：DBLP")
        papers.extend(fetch_dblp(dblp_cfg, run_date, limit, profile))

    return papers


def fetch_openreview(cfg: dict[str, Any], run_date: date, limit: int | None = None) -> list[Paper]:
    max_results = min(int(cfg.get("max_results", 120)), limit or int(cfg.get("max_results", 120)))
    per_venue_limit = min(int(cfg.get("per_venue_limit", max_results)), max_results)
    years = _resolve_years(cfg, run_date)
    conferences = _normalize_conferences(cfg.get("conferences", ["iclr", "icml", "neurips"]), OPENREVIEW_VENUE_PREFIX)
    sleep_seconds = float(cfg.get("sleep_between_requests_seconds", 0.5))
    papers: list[Paper] = []

    page_size = int(cfg.get("page_size", 1000))

    for conference in conferences:
        for year in years:
            venue_candidates = _openreview_venue_candidates(conference, year, cfg)
            if not venue_candidates:
                continue
            try:
                parsed = _cached_fetch(
                    cfg,
                    "openreview",
                    f"{conference}-{year}",
                    per_venue_limit,
                    lambda conference=conference, year=year, venue_candidates=venue_candidates: _fetch_openreview_papers(
                        conference, year, venue_candidates, per_venue_limit, page_size
                    ),
                )
                papers.extend(parsed[:per_venue_limit])
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            except Exception as exc:
                logger.warning("OpenReview fetch failed for %s %s: %s", conference.upper(), year, exc)
            if len(papers) >= max_results:
                return papers[:max_results]
    return papers[:max_results]


def _fetch_openreview_papers(
    conference: str, year: int, venue_candidates: list[str], per_venue_limit: int, page_size: int
) -> list[Paper]:
    """依次尝试候选 venueid，第一个能返回 note 的胜出，从而容忍年份/会议命名差异。"""

    for venue_id in venue_candidates:
        notes = _fetch_openreview_notes(venue_id, per_venue_limit, page_size)
        if not notes:
            continue
        papers = [
            paper
            for paper in (_openreview_note_to_paper(note, conference, year, venue_id) for note in notes)
            if paper is not None
        ]
        if papers:
            return papers
    logger.warning(
        "OpenReview %s %s 所有候选 venueid 均无结果（尝试了 %s）；如命名有差异，请在 openreview.venue_id_overrides 配置 \"%s-%s\"。",
        conference.upper(),
        year,
        ", ".join(venue_candidates),
        conference,
        year,
    )
    return []


def _fetch_openreview_notes(venue_id: str, max_results: int, page_size: int) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    offset = 0
    safe_page = min(max(int(page_size or 1), 1), 1000)
    headers = {"User-Agent": "obsidian-paper-radar/0.2"}
    while len(notes) < max_results:
        params = {
            "content.venueid": venue_id,
            "limit": min(safe_page, max_results - len(notes)),
            "offset": offset,
        }
        response = requests.get(OPENREVIEW_API2, params=params, headers=headers, timeout=DEFAULT_CONFERENCE_TIMEOUT)
        response.raise_for_status()
        batch = response.json().get("notes") or []
        if not batch:
            break
        notes.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < params["limit"]:
            break
        offset += len(batch)
    return notes


def _openreview_note_to_paper(note: dict[str, Any], conference: str, year: int, venue_id: str) -> Paper | None:
    content = note.get("content") if isinstance(note.get("content"), dict) else {}
    title = _content_value(content, "title")
    abstract = _content_value(content, "abstract")
    if not title or not abstract:
        return None

    note_id = str(note.get("id") or note.get("forum") or normalize_title(title)[:40]).strip()
    forum = str(note.get("forum") or note_id).strip()
    authors_raw = _content_value(content, "authors", default=[])
    authors = [str(item).strip() for item in authors_raw if str(item).strip()] if isinstance(authors_raw, list) else []
    pdf_field = _content_value(content, "pdf")
    pdf_url = ""
    if pdf_field:
        pdf_url = str(pdf_field)
        if pdf_url.startswith("/"):
            pdf_url = f"https://openreview.net{pdf_url}"
    if not pdf_url and forum:
        pdf_url = f"https://openreview.net/pdf?id={forum}"

    return Paper(
        paper_id=f"openreview:{conference}-{year}-{note_id}",
        title=str(title).strip(),
        authors=authors,
        abstract=str(abstract).strip(),
        published=str(year),
        url=f"https://openreview.net/forum?id={forum}" if forum else "",
        pdf_url=pdf_url,
        source=f"openreview:{conference}",
        categories=[conference.upper(), venue_id],
        extra_context={
            "venue": conference.upper(),
            "venue_year": year,
            "venue_id": venue_id,
            "decision": _content_value(content, "decision") or _content_value(content, "venue"),
        },
    )


def fetch_cvf(cfg: dict[str, Any], run_date: date, limit: int | None = None) -> list[Paper]:
    max_results = min(int(cfg.get("max_results", 80)), limit or int(cfg.get("max_results", 80)))
    per_venue_limit = min(int(cfg.get("per_venue_limit", max_results)), max_results)
    conferences = _normalize_conferences(cfg.get("conferences", ["cvpr", "iccv", "eccv"]), CVF_CONFERENCES)
    years = _resolve_years(cfg, run_date)
    sleep_seconds = float(cfg.get("sleep_between_requests_seconds", 0.4))
    papers: list[Paper] = []

    for conference in conferences:
        for year in years:
            try:
                parsed = _cached_fetch(
                    cfg,
                    "cvf",
                    f"{conference}-{year}",
                    per_venue_limit,
                    lambda conference=conference, year=year: _fetch_cvf_venue(conference, year, per_venue_limit, sleep_seconds),
                )
                papers.extend(parsed[:per_venue_limit])
                if len(papers) >= max_results:
                    return papers[:max_results]
            except Exception as exc:
                logger.warning("CVF fetch failed for %s %s: %s", conference.upper(), year, exc)
    return papers[:max_results]


def _fetch_cvf_venue(conference: str, year: int, per_venue_limit: int, sleep_seconds: float) -> list[Paper]:
    papers: list[Paper] = []
    listing = _fetch_cvf_listing(conference, year)
    _warn_if_listing_empty("CVF", f"{conference.upper()} {year}", f"{CVF_BASE}{conference.upper()}{year}", len(listing))
    summaries = listing[:per_venue_limit]
    for summary in summaries:
        paper = _fetch_cvf_detail(conference, year, summary)
        if paper is not None:
            papers.append(paper)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return papers


def _fetch_cvf_listing(conference: str, year: int) -> list[dict[str, str]]:
    url = f"{CVF_BASE}{conference.upper()}{year}?day=all"
    response = requests.get(url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    response.raise_for_status()
    parser = _CVFListingParser(url)
    parser.feed(response.text)
    return parser.papers


def _fetch_cvf_detail(conference: str, year: int, summary: dict[str, str]) -> Paper | None:
    detail_url = summary.get("url", "")
    if not detail_url:
        return None
    response = requests.get(detail_url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    response.raise_for_status()
    parser = _CVFDetailParser(detail_url)
    parser.feed(response.text)
    parser.close()
    title = parser.title or summary.get("title", "")
    abstract = parser.abstract
    if not title or not abstract:
        return None
    source_id = re.sub(r"\.html?$", "", detail_url.rstrip("/").split("/")[-1])
    return Paper(
        paper_id=f"cvf:{conference}-{year}-{source_id}",
        title=title,
        authors=parser.authors,
        abstract=abstract,
        published=str(year),
        url=detail_url,
        pdf_url=parser.pdf_url,
        source=f"cvf:{conference}",
        categories=[conference.upper(), f"{conference.upper()} {year}"],
        extra_context={
            "venue": conference.upper(),
            "venue_year": year,
        },
    )


def fetch_pmlr(cfg: dict[str, Any], run_date: date, limit: int | None = None) -> list[Paper]:
    max_results = min(int(cfg.get("max_results", 100)), limit or int(cfg.get("max_results", 100)))
    per_venue_limit = min(int(cfg.get("per_venue_limit", max_results)), max_results)
    conferences = _normalize_conferences(cfg.get("conferences", ["icml", "aistats"]), PMLR_CONFERENCES)
    years = _resolve_years(cfg, run_date)
    sleep_seconds = float(cfg.get("sleep_between_requests_seconds", 0.25))
    papers: list[Paper] = []

    for conference in conferences:
        for year in years:
            try:
                parsed = _cached_fetch(
                    cfg,
                    "pmlr",
                    f"{conference}-{year}",
                    per_venue_limit,
                    lambda conference=conference, year=year: _fetch_pmlr_venue(conference, year, per_venue_limit, sleep_seconds, cfg),
                )
                papers.extend(parsed[:per_venue_limit])
                if len(papers) >= max_results:
                    return papers[:max_results]
            except Exception as exc:
                logger.warning("PMLR fetch failed for %s %s: %s", conference.upper(), year, exc)
    return papers[:max_results]


def _fetch_pmlr_venue(conference: str, year: int, per_venue_limit: int, sleep_seconds: float, cfg: dict[str, Any] | None = None) -> list[Paper]:
    volume_url = _find_pmlr_volume_url(conference, year, cfg or {})
    if not volume_url:
        logger.warning(
            "PMLR 未能定位 %s %s 的 volume；若 proceedings 索引结构有变，可在 pmlr.volume_overrides 配置 \"%s-%s\"（如 \"v235\"）。",
            conference.upper(),
            year,
            conference,
            year,
        )
        return []
    listing = _fetch_pmlr_listing(volume_url)
    _warn_if_listing_empty("PMLR", f"{conference.upper()} {year}", volume_url, len(listing))
    detail_urls = listing[:per_venue_limit]
    papers: list[Paper] = []
    for detail_url in detail_urls:
        paper = _fetch_pmlr_detail(conference, year, volume_url, detail_url)
        if paper is not None:
            papers.append(paper)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return papers


def _find_pmlr_volume_url(conference: str, year: int, cfg: dict[str, Any] | None = None) -> str:
    override = _pmlr_volume_override(conference, year, cfg or {})
    if override:
        return override
    response = requests.get(PMLR_BASE, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    response.raise_for_status()
    text = response.text
    hints = PMLR_CONFERENCE_HINTS.get(conference, (conference,))
    for match in re.finditer(r'<li[^>]*>\s*<a\s+[^>]*href=["\']?([^"\'\s>]+)["\']?[^>]*>(.*?)</a>(.*?)</li>', text, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(match.group(1))
        label = _clean_html(match.group(2) + " " + match.group(3)).lower()
        if str(year) not in label:
            continue
        if any(hint in label for hint in hints):
            return urljoin(PMLR_BASE, href)
    for match in re.finditer(r'<a\s+[^>]*href=["\']?([^"\'\s>]+)["\']?[^>]*>(.*?)</a>', text, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(match.group(1))
        label = _clean_html(match.group(2)).lower()
        if str(year) in label and any(hint in label for hint in hints):
            return urljoin(PMLR_BASE, href)
    return ""


def _pmlr_volume_override(conference: str, year: int, cfg: dict[str, Any]) -> str:
    overrides = cfg.get("volume_overrides", {}) if isinstance(cfg.get("volume_overrides"), dict) else {}
    raw = overrides.get(f"{conference.lower()}-{year}") or overrides.get(conference.lower())
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("http"):
        return value
    return urljoin(PMLR_BASE, f"{value.strip('/')}/")


def _fetch_pmlr_listing(volume_url: str) -> list[str]:
    response = requests.get(volume_url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    response.raise_for_status()
    urls: list[str] = []
    for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+\.html?)["\'][^>]*>(.*?)</a>', response.text, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(match.group(1))
        label = _clean_html(match.group(2)).lower()
        if label == "abs" or "/v" in href:
            url = urljoin(volume_url, href)
            if not url.startswith(PMLR_BASE):
                continue
            if url not in urls:
                urls.append(url)
    return urls


def _fetch_pmlr_detail(conference: str, year: int, volume_url: str, detail_url: str) -> Paper | None:
    response = requests.get(detail_url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    response.raise_for_status()
    text = response.text
    title = _meta_content(text, "citation_title") or _regex_text(text, r"<h1[^>]*>(.*?)</h1>")
    abstract = _regex_text(text, r'<div[^>]+id=["\']abstract["\'][^>]*>(.*?)</div>')
    if not abstract:
        abstract = _regex_text(text, r"<h4[^>]*>\s*Abstract\s*</h4>\s*<p[^>]*>(.*?)</p>")
    if not title or not abstract:
        return None
    authors = _meta_contents(text, "citation_author")
    if not authors:
        authors = _split_authors(_regex_text(text, r'<span[^>]+class=["\']authors["\'][^>]*>(.*?)</span>'))
    pdf_url = _meta_content(text, "citation_pdf_url")
    source_id = re.sub(r"\.html?$", "", detail_url.rstrip("/").split("/")[-1])
    return Paper(
        paper_id=f"pmlr:{conference}-{year}-{source_id}",
        title=title,
        authors=authors,
        abstract=abstract,
        published=_normalize_publication_date(_meta_content(text, "citation_publication_date")) or str(year),
        url=detail_url,
        pdf_url=pdf_url,
        source=f"pmlr:{conference}",
        categories=[conference.upper(), f"PMLR {year}"],
        extra_context={
            "venue": conference.upper(),
            "venue_year": year,
            "volume_url": volume_url,
            "has_software_link": bool(re.search(r">\s*Software\s*<|github\.com|huggingface\.co", text, re.IGNORECASE)),
        },
    )


def fetch_acl_anthology(cfg: dict[str, Any], run_date: date, limit: int | None = None) -> list[Paper]:
    max_results = min(int(cfg.get("max_results", 100)), limit or int(cfg.get("max_results", 100)))
    backends = _acl_backend_order(cfg)

    for backend in backends:
        try:
            papers = _fetch_acl_anthology_backend(cfg, run_date, max_results, backend)
        except Exception as exc:
            logger.warning("ACL Anthology backend %s failed: %s", backend, exc)
            continue
        if papers:
            logger.info("ACL Anthology backend %s returned %d papers", backend, len(papers))
            return papers[:max_results]
        logger.info("ACL Anthology backend %s returned no papers; trying next backend", backend)
    return []


def _acl_backend_order(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("backend_order", cfg.get("backends", list(ACL_DEFAULT_BACKENDS)))
    items = raw if isinstance(raw, list) else [raw]
    allowed = set(ACL_DEFAULT_BACKENDS)
    out: list[str] = []
    for item in items:
        key = str(item or "").strip().lower().replace("-", "_")
        if key == "dblp_s2":
            key = "dblp_semantic_scholar"
        if key in allowed and key not in out:
            out.append(key)
    return out or list(ACL_DEFAULT_BACKENDS)


def _fetch_acl_anthology_backend(cfg: dict[str, Any], run_date: date, max_results: int, backend: str) -> list[Paper]:
    if backend == "package":
        return _fetch_acl_with_package(cfg, run_date, max_results)
    if backend == "github_xml":
        return _fetch_acl_with_github_xml(cfg, run_date, max_results)
    if backend == "dblp_semantic_scholar":
        return _fetch_acl_with_dblp_s2(cfg, run_date, max_results)
    if backend == "html":
        return _fetch_acl_with_html(cfg, run_date, max_results)
    return []


def _fetch_acl_with_package(cfg: dict[str, Any], run_date: date, max_results: int) -> list[Paper]:
    anthology = _load_acl_anthology(cfg)
    return _fetch_acl_structured_volumes(
        cfg,
        run_date,
        max_results,
        "package",
        lambda conference, year, volume_key, label, per_volume_limit: _fetch_acl_package_volume(
            anthology, conference, year, volume_key, label, per_volume_limit
        ),
    )


def _load_acl_anthology(cfg: dict[str, Any]) -> Any:
    try:
        from acl_anthology import Anthology
    except ImportError as exc:
        raise RuntimeError("acl-anthology package is not installed") from exc

    local_datadir = str(cfg.get("package_datadir") or "").strip()
    if local_datadir:
        return Anthology(datadir=local_datadir, verbose=False)

    repo_path = str(cfg.get("package_repo_path") or cfg.get("repo_path") or "").strip()
    if repo_path:
        repo = Path(repo_path)
        if not repo.is_absolute():
            repo = ROOT / repo
        return Anthology.from_repo(path=repo, verbose=False)
    return Anthology.from_repo(verbose=False)


def _fetch_acl_package_volume(
    anthology: Any, conference: str, year: int, volume_key: str, label: str, per_volume_limit: int
) -> list[Paper]:
    collection_id, volume_id = _acl_xml_ids(conference, year, volume_key)
    volume_full_id = f"{collection_id}-{volume_id}"
    volume = None
    for getter_name in ("get_volume", "get"):
        getter = getattr(anthology, getter_name, None)
        if callable(getter):
            volume = getter(volume_full_id)
            if volume is not None:
                break
    if volume is None:
        return []

    paper_items = _acl_volume_paper_items(volume)
    papers: list[Paper] = []
    for item in paper_items[:per_volume_limit]:
        paper = _acl_package_paper_to_model(item, conference, year, volume_key, label, collection_id, volume_id)
        if paper is not None:
            papers.append(paper)
    return papers


def _acl_volume_paper_items(volume: Any) -> list[Any]:
    papers = getattr(volume, "papers", None)
    if callable(papers):
        papers = papers()
    if isinstance(papers, dict):
        return list(papers.values())
    if isinstance(papers, (list, tuple)):
        return list(papers)
    return []


def _acl_package_paper_to_model(
    item: Any, conference: str, year: int, volume_key: str, label: str, collection_id: str, volume_id: str
) -> Paper | None:
    title = _clean_text(str(getattr(item, "title", "") or ""))
    abstract = _clean_text(str(getattr(item, "abstract", "") or ""))
    if not title or not abstract:
        return None
    paper_id_raw = str(getattr(item, "full_id", "") or getattr(item, "anthology_id", "") or "").strip()
    if not paper_id_raw:
        item_id = str(getattr(item, "id", "") or "").strip()
        paper_id_raw = f"{collection_id}-{volume_id}.{item_id}" if item_id else f"{collection_id}-{volume_id}.{_short_hash(title)}"
    url = str(getattr(item, "url", "") or "").strip() or f"{ACL_BASE}{paper_id_raw}/"
    pdf_url = str(getattr(item, "pdf_url", "") or "").strip() or f"{ACL_BASE}{paper_id_raw}.pdf"
    published = _normalize_publication_date(str(getattr(item, "publication_date", "") or "")) or str(
        getattr(item, "year", "") or year
    )
    return Paper(
        paper_id=f"acl:{paper_id_raw}",
        title=title,
        authors=[name for name in (_acl_author_name(author) for author in getattr(item, "authors", []) or []) if name],
        abstract=abstract,
        published=published,
        url=url,
        pdf_url=pdf_url,
        source=f"acl:{conference}",
        categories=[conference.upper(), label, volume_key],
        extra_context={
            "venue": conference.upper(),
            "venue_year": year,
            "volume": volume_key,
            "acl_backend": "package",
        },
    )


def _acl_author_name(author: Any) -> str:
    name = getattr(author, "name", author)
    first = str(getattr(name, "first", "") or "").strip()
    last = str(getattr(name, "last", "") or "").strip()
    return _clean_text(f"{first} {last}".strip() or str(name or ""))


def _fetch_acl_with_github_xml(cfg: dict[str, Any], run_date: date, max_results: int) -> list[Paper]:
    return _fetch_acl_structured_volumes(
        cfg,
        run_date,
        max_results,
        "github_xml",
        lambda conference, year, volume_key, label, per_volume_limit: _fetch_acl_github_xml_volume(
            conference, year, volume_key, label, per_volume_limit, cfg
        ),
    )


def _fetch_acl_structured_volumes(
    cfg: dict[str, Any],
    run_date: date,
    max_results: int,
    backend: str,
    fetch_volume: Any,
) -> list[Paper]:
    per_volume_limit = min(int(cfg.get("per_volume_limit", max_results)), max_results)
    conferences = _normalize_conferences(cfg.get("conferences", ["acl", "emnlp"]), set(ACL_VOLUME_SPECS))
    years = _resolve_years(cfg, run_date)
    papers: list[Paper] = []
    cache_source = f"acl_anthology_{backend}"

    for conference in conferences:
        for year in years:
            for volume_key, label in ACL_VOLUME_SPECS.get(conference, ()):
                parsed = _cached_fetch(
                    cfg,
                    cache_source,
                    f"{conference}-{year}-{volume_key}",
                    per_volume_limit,
                    lambda conference=conference, year=year, volume_key=volume_key, label=label: fetch_volume(
                        conference, year, volume_key, label, per_volume_limit
                    ),
                )
                papers.extend(parsed[:per_volume_limit])
                if len(papers) >= max_results:
                    return papers[:max_results]
    return papers[:max_results]


def _fetch_acl_github_xml_volume(
    conference: str, year: int, volume_key: str, label: str, per_volume_limit: int, cfg: dict[str, Any] | None = None
) -> list[Paper]:
    collection_id, volume_id = _acl_xml_ids(conference, year, volume_key)
    cfg = cfg or {}
    base = str(cfg.get("github_xml_base_url") or ACL_GITHUB_XML_BASE)
    url = f"{base.rstrip('/')}/{collection_id}.xml"
    response = requests.get(url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return _parse_acl_xml_volume(response.text, conference, year, volume_key, label, collection_id, volume_id)[:per_volume_limit]


def _acl_xml_ids(conference: str, year: int, volume_key: str) -> tuple[str, str]:
    if volume_key.startswith("findings-"):
        return f"{year}.findings", volume_key.removeprefix("findings-")
    prefix = f"{conference}-"
    return f"{year}.{conference}", volume_key.removeprefix(prefix)


def _parse_acl_xml_volume(
    text: str, conference: str, year: int, volume_key: str, label: str, collection_id: str, volume_id: str
) -> list[Paper]:
    root = ET.fromstring(text)
    volume_ids = {volume_id, volume_key}
    papers: list[Paper] = []
    for volume in root.findall(".//volume"):
        raw_volume_id = str(volume.attrib.get("id") or "").strip()
        if raw_volume_id and raw_volume_id not in volume_ids:
            continue
        for paper_node in volume.findall("paper"):
            paper = _acl_xml_paper_to_model(paper_node, conference, year, volume_key, label, collection_id, raw_volume_id or volume_id)
            if paper is not None:
                papers.append(paper)
    return papers


def _acl_xml_paper_to_model(
    node: ET.Element, conference: str, year: int, volume_key: str, label: str, collection_id: str, volume_id: str
) -> Paper | None:
    title = _xml_child_text(node, "title")
    abstract = _xml_child_text(node, "abstract")
    if not title or not abstract:
        return None
    item_id = str(node.attrib.get("id") or _short_hash(title)).strip()
    anthology_id = f"{collection_id}-{volume_id}.{item_id}"
    return Paper(
        paper_id=f"acl:{anthology_id}",
        title=title,
        authors=_acl_xml_authors(node),
        abstract=abstract,
        published=str(year),
        url=f"{ACL_BASE}{anthology_id}/",
        pdf_url=f"{ACL_BASE}{anthology_id}.pdf",
        source=f"acl:{conference}",
        categories=[conference.upper(), label, volume_key],
        extra_context={
            "venue": conference.upper(),
            "venue_year": year,
            "volume": volume_key,
            "acl_backend": "github_xml",
        },
    )


def _xml_child_text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    if child is None:
        return ""
    return _clean_text(" ".join(child.itertext()))


def _acl_xml_authors(node: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in node.findall("author"):
        first = _xml_child_text(author, "first")
        last = _xml_child_text(author, "last")
        name = _xml_child_text(author, "name") or f"{first} {last}".strip()
        if name:
            authors.append(name)
    return authors


def _fetch_acl_with_dblp_s2(cfg: dict[str, Any], run_date: date, max_results: int) -> list[Paper]:
    per_venue_limit = min(int(cfg.get("per_venue_limit", cfg.get("per_volume_limit", max_results))), max_results)
    roster_limit = int(cfg.get("roster_limit", 1000))
    conferences = _normalize_conferences(cfg.get("conferences", ["acl", "emnlp"]), set(DBLP_VENUES) & set(ACL_VOLUME_SPECS))
    years = _resolve_years(cfg, run_date)
    enrich = bool(cfg.get("enrich_abstracts", True))
    keep_without_abstract = bool(cfg.get("keep_without_abstract", False))
    papers: list[Paper] = []

    for conference in conferences:
        for year in years:
            parsed = _cached_fetch(
                cfg,
                "acl_anthology_dblp_semantic_scholar",
                f"{conference}-{year}",
                per_venue_limit,
                lambda conference=conference, year=year: [
                    _acl_from_dblp_paper(paper, conference, year)
                    for paper in _fetch_dblp_venue(
                        conference,
                        year,
                        per_venue_limit,
                        roster_limit,
                        [],
                        [],
                        enrich,
                        keep_without_abstract,
                        cfg,
                    )
                ],
            )
            papers.extend(parsed[:per_venue_limit])
            if len(papers) >= max_results:
                return papers[:max_results]
    return papers[:max_results]


def _acl_from_dblp_paper(paper: Paper, conference: str, year: int) -> Paper:
    paper.source = f"acl:{conference}"
    paper.categories = [conference.upper(), f"{conference.upper()} {year}"] + DBLP_VENUE_CATEGORIES.get(conference, [])
    paper.extra_context["acl_backend"] = "dblp_semantic_scholar"
    paper.extra_context["venue"] = conference.upper()
    paper.extra_context["venue_year"] = year
    if not paper.paper_id.startswith("acl:"):
        paper.paper_id = f"acl:{paper.paper_id}"
    return paper


def _fetch_acl_with_html(cfg: dict[str, Any], run_date: date, max_results: int) -> list[Paper]:
    per_volume_limit = min(int(cfg.get("per_volume_limit", max_results)), max_results)
    conferences = _normalize_conferences(cfg.get("conferences", ["acl", "emnlp"]), set(ACL_VOLUME_SPECS))
    years = _resolve_years(cfg, run_date)
    sleep_seconds = float(cfg.get("sleep_between_requests_seconds", 0.25))
    papers: list[Paper] = []

    for conference in conferences:
        for year in years:
            for volume_key, label in ACL_VOLUME_SPECS.get(conference, ()):
                try:
                    parsed = _cached_fetch(
                        cfg,
                        "acl_anthology",
                        f"{conference}-{year}-{volume_key}",
                        per_volume_limit,
                        lambda conference=conference, year=year, volume_key=volume_key, label=label: _fetch_acl_volume(
                            conference,
                            year,
                            volume_key,
                            label,
                            per_volume_limit,
                            sleep_seconds,
                        ),
                    )
                    papers.extend(parsed[:per_volume_limit])
                    if len(papers) >= max_results:
                        return papers[:max_results]
                except Exception as exc:
                    logger.warning("ACL Anthology HTML fetch failed for %s %s %s: %s", conference.upper(), year, volume_key, exc)
    return papers[:max_results]


def _fetch_acl_volume(conference: str, year: int, volume_key: str, label: str, per_volume_limit: int, sleep_seconds: float) -> list[Paper]:
    volume_url = f"{ACL_BASE}volumes/{year}.{volume_key}/"
    response = requests.get(volume_url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    if response.status_code == 404:
        logger.info("ACL Anthology volume 尚不存在，记为空结果缓存：%s", volume_url)
        return []
    response.raise_for_status()
    all_urls = _parse_acl_volume_urls(response.text, year, volume_key)
    _warn_if_listing_empty("ACL Anthology", f"{conference.upper()} {year} {volume_key}", volume_url, len(all_urls))
    detail_urls = all_urls[:per_volume_limit]
    papers: list[Paper] = []
    for detail_url in detail_urls:
        paper = _fetch_acl_detail(conference, year, volume_key, label, detail_url)
        if paper is not None:
            papers.append(paper)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return papers


def _parse_acl_volume_urls(text: str, year: int, volume_key: str) -> list[str]:
    urls: list[str] = []
    pattern = re.compile(rf'href=["\']?(/?{year}\.{re.escape(volume_key)}\.(\d+)/?)["\']?', re.IGNORECASE)
    for match in pattern.finditer(text):
        if int(match.group(2)) <= 0:
            continue
        url = urljoin(ACL_BASE, match.group(1))
        if url not in urls:
            urls.append(url)
    return urls


def _fetch_acl_detail(conference: str, year: int, volume_key: str, label: str, detail_url: str) -> Paper | None:
    response = requests.get(detail_url, headers={"User-Agent": "obsidian-paper-radar/0.2"}, timeout=DEFAULT_CONFERENCE_TIMEOUT)
    response.raise_for_status()
    text = response.text
    title = _meta_content(text, "citation_title") or _meta_property(text, "og:title")
    abstract = _meta_property(text, "og:description")
    if not abstract:
        abstract = _regex_text(text, r'<div[^>]+class=["\'][^"\']*acl-abstract[^"\']*["\'][^>]*>(.*?)</div>')
    if not title or not abstract:
        return None
    source_id = detail_url.rstrip("/").split("/")[-1]
    return Paper(
        paper_id=f"acl:{source_id}",
        title=title,
        authors=_meta_contents(text, "citation_author"),
        abstract=abstract,
        published=_normalize_publication_date(_meta_content(text, "citation_publication_date")) or str(year),
        url=detail_url,
        pdf_url=_meta_content(text, "citation_pdf_url") or detail_url.rstrip("/") + ".pdf",
        source=f"acl:{conference}",
        categories=[conference.upper(), label, volume_key],
        extra_context={
            "venue": conference.upper(),
            "venue_year": year,
            "volume": volume_key,
        },
    )


def fetch_dblp(cfg: dict[str, Any], run_date: date, limit: int | None = None, profile: dict[str, Any] | None = None) -> list[Paper]:
    """从 DBLP 抓顶会名录，关键词预筛后用 Semantic Scholar batch 按 DOI 补摘要。

    DBLP 只给标题/作者/年份/DOI，没有摘要；摘要靠 S2 batch 端点一次补一批（比逐篇搜索省）。
    补不到摘要的论文默认丢弃（`keep_without_abstract`），避免标题党条目污染日报。
    """

    max_results = min(int(cfg.get("max_results", 60)), limit or int(cfg.get("max_results", 60)))
    per_venue_limit = min(int(cfg.get("per_venue_limit", max_results)), max_results)
    roster_limit = int(cfg.get("roster_limit", 1000))
    conferences = _normalize_conferences(cfg.get("conferences", ["aaai", "miccai"]), set(DBLP_VENUES))
    years = _resolve_years(cfg, run_date)
    sleep_seconds = float(cfg.get("sleep_between_requests_seconds", 0.5))
    enrich = bool(cfg.get("enrich_abstracts", True))
    keep_without_abstract = bool(cfg.get("keep_without_abstract", False))
    interests, excluded = _profile_keyword_sets(profile)
    papers: list[Paper] = []

    for conference in conferences:
        for year in years:
            try:
                parsed = _cached_fetch(
                    cfg,
                    "dblp",
                    f"{conference}-{year}",
                    per_venue_limit,
                    lambda conference=conference, year=year: _fetch_dblp_venue(
                        conference,
                        year,
                        per_venue_limit,
                        roster_limit,
                        interests,
                        excluded,
                        enrich,
                        keep_without_abstract,
                        cfg,
                    ),
                )
                papers.extend(parsed[:per_venue_limit])
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            except Exception as exc:
                logger.warning("DBLP fetch failed for %s %s: %s", conference.upper(), year, exc)
            if len(papers) >= max_results:
                return papers[:max_results]
    return papers[:max_results]


def _fetch_dblp_venue(
    conference: str,
    year: int,
    per_venue_limit: int,
    roster_limit: int,
    interests: list[str],
    excluded: list[str],
    enrich: bool,
    keep_without_abstract: bool,
    cfg: dict[str, Any],
) -> list[Paper]:
    roster = _dblp_roster(conference, year, roster_limit)
    _warn_if_listing_empty("DBLP", f"{conference.upper()} {year}", f"{DBLP_SEARCH_API} ({conference}{year})", len(roster))
    filtered = _dblp_prefilter(roster, interests, excluded)[:per_venue_limit]
    if enrich and filtered:
        _enrich_abstracts_via_s2(filtered, cfg)
    if not keep_without_abstract:
        filtered = [paper for paper in filtered if paper.abstract.strip()]
    return filtered


def _dblp_roster(conference: str, year: int, roster_limit: int) -> list[Paper]:
    spec = DBLP_VENUES.get(conference, {})
    queries: list[str] = []
    toc, toc_name = spec.get("toc"), spec.get("toc_name")
    if toc and toc_name:
        queries.append(f"toc:db/{toc}/{toc_name.format(year=year)}.bht:")
    if spec.get("venue_query"):
        queries.append(f"venue:{spec['venue_query']} year:{year}")
    queries.append(f"venue:{conference} year:{year}")

    seen_titles: set[str] = set()
    papers: list[Paper] = []
    for query in queries:
        hits = _dblp_query(query, roster_limit)
        for paper in _parse_dblp_hits(hits, conference, year):
            key = normalize_title(paper.title)
            if key in seen_titles:
                continue
            seen_titles.add(key)
            papers.append(paper)
        if papers:
            break  # 第一个有结果的查询即采用（toc 优先，venue:year 兜底）
    return papers[:roster_limit]


def _dblp_query(query: str, roster_limit: int) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    offset = 0
    headers = {"User-Agent": "obsidian-paper-radar/0.2"}
    while len(hits) < roster_limit:
        params = {"q": query, "format": "json", "h": min(1000, roster_limit - len(hits)), "f": offset}
        response = requests.get(DBLP_SEARCH_API, params=params, headers=headers, timeout=DEFAULT_CONFERENCE_TIMEOUT)
        response.raise_for_status()
        batch = (((response.json() or {}).get("result") or {}).get("hits") or {}).get("hit") or []
        if isinstance(batch, dict):
            batch = [batch]
        if not isinstance(batch, list) or not batch:
            break
        hits.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < params["h"]:
            break
        offset += len(batch)
    return hits


def _parse_dblp_hits(hits: list[dict[str, Any]], conference: str, year: int) -> list[Paper]:
    papers: list[Paper] = []
    for hit in hits:
        info = hit.get("info") if isinstance(hit.get("info"), dict) else {}
        title = str(info.get("title") or "").rstrip(".").strip()
        if not title:
            continue
        doi = str(info.get("doi") or "").strip()
        ee = str(info.get("ee") or "").strip()
        url = str(info.get("url") or "").strip()
        try:
            pub_year = int(info.get("year") or year)
        except (TypeError, ValueError):
            pub_year = year
        arxiv_id = _arxiv_id_from_url(ee) or _arxiv_id_from_url(url)
        paper_id = f"dblp:{doi.lower()}" if doi else f"dblp:{conference}-{year}-{_short_hash(title)}"
        papers.append(
            Paper(
                paper_id=paper_id,
                title=title,
                authors=_dblp_authors(info),
                abstract="",
                published=str(pub_year),
                url=url or ee or (f"https://doi.org/{doi}" if doi else ""),
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
                source=f"dblp:{conference}",
                categories=[conference.upper(), f"{conference.upper()} {pub_year}"] + DBLP_VENUE_CATEGORIES.get(conference, []),
                extra_context={
                    "venue": conference.upper(),
                    "venue_year": pub_year,
                    "doi": doi,
                    "dblp_url": url,
                    "arxiv_id": arxiv_id or "",
                },
            )
        )
    return papers


def _dblp_authors(info: dict[str, Any]) -> list[str]:
    raw = info.get("authors")
    if not isinstance(raw, dict):
        return []
    author = raw.get("author")
    if isinstance(author, dict):
        author = [author]
    if not isinstance(author, list):
        return []
    names: list[str] = []
    for item in author:
        name = str(item.get("text") if isinstance(item, dict) else item or "").strip()
        name = re.sub(r"\s+\d{4}$", "", name).strip()  # DBLP 同名消歧编号，如 "John Smith 0001"
        if name:
            names.append(name)
    return names


def _enrich_abstracts_via_s2(papers: list[Paper], cfg: dict[str, Any]) -> None:
    targets = [paper for paper in papers if not paper.abstract.strip() and str(paper.extra_context.get("doi") or "").strip()]
    if not targets:
        return
    by_id: dict[str, list[Paper]] = {}
    ids: list[str] = []
    for paper in targets:
        key = f"DOI:{str(paper.extra_context['doi']).strip()}"
        if key not in by_id:
            by_id[key] = []
            ids.append(key)
        by_id[key].append(paper)

    headers = {"User-Agent": "obsidian-paper-radar/0.2", "Content-Type": "application/json"}
    api_key = cfg.get("api_key") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = str(api_key)
    cache = _build_s2_batch_cache(cfg)
    timeout = int(cfg.get("request_timeout_seconds", 30))
    max_retries = int(cfg.get("max_retries", 3))
    backoff = float(cfg.get("backoff_seconds", 2))

    for start in range(0, len(ids), 500):  # S2 batch 单次上限 500
        chunk = ids[start : start + 500]
        data = _s2_batch_lookup(chunk, headers, timeout, max_retries, backoff, cache)
        for key, item in zip(chunk, data):
            if not isinstance(item, dict):
                continue
            abstract = str(item.get("abstract") or "").strip()
            external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
            arxiv_id = external.get("ArXiv")
            for paper in by_id.get(key, []):
                if abstract:
                    paper.abstract = abstract
                paper.citation_count = int(item.get("citationCount") or paper.citation_count or 0)
                paper.influential_citation_count = int(item.get("influentialCitationCount") or paper.influential_citation_count or 0)
                if arxiv_id and not paper.pdf_url:
                    paper.pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
                    paper.extra_context["arxiv_id"] = arxiv_id


def _s2_batch_lookup(
    ids: list[str], headers: dict[str, str], timeout: int, max_retries: int, backoff: float, cache: JsonDiskCache | None
) -> list[Any]:
    cache_key = "batch:" + "|".join(sorted(ids))
    if cache:
        cached = cache.get(cache_key)
        if isinstance(cached, list):
            return cached
    try:
        response = request_with_retry(
            "POST",
            S2_BATCH_URL,
            headers=headers,
            params={"fields": S2_BATCH_FIELDS},
            json={"ids": ids},
            timeout=timeout,
            max_retries=max_retries,
            backoff_base=backoff,
        )
    except Exception as exc:
        logger.warning("Semantic Scholar batch 调用失败: %s", exc)
        return []
    if response.status_code == 429:
        logger.warning("Semantic Scholar batch 被限流，跳过本批 DBLP 摘要补全。")
        return []
    try:
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("Semantic Scholar batch 响应异常: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    if cache:
        cache.set(cache_key, data)
    return data


def _build_s2_batch_cache(cfg: dict[str, Any]) -> JsonDiskCache | None:
    if not cfg.get("enrich_cache", True):
        return None
    return JsonDiskCache(ROOT / "data/cache/semantic_scholar", ttl_days=int(cfg.get("enrich_cache_ttl_days", 14)))


def _profile_keyword_sets(profile: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    profile = profile or {}
    interests: list[str] = [item.lower() for item in profile.get("long_term_interests", []) if isinstance(item, str)]
    for project in profile.get("current_projects", []):
        if isinstance(project, dict):
            interests.extend(item.lower() for item in project.get("prefer", []) if isinstance(item, str))
    excluded = [item.lower() for item in profile.get("excluded_keywords", []) if isinstance(item, str)]
    return interests, excluded


def _dblp_prefilter(papers: list[Paper], interests: list[str], excluded: list[str]) -> list[Paper]:
    if not interests and not excluded:
        return papers
    kept: list[Paper] = []
    for paper in papers:
        title = paper.title.lower()
        if any(term in title for term in excluded):
            continue
        if interests and not any(term in title for term in interests):
            continue
        kept.append(paper)
    return kept


def _arxiv_id_from_url(url: str) -> str:
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", url or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _short_hash(text: str) -> str:
    return hashlib.sha1(normalize_title(text).encode("utf-8")).hexdigest()[:12]


class _CVFListingParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.papers: list[dict[str, str]] = []
        self._href = ""
        self._text_parts: list[str] = []
        self._inside_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        href = attrs_dict.get("href", "")
        if "/html/" not in href or not href.endswith(".html"):
            return
        self._href = urljoin(self.base_url, href)
        self._text_parts = []
        self._inside_link = True

    def handle_data(self, data: str) -> None:
        if self._inside_link:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._inside_link:
            return
        title = _clean_text(" ".join(self._text_parts))
        if title and not any(item["url"] == self._href for item in self.papers):
            self.papers.append({"title": title, "url": self._href})
        self._inside_link = False
        self._href = ""
        self._text_parts = []


class _CVFDetailParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.abstract = ""
        self.authors: list[str] = []
        self.pdf_url = ""
        self._supplemental_pdf_url = ""
        self._capture: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        node_id = attrs_dict.get("id", "").lower()
        class_name = attrs_dict.get("class", "").lower()
        if tag == "div" and node_id in {"abstract", "authors"}:
            self._capture = node_id
            self._parts = []
        elif tag in {"div", "h1"} and class_name == "ptitle":
            self._capture = "title"
            self._parts = []
        elif tag == "a":
            href = attrs_dict.get("href", "")
            text_href = href.lower()
            if "content" in text_href and text_href.endswith(".pdf"):
                pdf_url = urljoin(self.base_url, href)
                if any(term in text_href for term in ("supp", "supplement", "supplemental")):
                    if not self._supplemental_pdf_url:
                        self._supplemental_pdf_url = pdf_url
                elif not self.pdf_url:
                    self.pdf_url = pdf_url

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture or tag.lower() not in {"div", "h1"}:
            return
        text = _clean_text(" ".join(self._parts))
        if self._capture == "title":
            self.title = text
        elif self._capture == "abstract":
            self.abstract = text
        elif self._capture == "authors":
            self.authors = [item.strip() for item in re.split(r"\s*,\s*|\s{2,}", text) if item.strip()]
        self._capture = None
        self._parts = []

    def close(self) -> None:
        super().close()
        if not self.pdf_url:
            self.pdf_url = self._supplemental_pdf_url


def _content_value(content: dict[str, Any], key: str, default: Any = "") -> Any:
    raw = content.get(key, default)
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value", default)
    return raw


def _resolve_years(cfg: dict[str, Any], run_date: date) -> list[int]:
    explicit = cfg.get("years")
    if isinstance(explicit, list):
        years = []
        for item in explicit:
            try:
                years.append(int(item))
            except Exception:
                continue
        if years:
            return sorted(set(years), reverse=True)
    year_end = int(cfg.get("year_end") or run_date.year)
    year_count = max(int(cfg.get("year_count", 2)), 1)
    return list(range(year_end, year_end - year_count, -1))


def _normalize_conferences(raw: Any, allowed: dict[str, str] | set[str]) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    allowed_keys = set(allowed.keys()) if isinstance(allowed, dict) else set(allowed)
    out: list[str] = []
    for item in items:
        key = str(item or "").strip().lower()
        if key in allowed_keys and key not in out:
            out.append(key)
    return out


def _openreview_venue_id(conference: str, year: int) -> str:
    prefix = OPENREVIEW_VENUE_PREFIX.get(conference.lower(), "")
    return f"{prefix}/{int(year)}/Conference" if prefix else ""


def _openreview_venue_candidates(conference: str, year: int, cfg: dict[str, Any]) -> list[str]:
    """返回按优先级排序的候选 venueid：用户覆盖优先，其次默认与常见变体。

    OpenReview 不同年份/会议的 ``venueid`` 命名时有差异（如 NeurIPS 的 Datasets &
    Benchmarks track），用户可通过 ``openreview.venue_id_overrides`` 用
    ``"{conference}-{year}"`` 或 ``"{conference}"`` 为键覆盖或追加候选。
    """

    candidates: list[str] = []

    overrides = cfg.get("venue_id_overrides", {}) if isinstance(cfg.get("venue_id_overrides"), dict) else {}
    for key in (f"{conference.lower()}-{year}", conference.lower()):
        override = overrides.get(key)
        if isinstance(override, str) and override.strip():
            candidates.append(override.strip())
        elif isinstance(override, list):
            candidates.extend(str(item).strip() for item in override if str(item).strip())

    default_id = _openreview_venue_id(conference, year)
    if default_id:
        candidates.append(default_id)
        if conference.lower() in {"neurips", "nips"}:
            prefix = OPENREVIEW_VENUE_PREFIX.get(conference.lower(), "")
            candidates.append(f"{prefix}/{int(year)}/Datasets_and_Benchmarks_Track")

    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _cached_fetch(cfg: dict[str, Any], source: str, stable_key: str, needed: int, fetcher: Any) -> list[Paper]:
    """按 ``conference-year`` 稳定键缓存，并在重抓时按 ``paper_id`` 增量合并。

    旧实现把抓取数量写进文件名（``...-n60.json``），数量一变就产生重复缓存文件且互不合并。
    这里改为每个 ``source/stable_key`` 只有一个文件，命中条件为“仍在 TTL 内且历史抓取量
    不小于本次需要量”；否则重抓并与历史结果按 ``paper_id`` 取并集，旧的 ``-n*`` 文件会被
    合并后删除。
    """

    cache_cfg = cfg.get("cache", {}) if isinstance(cfg.get("cache"), dict) else {}
    enabled = bool(cache_cfg.get("enabled", cfg.get("cache_enabled", True)))
    if not enabled:
        return list(fetcher())

    path = _cache_path(cache_cfg, source, stable_key)
    ttl_days = int(cache_cfg.get("ttl_days", cfg.get("cache_ttl_days", DEFAULT_CACHE_TTL_DAYS)))

    _merge_legacy_cache_files(path, stable_key)
    existing, fresh, requested_max = _read_cache(path, ttl_days)
    if fresh and requested_max >= needed:
        logger.info("Using %s cache: %s", source, path)
        return existing

    fetched = list(fetcher())
    merged = _merge_papers(existing, fetched)
    _write_cache(path, merged, requested_max=max(requested_max, needed))
    return merged


def _cache_path(cache_cfg: dict[str, Any], source: str, key: str) -> Path:
    root_raw = str(cache_cfg.get("dir") or "data/cache/conference_sources").strip()
    root = Path(root_raw)
    if not root.is_absolute():
        root = ROOT / root
    return root / source / f"{_safe_cache_key(key)}.json"


def _safe_cache_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._") or "items"


def _read_cache(path: Path, ttl_days: int = -1) -> tuple[list[Paper], bool, int]:
    """读取缓存，返回 (papers, 是否新鲜且在 TTL 内, 历史最大抓取量)。

    无论是否过期都会返回已有论文（供增量合并），``ttl_days < 0`` 时只看版本号不看时效。
    """

    if not path.exists():
        return [], False, 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], False, 0
    items = payload.get("papers")
    papers: list[Paper] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                try:
                    papers.append(_paper_from_dict(item))
                except Exception:
                    continue
    fetched_at = _parse_cache_time(payload.get("fetched_at"))
    fresh = int(payload.get("version") or 0) == CACHE_VERSION and fetched_at is not None
    if fresh and ttl_days >= 0 and datetime.now(timezone.utc) - fetched_at > timedelta(days=ttl_days):
        fresh = False
    requested_max = int(payload.get("requested_max") or len(papers))
    return papers, fresh, requested_max


def _parse_cache_time(value: Any) -> datetime | None:
    try:
        fetched_at = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return fetched_at


def _merge_papers(existing: list[Paper], fetched: list[Paper]) -> list[Paper]:
    merged: list[Paper] = []
    seen: set[str] = set()
    for paper in list(existing) + list(fetched):
        key = (paper.paper_id or "").lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(paper)
    return merged


def _merge_legacy_cache_files(path: Path, stable_key: str) -> None:
    """把旧的 ``{stable_key}-n*.json`` 合并进稳定文件并删除，减少重复缓存。"""

    directory = path.parent
    if not directory.exists():
        return
    safe_key = _safe_cache_key(stable_key)
    legacy_files = [item for item in directory.glob(f"{safe_key}-n*.json") if item != path]
    if not legacy_files:
        return
    base_papers, _, requested_max = _read_cache(path)
    merged = base_papers
    for legacy in legacy_files:
        legacy_papers, _, legacy_max = _read_cache(legacy)
        merged = _merge_papers(merged, legacy_papers)
        requested_max = max(requested_max, legacy_max)
        try:
            legacy.unlink()
        except OSError:
            logger.debug("无法删除旧缓存文件: %s", legacy)
    _write_cache(path, merged, requested_max=max(requested_max, len(merged)))
    logger.info("已合并 %d 个旧缓存文件到 %s", len(legacy_files), path)


def _write_cache(path: Path, papers: list[Paper], requested_max: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "requested_max": int(requested_max),
        "papers": [_paper_to_dict(paper) for paper in papers],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _warn_if_listing_empty(source: str, label: str, url: str, count: int) -> None:
    """HTTP 抓取成功但解析出 0 条时告警——通常意味着页面结构变化，解析器需调整。"""

    if count <= 0:
        logger.warning("%s %s 抓取成功但解析出 0 条，页面结构可能已变化，请检查解析器：%s", source, label, url)


def _paper_to_dict(paper: Paper) -> dict[str, Any]:
    return {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "authors": paper.authors,
        "abstract": paper.abstract,
        "published": paper.published,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "source": paper.source,
        "categories": paper.categories,
        "citation_count": paper.citation_count,
        "influential_citation_count": paper.influential_citation_count,
        "extra_context": paper.extra_context,
    }


def _paper_from_dict(item: dict[str, Any]) -> Paper:
    return Paper(
        paper_id=str(item.get("paper_id") or ""),
        title=str(item.get("title") or ""),
        authors=[str(author) for author in item.get("authors", []) if str(author).strip()],
        abstract=str(item.get("abstract") or ""),
        published=str(item.get("published") or ""),
        url=str(item.get("url") or ""),
        pdf_url=str(item.get("pdf_url") or ""),
        source=str(item.get("source") or ""),
        categories=[str(category) for category in item.get("categories", []) if str(category).strip()],
        citation_count=int(item.get("citation_count") or 0),
        influential_citation_count=int(item.get("influential_citation_count") or 0),
        extra_context=item.get("extra_context") if isinstance(item.get("extra_context"), dict) else {},
    )


def _clean_text(value: str) -> str:
    text = html.unescape(value or "")
    return re.sub(r"\s+", " ", text).strip()


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return _clean_text(text)


def _regex_text(text: str, pattern: str) -> str:
    match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
    return _clean_html(match.group(1)) if match else ""


def _meta_content(text: str, name: str) -> str:
    for tag in re.findall(r"<meta\s+[^>]*>", text or "", flags=re.IGNORECASE | re.DOTALL):
        if _attr_value(tag, "name").lower() == name.lower():
            return _clean_text(_attr_value(tag, "content"))
    return ""


def _meta_contents(text: str, name: str) -> list[str]:
    out: list[str] = []
    for tag in re.findall(r"<meta\s+[^>]*>", text or "", flags=re.IGNORECASE | re.DOTALL):
        if _attr_value(tag, "name").lower() != name.lower():
            continue
        value = _clean_text(_attr_value(tag, "content"))
        if value and value not in out:
            out.append(value)
    return out


def _meta_property(text: str, prop: str) -> str:
    for tag in re.findall(r"<meta\s+[^>]*>", text or "", flags=re.IGNORECASE | re.DOTALL):
        if _attr_value(tag, "property").lower() == prop.lower():
            return _clean_text(_attr_value(tag, "content"))
    return ""


def _attr_value(tag: str, attr: str) -> str:
    escaped = re.escape(attr)
    patterns = [
        rf'\b{escaped}\s*=\s*"([^"]*)"',
        rf"\b{escaped}\s*=\s*'([^']*)'",
        rf"\b{escaped}\s*=\s*([^\s>]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, tag or "", flags=re.IGNORECASE | re.DOTALL)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _split_authors(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"\s*,\s*|\s{2,}", value or "") if item.strip()]


def _normalize_publication_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y/%m/%d", "%Y/%m", "%Y-%m-%d", "%Y"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt).date().isoformat()
        except ValueError:
            continue
    return text
