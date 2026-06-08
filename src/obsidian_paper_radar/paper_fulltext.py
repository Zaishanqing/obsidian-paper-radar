from __future__ import annotations

import logging
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
import requests

from .models import Paper

logger = logging.getLogger(__name__)

SECTION_RE = re.compile(r"\\(?:section|subsection)\*?\{([^{}]+)\}", re.IGNORECASE)
TEXT_HINTS = {
    "background": ("introduction", "background", "motivation", "related work"),
    "method": ("method", "approach", "model", "framework", "architecture", "pipeline", "algorithm", "proposed"),
    "experiment": ("experiment", "evaluation", "result", "ablation", "benchmark"),
    "limitation": ("limitation", "discussion", "conclusion", "future work"),
}


@dataclass
class FullTextContext:
    source: str
    sections: dict[str, str]
    full_text_chars: int = 0
    failed_reason: str = ""

    def to_prompt_dict(self, max_chars_per_section: int = 2400) -> dict[str, Any]:
        return {
            "source": self.source,
            "full_text_chars": self.full_text_chars,
            "failed_reason": self.failed_reason,
            "sections": {k: v[:max_chars_per_section] for k, v in self.sections.items() if v.strip()},
        }


def enrich_papers_with_fulltext(
    papers: list[Paper],
    *,
    enabled: bool = True,
    max_papers: int = 3,
    max_chars_per_section: int = 2400,
) -> None:
    if not enabled:
        return
    for paper in papers[: max(int(max_papers or 0), 0)]:
        try:
            context = fetch_fulltext_context(paper)
        except Exception as exc:
            logger.warning("Fulltext extraction failed for %s: %s", paper.paper_id, exc)
            context = FullTextContext(source="failed", sections={}, failed_reason=str(exc))
        paper.extra_context["fulltext_context"] = context.to_prompt_dict(max_chars_per_section=max_chars_per_section)


def fetch_fulltext_context(paper: Paper) -> FullTextContext:
    if paper.source == "arxiv" or paper.paper_id.startswith("arxiv:"):
        arxiv_id = paper.paper_id.removeprefix("arxiv:")
        try:
            return _fetch_arxiv_source_context(arxiv_id)
        except Exception as exc:
            logger.debug("arXiv source fulltext failed for %s, falling back to PDF: %s", paper.paper_id, exc)
    if paper.pdf_url:
        return _fetch_pdf_context(paper.pdf_url)
    return FullTextContext(source="none", sections={}, failed_reason="missing pdf_url")


def _fetch_arxiv_source_context(arxiv_id: str) -> FullTextContext:
    response = requests.get(f"https://arxiv.org/e-print/{arxiv_id}", timeout=90)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as temp_dir:
        tar_path = Path(temp_dir) / f"{arxiv_id}.tar.gz"
        tar_path.write_bytes(response.content)
        extract_dir = Path(temp_dir) / "src"
        extract_dir.mkdir()
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(extract_dir, members=[m for m in tar.getmembers() if _safe_member(m)])
        tex_files = sorted(extract_dir.rglob("*.tex"), key=lambda p: p.stat().st_size, reverse=True)
        tex = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in tex_files[:6])
        sections = _sections_from_tex(tex)
        return FullTextContext(source="arxiv_source", sections=sections, full_text_chars=len(_strip_tex(tex)))


def _fetch_pdf_context(pdf_url: str) -> FullTextContext:
    response = requests.get(pdf_url, timeout=120)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(response.content)
    try:
        doc = fitz.open(tmp_path)
        pages = [page.get_text("text") for page in doc]
        doc.close()
    finally:
        tmp_path.unlink(missing_ok=True)
    text = _clean_pdf_text("\n".join(pages))
    return FullTextContext(source="pdf_text", sections=_sections_from_plain_text(text), full_text_chars=len(text))


def _sections_from_tex(tex: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(tex))
    buckets: dict[str, list[str]] = {key: [] for key in TEXT_HINTS}
    if not matches:
        return {"body": _strip_tex(tex)[:18_000]}
    for idx, match in enumerate(matches):
        title = _strip_tex(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(tex)
        body = _strip_tex(tex[start:end])
        key = _section_bucket(title)
        if key:
            buckets[key].append(f"{title}\n{body}")
    return {key: "\n\n".join(parts)[:18_000] for key, parts in buckets.items() if parts}


def _sections_from_plain_text(text: str) -> dict[str, str]:
    chunks = re.split(r"\n(?=(?:\d+(?:\.\d+)*\s+)?[A-Z][A-Za-z /-]{3,60}\n)", text)
    buckets: dict[str, list[str]] = {key: [] for key in TEXT_HINTS}
    for chunk in chunks:
        title = chunk.strip().splitlines()[0] if chunk.strip() else ""
        key = _section_bucket(title)
        if key:
            buckets[key].append(chunk.strip())
    if not any(buckets.values()):
        return {"body": text[:18_000]}
    return {key: "\n\n".join(parts)[:18_000] for key, parts in buckets.items() if parts}


def _section_bucket(title: str) -> str:
    lowered = title.lower()
    for key, hints in TEXT_HINTS.items():
        if any(hint in lowered for hint in hints):
            return key
    return ""


def _safe_member(member: tarfile.TarInfo) -> bool:
    return not (member.name.startswith("/") or ".." in member.name or member.issym() or member.islnk())


def _strip_tex(text: str) -> str:
    text = re.sub(r"%.*", "", text)
    text = re.sub(r"\\(?:cite|ref|label|url|href)(?:\[[^\]]*\])?\{[^{}]*\}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    text = re.sub(r"[{}$]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_pdf_text(text: str) -> str:
    text = re.sub(r"-\n", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\d+\s*\n", "\n", text)
    return text.strip()
