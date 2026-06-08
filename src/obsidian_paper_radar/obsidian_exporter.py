from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

from .models import Paper, RerankResult
from .utils import fallback_note_title, safe_chinese_title, short_paper_suffix, vault_relative

_HEADING_RE = re.compile(r"^#{1,6} \S")
_LIST_RE = re.compile(r"^\s*([-*+] |\d+\. )")
_DEFAULT_IMAGE_WIDTH = 600    # 读不到尺寸时的回退宽度
_IMAGE_DISPLAY_HEIGHT = 450   # 统一展示高度：按真实比例反算宽度，使渲染高度≈450（不裁剪、不变形）
_MAX_IMAGE_WIDTH = 1000       # 极宽图的宽度上限，避免横向超出笔记栏


def _embed_width(path: Path) -> int:
    """按图片真实比例反算展示宽度，使其在 Obsidian 里渲染高度约为 450px。

    Obsidian 的 ``![[img|宽度]]`` 等比缩放：宽度=450×(宽/高) 时渲染高度正好≈450，
    整张图完整、不裁剪、不变形。极宽图的宽度封顶 1000。读不到尺寸时回退默认宽度。
    """
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        return _DEFAULT_IMAGE_WIDTH
    if not w or not h:
        return _DEFAULT_IMAGE_WIDTH
    width = round(_IMAGE_DISPLAY_HEIGHT * w / h)
    return max(200, min(width, _MAX_IMAGE_WIDTH))
_TOPIC_ZH = {
    "agent": "智能体",
    "agents": "智能体",
    "rag": "检索增强生成",
    "retrieval": "信息检索",
    "multimodal": "多模态学习",
    "vision": "计算机视觉",
    "language": "自然语言处理",
    "llm": "大语言模型",
    "alignment": "模型对齐",
    "reasoning": "推理能力",
    "planning": "规划决策",
    "benchmark": "评测基准",
    "dataset": "数据集构建",
    "robot": "机器人",
    "medical": "医学人工智能",
    "graph": "图学习",
    "recommendation": "推荐系统",
}

_VALUE_ZH = {
    "true": "是",
    "false": "否",
    "yes": "是",
    "no": "否",
    "high": "高",
    "medium": "中",
    "low": "低",
    "strong": "强",
    "weak": "弱",
    "unknown": "未知",
    "not_found": "未找到",
    "available": "已开源",
    "mentioned": "论文提到",
    "method": "方法论文",
    "benchmark": "基准评测",
    "survey": "综述",
    "system": "系统论文",
    "theory": "理论论文",
    "dataset": "数据集论文",
    "application": "应用论文",
    "architecture": "架构图",
    "mechanism": "机制图",
    "pipeline": "流程图",
    "framework": "框架图",
    "workflow": "工作流图",
    "benchmark_table": "基准表格",
    "ablation_chart": "消融实验图",
    "result_chart": "结果图",
    "example": "样例图",
    "table": "表格",
    "other": "其他",
}

_DICT_KEY_ZH = {
    "description": "说明",
    "function": "功能",
    "purpose": "用途",
    "reason": "理由",
    "role": "作用",
    "input": "输入",
    "output": "输出",
    "why_needed": "为什么需要",
    "why_it_works": "为什么有效",
    "possible_transfer": "可迁移方向",
    "how_to_adapt": "如何改造",
    "plain_explanation": "通俗解释",
    "role_in_paper": "在论文中的作用",
    "need_to_remember": "是否需要记住",
}


def _chinese_topic_label(tag: str) -> str:
    text = str(tag).strip()
    lowered = text.lower()
    for key, value in _TOPIC_ZH.items():
        if key in lowered:
            return value
    return text


