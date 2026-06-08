"""主题 MOC（Map of Content）自动索引：按方向/标签把已写的论文笔记聚合成索引页。

从 seen_papers 的 tags 聚合出方向，为每个达到阈值的方向生成/覆盖一个 MOC 笔记，
里面用 `[[路径|标题]]` 列出该方向的全部论文笔记，给 wikilink 之外再加一个总览入口。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from .utils import safe_chinese_title

logger = logging.getLogger(__name__)


def update_mocs(vault_path: Path, moc_dir: str, seen: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    if not cfg.get("enabled", True):
        return []
    min_notes = int(cfg.get("min_notes_per_topic", 2))
    max_topics = int(cfg.get("max_topics", 40))

    topic_notes: dict[str, dict[str, str]] = defaultdict(dict)  # topic -> {note_path: title}
    for entry in seen.values():
        if not isinstance(entry, dict):
            continue
        note_path = entry.get("note_path")
        if not note_path or entry.get("status") != "noted":
            continue
        title = entry.get("title", "") or str(note_path).split("/")[-1]
        for tag in entry.get("tags", []):
            topic = str(tag).strip()
            if topic:
                topic_notes[topic][str(note_path)] = title

    ranked = sorted(topic_notes.items(), key=lambda kv: len(kv[1]), reverse=True)
    base = vault_path / moc_dir
    written: list[str] = []
    for topic, note_map in ranked[:max_topics]:
        if len(note_map) < min_notes:
            continue
        path = base / f"{safe_chinese_title(topic)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_moc(topic, note_map), encoding="utf-8")
        written.append(str(path))
    return written


def _render_moc(topic: str, note_map: dict[str, str]) -> str:
    lines = [
        "---",
        'tags:',
        '  - MOC',
        f'  - {safe_chinese_title(topic)}',
        "---",
        "",
        f"# {topic} · 主题索引",
        "",
        f"共 {len(note_map)} 篇相关论文笔记。",
        "",
    ]
    for note_path, title in sorted(note_map.items(), key=lambda kv: kv[1]):
        lines.append(f"- [[{note_path}|{title}]]")
    lines.append("")
    return "\n".join(lines)
