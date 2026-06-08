from __future__ import annotations

import logging
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import requests

from .models import Paper

logger = logging.getLogger(__name__)

SECTION_RE = re.compile(r"\\(?:section|subsection)\*?\{([^{}]+)\}", re.IGNORECASE)
CAPTION_RE = re.compile(r"\\caption(?:\[[^\]]*\])?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", re.IGNORECASE | re.DOTALL)
INCLUDE_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}", re.IGNORECASE)

METHOD_HINTS = ("method", "approach", "model", "framework", "architecture", "pipeline", "algorithm", "proposed")
EXPERIMENT_HINTS = ("experiment", "evaluation", "result", "ablation", "benchmark")

URL_IN_TEX_RE = re.compile(r"https?://[^\s{}\\,)\"']+", re.IGNORECASE)
# 抽取 LaTeX 正文里的代码/数据链接，供开源情况联网核验使用。
REPO_URL_HOSTS = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "huggingface.co",
    "zenodo.org",
    "kaggle.com",
    "paperswithcode.com",
)


def enrich_papers_with_context(papers: list[Paper], max_papers: int = 8) -> None:
    for paper in papers[:max_papers]:
        if paper.source != "arxiv":
            continue
        arxiv_id = paper.paper_id.removeprefix("arxiv:")
        try:
            paper.extra_context = fetch_arxiv_context(arxiv_id)
        except Exception as exc:
            logger.debug("Context extraction failed for %s: %s", paper.paper_id, exc)
            paper.extra_context = {}


def fetch_arxiv_context(arxiv_id: str) -> dict[str, Any]:
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as temp_dir:
        tar_path = Path(temp_dir) / f"{arxiv_id}.tar.gz"
        tar_path.write_bytes(response.content)
        extract_dir = Path(temp_dir) / "src"
        extract_dir.mkdir()
        with tarfile.open(tar_path, "r:gz") as tar:
            members = [m for m in tar.getmembers() if _safe_member(m)]
            tar.extractall(extract_dir, members=members)
        tex_files = sorted(extract_dir.rglob("*.tex"), key=lambda p: p.stat().st_size, reverse=True)
        tex_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in tex_files[:4])
        image_files = [p.relative_to(extract_dir).as_posix() for p in extract_dir.rglob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg"}]
        return {
            "method_context": _extract_sections(tex_text, METHOD_HINTS, max_chars=3500),
            "experiment_context": _extract_sections(tex_text, EXPERIMENT_HINTS, max_chars=1800),
            "figure_candidates": _figure_candidates(tex_text, image_files)[:12],
            "source_image_files": image_files[:30],
            "repo_url_candidates": _extract_repo_urls(tex_text)[:10],
        }


def _extract_repo_urls(tex: str) -> list[str]:
    urls: list[str] = []
    for raw in URL_IN_TEX_RE.findall(tex or ""):
        url = raw.rstrip(".,;:)]}")
        host_match = re.match(r"https?://([^/]+)", url, re.IGNORECASE)
        host = host_match.group(1).lower() if host_match else ""
        if not any(repo_host in host for repo_host in REPO_URL_HOSTS):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def _safe_member(member: tarfile.TarInfo) -> bool:
    return not (member.name.startswith("/") or ".." in member.name or member.issym() or member.islnk())


def _extract_sections(tex: str, hints: tuple[str, ...], max_chars: int) -> str:
    matches = list(SECTION_RE.finditer(tex))
    chunks: list[str] = []
    for idx, match in enumerate(matches):
        title = _strip_tex(match.group(1))
        if not any(hint in title.lower() for hint in hints):
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(tex), start + max_chars)
        body = _strip_tex(tex[start:end])
        chunks.append(f"{title}\n{body[:max_chars]}")
    return "\n\n".join(chunks)[:max_chars]


def _figure_candidates(tex: str, image_files: list[str]) -> list[dict[str, str]]:
    captions = [_strip_tex(caption) for caption in CAPTION_RE.findall(tex)]
    includes = [item.strip() for item in INCLUDE_GRAPHICS_RE.findall(tex)]
    candidates: list[dict[str, str]] = []
    for idx, include in enumerate(includes):
        caption = captions[idx] if idx < len(captions) else ""
        candidates.append({"file": include, "caption": caption, "reason_hint": _image_reason_hint(include, caption)})
    for image in image_files:
        if not any(image in candidate["file"] or candidate["file"] in image for candidate in candidates):
            candidates.append({"file": image, "caption": "", "reason_hint": _image_reason_hint(image, "")})
    return candidates


def _image_reason_hint(filename: str, caption: str) -> str:
    text = f"{filename} {caption}".lower()
    if any(word in text for word in ("arch", "architecture", "framework", "pipeline", "overview", "method", "model", "system")):
        return "likely_method_or_architecture"
    if any(word in text for word in ("result", "ablation", "benchmark", "performance", "accuracy", "table")):
        return "likely_experiment_result"
    return "unknown"


def _strip_tex(text: str) -> str:
    text = re.sub(r"%.*", "", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    text = re.sub(r"[{}$]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
