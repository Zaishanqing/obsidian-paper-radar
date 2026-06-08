from __future__ import annotations

import json
import logging
from typing import Any

from .deepseek_client import DeepSeekClient
from .models import Paper, RerankResult
from .rerank import _list_of_dicts

logger = logging.getLogger(__name__)


# ── 各正文字段的写作要求（schema 与分组/补充生成共用，避免重复维护）──
_NOTE_TEXT_FIELD_SPECS: dict[str, str] = {
    "summary_zh": "3-5段，给科研入门选手说明背景、问题、方法直觉、结论；不要只写一句话，也不要堆术语",
    "core_problem": "至少2段：问题是什么、为什么难、现有方法哪里不够；先用普通中文解释，再补必要术语",
    "method_overview": "至少3段：总体思路、架构图引用（available_images 非空时必须嵌入 ![[assets/图名]]）、核心流程、关键假设；先讲直觉，再讲机制",
    "implementation_details": "至少3段：数据流、训练/推理流程、使用的技术、底层原理解释；按步骤写，术语后补一句白话解释；如有流程/数据流图片，在相关段落就近引用",
    "key_innovation": "至少2段：创新点与为什么有效；用易懂语言解释新在哪里，不要只罗列方法名",
    "results": "至少2段：主要结果、实验设置、证据是否充分；说明结果意味着什么",
    "project_inspiration": "至少2段：可以迁移到用户项目的模块、迁移方式、注意事项；写成能照着思考的建议",
    "evidence_notes": "至少1段：证据强弱与可信度；用简单话说明哪些结论可靠、哪些还要谨慎",
}

_NOTE_DICT_FIELD_SPECS: dict[str, list[dict[str, str]]] = {
    "core_modules": [{"name": "模块名", "role": "作用", "input": "输入", "output": "输出", "why_needed": "为什么需要，至少2句"}],
    "transferable_modules": [{"name": "模块名", "role": "解决什么问题", "input": "输入", "output": "输出", "why_it_works": "原理", "possible_transfer": "迁移方向", "how_to_adapt": "怎么改造"}],
    "technical_terms": [{"term": "术语", "plain_explanation": "通俗解释", "role_in_paper": "论文中的作用", "need_to_remember": "true|false"}],
}

_NOTE_LIST_FIELD_SPECS: dict[str, list[str]] = {
    "future_improvements": ["具体可改进点"],
    "reading_questions": ["待读问题"],
    "risks": ["风险或局限"],
}

# ── E4：Map-Reduce 分组生成。每组聚焦 3-5 个字段，注意力集中、质量更高。──
# (分组名, 调用模型, 字段列表)
_NOTE_FIELD_GROUPS: list[tuple[str, str, list[str]]] = [
    ("问题与方法", "pro", ["summary_zh", "core_problem", "method_overview", "key_innovation"]),
    ("实现与证据", "pro", ["implementation_details", "core_modules", "results", "evidence_notes"]),
    ("迁移与启发", "pro", ["project_inspiration", "transferable_modules", "future_improvements"]),
    ("补充字段", "fast", ["technical_terms", "reading_questions", "risks"]),
]

# 传给后续分组做上下文，保证各组术语/口径一致。
_GROUP_CONTEXT_FIELDS = (
    "summary_zh", "core_problem", "method_overview", "key_innovation",
    "implementation_details", "results", "project_inspiration",
)

# ── E5：精读字段最低质量门槛（字符数, 提示语）──
_NOTE_QUALITY_CHECKS: dict[str, tuple[int, str]] = {
    "core_problem": (200, "核心问题内容偏短"),
    "method_overview": (300, "方法概览内容偏短"),
    "implementation_details": (200, "实现细节内容偏短"),
    "key_innovation": (150, "关键创新内容偏短"),
    "results": (150, "结果内容偏短"),
    "project_inspiration": (150, "项目启发内容偏短"),
}

