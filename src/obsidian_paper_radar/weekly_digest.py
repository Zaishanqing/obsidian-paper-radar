"""周报/趋势汇总：把最近一周推荐过的论文聚合，用 DeepSeek 生成本周热点与补读清单。

复用 seen_papers，无需重新抓取。无 DeepSeek client 时降级为规则汇总（按方向与得分列出）。
由 ``scripts/run_weekly_digest.py`` 调度，输出写入 Vault 的周报目录。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from .deepseek_client import DeepSeekClient

logger = logging.getLogger(__name__)


def build_weekly_digest(
    items: list[dict[str, Any]],
    run_date: date,
    *,
    days: int = 7,
    client: DeepSeekClient | None = None,
    top_n: int = 30,
) -> str | None:
    """根据最近一周推荐条目生成周报 markdown；无条目时返回 None。"""
    items = items[:top_n]
    if not items:
        return None
    summary: dict[str, Any] | None = None
    if client is not None:
        try:
            payload = client.chat_json(_build_messages(items, days), model="fast")
            if isinstance(payload, dict):
                summary = payload
        except Exception as exc:
            logger.warning("周报 LLM 总结失败，改用规则汇总：%s", exc)
    return _render(items, run_date, summary)


def _build_messages(items: list[dict[str, Any]], days: int) -> list[dict[str, str]]:
    payload = {
        "instruction": f"以下是最近 {days} 天推荐过的论文，请生成一份中文科研周报。",
        "papers": [
            {"title": item.get("title", ""), "tags": item.get("tags", [])[:5], "score": item.get("last_score", 0)}
            for item in items
        ],
        "output_schema": {
            "overview": "一段话总览本周方向分布",
            "hot_directions": ["本周出现最多/最值得注意的方向"],
            "must_read": [{"title": "标题", "reason": "为什么最该补读"}],
            "backlog_note": "对还没读的积压论文的提醒",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是科研助理。基于最近一周的论文推荐记录生成中文周报，突出热点方向、最该补读的 3 篇、"
                "以及积压提醒。只返回合法 JSON，不要 Markdown。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _render(items: list[dict[str, Any]], run_date: date, summary: dict[str, Any] | None) -> str:
    iso_year, iso_week, _ = run_date.isocalendar()
    lines = [
        "---",
        f'date: "{run_date.isoformat()}"',
        'tags: ["weekly-paper", "deepseek", "llm-generated"]',
        f"paper_count: {len(items)}",
        "---",
        "",
        f"# {iso_year}-W{iso_week:02d} 论文周报",
        "",
    ]
    if isinstance(summary, dict):
        overview = str(summary.get("overview") or "").strip()
        if overview:
            lines.extend([overview, ""])
        hot = [str(item) for item in (summary.get("hot_directions") or []) if str(item).strip()]
        if hot:
            lines.extend(["## 本周热点方向", *[f"- {item}" for item in hot], ""])
        must = summary.get("must_read") or []
        if isinstance(must, list) and must:
            lines.append("## 最该补读")
            for item in must:
                if isinstance(item, dict):
                    lines.append(f"- **{item.get('title', '')}** — {item.get('reason', '')}")
            lines.append("")
        backlog = str(summary.get("backlog_note") or "").strip()
        if backlog:
            lines.extend(["## 积压提醒", backlog, ""])

    lines.append("## 本周推荐清单")
    for item in items:
        title = item.get("title", "")
        score = item.get("last_score", 0)
        tags = "、".join(item.get("tags", [])[:4])
        note_path = item.get("note_path", "")
        link = f"[[{note_path}|{title}]]" if note_path else title
        lines.append(f"- {link}（{score}）" + (f" — {tags}" if tags else ""))
    lines.append("")
    return "\n".join(lines)


def weekly_path(vault_path: Path, weekly_dir: str, run_date: date) -> Path:
    iso_year, iso_week, _ = run_date.isocalendar()
    return vault_path / weekly_dir / f"{iso_year}-W{iso_week:02d} 周报.md"


def write_weekly_digest(vault_path: Path, weekly_dir: str, content: str, run_date: date) -> Path:
    path = weekly_path(vault_path, weekly_dir, run_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