def _plain_daily_text(value: str) -> str:
    text = str(value or "").strip()
    if not text or text == "--":
        return ""
    text = re.sub(r"!\[\[[^\]]+\]\]", "", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first_nonempty(*values: str) -> str:
    for value in values:
        compact = _plain_daily_text(value)
        if compact:
            return compact
    return ""


def _meaningful_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"--", "unknown", "not_found", "not found", "none", "null", "n/a", "na"}:
        return ""
    return text


def _display_value(value: object) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    return _VALUE_ZH.get(lowered, text)


def _display_list(values: object) -> str:
    if isinstance(values, list):
        return "、".join(_display_value(item) for item in values if _display_value(item))
    if isinstance(values, str):
        parts = [part.strip() for part in re.split(r"[,，、]\s*", values) if part.strip()]
        if len(parts) > 1:
            return "、".join(_display_value(part) for part in parts if _display_value(part))
    return _display_value(values)


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    return bool(str(value).strip())


def _strip_overview_heading(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    while lines and re.match(r"^\s*#{1,6}\s*今日概览\s*$", lines[0].strip()):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _normalize_markdown(text: str) -> str:
    """确定性修正 Markdown 排版：标题后补空行、列表前补空行，规避 LLM/模板把内容
    紧贴在标题或上一段下面导致 Obsidian 渲染异常。跳过 YAML frontmatter 与代码块，
    避免误伤。这是排版的兜底，不依赖 LLM 是否遵守提示里的排版规则。
    """
    # 跳过开头的 YAML frontmatter，原样保留（其内的 "  - tag" 不能被当成列表处理）。
    front = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            front = text[: end + 5]
            body = text[end + 5 :]

    lines = body.split("\n")
    result: list[str] = []
    in_code = False
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            result.append(line)
            continue
        if in_code:
            result.append(line)
            continue
        # 列表项前补空行：上一行非空、且不是列表项、不是标题。
        if _LIST_RE.match(line) and result:
            prev = result[-1]
            if prev.strip() and not _LIST_RE.match(prev) and not prev.lstrip().startswith("#"):
                result.append("")
        result.append(line)
        # 标题后补空行：下一行非空时插入。
        if _HEADING_RE.match(line):
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if nxt.strip():
                result.append("")

    normalized = re.sub(r"\n{3,}", "\n\n", "\n".join(result))
    return front + normalized


class ObsidianExporter:
    def __init__(
        self,
        vault_path: Path,
        daily_dir: str,
        paper_dir: str,
        link_processor: Callable[[str, set[str]], str] | None = None,
    ) -> None:
        self.vault_path = vault_path
        self.daily_dir = daily_dir
        self.paper_dir = paper_dir
        self.link_processor = link_processor

    def day_slug(self, run_date: date) -> str:
        return run_date.strftime("%m-%d")

    def daily_path(self, run_date: date) -> Path:
        return self.vault_path / self.daily_dir / f"{self.day_slug(run_date)} 论文日报.md"

    def note_title(self, paper: Paper, result: RerankResult) -> str:
        title = result.note_title_zh.strip() or fallback_note_title(result.summary_zh or paper.abstract, paper.title)
        return safe_chinese_title(title)

    def note_dir(self, paper: Paper, result: RerankResult, run_date: date, used_titles: set[str] | None = None) -> Path:
        title = self.note_title(paper, result)
        if used_titles is not None:
            if title in used_titles:
                title = f"{title}_{short_paper_suffix(paper.paper_id)}"
            used_titles.add(title)
        return self.vault_path / self.paper_dir / self.day_slug(run_date) / title

    def note_path(self, paper: Paper, result: RerankResult, run_date: date, used_titles: set[str] | None = None) -> Path:
        note_dir = self.note_dir(paper, result, run_date, used_titles)
        return note_dir / f"{note_dir.name}.md"

    def asset_path(self, paper: Paper, result: RerankResult, run_date: date, used_titles: set[str] | None = None) -> Path:
        return self.note_dir(paper, result, run_date, used_titles) / "assets"

    def build_note_paths(self, papers: list[Paper], results: list[RerankResult], run_date: date, top_n: int) -> dict[str, Path]:
        paper_map = {p.paper_id: p for p in papers}
        used: set[str] = set()
        note_paths: dict[str, Path] = {}
        for result in results[:top_n]:
            paper = paper_map[result.paper_id]
            note_paths[paper.paper_id] = self.note_path(paper, result, run_date, used)
        return note_paths

    def build_asset_paths(self, note_paths: dict[str, Path]) -> dict[str, Path]:
        return {paper_id: note_path.parent / "assets" for paper_id, note_path in note_paths.items()}

    def render_daily(
        self,
        papers: list[Paper],
        results: list[RerankResult],
        run_date: date,
        note_paths: dict[str, Path],
        daily_overview: str = "",
        image_paths: dict[str, list[Path]] | None = None,
    ) -> str:
        paper_map = {p.paper_id: p for p in papers}
        day = self.day_slug(run_date)
        lines = [
            "---",
            f'date: "{run_date.isoformat()}"',
            'tags: ["daily-paper", "deepseek", "llm-generated"]',
            f"paper_count: {len(results)}",
            "---",
            "",
            "## 今日概览",
            "",
        ]
        overview = _strip_overview_heading(daily_overview)
        if overview:
            lines.append(overview)
        elif results:
            top_tags = []
            for result in results:
                top_tags.extend(result.tags or result.matched_interests)
            tag_text = "、".join(_chinese_topic_label(tag) for tag in list(dict.fromkeys(top_tags))[:8]) or "当前研究兴趣"
            lines.append(f"今日筛选出 {len(results)} 篇推荐论文，主要集中在 {tag_text}。")
        else:
            lines.append("今天没有符合筛选条件的论文。")

        lines.extend(["", "## 推荐列表", ""])
        for idx, result in enumerate(results, 1):
            paper = paper_map[result.paper_id]
            note_path = note_paths.get(paper.paper_id)
            note_link = vault_relative(note_path.with_suffix(""), self.vault_path) if note_path else ""
            title_link = f"[[{note_link}|{self.note_title(paper, result)}]]" if note_link else self.note_title(paper, result)
            daily_image = self._daily_image_embed(paper.paper_id, image_paths or {})
            summary = _first_nonempty(result.daily_one_sentence_zh, result.summary_zh, paper.abstract)
            contribution = _first_nonempty(
                result.daily_core_contribution_zh,
                result.key_innovation,
                result.method_overview,
            )
            why_read = _first_nonempty(result.why_read, result.project_relevance, result.reading_priority_reason)
            inspiration = _first_nonempty(result.project_inspiration, result.project_relevance)
            core_points = [_plain_daily_text(p) for p in (result.daily_core_points or []) if _meaningful_text(p)]
            key_results = _first_nonempty(result.daily_key_results, result.results)
            modules = self._inline_modules(result.core_modules) or _plain_daily_text(result.key_innovation)
            lines.extend([f"### {idx}. {title_link}", f"- **原题**：{paper.title}", f"- **推荐分**：{result.recommend_score:.1f}/10"])
            paper_type = _meaningful_text(result.paper_type)
            if paper_type:
                lines.append(f"- **论文类型**：{_display_value(paper_type)}")
            reading_decision = _meaningful_text(result.reading_decision)
            reading_time = _meaningful_text(result.estimated_reading_time)
            if reading_decision or reading_time:
                suffix = f"（{reading_time}）" if reading_time else ""
                lines.append(f"- **阅读建议**：{reading_decision}{suffix}")
            authors = ", ".join(paper.authors[:6]).strip()
            if authors:
                lines.append(f"- **作者**：{authors}")
            source = _meaningful_text(paper.source)
            if source:
                lines.append(f"- **来源**：{source}")
            published = _meaningful_text(paper.published)
            if published:
                lines.append(f"- **发布日期**：{published}")
            links = []
            if _meaningful_text(paper.url):
                links.append(f"[Paper]({paper.url})")
            if _meaningful_text(paper.pdf_url):
                links.append(f"[PDF]({paper.pdf_url})")
            if links:
                lines.append(f"- **链接**：{' | '.join(links)}")
            tags = [tag for tag in (result.tags or result.matched_interests)[:6] if _meaningful_text(tag)]
            if tags:
                lines.append(f"- **标签**：{' '.join(f'[[{tag}]]' for tag in tags)}")
            lines.append("")
            if summary:
                lines.extend([f"**一句话总结**：{summary}", ""])
            if daily_image:
                lines.extend([daily_image, ""])
            if why_read:
                lines.extend([f"**看点**：{why_read}", ""])
            if core_points:
                lines.append("**核心贡献/观点**：")
                lines.append("")
                lines.extend(f"- {point}" for point in core_points)
                lines.append("")
            elif contribution:
                lines.extend([f"**核心贡献**：{contribution}", ""])
            if key_results:
                lines.extend([f"**关键结果**：{key_results}", ""])
            if modules and not core_points:
                lines.extend([f"**创新模块**：{modules}", ""])
            if inspiration:
                lines.extend([f"**项目启发**：{inspiration}", ""])
            open_parts = []
            code_status = _meaningful_text(result.code_availability)
            dataset_status = _meaningful_text(result.dataset_availability)
            if code_status:
                open_parts.append(f"代码：{_display_value(code_status)}")
            if dataset_status:
                open_parts.append(f"数据集：{_display_value(dataset_status)}")
            if open_parts:
                lines.extend([f"**开源/数据**：{'；'.join(open_parts)}", ""])
            risks = [risk for risk in result.risks if _meaningful_text(risk)]
            if risks:
                lines.extend(["**风险/局限**：", *[f"- {risk}" for risk in risks], ""])

        # ── 快速反馈复选框 ──
        lines.extend(
            [
                "---",
                "",
                "## 快速反馈",
                "",
                "勾选 = 不想再看到这个方向（默认全部保留，不勾不影响）。",
                "",
            ]
        )
        for result in results:
            paper = paper_map[result.paper_id]
            note_path = note_paths.get(paper.paper_id)
            note_link = vault_relative(note_path.with_suffix(""), self.vault_path) if note_path else ""
            display = self.note_title(paper, result)
            title_display = f"[[{note_link}|{display}]]" if note_link else display
            # 不放标签也不放机器标记：本区块内勾选即记为「不想再看」；脚本通过 wikilink→笔记
            # frontmatter 的 paper_id 反查，无笔记的论文用中文标题在 seen_papers 反查。
            lines.append(f"- [ ] {title_display}")
        lines.append("")
        return _normalize_markdown("\n".join(lines)).rstrip() + "\n"

    def write_daily(
        self,
        papers: list[Paper],
        results: list[RerankResult],
        run_date: date,
        note_paths: dict[str, Path],
        daily_overview: str = "",
        image_paths: dict[str, list[Path]] | None = None,
    ) -> Path:
        path = self.daily_path(run_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self.render_daily(papers, results, run_date, note_paths, daily_overview, image_paths)
        if self.link_processor:
            content = self.link_processor(content, set())
        path.write_text(content, encoding="utf-8")
        return path

    def write_paper_notes(
        self,
        papers: list[Paper],
        results: list[RerankResult],
        run_date: date,
        note_paths: dict[str, Path],
        image_paths: dict[str, list[Path]],
    ) -> dict[str, str]:
        paper_map = {p.paper_id: p for p in papers}
        written: dict[str, str] = {}
        for result in results:
            if result.paper_id not in note_paths:
                continue
            paper = paper_map[result.paper_id]
            path = note_paths[result.paper_id]
            path.parent.mkdir(parents=True, exist_ok=True)
            rel_path = vault_relative(path.with_suffix(""), self.vault_path)
            if not path.exists():
                content = self.render_paper_note(paper, result, run_date, image_paths.get(paper.paper_id, []))
                if self.link_processor:
                    content = self.link_processor(content, {rel_path})
                path.write_text(content, encoding="utf-8")
            written[paper.paper_id] = rel_path
        return written

    def render_paper_note(self, paper: Paper, result: RerankResult, run_date: date, images: list[Path]) -> str:
        tags = result.tags or result.matched_interests or ["paper"]
        tags_yaml = "\n".join(f"  - {safe_chinese_title(tag)}" for tag in tags[:8])
        method_overview = self._with_inline_method_image(result.method_overview or "--", images, result.image_guidance)
        questions = result.reading_questions or ["这篇论文的方法是否能迁移到当前项目？", "它的实验设置是否足以支持结论？"]
        questions_md = "\n".join(f"- {q}" for q in questions)
        core_modules_md = self._render_dict_list(
            result.core_modules,
            ["name", "role", "input", "output", "why_needed"],
            "暂无明确模块拆解。",
        )
        transferable_md = self._render_dict_list(
            result.transferable_modules,
            ["name", "role", "input", "output", "why_it_works", "possible_transfer", "how_to_adapt"],
            "暂无明确可迁移模块。",
        )
        terms_md = self._render_dict_list(
            result.technical_terms,
            ["term", "plain_explanation", "role_in_paper", "need_to_remember"],
            "暂无必须解释的陌生术语。",
        )
        reproducibility = "\n".join(f"- {item}" for item in result.reproducibility_signals) or "- --"
        improvements = "\n".join(f"- {item}" for item in result.future_improvements) or "- --"
        skip_risks = "\n".join(f"- {item}" for item in result.skip_risks) or "- --"
        paper_type = _display_value(result.paper_type) or "--"
        evidence_strength = _display_value(result.evidence_strength) or "--"
        implementation_feasibility = _display_value(result.implementation_feasibility) or "--"
        code_availability = _display_value(result.code_availability) or "未知"
        dataset_availability = _display_value(result.dataset_availability) or "未知"
        content = f"""---
date: "{run_date.isoformat()}"
paper_id: "{paper.paper_id}"
title: "{paper.title}"
note_title: "{self.note_title(paper, result)}"
source: "{paper.source}"
status: unread
recommend_score: {result.recommend_score:.1f}
tags:
{tags_yaml}
---

## 基本信息
- **原题**：{paper.title}
- **作者**：{', '.join(paper.authors) or '--'}
- **发布日期**：{paper.published or '--'}
- **链接**：[Paper]({paper.url}){f' | [PDF]({paper.pdf_url})' if paper.pdf_url else ''}
- **推荐分**：{result.recommend_score:.1f}/10
- **论文类型**：{paper_type}
- **阅读建议**：{result.reading_decision or '--'}{f'（预计 {result.estimated_reading_time}）' if result.estimated_reading_time else ''}
- **代码开源情况**：{code_availability}
- **数据集开放情况**：{dataset_availability}

## 为什么进入今日推荐
{result.why_read or result.project_relevance or '--'}

{result.reading_priority_reason or ''}

## 摘要
{result.summary_zh or paper.abstract}

## 核心问题
{result.core_problem or '--'}

## 方法概览
{method_overview}

## 核心模块
{core_modules_md}

## 具体实现
{result.implementation_details or '--'}

## 关键创新
{result.key_innovation or '--'}

## 结果与证据强度
- **主要结果**：{result.results or '--'}
- **证据强度**：{evidence_strength}
- **证据说明**：{result.evidence_notes or '--'}

## 可复现与落地
- **实现可行性**：{implementation_feasibility}
- **代码开源情况**：{code_availability}
- **数据集开放情况**：{dataset_availability}

### 可复现信号
{reproducibility}

## 项目启发
{result.project_inspiration or result.project_relevance or '--'}

## 引用网络
{self._render_citation_network(paper)}

## 可迁移模块
{transferable_md}

## 风险与局限
{chr(10).join(f'- {risk}' for risk in result.risks) if result.risks else '- --'}

## 可以改进的方向
{improvements}

## 跳过或暂缓精读的风险
{skip_risks}

## 陌生术语与方法解释
{terms_md}

## 待读问题
{questions_md}

## 我的笔记

"""
        return _normalize_markdown(content)

    def _render_images(self, images: list[Path], guidance: dict[str, object]) -> str:
        existing = [path for path in images if path.exists()]
        if not existing:
            return "未提取到图片。"
        reason = str(guidance.get("selection_reason", "")) if guidance else ""
        if not reason:
            reason = "这张图被选入是因为它更可能展示论文的方法、机制、架构或流程，而不是单纯实验结果。"
        blocks = []
        for path in existing:
            blocks.append(f"![[assets/{path.name}]]\n\n图示说明：{reason}")
        return "\n\n".join(blocks)

    def _daily_image_embed(self, paper_id: str, image_paths: dict[str, list[Path]]) -> str:
        images = [path for path in image_paths.get(paper_id, []) if path.exists()]
        if not images:
            return ""
        rel = vault_relative(images[0], self.vault_path)
        return f"![[{rel}|{_embed_width(images[0])}]]"

    def _with_inline_method_image(self, text: str, images: list[Path], guidance: dict[str, object]) -> str:
        if "![[" in text:
            return text
        existing = [path for path in images if path.exists()]
        if not existing:
            return text
        reason = str(guidance.get("selection_reason", "")).strip() if guidance else ""
        if not reason:
            reason = "这张图更可能展示论文的方法、机制、架构或流程，可辅助理解方法概览。"
        return f"{text.rstrip()}\n\n![[assets/{existing[0].name}|{_embed_width(existing[0])}]]\n\n图示说明：{reason}"

    def _render_citation_network(self, paper: Paper) -> str:
        network = paper.extra_context.get("citation_network") if isinstance(paper.extra_context, dict) else None
        if not isinstance(network, dict):
            return "暂无引用网络数据。"
        sections: list[str] = []
        references = network.get("references") or []
        citations = network.get("citations") or []
        if references:
            sections.append("**站在哪些工作上（关键参考）**：")
            sections.extend(self._citation_line(item) for item in references)
        if citations:
            if sections:
                sections.append("")
            sections.append("**谁在引用它（后续工作）**：")
            sections.extend(self._citation_line(item) for item in citations)
        return "\n".join(sections) if sections else "暂无引用网络数据。"

    def _citation_line(self, item: dict[str, object]) -> str:
        title = str(item.get("title") or "").strip() or "（无标题）"
        year = item.get("year")
        cites = item.get("citations")
        meta = []
        if year:
            meta.append(str(year))
        if isinstance(cites, int) and cites > 0:
            meta.append(f"{cites}引")
        suffix = f"（{' · '.join(meta)}）" if meta else ""
        url = str(item.get("url") or "").strip()
        linked = f"[{title}]({url})" if url else title
        return f"- {linked}{suffix}"

    def _inline_modules(self, modules: list[dict[str, str]]) -> str:
        names = [module.get("name", "") for module in modules if module.get("name")]
        return "、".join(names[:4])

    def _render_dict_list(self, items: list[dict[str, str]], keys: list[str], empty: str) -> str:
        if not items:
            return empty
        lines: list[str] = []
        for idx, item in enumerate(items, 1):
            title = item.get("name") or item.get("term") or f"条目 {idx}"
            lines.append(f"### {title}")
            for key in keys:
                if key in {"name", "term"}:
                    continue
                value = item.get(key)
                if _has_value(value):
                    label = _DICT_KEY_ZH.get(key, key)
                    lines.append(f"- **{label}**：{_display_value(value)}")
            lines.append("")
        return "\n".join(lines).strip()