# ── E3：注入系统提示的 TOP 硬性写作规则（最重要、必须优先遵守）──
_HARD_RULES = (
    "以下是必须优先遵守的硬性规范（obsidian_style_specs 是完整规范，这里是其中最关键的几条）：\n"
    "1. 正文全部使用中文，面向科研入门选手：先用普通中文讲清直觉、问题和用途，再保留必要英文术语"
    "（如 attention mechanism、transformer）。\n"
    "2. 禁止对话语气（如“我们可以看到”“值得注意的是”“接下来”），用书面、客观、分析性的表达。\n"
    "3. 控制段落长度：每段 3-5 句、只讲一个点；同一字段内容较多时，用 ### 小标题或 - 列表把它拆成几块，"
    "禁止在一个标题下堆一整坨长段落，也禁止一句话或一行就交差。\n"
    "4. 不能只复述摘要，必须结合 fulltext_context 做深度分析，给出论文里的具体做法、参数、数据流；"
    "但解释顺序必须是“白话直觉 → 关键术语 → 具体机制”。\n"
    "5. 必须解释“为什么”：为什么这个问题难、为什么这个方法有效、为什么这样设计。\n"
    "6. 涉及已有概念/方法时使用 Obsidian wikilink 形式 [[名称]]，方便织入知识图谱；"
    "生僻概念或缩写第一次出现时必须补一句通俗解释。\n"
    "7. 全文上下文不足时必须明确写出证据不足之处，再基于已有信息做谨慎分析，不能用空泛短句填充。\n"
    "8. core_modules / transferable_modules / technical_terms 要落到模块级别，给出输入输出、作用、为什么需要。\n"
    "9. project_inspiration 必须具体到可迁移的模块与改造方式，而非泛泛而谈“有启发”。\n"
    "10. Markdown 排版：字段内如用 ## / ### 小标题，标题与正文之间空一行；列表（- 或 1.）与上一行之间空一行。"
    "标题层级最多到 ###，不跳级。\n"
    "11. 可枚举的内容（步骤、模块要点、对比、局限、迁移要点、待读问题）一律用列表呈现，不要塞进一个长句。\n"
    "12. 数学公式：行内用 $...$，独立公式用 $$...$$（$$ 单独成行），且每个公式后配一句中文说明它表示什么；"
    "禁止用 \\(...\\) 或 \\[...\\]。代码块必须标注语言（如 ```python）。\n"
    "13. 禁止 emoji、装饰性符号、寒暄语，以及“本笔记由 AI 生成”“作为 AI”等元信息。\n"
    "14. 图文并茂：available_images 非空时，method_overview 必须就近引用一张架构/机制/流程图；"
    "implementation_details 和 core_modules 如解释数据流、流程或模块关系，也要在对应段落就近引用相关图片。\n"
    "15. 禁止术语堆叠：同一句不要连续塞入多个英文缩写、模型名或抽象概念；如果不可避免，拆成列表并逐条解释。\n"
    "16. required_note_schema 只规定 JSON 字段结构，不是要写进笔记正文的内容模板；不要把 role、input、plain_explanation、need_to_remember 等字段名写进正文。"
    "图片选择指导只用于程序选图，不要在正文里单独写“图示选择指导”或列出 should_use_images、preferred_types、avoid_types 等内部字段。"
)

# ── E6：生成前自检清单 ──
_SELF_CHECK = (
    "输出 JSON 前必须逐条自检，不满足就重写对应字段直到满足：\n"
    "1. summary_zh 是否 3-5 段（不是一句话）？\n"
    "2. core_problem 是否至少 2 段且解释了“为什么难”？\n"
    "3. method_overview 是否至少 3 段且包含总体思路、核心流程、关键假设？\n"
    "4. implementation_details 是否至少 3 段且包含数据流、训练/推理流程、底层原理？\n"
    "5. key_innovation 是否至少 2 段且解释了“为什么有效”？\n"
    "6. results 是否至少 2 段且说明实验设置与证据是否充分？\n"
    "7. project_inspiration 是否具体到模块级别而非泛泛而谈？\n"
    "8. 全文是否适合科研入门选手浏览：先讲人话，再讲术语，且无对话语气？\n"
    "9. 有没有“一个标题下一整坨长段落”？若有，拆成多段 / ### 小标题 / 列表，每段不超过 5 句。\n"
    "10. 字段内的 ## / ### 标题后、列表前是否都空了一行？\n"
    "11. 数学公式是否用 $ 或 $$ 包裹并配中文说明？代码块是否标注语言？\n"
    "12. available_images 非空时，method_overview 是否已经包含 ![[assets/...]] 图片引用，且图片紧贴对应解释文字？\n"
    "13. 是否存在未解释的生僻术语、缩写堆叠或晦涩长句？若有，改成短句或列表。"
)


