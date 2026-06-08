from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT
from .deepseek_client import DeepSeekClient
from .models import Paper, RerankResult
from .utils import fallback_note_title

logger = logging.getLogger(__name__)

# ── schema validation ────────────────────────────────────────────
_VALID_DECISIONS = {"keep", "maybe", "drop"}
_VALID_ACTIONS = {"daily_only", "detailed_note", "skip"}
_VALID_EVIDENCE = {"strong", "medium", "weak"}
_VALID_FEASIBILITY = {"high", "medium", "low", "unknown"}
_VALID_CODE_DATASET = {"available", "mentioned", "not_found", "unknown"}
_VALID_READING_TIMES = {"15min", "30min", "1h", "2h+"}
_VALID_PAPER_TYPES = {"method", "benchmark", "survey", "system", "theory", "dataset", "application", "unknown"}
_VALID_READING_DECISIONS = {"精读", "泛读", "收藏观察", "跳过"}


def validate_result_item(item: dict[str, Any], expected_paper_ids: set[str] | None = None) -> dict[str, Any]:
    """严格校验单条 LLM 结果；失败时由调用方只 fallback 对应论文。"""
    if not isinstance(item, dict):
        raise ValueError("result item must be an object")
    paper_id = str(item.get("paper_id", "")).strip()
    if not paper_id:
        raise ValueError("missing paper_id")
    if expected_paper_ids is not None and paper_id not in expected_paper_ids:
        raise ValueError(f"unknown paper_id: {paper_id}")
    if "recommend_score" not in item:
        raise ValueError(f"{paper_id}: missing recommend_score")
    try:
        float(item.get("recommend_score"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{paper_id}: invalid recommend_score") from exc
    decision = str(item.get("decision", "")).strip().lower()
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"{paper_id}: invalid decision")
    action = str(item.get("action", "")).strip().lower()
    if action not in _VALID_ACTIONS:
        raise ValueError(f"{paper_id}: invalid action")
    for field in ("summary_zh", "note_title_zh"):
        if not str(item.get(field, "")).strip():
            raise ValueError(f"{paper_id}: missing {field}")
    return _validate_and_normalize(item)


def _validate_and_normalize(item: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化单个 rerank 结果，补默认值、钳制范围。"""
    normalized: dict[str, Any] = {}

    # paper_id
    normalized["paper_id"] = str(item.get("paper_id", ""))

    # score: clamp 0-10
    try:
        score = float(item.get("recommend_score", 0))
    except (TypeError, ValueError):
        score = 0.0
    normalized["recommend_score"] = max(0.0, min(10.0, score))

    # decision / action: validate, default on invalid
    decision = str(item.get("decision", "maybe")).strip().lower()
    normalized["decision"] = decision if decision in _VALID_DECISIONS else "maybe"
    action = str(item.get("action", "daily_only")).strip().lower()
    normalized["action"] = action if action in _VALID_ACTIONS else "daily_only"

    # string fields with defaults
    for field in (
        "project_relevance", "summary_zh", "note_title_zh", "core_problem",
        "method_overview", "key_innovation", "implementation_details",
        "evidence_notes", "results", "project_inspiration", "why_read",
        "reading_priority_reason",
    ):
        normalized[field] = str(item.get(field, ""))

    # enum fields with validation
    for field, valid_set in (
        ("paper_type", _VALID_PAPER_TYPES),
        ("evidence_strength", _VALID_EVIDENCE),
        ("implementation_feasibility", _VALID_FEASIBILITY),
        ("code_availability", _VALID_CODE_DATASET),
        ("dataset_availability", _VALID_CODE_DATASET),
        ("estimated_reading_time", _VALID_READING_TIMES),
        ("reading_decision", _VALID_READING_DECISIONS),
    ):
        val = str(item.get(field, "")).strip()
        normalized[field] = val if val in valid_set else ""

    # list fields
    for field in (
        "matched_interests", "reproducibility_signals", "future_improvements",
        "reading_questions", "skip_risks", "risks", "tags",
    ):
        raw = item.get(field, [])
        normalized[field] = [str(x) for x in raw] if isinstance(raw, list) else []

    # dict-list fields
    for field in ("core_modules", "transferable_modules", "technical_terms"):
        raw = item.get(field, [])
        normalized[field] = (
            [{str(k): str(v) for k, v in d.items()} for d in raw if isinstance(d, dict)]
            if isinstance(raw, list) else []
        )

    # image_guidance
    guidance = item.get("image_guidance", {})
    normalized["image_guidance"] = guidance if isinstance(guidance, dict) else {}
    normalized["fallback_used"] = bool(item.get("fallback_used", False))

    return normalized


def _save_debug_response(raw_text: str, batch_index: int) -> None:
    """保存 DeepSeek 原始响应到调试文件。"""
    try:
        debug_dir = ROOT / "logs"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = debug_dir / f"debug_deepseek_{stamp}_batch{batch_index}.json"
        path.write_text(raw_text[:200_000], encoding="utf-8")
        logger.info("DeepSeek 原始响应已保存至 %s", path)
    except Exception as exc:
        logger.debug("保存调试响应失败：%s", exc)


def _result_from_dict(item: dict[str, Any]) -> RerankResult:
    """从（已校验的）字典构建 RerankResult。"""
    return RerankResult(
        paper_id=str(item.get("paper_id", "")),
        recommend_score=float(item.get("recommend_score", 0)),
        decision=str(item.get("decision", "maybe")),
        action=str(item.get("action", "daily_only")),
        matched_interests=[str(x) for x in item.get("matched_interests", [])],
        project_relevance=str(item.get("project_relevance", "")),
        summary_zh=str(item.get("summary_zh", "")),
        note_title_zh=str(item.get("note_title_zh", "")),
        paper_type=str(item.get("paper_type", "")),
        reading_decision=str(item.get("reading_decision", "")),
        reading_priority_reason=str(item.get("reading_priority_reason", "")),
        estimated_reading_time=str(item.get("estimated_reading_time", "")),
        core_problem=str(item.get("core_problem", "")),
        method_overview=str(item.get("method_overview", "")),
        key_innovation=str(item.get("key_innovation", "")),
        implementation_details=str(item.get("implementation_details", "")),
        evidence_strength=str(item.get("evidence_strength", "")),
        evidence_notes=str(item.get("evidence_notes", "")),
        implementation_feasibility=str(item.get("implementation_feasibility", "")),
        code_availability=str(item.get("code_availability", "")),
        dataset_availability=str(item.get("dataset_availability", "")),
        reproducibility_signals=[str(x) for x in item.get("reproducibility_signals", [])],
        core_modules=_list_of_dicts(item.get("core_modules", [])),
        transferable_modules=_list_of_dicts(item.get("transferable_modules", [])),
        technical_terms=_list_of_dicts(item.get("technical_terms", [])),
        results=str(item.get("results", "")),
        future_improvements=[str(x) for x in item.get("future_improvements", [])],
        project_inspiration=str(item.get("project_inspiration", "")),
        reading_questions=[str(x) for x in item.get("reading_questions", [])],
        skip_risks=[str(x) for x in item.get("skip_risks", [])],
        image_guidance=item.get("image_guidance", {}) if isinstance(item.get("image_guidance", {}), dict) else {},
        why_read=str(item.get("why_read", "")),
        risks=[str(x) for x in item.get("risks", [])],
        tags=[str(x) for x in item.get("tags", [])],
        fallback_used=bool(item.get("fallback_used", False)),
    )


def _build_messages(papers: list[Paper], profile: dict[str, Any], style_specs: str = "") -> list[dict[str, str]]:
    """第一层 fast 模型：轻量 schema，只输出筛选必需字段。"""
    compact_profile = {
        "long_term_interests": profile.get("long_term_interests", []),
        "current_projects": profile.get("current_projects", []),
        "excluded_keywords": profile.get("excluded_keywords", []),
        "scoring_weights": profile.get("scoring_weights", {}),
        "selection_policy": profile.get("selection_policy", {}),
    }
    user = {
        "profile": compact_profile,
        "papers": [_paper_to_fast_prompt_dict(p) for p in papers],
        "output_schema": {
            "results": [
                {
                    "paper_id": "string",
                    "recommend_score": "number 0-10",
                    "decision": "keep|maybe|drop",
                    "action": "daily_only|detailed_note|skip",
                    "tags": ["string 研究方向标签"],
                    "matched_interests": ["string 命中的兴趣方向"],
                    "summary_zh": "string 中文摘要（2-3句话，给科研入门选手看，先讲问题和用途，不要全文翻译或术语堆叠）",
                    "note_title_zh": "中文短名，抽取最重要的机制/架构/原理/方法",
                    "why_read": "string 一句话说明为什么值得读，用普通中文说明看点",
                }
            ]
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是论文快速筛选助手。根据用户研究画像对每篇论文打分和分类。"
                "用户兴趣只是优先方向，不是硬性边界；论文很火或有启发性也可以保留。"
                "所有中文解释都要适合科研入门选手快速浏览：先说清直觉、问题和用途，再给必要术语；"
                "避免缩写堆叠、晦涩长句和没有解释的专业名词。"
                "只返回合法 JSON，不要 Markdown。decision: keep/maybe/drop，action: daily_only/detailed_note/skip。"
            ),
        },
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _build_deep_messages(papers: list[Paper], profile: dict[str, Any], style_specs: str = "") -> list[dict[str, str]]:
    """第二层 pro 模型：完整 schema，深挖模块/术语/可迁移性/图片指导。"""
    compact_profile = {
        "long_term_interests": profile.get("long_term_interests", []),
        "current_projects": profile.get("current_projects", []),
        "excluded_keywords": profile.get("excluded_keywords", []),
        "scoring_weights": profile.get("scoring_weights", {}),
        "selection_policy": profile.get("selection_policy", {}),
    }
    user = {
        "profile": compact_profile,
        # E2：完整传递写作规范，不做关键词过滤、不截断。
        "obsidian_style_specs": style_specs,
        "papers": [_paper_to_fast_prompt_dict(p) for p in papers],
        "output_schema": {
            "results": [
                {
                    "paper_id": "string",
                    "recommend_score": "number 0-10",
                    "decision": "keep|maybe|drop",
                    "action": "daily_only|detailed_note|skip",
                    "summary_zh": "string 中文摘要，必须非空；面向科研入门选手，先讲问题、方法直觉和用途",
                    "note_title_zh": "中文短名，必须非空，抽取最重要的机制/架构/原理/方法",
                    "paper_type": "method|benchmark|survey|system|theory|dataset|application",
                    "reading_decision": "精读|泛读|收藏观察|跳过",
                    "reading_priority_reason": "string",
                    "estimated_reading_time": "15min|30min|1h|2h+",
                    "core_problem": "string，用普通中文说明问题是什么、为什么难",
                    "method_overview": "string，先讲方法直觉，再讲必要术语",
                    "key_innovation": "string，用易懂语言说明新在哪里、为什么有效",
                    "implementation_details": "string，按步骤解释流程，避免术语堆叠",
                    "evidence_strength": "strong|medium|weak",
                    "evidence_notes": "string",
                    "implementation_feasibility": "high|medium|low",
                    "code_availability": "available|mentioned|not_found|unknown",
                    "dataset_availability": "available|mentioned|not_found|unknown",
                    "reproducibility_signals": ["string"],
                    "core_modules": [{"name": "模块名", "role": "作用", "input": "输入", "output": "输出", "why_needed": "为什么需要"}],
                    "technical_terms": [{"term": "术语", "plain_explanation": "通俗解释", "role_in_paper": "本文中的作用", "need_to_remember": "true|false"}],
                    "results": "string",
                    "limitations": ["string"],
                    "future_improvements": ["string"],
                    "transferable_modules": [{"name": "可迁移模块", "role": "解决什么问题", "input": "输入", "output": "输出", "why_it_works": "为什么有效", "possible_transfer": "迁移方向", "how_to_adapt": "需要怎么改"}],
                    "project_inspiration": "string",
                    "reading_questions": ["string"],
                    "skip_risks": ["string"],
                    "image_guidance": {"should_use_images": "true|false", "preferred_types": ["architecture", "mechanism", "pipeline"], "avoid_types": ["benchmark_table", "ablation_chart"], "selection_reason": "string"},
                    "risks": ["string"],
                }
            ]
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是论文学术分析助手。对每篇已入选的论文做深度拆解："
                "论文类型、阅读决策、证据强度、可复现性、核心模块、可迁移模块、术语解释、图片选择指导。"
                "写作对象是科研入门选手：先用普通中文解释直觉和作用，再保留必要英文术语；"
                "每段只引入少量术语，生僻概念必须顺手解释，禁止缩写和概念密集堆叠。"
                "output_schema 只规定 JSON 字段结构，不是笔记正文模板；不要把 role、input、plain_explanation、need_to_remember 等字段名当成正文内容。"
                "image_guidance 是程序选图用的内部字段，不是正文内容；不要在任何正文类字段里单独写“图示选择指导”或列出 should_use_images、preferred_types、avoid_types。"
                "必须遵守 obsidian_style_specs 中的精读笔记规范。"
                "只返回合法 JSON，不要 Markdown。"
            ),
        },
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _paper_to_fast_prompt_dict(paper: Paper) -> dict[str, Any]:
    """筛选阶段的瘦身论文表示。

    只保留标题、摘要、来源、评分、引用和少量开源/会议信号；不带全文、
    引用网络等长上下文。对应原项目先用元数据筛选，再对少量论文深挖的策略。
    """

    extra = paper.extra_context if isinstance(paper.extra_context, dict) else {}
    compact_extra: dict[str, Any] = {}
    for key in ("venue", "venue_year", "decision", "has_software_link", "code_links", "dataset_links", "open_source"):
        if key in extra:
            compact_extra[key] = extra[key]
    return {
        "id": paper.paper_id,
        "title": paper.title,
        "authors": paper.authors[:6],
        "abstract": paper.abstract[:900],
        "published": paper.published,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "source": paper.source,
        "categories": paper.categories[:8],
        "rough_score": paper.rough_score,
        "matched_interests": paper.matched_interests[:6],
        "citation_count": paper.citation_count,
        "influential_citation_count": paper.influential_citation_count,
        "extra_context": compact_extra,
    }


def _list_of_dicts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            output.append({str(k): str(v) for k, v in item.items()})
    return output


def fallback_result(paper: Paper) -> RerankResult:
    action = "detailed_note" if paper.rough_score >= 6 else "daily_only"
    decision = "keep" if paper.rough_score >= 5 else "maybe"
    return RerankResult(
        paper_id=paper.paper_id,
        recommend_score=min(10.0, max(0.0, paper.rough_score)),
        decision=decision,
        action=action,
        matched_interests=paper.matched_interests,
        summary_zh=paper.abstract[:220],
        note_title_zh="",
        paper_type="unknown",
        reading_decision="收藏观察",
        estimated_reading_time="15min",
        evidence_strength="weak",
        implementation_feasibility="unknown",
        code_availability="unknown",
        dataset_availability="unknown",
        why_read="规则粗筛得分较高，建议进入日报候选。",
        tags=paper.matched_interests[:5],
        fallback_used=True,
    )


def rerank_papers(
    papers: list[Paper],
    profile: dict[str, Any],
    client: DeepSeekClient | None,
    batch_size: int = 8,
    style_specs: str = "",
    deep_top_n: int = 0,
) -> list[RerankResult]:
    """两层语义筛选：fast 全量 → pro 深挖 Top N。

    Args:
        deep_top_n: 需要 pro 模型深挖的篇数。0 表示仅 fast 筛选，不做深挖。
    """
    if not papers:
        return []

    # ── 第一层：fast 模型全量轻量筛选 ──
    results = _run_batch(
        papers, profile, client, batch_size,
        message_builder=lambda batch, prof, _ss: _build_messages(batch, prof),
        model="fast",
    )

    if deep_top_n <= 0 or client is None:
        results.sort(key=lambda r: r.recommend_score, reverse=True)
        return results

    # ── 第二层：pro 模型深挖 Top N ──
    deep_candidates = [r for r in results if r.decision in {"keep", "maybe"} and r.action != "skip"]
    deep_candidates.sort(key=lambda r: r.recommend_score, reverse=True)
    deep_paper_ids = {r.paper_id for r in deep_candidates[:deep_top_n]}
    if not deep_paper_ids:
        results.sort(key=lambda r: r.recommend_score, reverse=True)
        return results

    deep_papers = [p for p in papers if p.paper_id in deep_paper_ids]
    deep_results = _run_batch(
        deep_papers, profile, client, batch_size,
        message_builder=_build_deep_messages,
        model="pro",
        extra_style_specs=style_specs,
    )
    # 把深挖结果合并回第一层结果（仅覆盖 non-empty 字段）
    deep_map = {r.paper_id: r for r in deep_results}
    for result in results:
        if result.paper_id in deep_map:
            _merge_deep(result, deep_map[result.paper_id])

    results.sort(key=lambda r: r.recommend_score, reverse=True)
    return results


def _run_batch(
    papers: list[Paper],
    profile: dict[str, Any],
    client: DeepSeekClient | None,
    batch_size: int,
    *,
    message_builder,
    model: str,
    extra_style_specs: str = "",
) -> list[RerankResult]:
    """通用批量调用：对 papers 分批调 DeepSeek，校验并返回结果列表。"""
    if not papers:
        return []
    results: list[RerankResult] = []
    for batch_index, start in enumerate(range(0, len(papers), batch_size)):
        batch = papers[start : start + batch_size]
        if client is None:
            results.extend([fallback_result(p) for p in batch])
            continue
        raw_text: str | None = None
        try:
            messages = message_builder(batch, profile, extra_style_specs)
            payload = client.chat_json(messages, model=model)
            if isinstance(payload, str):
                raw_text = payload
                raise ValueError("DeepSeek 返回了字符串而非 JSON 对象")
            parsed = payload.get("results", payload if isinstance(payload, list) else [])
            if not isinstance(parsed, list):
                raw_text = json.dumps(payload, ensure_ascii=False)
                raise ValueError("DeepSeek result must contain a results list")
            expected_ids = {paper.paper_id for paper in batch}
            paper_map = {paper.paper_id: paper for paper in batch}
            validated: list[dict[str, Any]] = []
            for item in parsed:
                try:
                    if isinstance(item, dict):
                        item = _repair_required_fields(item, paper_map)
                    validated.append(validate_result_item(item, expected_ids))
                except Exception as item_exc:
                    logger.warning("Invalid LLM result item ignored (%s): %s", model, item_exc)
            result_map = {str(item["paper_id"]): _result_from_dict(item) for item in validated if item["paper_id"]}
            for paper in batch:
                results.append(result_map.get(paper.paper_id, fallback_result(paper)))
        except Exception as exc:
            logger.warning("Falling back to rough scores for batch %d (%s): %s", batch_index, model, exc)
            if raw_text is not None:
                _save_debug_response(raw_text, batch_index)
            if len(batch) > 1:
                logger.warning("DeepSeek batch 失败，改用更小批次重试：model=%s size=%d", model, len(batch))
                results.extend(
                    _run_batch(
                        batch,
                        profile,
                        client,
                        max(1, len(batch) // 2),
                        message_builder=message_builder,
                        model=model,
                        extra_style_specs=extra_style_specs,
                    )
                )
                continue
            results.extend([fallback_result(p) for p in batch])
    return results


def _repair_required_fields(item: dict[str, Any], paper_map: dict[str, Paper]) -> dict[str, Any]:
    repaired = dict(item)
    paper_id = str(repaired.get("paper_id", "")).strip()
    paper = paper_map.get(paper_id)
    if paper is None:
        return repaired
    changed = False
    if not str(repaired.get("summary_zh", "")).strip():
        repaired["summary_zh"] = (paper.abstract or paper.title)[:260]
        changed = True
    if not str(repaired.get("note_title_zh", "")).strip():
        repaired["note_title_zh"] = fallback_note_title(str(repaired.get("summary_zh", "")), paper.title)
        changed = True
    if changed:
        repaired["fallback_used"] = True
        logger.warning("LLM 结果缺少必要字段，已用论文元数据补全：%s", paper_id)
    return repaired


def _merge_deep(target: RerankResult, source: RerankResult) -> None:
    """将 pro 模型的深挖字段覆盖到 fast 结果上（仅覆盖 non-empty 值）。"""
    _copy_if_set(target, source, "paper_type")
    _copy_if_set(target, source, "reading_decision")
    _copy_if_set(target, source, "reading_priority_reason")
    _copy_if_set(target, source, "estimated_reading_time")
    _copy_if_set(target, source, "core_problem")
    _copy_if_set(target, source, "method_overview")
    _copy_if_set(target, source, "key_innovation")
    _copy_if_set(target, source, "implementation_details")
    _copy_if_set(target, source, "evidence_strength")
    _copy_if_set(target, source, "evidence_notes")
    _copy_if_set(target, source, "implementation_feasibility")
    _copy_if_set(target, source, "code_availability")
    _copy_if_set(target, source, "dataset_availability")
    if source.reproducibility_signals:
        target.reproducibility_signals = source.reproducibility_signals
    if source.core_modules:
        target.core_modules = source.core_modules
    if source.transferable_modules:
        target.transferable_modules = source.transferable_modules
    if source.technical_terms:
        target.technical_terms = source.technical_terms
    _copy_if_set(target, source, "results")
    if source.future_improvements:
        target.future_improvements = source.future_improvements
    _copy_if_set(target, source, "project_inspiration")
    if source.reading_questions:
        target.reading_questions = source.reading_questions
    if source.skip_risks:
        target.skip_risks = source.skip_risks
    if source.image_guidance:
        target.image_guidance = source.image_guidance
    if source.risks:
        target.risks = source.risks


def _copy_if_set(target: RerankResult, source: RerankResult, field: str) -> None:
    src_val = getattr(source, field, None)
    if src_val is not None and src_val != "" and src_val != []:
        setattr(target, field, src_val)
