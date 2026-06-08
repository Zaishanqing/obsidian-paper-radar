"""CLI entry point for Obsidian Paper Radar."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from obsidian_paper_radar.config import ROOT, load_app_config
from obsidian_paper_radar.daily import RunOptions, parse_run_date, run_daily
from obsidian_paper_radar.dedup import load_seen, recent_recommended
from obsidian_paper_radar.deepseek_client import DeepSeekClient
from obsidian_paper_radar.feedback import ALL_LABELS, record_feedback
from obsidian_paper_radar.weekly_digest import build_weekly_digest, write_weekly_digest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "weekly":
        return weekly_main(argv[1:])
    if argv and argv[0] == "feedback":
        return feedback_main(argv[1:])
    return daily_main(argv)


def daily_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 驱动的 Obsidian 每日论文推荐")
    parser.add_argument("--dry-run", action="store_true", help="不写入 Vault，只打印日报预览")
    parser.add_argument("--limit", type=int, default=None, help="限制候选和 LLM 筛选数量")
    parser.add_argument("--no-images", action="store_true", help="跳过图片提取")
    parser.add_argument("--date", type=str, default=None, help="运行日期 YYYY-MM-DD")
    parser.add_argument("--profile", type=Path, default=None, help="paper_profile.yaml 路径")
    parser.add_argument("--config", type=Path, default=None, help="daily_papers.yaml 路径")
    args = parser.parse_args(argv)

    run_date = parse_run_date(args.date)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"daily_papers_{run_date.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stderr)],
    )

    try:
        config = load_app_config(profile_path=args.profile, daily_path=args.config)
        summary = run_daily(
            config,
            RunOptions(
                run_date=run_date,
                dry_run=args.dry_run,
                limit=args.limit,
                no_images=args.no_images,
                profile_path=args.profile,
                daily_path=args.config,
            ),
            log_path,
        )
    except Exception as exc:
        logging.exception("Daily paper run failed")
        print(f"运行失败：{exc}", file=sys.stderr)
        print("请检查 .env、config/*.yaml 和日志文件。", file=sys.stderr)
        return 1

    print(f"候选论文：{summary.candidate_count}")
    print(f"进入 LLM 筛选：{summary.llm_count}")
    print(f"最终推荐：{summary.recommended_count}")
    print(f"详细笔记：{summary.detailed_note_count}")
    print(f"图片提取：成功 {summary.image_success} / 失败 {summary.image_failed}")
    if summary.report:
        from obsidian_paper_radar.health import format_report_line

        print(format_report_line(summary.report))
    print(f"日报路径：{summary.daily_note_path}")
    print(f"日志路径：{summary.log_path}")
    return 0


def weekly_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成本周论文周报")
    parser.add_argument("--days", type=int, default=7, help="回溯天数")
    parser.add_argument("--date", type=str, default=None, help="基准日期 YYYY-MM-DD")
    parser.add_argument("--config", type=Path, default=None, help="daily_papers.yaml 路径")
    parser.add_argument("--profile", type=Path, default=None, help="paper_profile.yaml 路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写入 Vault")
    args = parser.parse_args(argv)

    run_date = parse_run_date(args.date)
    try:
        config = load_app_config(profile_path=args.profile, daily_path=args.config)
    except Exception as exc:
        print(f"加载配置失败：{exc}", file=sys.stderr)
        return 1
    items = recent_recommended(load_seen(), run_date, args.days)
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
    content = build_weekly_digest(items, run_date, days=args.days, client=client)
    if content is None:
        print(f"最近 {args.days} 天没有推荐记录，无法生成周报。")
        return 0
    if args.dry_run:
        print(content)
        return 0
    weekly_dir = config.daily.get("output", {}).get("weekly_dir", "每日科研论文/周报")
    path = write_weekly_digest(config.vault_path, weekly_dir, content, run_date)
    print(f"周报已写入：{path}")
    return 0


def feedback_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记录论文反馈")
    parser.add_argument("paper_id", nargs="?", help="论文 ID，例如 arxiv:2606.12345")
    parser.add_argument("label", nargs="?", help=f"反馈标签：{' / '.join(sorted(ALL_LABELS))}")
    parser.add_argument("--list", action="store_true", help="列出最近推荐过的论文")
    parser.add_argument("--days", type=int, default=14, help="--list 的回溯天数")
    args = parser.parse_args(argv)

    seen = load_seen()
    if args.list or not args.paper_id:
        items = recent_recommended(seen, parse_run_date(None), args.days)
        if not items:
            print("最近没有推荐记录。先跑一次日报。")
            return 0
        for item in items[:40]:
            tags = "、".join(item.get("tags", [])[:4])
            print(f"{item['paper_id']}\t{item.get('last_score', 0)}\t{item.get('title', '')[:50]}\t[{tags}]")
        return 0
    if not args.label or args.label.lower() not in ALL_LABELS:
        print(f"label 必须是：{' / '.join(sorted(ALL_LABELS))}", file=sys.stderr)
        return 1
    entry = seen.get(args.paper_id, {})
    record = record_feedback(args.paper_id, args.label, tags=entry.get("tags"), title=entry.get("title", ""))
    print(f"已记录反馈：{args.paper_id} -> {record['label']}（方向：{'、'.join(record['tags']) or '无'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
