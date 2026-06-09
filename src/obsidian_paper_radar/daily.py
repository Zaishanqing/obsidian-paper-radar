from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .citation_network import enrich_citation_network
from .config import AppConfig, ROOT
from .dedup import append_run_history, filter_recent_seen, load_seen, save_seen
from .deepseek_client import DeepSeekClient
from .feedback import import_feedback_from_daily, import_feedback_from_vault
from .health import build_run_report, render_report
from .image_utils import extract_images_for_papers
from .models import RunSummary
from .moc import update_mocs
from .note_generator import generate_detailed_notes
from .obsidian_exporter import ObsidianExporter, _normalize_daily_field_text
from .open_source_check import reconcile_results, verify_papers_open_source
from .paper_fulltext import enrich_papers_with_fulltext
from .paper_context import enrich_papers_with_context
from .rerank import rerank_papers
from .search import fetch_candidates
from .skill_specs import load_obsidian_skill_specs
from .utils import safe_chinese_title
from .vault_links import build_note_index, make_link_processor

logger = logging.getLogger(__name__)


def _generate_daily_cards(
    papers: list,
    results: list,
    client: DeepSeekClient | None,
) -> None:
    """G1c：日报摘要 pass。只生成日报短卡片字段，不替代精读笔记。"""
    if client is None or not results:
        return
    paper_map = {paper.paper_id: paper for paper in papers}
    items: list[dict[str, Any]] = []
    for result in results:
        paper = paper_map.get(result.paper_id)
        if paper is None:
            continue
        items.append(
            {
                "paper_id": result.paper_id,
                "title": paper.title,
                "abstract": paper.abstract,
                "score": result.recommend_score,
                "tags": result.tags or result.matched_interests,
                "paper_type": result.paper_type,
                "why_read": result.why_read,
                "summary_zh": result.summary_zh,
                "method_overview": result.method_overview,
                "key_innovation": result.key_innovation,
                "project_relevance": result.project_relevance,
            }
        )
    if not items:
        return
    messages = [
        {
            "role": "system",
            "content": (
                "你是中文科研日报编辑。请为每篇论文生成精炼、易懂的日报卡片字段，只返回合法 JSON。"
                "读者是科研入门选手：先用普通中文讲清直觉、问题和用途，再保留必要英文术语；"
                "不要把多个缩写和概念堆在同一句里，出现生僻术语时必须顺手解释它大概是什么意思。"
                "要求：1) 全部中文，必要专业术语保留英文原名；2) one_sentence_summary_zh 只写 1 句，50-90 字；"
                "3) core_contribution_zh 只写 1 句，60-110 字，说明核心贡献/机制/为什么有用；"
                "4) core_points_zh 给 3-5 条核心贡献/观点要点，每条一个点、20-45 字，尽量用“标签：说明”形式"
                "（如“混合选择性回放：优先采样不确定样本”），从摘要里提炼，不要照抄整句；"
                "5) key_results_zh 用 1 句写关键结果，**尽量带上摘要里的具体数字/指标**（如 F1、提升幅度、加速比），"
                "摘要没有数字就简述主要结论，实在没有就留空；"
                "6) short_title_zh 是可选中文短名，4-18 个汉字或中英混合短语，必须像机制/方法名，不要整句；"
                "7) 不要写 Markdown 标题、客套话、晦涩长句或术语堆叠。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "papers": items,
                    "schema": {
                        "items": [
                            {
                                "paper_id": "string",
                                "short_title_zh": "string 可选，无法提取短名就留空",
                                "one_sentence_summary_zh": "string",
                                "core_contribution_zh": "string",
                                "core_points_zh": ["string 3-5 条核心贡献/观点要点"],
                                "key_results_zh": "string 关键结果，尽量带数字，没有就留空",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        payload = client.chat_json(messages, model="fast")
    except Exception as exc:
        logger.warning("生成日报摘要 pass 失败，回退到已有字段：%s", exc)
        return
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        logger.warning("日报摘要 pass 返回结构异常，回退到已有字段")
        return
    result_map = {result.paper_id: result for result in results}
    updated = 0
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paper_id", "")).strip()
        result = result_map.get(paper_id)
        if result is None:
            continue
        one_sentence = _clean_card_text(str(item.get("one_sentence_summary_zh", "")))
        contribution = _clean_card_text(str(item.get("core_contribution_zh", "")))
        raw_points = item.get("core_points_zh", [])
        core_points = (
            [p for p in (_clean_card_text(str(x)) for x in raw_points) if p]
            if isinstance(raw_points, list)
            else []
        )
        key_results = _clean_card_text(str(item.get("key_results_zh", "")))
        if one_sentence:
            result.daily_one_sentence_zh = one_sentence
        if contribution:
            result.daily_core_contribution_zh = contribution
        if core_points:
            result.daily_core_points = core_points
        if key_results:
            result.daily_key_results = key_results
        short_title = _valid_daily_short_title(str(item.get("short_title_zh", "")))
        if short_title and _should_replace_title(result.note_title_zh):
            result.note_title_zh = short_title
        updated += int(bool(one_sentence or contribution or core_points or key_results or short_title))
    if updated:
        logger.info("日报摘要 pass 完成：更新 %d 条日报卡片", updated)


def _clean_card_text(value: str) -> str:
    text = re.sub(r"^#{1,6}\s+", "", value.strip(), flags=re.MULTILINE)
    text = re.sub(r"!\[\[[^\]]+\]\]", "", text)
    text = _normalize_daily_field_text(text).strip(" -")
    return text


def _valid_daily_short_title(value: str) -> str:
    text = safe_chinese_title(value.strip(), "")
    if not text:
        return ""
    if len(text) > 24:
        return ""
    if re.search(r"[。！？!?，,；;：:]", text):
        return ""
    if not re.search(r"[\u4e00-\u9fff]", text):
        return ""
    return text


def _should_replace_title(title: str) -> bool:
    text = title.strip()
    if not text:
        return True
    return len(text) > 28 or bool(re.search(r"[。！？!?，,；;]", text))


def _generate_daily_overview(
    papers: list,
    results: list,
    client: DeepSeekClient | None,
) -> str:
    if client is None or not results:
        return ""
    paper_map = {paper.paper_id: paper for paper in papers}
    items: list[dict[str, Any]] = []
    for result in results:
        paper = paper_map.get(result.paper_id)
        if paper is None:
            continue
        items.append(
            {
                "title": paper.title,
                "abstract": paper.abstract,
                "score": result.recommend_score,
                "tags": result.tags or result.matched_interests,
                "summary_zh": result.summary_zh,
                "paper_type": result.paper_type,
                "why_read": result.why_read,
                "reading_decision": result.reading_decision,
            }
        )
    if not items:
        return ""
    messages = [
        {
            "role": "system",
            "content": (
                "你是中文科研日报编辑。请根据当天推荐论文生成“今日概览”正文，只返回合法 JSON。"
                "要求：1) 全部使用中文；2) 介绍论文集中领域时使用中文词汇，不要直接堆英文标签；"
                "3) 面向科研入门选手，用短句解释“这些论文大概在解决什么问题、为什么值得看”，"
                "必要英文术语后补一句中文解释；4) 输出可直接放在 Markdown 的 ## 今日概览 下；"
                "5) 包含总体趋势、质量分布、研究热点、阅读建议。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "papers": items,
                    "schema": {
                        "overview_markdown": (
                            "Markdown 字符串：第一段概括今日推荐数量与中文领域分布；随后用列表给出"
                            "总体趋势、质量分布、研究热点（2-4项，每项有中文热点名和解释）、阅读建议。"
                        )
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        payload = client.chat_json(messages, model="fast")
    except Exception as exc:
        logger.warning("生成今日概览失败，回退到规则概览：%s", exc)
        return ""
    overview = payload.get("overview_markdown", "") if isinstance(payload, dict) else ""
    return str(overview).strip()


def _probe_connectivity(deepseek_base_url: str, max_wait_seconds: int = 180) -> bool:
    """主流程前的轻量网络连通性探测。

    代理/网络不稳定时 DeepSeek 和学术站点的 HTTPS 可能同时失败。这里在正式抓取前
    快速探测，不通则等待 60s 重试，最多等 max_wait_seconds；是否在超时后退出由调用方决定。
    """
    import time as _time

    import requests

    test_urls = [
        f"{deepseek_base_url.rstrip('/')}/v1/models",  # DeepSeek，配 NO_PROXY 后直连
        "https://openreview.net",                      # 学术站点，走代理
    ]
    waited = 0
    while True:
        failed: str | None = None
        for url in test_urls:
            try:
                requests.get(url, timeout=10)
            except Exception as exc:  # 401/403 等 HTTP 响应不会抛异常，能连通即视为正常
                failed = f"{url}（{exc}）"
                break
        if failed is None:
            return True
        if waited >= max_wait_seconds:
            logger.warning("网络连通性探测超过 %ds 仍失败：%s；继续执行，单点失败不阻断整体", max_wait_seconds, failed)
            return False
        logger.warning("网络连通性探测失败：%s；等待 60s 后重试", failed)
        _time.sleep(60)
        waited += 60


@dataclass
class RunOptions:
    run_date: date
    dry_run: bool = False
    limit: int | None = None
    no_images: bool = False
    profile_path: Path | None = None
    daily_path: Path | None = None


def run_daily(config: AppConfig, options: RunOptions, log_path: Path) -> RunSummary:
    if not options.dry_run and not config.vault_path.exists():
        raise RuntimeError(f"Obsidian Vault 路径不存在: {config.vault_path}")

    # ── 一天只跑一次守卫：解锁兜底 / 手动触发 / 失败重试时，避免重复运行 ──
    if not options.dry_run:
        stamp_dir = ROOT / "data" / "state"
        stamp_dir.mkdir(parents=True, exist_ok=True)
        stamp_file = stamp_dir / f"daily_done_{options.run_date.isoformat()}.stamp"
        if stamp_file.exists():
            logger.info("今日 (%s) 已完成运行，跳过", options.run_date.isoformat())
            return RunSummary(
                candidate_count=0,
                llm_count=0,
                recommended_count=0,
                detailed_note_count=0,
                image_success=0,
                image_failed=0,
                daily_note_path="",
                log_path=str(log_path),
                run_date=options.run_date,
                report={},
            )

    quota = config.profile.get("daily_quota", {})
    llm_limit = int(options.limit or quota.get("llm_filter_limit", 60))
    digest_top_n = int(quota.get("daily_digest_top_n", 10))
    detailed_top_n = int(quota.get("detailed_note_top_n", 3))
    image_top_n = int(quota.get("image_extract_top_n", 3))

    # ── 自动导入反馈（日报复选框 + 笔记 frontmatter），必须在 fetch 前 ──
    if config.daily.get("feedback", {}).get("enabled", True):
        try:
            count = import_feedback_from_daily(config.vault_path, config.daily_dir)
            if count:
                logger.info("从日报复选框导入了 %d 条反馈", count)
        except Exception as exc:
            logger.warning("从日报导入反馈失败：%s", exc)
        try:
            count = import_feedback_from_vault(config.vault_path, config.paper_dir)
            if count:
                logger.info("从笔记 frontmatter 导入了 %d 条反馈", count)
        except Exception as exc:
            logger.warning("从笔记导入反馈失败：%s", exc)

    runtime_cfg = config.daily.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    if not options.dry_run and runtime_cfg.get("connectivity_check", True):
        logger.info("阶段 0/9：探测网络连通性")
        connectivity_ok = _probe_connectivity(
            config.deepseek_base_url,
            max_wait_seconds=int(runtime_cfg.get("connectivity_max_wait_seconds", 180)),
        )
        if not connectivity_ok and runtime_cfg.get("connectivity_fail_on_timeout", False):
            raise RuntimeError(
                "网络连通性探测超时，按 runtime.connectivity_fail_on_timeout=true 退出，等待计划任务重试"
            )

    logger.info("阶段 1/9：抓取候选论文")
    candidates = fetch_candidates(config.profile, config.daily, options.run_date, options.limit)
    logger.info("候选论文抓取完成：%d 篇", len(candidates))
    seen = load_seen()
    logger.info("阶段 2/9：按历史记录去重")
    candidates = filter_recent_seen(candidates, seen, config.daily.get("runtime", {}).get("disable_dedup", False))
    logger.info("去重后候选：%d 篇", len(candidates))
    llm_candidates = candidates[:llm_limit]
    logger.info("阶段 3/9：抽取轻量上下文，进入 LLM 候选：%d 篇", len(llm_candidates))
    enrich_papers_with_context(
        llm_candidates,
        max_papers=int(config.daily.get("runtime", {}).get("context_extract_top_n", 8)),
    )

    open_source_cfg = config.daily.get("open_source_check", {})
    if not isinstance(open_source_cfg, dict):
        open_source_cfg = {}
    if open_source_cfg.get("enabled", True):
        logger.info("阶段 4/9：核验开源代码和数据集")
        verify_papers_open_source(llm_candidates, open_source_cfg)

    client = None
    if config.deepseek_api_key:
        runtime = config.daily.get("runtime", {})
        client = DeepSeekClient(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
            model_fast=config.deepseek_model_fast,
            model_pro=config.deepseek_model_pro,
            timeout_fast=int(runtime.get("request_timeout_seconds", 60)),
            timeout_pro=int(runtime.get("request_timeout_pro", 300)),
            max_tokens_fast=int(runtime.get("max_tokens_fast", 8192)),
            max_tokens_pro=int(runtime.get("max_tokens_pro", 16384)),
            max_retries=int(runtime.get("max_retries", 3)),
            stream=bool(runtime.get("stream", True)),
            disable_thinking=bool(runtime.get("disable_thinking", False)),
        )

    logger.info("阶段 5/9：读取 Obsidian 写作规范")
    style_specs = load_obsidian_skill_specs(config.vault_path)
    logger.info("阶段 6/9：调用 DeepSeek 筛选论文")
    results = rerank_papers(
        llm_candidates,
        config.profile,
        client,
        batch_size=int(config.daily.get("runtime", {}).get("llm_batch_size", 5)),
        style_specs=style_specs,
        deep_top_n=detailed_top_n,
    )
    logger.info("DeepSeek 筛选完成：%d 条结果", len(results))
    if open_source_cfg.get("enabled", True):
        reconcile_results(results, llm_candidates)
    results = [r for r in results if r.decision in {"keep", "maybe"} and r.action != "skip"][:digest_top_n]
    result_paper_ids = {r.paper_id for r in results}
    final_papers = [p for p in llm_candidates if p.paper_id in result_paper_ids]
    final_papers.sort(key=lambda p: next(r.recommend_score for r in results if r.paper_id == p.paper_id), reverse=True)

    logger.info("阶段 6.5/9：生成日报摘要卡片")
    _generate_daily_cards(final_papers, results, client)

    exporter = ObsidianExporter(config.vault_path, config.daily_dir, config.paper_dir)
    note_paths = exporter.build_note_paths(final_papers, results, options.run_date, detailed_top_n)
    daily_image_note_paths = exporter.build_note_paths(final_papers, results, options.run_date, len(results))

    daily_overview = _generate_daily_overview(final_papers, results, client)

    image_paths: dict[str, list[Path]] = {}
    written_note_paths: dict[str, str] = {}
    image_success = 0
    image_failed = 0

    if options.dry_run:
        logger.info("阶段 7/9：dry-run 跳过图片提取")
    elif config.daily.get("output", {}).get("extract_images", True) and not options.no_images:
        logger.info("阶段 7/9：提取和筛选论文图片")
        asset_paths = exporter.build_asset_paths(daily_image_note_paths)
        image_papers = [paper for paper in final_papers if paper.paper_id in asset_paths]
        result_map = {result.paper_id: result for result in results}
        image_paths, image_success, image_failed = extract_images_for_papers(
            image_papers,
            asset_paths,
            result_map,
            max(image_top_n, len(image_papers)),
            int(config.daily.get("output", {}).get("max_images_per_paper", 2)),
            ROOT,
            client=client,
            extract_limit_per_paper=int(config.daily.get("output", {}).get("max_extracted_images_per_paper", 10)),
        )
    else:
        logger.info("阶段 7/9：跳过图片提取")

    detailed_target_ids = {result.paper_id for result in results[:detailed_top_n]}
    detailed_papers_for_note = [paper for paper in final_papers if paper.paper_id in detailed_target_ids]
    fulltext_cfg = config.daily.get("fulltext_notes", {})
    if not isinstance(fulltext_cfg, dict):
        fulltext_cfg = {}
    if fulltext_cfg.get("enabled", True):
        logger.info("阶段 8/9：抽取全文上下文并生成精读字段")
        enrich_papers_with_fulltext(
            detailed_papers_for_note,
            enabled=True,
            max_papers=int(fulltext_cfg.get("max_papers", detailed_top_n)),
            max_chars_per_section=int(fulltext_cfg.get("max_chars_per_section", 2400)),
        )
        note_image_links = {
            paper_id: [f"![[assets/{path.name}]]" for path in paths if path.exists()]
            for paper_id, paths in image_paths.items()
        }
        generate_detailed_notes(
            detailed_papers_for_note,
            results,
            config.profile,
            client,
            style_specs=style_specs,
            grouped=bool(fulltext_cfg.get("grouped_generation", False)),
            harmonize=bool(fulltext_cfg.get("harmonize", False)),
            image_paths=note_image_links,
        )

    link_processor = None
    vault_links_cfg = config.daily.get("vault_links", {})
    if not isinstance(vault_links_cfg, dict):
        vault_links_cfg = {}
    if not options.dry_run and vault_links_cfg.get("enabled", True):
        try:
            logger.info("准备构建 Vault wikilink 索引")
            scan_dirs = vault_links_cfg.get("scan_dirs") or [config.paper_dir]
            note_index = build_note_index(
                config.vault_path,
                [str(item) for item in scan_dirs],
                include_aliases=bool(vault_links_cfg.get("include_aliases", True)),
            )
            link_processor = make_link_processor(note_index, vault_links_cfg)
            logger.info("Vault 笔记索引：%d 个可链接标题", len(note_index))
        except Exception as exc:
            logger.warning("构建 Vault 笔记索引失败，跳过自动 wikilink：%s", exc)

    exporter.link_processor = link_processor

    citation_cfg = config.daily.get("citation_network", {})
    if isinstance(citation_cfg, dict) and citation_cfg.get("enabled", True):
        detailed_papers = [paper for paper in final_papers if paper.paper_id in note_paths]
        enrich_citation_network(detailed_papers, citation_cfg)

    if options.dry_run:
        preview = exporter.render_daily(final_papers, results, options.run_date, note_paths, daily_overview, image_paths)
        print(preview)
        daily_note_path = str(exporter.daily_path(options.run_date))
    else:
        logger.info("阶段 9/9：写入 Obsidian 输出")
        if config.daily.get("output", {}).get("create_individual_notes", True):
            written_note_paths = exporter.write_paper_notes(final_papers, results, options.run_date, note_paths, image_paths)
        daily_path = exporter.write_daily(final_papers, results, options.run_date, note_paths, daily_overview, image_paths)
        daily_note_path = str(daily_path)
        # 日报已落盘，后续都是记账。任一失败都不应阻断流程，更不能挡住下方"今日完成标记"，
        # 否则解锁兜底/重试会因为缺标记而重复出报告。各自 try 包裹，失败只告警。
        try:
            save_seen(final_papers, results, written_note_paths, options.run_date)
        except Exception as exc:
            logger.warning("保存去重记录失败：%s", exc)
        try:
            append_run_history(
                {
                    "date": options.run_date.isoformat(),
                    "candidate_count": len(candidates),
                    "llm_count": len(llm_candidates),
                    "recommended_count": len(results),
                    "daily_note_path": daily_note_path,
                }
            )
        except Exception as exc:
            logger.warning("追加运行历史失败：%s", exc)
        moc_cfg = config.daily.get("moc", {})
        if isinstance(moc_cfg, dict) and moc_cfg.get("enabled", True):
            try:
                written_mocs = update_mocs(
                    config.vault_path,
                    config.daily.get("output", {}).get("moc_dir") or moc_cfg.get("moc_dir", "每日科研论文/MOC"),
                    load_seen(),
                    moc_cfg,
                )
                logger.info("已更新 %d 个主题 MOC 索引", len(written_mocs))
            except Exception as exc:
                logger.warning("更新主题 MOC 失败：%s", exc)

    report = build_run_report(candidates, llm_candidates, results, results, image_success, image_failed, options.run_date)
    if not options.dry_run:
        try:
            report_path = log_path.parent / f"report_{options.run_date.isoformat()}.md"
            report_path.write_text(render_report(report), encoding="utf-8")
        except Exception as exc:
            logger.warning("写运行健康报告失败：%s", exc)
        # 写入今日完成标记，防止解锁兜底 / 重试 / 手动触发重复执行
        try:
            stamp_dir = ROOT / "data" / "state"
            stamp_dir.mkdir(parents=True, exist_ok=True)
            stamp_file = stamp_dir / f"daily_done_{options.run_date.isoformat()}.stamp"
            stamp_file.write_text(datetime.now().isoformat(), encoding="utf-8")
            logger.info("已写入今日完成标记：%s", stamp_file)
        except Exception as exc:
            logger.warning("写入今日完成标记失败：%s", exc)

    return RunSummary(
        candidate_count=len(candidates),
        llm_count=len(llm_candidates),
        recommended_count=len(results),
        detailed_note_count=len(note_paths),
        image_success=image_success,
        image_failed=image_failed,
        daily_note_path=daily_note_path,
        log_path=str(log_path),
        run_date=options.run_date,
        report=report,
    )


def parse_run_date(value: str | None) -> date:
    if not value:
        return datetime.now().date()
    return datetime.strptime(value, "%Y-%m-%d").date()
