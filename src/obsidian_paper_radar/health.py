"""运行健康报告：统计来源、LLM 降级、开源核验与图片结果。"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .models import Paper, RerankResult


def build_run_report(
    candidates: list[Paper],
    llm_candidates: list[Paper],
    results: list[RerankResult],
    recommended: list[RerankResult],
    image_success: int,
    image_failed: int,
    run_date: date,
) -> dict[str, Any]:
    source_counts: Counter[str] = Counter(_source_group(paper.source) for paper in candidates)
    llm_counts: Counter[str] = Counter(_source_group(paper.source) for paper in llm_candidates)
    paper_sources = {paper.paper_id: _source_group(paper.source) for paper in candidates + llm_candidates}
    recommended_counts: Counter[str] = Counter(paper_sources.get(result.paper_id, "unknown") for result in recommended)
    fallback = sum(1 for result in results if result.fallback_used)
    verified_code = sum(1 for paper in llm_candidates if _verified(paper, "has_verified_code"))
    verified_dataset = sum(1 for paper in llm_candidates if _verified(paper, "has_verified_dataset"))
    return {
        "date": run_date.isoformat(),
        "candidates_total": len(candidates),
        "candidates_by_source": dict(sorted(source_counts.items())),
        "llm_by_source": dict(sorted(llm_counts.items())),
        "recommended_by_source": dict(sorted(recommended_counts.items())),
        "llm_filtered": len(llm_candidates),
        "llm_fallback": fallback,
        "recommended": len(recommended),
        "open_source_verified_code": verified_code,
        "open_source_verified_dataset": verified_dataset,
        "images_success": image_success,
        "images_failed": image_failed,
    }


def render_report(report: dict[str, Any]) -> str:
    by_source = report.get("candidates_by_source", {})
    source_lines = "\n".join(f"- {name}: {count}" for name, count in by_source.items()) or "- （无）"
    return "\n".join(
        [
            f"# {report.get('date', '')} 运行健康报告",
            "",
            f"- 候选总数：{report.get('candidates_total', 0)}",
            "- 各来源候选：",
            source_lines,
            "",
            "## 来源贡献",
            "",
            _source_table(report),
            "",
            f"- 进入 LLM 筛选：{report.get('llm_filtered', 0)}",
            f"- LLM 降级为规则评分：{report.get('llm_fallback', 0)}",
            f"- 最终推荐：{report.get('recommended', 0)}",
            f"- 开源核验：代码 {report.get('open_source_verified_code', 0)} / 数据集 {report.get('open_source_verified_dataset', 0)}",
            f"- 图片：成功 {report.get('images_success', 0)} / 失败 {report.get('images_failed', 0)}",
            "",
        ]
    )


def format_report_line(report: dict[str, Any]) -> str:
    by_source = report.get("candidates_by_source", {})
    sources = "，".join(f"{name} {count}" for name, count in by_source.items()) or "无"
    return (
        f"健康自检：候选 {report.get('candidates_total', 0)}（{sources}）｜"
        f"降级 {report.get('llm_fallback', 0)}｜"
        f"核验代码 {report.get('open_source_verified_code', 0)}/数据 {report.get('open_source_verified_dataset', 0)}"
    )


def _source_table(report: dict[str, Any]) -> str:
    candidates = report.get("candidates_by_source", {}) or {}
    llm = report.get("llm_by_source", {}) or {}
    recommended = report.get("recommended_by_source", {}) or {}
    sources = sorted(set(candidates) | set(llm) | set(recommended))
    lines = ["| 来源 | 候选数 | 进入 LLM | 最终推荐 |", "|---|---:|---:|---:|"]
    if not sources:
        lines.append("| 无 | 0 | 0 | 0 |")
    for source in sources:
        lines.append(
            f"| {source} | {int(candidates.get(source, 0))} | {int(llm.get(source, 0))} | {int(recommended.get(source, 0))} |"
        )
    return "\n".join(lines)


def _source_group(source: str) -> str:
    source = (source or "").strip().lower()
    if not source:
        return "unknown"
    return source.split(":", 1)[0]


def _verified(paper: Paper, key: str) -> bool:
    evidence = paper.extra_context.get("open_source") if isinstance(paper.extra_context, dict) else None
    return bool(isinstance(evidence, dict) and evidence.get(key))
