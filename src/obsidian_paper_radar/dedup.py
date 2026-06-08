from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import ROOT
from .models import Paper, RerankResult


STATE_DIR = ROOT / "data" / "state"


def state_path() -> Path:
    return STATE_DIR / "seen_papers.json"


def load_seen() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def filter_recent_seen(papers: list[Paper], seen: dict, disable_dedup: bool = False) -> list[Paper]:
    if disable_dedup:
        return papers
    filtered: list[Paper] = []
    for paper in papers:
        item = seen.get(paper.paper_id)
        if item and item.get("status") == "noted":
            continue
        filtered.append(paper)
    return filtered


def save_seen(papers: list[Paper], results: list[RerankResult], note_paths: dict[str, str], run_date: date) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    result_map = {r.paper_id: r for r in results}
    for paper in papers:
        result = result_map.get(paper.paper_id)
        existing = seen.get(paper.paper_id, {})
        count = int(existing.get("recommend_count", 0))
        note_path = note_paths.get(paper.paper_id) or existing.get("note_path", "")
        tags = (result.tags or result.matched_interests) if result else paper.matched_interests
        note_title = (result.note_title_zh if result else "") or existing.get("note_title", "")
        seen[paper.paper_id] = {
            "title": paper.title,
            "note_title": note_title,  # 中文短名：日报快速反馈用它反查 paper_id（无笔记的论文）
            "first_seen": existing.get("first_seen") or run_date.isoformat(),
            "last_recommended": run_date.isoformat(),
            "recommend_count": count + 1,
            "note_path": note_path,
            "status": "noted" if note_path else existing.get("status", "unread"),
            "last_score": result.recommend_score if result else paper.rough_score,
            "tags": [str(tag) for tag in (tags or [])][:8],
            "source": paper.source,
        }
    path.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def recent_recommended(seen: dict, run_date: date, days: int) -> list[dict]:
    """返回最近 ``days`` 天内推荐过的论文条目（含 paper_id），按得分降序。"""
    start = run_date - timedelta(days=max(days - 1, 0))
    items: list[dict] = []
    for paper_id, entry in seen.items():
        if not isinstance(entry, dict):
            continue
        last = _parse_day(entry.get("last_recommended"))
        if last is None or last < start or last > run_date:
            continue
        items.append({"paper_id": paper_id, **entry})
    items.sort(key=lambda item: float(item.get("last_score") or 0), reverse=True)
    return items


def _parse_day(value: object) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def append_run_history(summary: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "run_history.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