def generate_detailed_notes(
    papers: list[Paper],
    results: list[RerankResult],
    profile: dict[str, Any],
    client: DeepSeekClient | None,
    *,
    style_specs: str = "",
    grouped: bool = True,
    harmonize: bool = False,
    image_paths: dict[str, list[str]] | None = None,
) -> None:
    if client is None or not papers:
        return
    result_map = {result.paper_id: result for result in results}
    for paper in papers:
        result = result_map.get(paper.paper_id)
        if result is None:
            continue
        available_images = (image_paths or {}).get(paper.paper_id, [])
        if grouped:
            _generate_grouped(client, paper, result, profile, style_specs, available_images, harmonize=harmonize)
        else:
            _generate_single(client, paper, result, profile, style_specs, available_images)
        # E5：校验精读字段质量，不达标时只对薄弱字段补充生成（用 flash 模型）。
        issues = _validate_note_quality(result)
        if issues:
            logger.info(
                "精读字段质量不足，触发补充生成：%s -> %s",
                paper.paper_id, "、".join(msg for _, msg in issues),
            )
            _supplement_weak_fields(client, paper, result, profile, style_specs, [field for field, _ in issues], available_images)


# ── 单次生成（grouped=False 时的旧路径）────────────────────────────
def _generate_single(
    client: DeepSeekClient,
    paper: Paper,
    result: RerankResult,
    profile: dict[str, Any],
    style_specs: str,
    available_images: list[str],
) -> None:
    all_fields = list(_NOTE_TEXT_FIELD_SPECS) + list(_NOTE_DICT_FIELD_SPECS) + list(_NOTE_LIST_FIELD_SPECS)
    messages = _build_field_messages(
        paper, result, profile, style_specs, all_fields, available_images,
        task="生成完整的中文精读笔记字段。",
        include_self_check=True,
    )
    item = _call_note_with_fallback(client, messages, paper.paper_id, primary="pro", fallback="fast")
    if item is not None:
        _merge_note_fields(result, item)


# ── E4：分组（Map-Reduce）生成 ─────────────────────────────────────
def _generate_grouped(
    client: DeepSeekClient,
    paper: Paper,
    result: RerankResult,
    profile: dict[str, Any],
    style_specs: str,
    available_images: list[str],
    *,
    harmonize: bool,
) -> None:
    any_success = False
    for name, model, fields in _NOTE_FIELD_GROUPS:
        fallback = "fast" if model == "pro" else None
        messages = _build_field_messages(
            paper, result, profile, style_specs, fields, available_images,
            task=f"本次只生成「{name}」分组的字段，集中精力把这几项写深写透。已写好的字段见 already_written，保持术语与口径一致。",
            include_self_check=(model == "pro"),
        )
        item = _call_note_with_fallback(client, messages, paper.paper_id, primary=model, fallback=fallback)
        if item is not None:
            _merge_note_fields(result, item)
            any_success = True
        else:
            logger.warning("精读分组「%s」生成失败：%s", name, paper.paper_id)
    if any_success and harmonize:
        _harmonize_note(client, paper, result, profile, style_specs)


