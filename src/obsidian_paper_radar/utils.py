from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def safe_filename(value: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r'[ /\\:*?"<>|]+', "_", value).strip("._ ")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:160] or fallback


def safe_chinese_title(value: str, fallback: str = "论文方法") -> str:
    cleaned = re.sub(r'[ /\\:*?"<>|#\[\]\n\r\t]+', "_", value).strip("._ ")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:60] or fallback


def short_paper_suffix(paper_id: str) -> str:
    value = paper_id.split(":", 1)[-1].replace("/", "_")
    return re.sub(r"v\d+$", "", value)


def fallback_note_title(summary: str, title: str = "") -> str:
    text = summary or title
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    if chinese:
        return safe_chinese_title("".join(chinese[:2])[:18], "论文方法")
    words = re.findall(r"[A-Za-z][A-Za-z-]{3,}", title)
    stop = {"with", "from", "using", "based", "towards", "through", "benchmark", "paper"}
    picked = [w for w in words if w.lower() not in stop][:4]
    return safe_chinese_title("_".join(picked), "论文方法")


def safe_paper_id(paper_id: str) -> str:
    return safe_filename(paper_id.replace(":", "_").replace("/", "_"), "paper")


def parse_date(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value[: len(fmt)], fmt)
        except (ValueError, TypeError):
            continue
    return None


def vault_relative(path: Path, vault_path: Path) -> str:
    try:
        return path.resolve().relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
