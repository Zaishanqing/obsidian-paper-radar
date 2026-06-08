"""反馈闭环：记录你对推荐论文的反馈，聚合成关键词增量，下次运行微调粗排得分。

两种记录方式：
1. Obsidian 笔记 frontmatter（推荐）：在论文笔记的 --- 区块加 ``feedback: useful`` 或
   ``feedback: useless``，下次运行自动吸收，无需离开 Obsidian。
2. CLI：``python scripts/record_feedback.py arxiv:2606.12345 useful``

反馈存在 ``data/state/feedback.json``。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT
from .utils import safe_chinese_title

logger = logging.getLogger(__name__)

STATE_DIR = ROOT / "data" / "state"
USEFUL_LABELS = {"useful", "read", "like", "good", "y", "yes"}
NOT_USEFUL_LABELS = {"useless", "skip", "dislike", "bad", "n", "no"}
ALL_LABELS = USEFUL_LABELS | NOT_USEFUL_LABELS

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def feedback_path() -> Path:
    return STATE_DIR / "feedback.json"


def load_feedback() -> dict[str, Any]:
    path = feedback_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def record_feedback(
    paper_id: str,
    label: str,
    *,
    tags: list[str] | None = None,
    title: str = "",
    run_date: date | None = None,
) -> dict[str, Any]:
    data = load_feedback()
    existing = data.get(paper_id, {}) if isinstance(data.get(paper_id), dict) else {}
    resolved_tags = tags if tags is not None else existing.get("tags", [])
    entry = {
        "label": str(label).strip().lower(),
        "tags": [str(tag) for tag in (resolved_tags or [])][:8],
        "title": title or existing.get("title", ""),
        "date": (run_date or datetime.now().date()).isoformat(),
    }
    data[paper_id] = entry
    path = feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def derive_adjustments(feedback: dict[str, Any], *, boost: float = 1.5, penalty: float = 1.5) -> dict[str, float]:
    """聚合反馈得到 {小写关键词: 分数增量}；useful 方向加分，useless 方向减分。"""
    deltas: dict[str, float] = {}
    for entry in feedback.values():
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).lower()
        if label in USEFUL_LABELS:
            sign = float(boost)
        elif label in NOT_USEFUL_LABELS:
            sign = -float(penalty)
        else:
            continue
        for tag in entry.get("tags", []):
            key = str(tag).strip().lower()
            if key:
                deltas[key] = deltas.get(key, 0.0) + sign
    return deltas


def adjustment_for_text(text: str, deltas: dict[str, float], *, max_adjust: float = 3.0) -> float:
    if not deltas:
        return 0.0
    lowered = (text or "").lower()
    total = sum(delta for term, delta in deltas.items() if term and term in lowered)
    return max(-float(max_adjust), min(float(max_adjust), total))


def load_adjustments(cfg: dict[str, Any]) -> tuple[dict[str, float], float]:
    """从配置和反馈文件构建调整表；未启用或无反馈时返回空表。"""
    if not cfg.get("enabled", True):
        return {}, 0.0
    feedback = load_feedback()
    if not feedback:
        return {}, 0.0
    deltas = derive_adjustments(
        feedback,
        boost=float(cfg.get("boost", 1.5)),
        penalty=float(cfg.get("penalty", 1.5)),
    )
    return deltas, float(cfg.get("max_adjust", 3.0))


# 匹配「## 快速反馈」区块内的复选框行，捕获后半段（标题/链接）。标签可有可无：
# 新版（无标签）："- [x] [[笔记路径|中文短名]]"
# 兼容旧版："- [x] **不推荐/有用/无用** · [[...]] %%pid:...%%" / "... <!-- feedback:... -->"
_FEEDBACK_LINE_RE = re.compile(
    r"-\s*\[(?P<checked>.)\]\s*(?:\*{1,2}(?P<label>有用|无用|不推荐)\*{1,2}\s*·\s*)?(?P<rest>.+)"
)
_FEEDBACK_SECTION = "## 快速反馈"
_LEGACY_PID_RE = re.compile(r"(?:%%|<!--)\s*(?:feedback:(?P<lab>useful|useless)\s+)?pid:(?P<pid>.+?)\s*(?:%%|-->)")
_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^\]|]+?)(?:\|(?P<alias>[^\]]+))?\]\]")


def _norm_title(text: object) -> str:
    return safe_chinese_title(str(text or "").strip())


def _plain_title(rest: str) -> str:
    """从复选框后半段提取可读标题：去掉旧标记、把 [[a|b]] 取显示名、去掉粗体。"""
    text = _LEGACY_PID_RE.sub("", rest).strip()
    text = _WIKILINK_RE.sub(lambda m: (m.group("alias") or m.group("target") or ""), text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return text.strip()


def _build_title_index() -> dict[str, str]:
    """{规范化中文短名: paper_id}，用于无笔记论文的标题反查。"""
    index: dict[str, str] = {}
    try:
        from .dedup import load_seen  # noqa: PLC0415

        seen = load_seen()
    except Exception:
        return index
    for pid, item in seen.items():
        if not isinstance(item, dict):
            continue
        for key in (item.get("note_title"), item.get("title")):
            norm = _norm_title(key)
            if norm:
                index.setdefault(norm, str(pid))
    return index


def _paper_id_from_note(vault_path: Path, target: str) -> str:
    """从 wikilink 目标笔记的 frontmatter 读 paper_id。"""
    target = target.strip()
    if not target:
        return ""
    path = vault_path / f"{target}.md"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return ""
    try:
        fm = yaml.safe_load(match.group(1))
    except Exception:
        return ""
    return str(fm.get("paper_id", "")).strip() if isinstance(fm, dict) else ""


def _resolve_paper_id(rest: str, vault_path: Path, title_index: dict[str, str]) -> str:
    """反查 paper_id：旧内联标记 > wikilink→笔记 frontmatter > 中文标题在 seen 反查。"""
    legacy = _LEGACY_PID_RE.search(rest)
    if legacy:
        return legacy.group("pid").strip()
    display = ""
    link = _WIKILINK_RE.search(rest)
    if link:
        pid = _paper_id_from_note(vault_path, link.group("target"))
        if pid:
            return pid
        display = (link.group("alias") or link.group("target") or "").strip()
    if not display:
        display = _plain_title(rest)
    return title_index.get(_norm_title(display), "")


def import_feedback_from_daily(vault_path: Path, daily_dir: str, max_digests: int = 7) -> int:
    """扫描最近 N 篇日报底部的快速反馈复选框，导入已勾选的条目。

    新版复选框不带任何机器标记：勾选「不推荐」后，通过 wikilink 指向的笔记 frontmatter
    反查 paper_id；无笔记的论文用中文短名在 seen_papers 反查。兼容旧版带标记/「有用/无用」格式。
    同一 paper 同 label 不重复写入。返回新导入的条数。
    """
    daily_root = vault_path / daily_dir
    if not daily_root.exists():
        return 0

    digests = sorted(daily_root.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_digests]
    existing = load_feedback()
    title_index = _build_title_index()
    imported = 0

    for digest_path in digests:
        try:
            text = digest_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # 只扫描「快速反馈」区块，避免误匹配正文里别的复选框；勾选即记为 useless。
        section_idx = text.find(_FEEDBACK_SECTION)
        section = text[section_idx:] if section_idx >= 0 else text
        for match in _FEEDBACK_LINE_RE.finditer(section):
            if match.group("checked").strip() not in ("x", "X"):
                continue
            rest = match.group("rest")
            label = "useful" if match.group("label") == "有用" else "useless"
            legacy_label = _LEGACY_PID_RE.search(rest)
            if legacy_label and legacy_label.group("lab") in ALL_LABELS:
                label = legacy_label.group("lab")
            paper_id = _resolve_paper_id(rest, vault_path, title_index)
            if not paper_id:
                logger.debug("快速反馈行无法解析 paper_id，跳过：%s", rest[:80])
                continue
            prev = existing.get(paper_id)
            if isinstance(prev, dict) and prev.get("label") == label:
                continue
            tags = _seen_tags(paper_id)
            title = _seen_title(paper_id)
            record_feedback(paper_id, label, tags=tags, title=title)
            existing[paper_id] = {"label": label, "tags": tags}
            imported += 1
            logger.info("从日报复选框导入反馈：%s -> %s", paper_id, label)

    return imported


def _seen_tags(paper_id: str) -> list[str]:
    try:
        from .dedup import load_seen  # noqa: PLC0415

        item = load_seen().get(paper_id)
        return list(item.get("tags", [])) if isinstance(item, dict) else []
    except Exception:
        return []


def _seen_title(paper_id: str) -> str:
    try:
        from .dedup import load_seen  # noqa: PLC0415

        item = load_seen().get(paper_id)
        return str(item.get("title", "")) if isinstance(item, dict) else ""
    except Exception:
        return ""


def import_feedback_from_vault(vault_path: Path, paper_dir: str) -> int:
    """扫描 Vault 中论文笔记的 frontmatter，自动导入 feedback 字段。

    笔记的 frontmatter 中包含 ``paper_id`` 和 ``feedback: useful``（或 useless）
    时，自动调用 record_feedback 写入 feedback.json。已存在的反馈会被跳过（不重复写入）。
    返回新导入的条数。
    """
    notes_root = vault_path / paper_dir
    if not notes_root.exists():
        return 0

    existing = load_feedback()
    imported = 0
    for md_path in notes_root.rglob("*.md"):
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        match = _FRONTMATTER_RE.match(text)
        if not match:
            continue
        try:
            fm = yaml.safe_load(match.group(1))
        except Exception:
            continue
        if not isinstance(fm, dict):
            continue
        paper_id = str(fm.get("paper_id", "")).strip()
        label = str(fm.get("feedback", "")).strip().lower()
        if not paper_id or label not in ALL_LABELS:
            continue
        # 已有反馈且 label 相同则跳过
        prev = existing.get(paper_id)
        if isinstance(prev, dict) and prev.get("label") == label:
            continue
        tags = fm.get("tags", [])
        if isinstance(tags, list):
            tags = [str(t) for t in tags]
        else:
            tags = []
        title = str(fm.get("title", ""))
        record_feedback(paper_id, label, tags=tags, title=title)
        existing[paper_id] = {"label": label, "tags": tags}
        imported += 1
        logger.info("从笔记导入反馈：%s -> %s", paper_id, label)

    return imported
