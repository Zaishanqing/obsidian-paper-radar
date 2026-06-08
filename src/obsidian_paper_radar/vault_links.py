"""把生成内容里出现的「已有笔记标题」自动转成 Obsidian `[[wikilink]]`。

借鉴上游 start-my-day 的 scan_existing_notes + link_keywords：扫描 Vault 里已有的论文
笔记 → 建「标题 → 笔记路径」索引 → 在新生成的日报/精读笔记里，把命中已有笔记标题的文字
替换成指向该笔记的链接，从而把每天的新论文织进已有知识图谱。

为降噪只索引**笔记标题/别名**（不含通用 tag），并保护 frontmatter、代码块、行内代码、已有
`[[..]]`/`![[..]]`/`[..](..)` 不被改动；同一篇笔记不会链接到自己。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from .utils import vault_relative

logger = logging.getLogger(__name__)

# 命中后保持原样、不在内部做关键词替换的区域
_PROTECTED_RE = re.compile(r"(```.*?```|`[^`]*`|!?\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\))", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)

DEFAULT_STOPWORDS = {
    "方法", "模型", "论文", "研究", "实验", "结果", "数据", "数据集", "框架", "系统", "任务", "问题", "分析",
    "method", "model", "paper", "approach", "framework", "system", "results", "dataset", "task", "overview",
}

LinkProcessor = Callable[[str, set[str]], str]


def build_note_index(
    vault_path: Path,
    scan_dirs: list[str],
    *,
    include_aliases: bool = True,
    min_ascii_len: int = 3,
    min_cjk_len: int = 2,
    max_terms: int = 2000,
    stopwords: set[str] | None = None,
) -> dict[str, str]:
    """扫描 Vault 指定目录下的 `.md`，返回 {小写标题: 笔记的 vault 相对路径(无扩展名)}。"""

    stop = {item.lower() for item in (stopwords or DEFAULT_STOPWORDS)}
    index: dict[str, str] = {}
    for rel_dir in scan_dirs:
        base = (vault_path / rel_dir)
        if not base.exists():
            continue
        for path in base.rglob("*.md"):
            try:
                rel_path = vault_relative(path.with_suffix(""), vault_path)
                for key in _note_keys(path, include_aliases):
                    norm = key.strip()
                    if not _acceptable(norm, stop, min_ascii_len, min_cjk_len):
                        continue
                    index.setdefault(norm.lower(), rel_path)
                    if len(index) >= max_terms:
                        return index
            except Exception as exc:  # 单个笔记解析失败不影响整体
                logger.debug("扫描笔记失败 %s: %s", path, exc)
    return index


def make_link_processor(index: dict[str, str], cfg: dict[str, Any] | None = None) -> LinkProcessor | None:
    """根据标题索引构造一个文本处理器；索引为空时返回 None。"""

    cfg = cfg or {}
    if not index:
        return None
    pattern = _compile_pattern(index.keys())
    if pattern is None:
        return None
    max_total = int(cfg.get("max_links_per_doc", 30))
    per_key = int(cfg.get("max_links_per_term", 1))

    def processor(text: str, skip_paths: set[str]) -> str:
        return link_text(text, index, pattern, skip_paths, max_total=max_total, per_key=per_key)

    return processor


def link_text(
    text: str,
    index: dict[str, str],
    pattern: re.Pattern[str],
    skip_paths: set[str],
    *,
    max_total: int = 30,
    per_key: int = 1,
) -> str:
    """在 frontmatter 与受保护区域之外，把命中已有笔记标题的文字替换成 wikilink。"""

    frontmatter = ""
    match = _FRONTMATTER_RE.match(text)
    if match:
        frontmatter = match.group(0)
        text = text[match.end():]

    counts: dict[str, int] = {}
    state = {"total": 0}

    def repl(found: re.Match[str]) -> str:
        matched = found.group(0)
        key = matched.lower()
        target = index.get(key)
        if target is None or target in skip_paths:
            return matched
        if state["total"] >= max_total or counts.get(key, 0) >= per_key:
            return matched
        counts[key] = counts.get(key, 0) + 1
        state["total"] += 1
        return f"[[{target}|{matched}]]"

    out: list[str] = []
    cursor = 0
    for protected in _PROTECTED_RE.finditer(text):
        out.append(pattern.sub(repl, text[cursor:protected.start()]))
        out.append(protected.group(0))
        cursor = protected.end()
    out.append(pattern.sub(repl, text[cursor:]))
    return frontmatter + "".join(out)


def _note_keys(path: Path, include_aliases: bool) -> list[str]:
    keys: list[str] = [path.stem]
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return keys
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except Exception:
        return keys
    if not isinstance(frontmatter, dict):
        return keys
    for field in ("note_title", "title"):
        value = frontmatter.get(field)
        if isinstance(value, str):
            keys.append(value)
    if include_aliases:
        aliases = frontmatter.get("aliases")
        if isinstance(aliases, list):
            keys.extend(str(item) for item in aliases)
        elif isinstance(aliases, str):
            keys.append(aliases)
    return keys


def _acceptable(value: str, stop: set[str], min_ascii_len: int, min_cjk_len: int) -> bool:
    if not value or value.lower() in stop:
        return False
    if not re.search(r"[A-Za-z一-鿿]", value):  # 纯数字/标点不索引
        return False
    if value.isascii():
        return len(value) >= min_ascii_len
    return len(value) >= min_cjk_len


def _compile_pattern(keys: Any) -> re.Pattern[str] | None:
    parts: list[str] = []
    for key in sorted(keys, key=len, reverse=True):
        escaped = re.escape(key)
        parts.append(rf"\b{escaped}\b" if key.isascii() else escaped)
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)