def _harmonize_note(
    client: DeepSeekClient,
    paper: Paper,
    result: RerankResult,
    profile: dict[str, Any],
    style_specs: str,
) -> None:
    """E4 收尾：一次轻量一致性整合，仅统一术语/消除矛盾，不缩短或删减内容。"""
    text_fields = [f for f in _NOTE_TEXT_FIELD_SPECS if (getattr(result, f, "") or "").strip()]
    if len(text_fields) < 2:
        return
    user = {
        "obsidian_style_specs": style_specs,
        "task": "下列精读字段由多组分别生成，请只做一致性润色：统一术语与符号、消除前后矛盾与重复表述，并把晦涩长句改成科研入门选手也能看懂的短句。必须保持每个字段原有的深度与篇幅，不得缩短或删除内容。",
        "fields": {field: getattr(result, field) for field in text_fields},
        "required_note_schema": {"note": {field: _NOTE_TEXT_FIELD_SPECS[field] for field in text_fields}},
    }
    system = (
        "你是中文论文精读笔记的编辑，只做一致性润色：统一术语、消除矛盾与重复，绝不缩短或删除内容。"
        "语言要精炼易懂，先讲普通中文直觉，再保留必要术语；不要堆缩写和概念。"
        "只返回合法 JSON，不要 Markdown 围栏。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
    item = _call_note_with_fallback(client, messages, paper.paper_id, primary="fast", fallback=None)
    if not item:
        return
    # 防止整合反而丢内容：只合并长度不低于原文 70% 的字段。
    safe: dict[str, Any] = {}
    for field in text_fields:
        new_val = str(item.get(field, "")).strip()
        old_len = len((getattr(result, field, "") or "").strip())
        if new_val and len(new_val) >= 0.7 * old_len:
            safe[field] = new_val
    if safe:
        _merge_note_fields(result, safe)


def _call_note_with_fallback(
    client: DeepSeekClient,
    messages: list[dict[str, str]],
    paper_id: str,
    *,
    primary: str = "pro",
    fallback: str | None = "fast",
) -> dict[str, Any] | None:
    """A4：primary 调用失败时降级为 fallback，都失败才放弃。"""
    try:
        payload = client.chat_json(messages, model=primary)
    except Exception as primary_exc:
        if fallback and fallback != primary:
            logger.warning("%s 模型生成精读失败，降级为 %s：%s（%s）", primary, fallback, paper_id, primary_exc)
            try:
                payload = client.chat_json(messages, model=fallback)
            except Exception as fb_exc:
                logger.warning("%s 降级也失败，跳过：%s（%s）", fallback, paper_id, fb_exc)
                return None
        else:
            logger.warning("%s 模型生成精读失败，跳过：%s（%s）", primary, paper_id, primary_exc)
            return None
    item = payload.get("note", payload) if isinstance(payload, dict) else {}
    if not isinstance(item, dict):
        logger.warning("note payload 不是对象，跳过：%s", paper_id)
        return None
    return item


def _schema_for_fields(fields: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    for field in fields:
        if field in _NOTE_TEXT_FIELD_SPECS:
            schema[field] = _NOTE_TEXT_FIELD_SPECS[field]
        elif field in _NOTE_DICT_FIELD_SPECS:
            schema[field] = _NOTE_DICT_FIELD_SPECS[field]
        elif field in _NOTE_LIST_FIELD_SPECS:
            schema[field] = _NOTE_LIST_FIELD_SPECS[field]
    return schema


def _collect_written_context(result: RerankResult, target_fields: list[str]) -> dict[str, str]:
    """收集已写好的主字段，作为后续分组的一致性上下文（排除本组待写字段）。"""
    written: dict[str, str] = {}
    for field in _GROUP_CONTEXT_FIELDS:
        if field in target_fields:
            continue
        value = (getattr(result, field, "") or "").strip()
        if value:
            written[field] = value
    return written


def _build_field_messages(
    paper: Paper,
    result: RerankResult,
    profile: dict[str, Any],
    style_specs: str,
    fields: list[str],
    available_images: list[str],
    *,
    task: str,
    include_self_check: bool,
) -> list[dict[str, str]]:
    user = {
        "profile": {
            "long_term_interests": profile.get("long_term_interests", []),
            "current_projects": profile.get("current_projects", []),
            "selection_policy": profile.get("selection_policy", {}),
        },
        # E2：完整传递写作规范，不做关键词过滤、不截断。
        "obsidian_style_specs": style_specs,
        "paper": _paper_to_note_prompt_dict(paper),
        "rerank_result": {
            "recommend_score": result.recommend_score,
            "decision": result.decision,
            "action": result.action,
            "tags": result.tags,
            "matched_interests": result.matched_interests,
            "why_read": result.why_read,
        },
        "fulltext_context": _compact_fulltext_context(paper.extra_context.get("fulltext_context", {})),
        "available_images": available_images,
        "already_written": _collect_written_context(result, fields),
        "task": task,
        "required_note_schema": {"note": _schema_for_fields(fields)},
    }
    system = (
        "你是严谨的中文论文精读笔记作者。obsidian_style_specs 是硬性写作规范，不是参考材料，必须严格遵守。\n"
        + _HARD_RULES
        + "\n必须基于 fulltext_context、摘要、图注和已有核验信息写深度笔记字段，不能只复述摘要，不能每节只写一句话。"
        + "语言必须精炼易懂，适合科研入门选手浏览；不要为了显得专业而堆术语。"
        + "required_note_schema 是机器读取的 JSON 结构，不是正文模板；正文内容只写可读中文，不要显式写出 schema 字段名。"
        + "图片选择指导是内部控制信息，只用于帮助程序选图，不要把它写成精读正文里的独立小节。"
        + "如果 available_images 非空，必须在 method_overview 中就近嵌入最能解释方法的图片 wikilink（例如 ![[assets/fig1.png]]），"
        + "并在 implementation_details 或 core_modules 的相关段落中引用能解释数据流、流程或模块关系的图片；图片必须贴着解释它的文字出现，不要集中放到文末。"
        + "只生成 required_note_schema 中列出的字段；每个字段要能直接写入 Obsidian 精读笔记。只返回合法 JSON，不要 Markdown 围栏。"
    )
    if include_self_check:
        system += "\n" + _SELF_CHECK
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _validate_note_quality(result: RerankResult) -> list[tuple[str, str]]:
    """校验精读字段是否满足最低质量要求，返回 (字段, 提示语) 列表。"""
    issues: list[tuple[str, str]] = []
    for field, (min_chars, msg) in _NOTE_QUALITY_CHECKS.items():
        if len((getattr(result, field, "") or "").strip()) < min_chars:
            issues.append((field, msg))
    return issues


def _supplement_weak_fields(
    client: DeepSeekClient,
    paper: Paper,
    result: RerankResult,
    profile: dict[str, Any],
    style_specs: str,
    weak_fields: list[str],
    available_images: list[str],
) -> None:
    """E5：只对不达标字段发起一次补充生成（flash 模型），合并回结果。"""
    fields = [f for f in weak_fields if f in _NOTE_TEXT_FIELD_SPECS]
    if not fields:
        return
    user = {
        "profile": {
            "current_projects": profile.get("current_projects", []),
            "long_term_interests": profile.get("long_term_interests", []),
        },
        "obsidian_style_specs": style_specs,
        "paper": _paper_to_note_prompt_dict(paper),
        "fulltext_context": _compact_fulltext_context(paper.extra_context.get("fulltext_context", {})),
        "available_images": available_images,
        "existing_fields": {field: getattr(result, field, "") for field in fields},
        "task": "下列字段内容偏短或不够深入，请基于全文上下文重写这些字段，使其更详尽、更有分析深度。",
        "required_note_schema": {"note": {field: _NOTE_TEXT_FIELD_SPECS[field] for field in fields}},
    }
    system = (
        "你是严谨的中文论文精读笔记作者，正在补写不达标的精读字段。\n"
        + _HARD_RULES
        + "\n只补写 required_note_schema 中列出的字段，内容必须比 existing_fields 更详尽、结合 fulltext_context 做深度分析。"
        + "语言必须精炼易懂，适合科研入门选手浏览；不要堆术语。"
        + "required_note_schema 是机器读取的 JSON 结构，不是正文模板；不要把字段名写进正文。"
        + "只返回合法 JSON，不要 Markdown 围栏。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
    item = _call_note_with_fallback(client, messages, paper.paper_id, primary="fast", fallback=None)
    if item is not None:
        _merge_note_fields(result, item)


def _merge_note_fields(result: RerankResult, item: dict[str, Any]) -> None:
    for field in (
        "summary_zh",
        "core_problem",
        "method_overview",
        "implementation_details",
        "key_innovation",
        "results",
        "project_inspiration",
        "evidence_notes",
    ):
        value = str(item.get(field, "")).strip()
        if value:
            setattr(result, field, value)
    for field in ("future_improvements", "reading_questions", "risks"):
        value = item.get(field)
        if isinstance(value, list) and value:
            setattr(result, field, [str(x) for x in value if str(x).strip()])
    for field in ("core_modules", "transferable_modules", "technical_terms"):
        value = _list_of_dicts(item.get(field, []))
        if value:
            setattr(result, field, value)


def _paper_to_note_prompt_dict(paper: Paper) -> dict[str, Any]:
    extra = paper.extra_context if isinstance(paper.extra_context, dict) else {}
    compact_extra: dict[str, Any] = {}
    for key in (
        "venue",
        "venue_year",
        "decision",
        "has_software_link",
        "code_links",
        "dataset_links",
        "open_source",
        "citation_network",
    ):
        if key in extra:
            compact_extra[key] = extra[key]
    return {
        "id": paper.paper_id,
        "title": paper.title,
        "authors": paper.authors[:8],
        "abstract": paper.abstract,
        "published": paper.published,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "source": paper.source,
        "categories": paper.categories[:10],
        "rough_score": paper.rough_score,
        "matched_interests": paper.matched_interests[:8],
        "citation_count": paper.citation_count,
        "influential_citation_count": paper.influential_citation_count,
        "extra_context": compact_extra,
    }


def _compact_fulltext_context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sections = value.get("sections", {})
    if not isinstance(sections, dict):
        sections = {}
    compact_sections = {}
    for key in ("background", "method", "experiment", "limitation", "body"):
        text = str(sections.get(key, "")).strip()
        if text:
            compact_sections[key] = text
    return {
        "source": str(value.get("source", "")),
        "full_text_chars": int(value.get("full_text_chars") or 0),
        "failed_reason": str(value.get("failed_reason", "")),
        "sections": compact_sections,
    }
