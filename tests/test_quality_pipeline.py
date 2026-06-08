from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar import image_utils
from obsidian_paper_radar.cli import feedback_main
from obsidian_paper_radar.health import build_run_report, render_report
from obsidian_paper_radar.models import Paper, RerankResult
from obsidian_paper_radar.note_generator import _HARD_RULES
from obsidian_paper_radar.obsidian_exporter import _normalize_markdown
from obsidian_paper_radar.paper_fulltext import _sections_from_plain_text
from obsidian_paper_radar.rerank import _build_messages, _paper_to_fast_prompt_dict, rerank_papers, validate_result_item


def _paper(pid: str, source: str = "arxiv") -> Paper:
    return Paper(pid, pid, [], "abstract", "2026", "", "", source)


class QualityPipelineTest(unittest.TestCase):
    def test_validate_result_item_rejects_missing_required_fields(self) -> None:
        with self.assertRaises(ValueError):
            validate_result_item({"paper_id": "p1", "recommend_score": 8, "decision": "keep", "action": "daily_only"})

    def test_invalid_llm_item_only_fallbacks_that_paper(self) -> None:
        papers = [_paper("p1"), _paper("p2")]
        for paper in papers:
            paper.rough_score = 6
        client = Mock()
        client.chat_json.return_value = {
            "results": [
                {
                    "paper_id": "p1",
                    "recommend_score": 9,
                    "decision": "keep",
                    "action": "detailed_note",
                    "summary_zh": "有效摘要",
                    "note_title_zh": "有效短名",
                },
                {"paper_id": "p2", "recommend_score": 7, "decision": "keep", "action": "daily_only"},
            ]
        }
        results = rerank_papers(papers, {}, client, batch_size=2)
        by_id = {result.paper_id: result for result in results}
        self.assertFalse(by_id["p1"].fallback_used)
        self.assertTrue(by_id["p2"].fallback_used)

    def test_missing_required_llm_text_fields_are_repaired(self) -> None:
        paper = _paper("p1")
        paper.rough_score = 6
        client = Mock()
        client.chat_json.return_value = {
            "results": [
                {
                    "paper_id": "p1",
                    "recommend_score": 8,
                    "decision": "keep",
                    "action": "daily_only",
                }
            ]
        }
        result = rerank_papers([paper], {}, client, batch_size=1)[0]
        self.assertTrue(result.fallback_used)
        self.assertTrue(result.summary_zh)
        self.assertTrue(result.note_title_zh)
        self.assertEqual(result.recommend_score, 8)

    def test_fast_prompt_excludes_fulltext_context(self) -> None:
        paper = _paper("p1")
        paper.extra_context["fulltext_context"] = {"sections": {"method": "x" * 10_000}}
        prompt = _paper_to_fast_prompt_dict(paper)
        self.assertNotIn("fulltext_context", prompt["extra_context"])
        self.assertLessEqual(len(prompt["abstract"]), 900)

    def test_rerank_prompt_requires_beginner_friendly_language(self) -> None:
        messages = _build_messages([_paper("p1")], {})
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("科研入门选手", system)
        self.assertIn("避免缩写堆叠", system)
        self.assertIn("不要全文翻译或术语堆叠", user)

    def test_note_hard_rules_require_plain_explanations(self) -> None:
        self.assertIn("科研入门选手", _HARD_RULES)
        self.assertIn("白话直觉", _HARD_RULES)
        self.assertIn("禁止术语堆叠", _HARD_RULES)

    def test_failed_batch_is_split_before_fallback(self) -> None:
        papers = [_paper("p1"), _paper("p2")]
        for paper in papers:
            paper.rough_score = 6
        client = Mock()
        client.chat_json.side_effect = [
            RuntimeError("timeout"),
            {
                "results": [
                    {
                        "paper_id": "p1",
                        "recommend_score": 9,
                        "decision": "keep",
                        "action": "daily_only",
                        "summary_zh": "摘要1",
                        "note_title_zh": "短名1",
                    }
                ]
            },
            {
                "results": [
                    {
                        "paper_id": "p2",
                        "recommend_score": 8,
                        "decision": "keep",
                        "action": "daily_only",
                        "summary_zh": "摘要2",
                        "note_title_zh": "短名2",
                    }
                ]
            },
        ]
        results = rerank_papers(papers, {}, client, batch_size=2)
        self.assertEqual(client.chat_json.call_count, 3)
        self.assertFalse(any(result.fallback_used for result in results))

    def test_report_includes_source_contribution_table(self) -> None:
        candidates = [_paper("a1", "arxiv"), _paper("c1", "cvf:cvpr")]
        llm = [candidates[0]]
        recommended = [RerankResult("a1", 8, "keep", "detailed_note")]
        report = build_run_report(candidates, llm, recommended, recommended, 1, 0, date(2026, 6, 7))
        self.assertEqual(report["llm_by_source"], {"arxiv": 1})
        self.assertEqual(report["recommended_by_source"], {"arxiv": 1})
        rendered = render_report(report)
        self.assertIn("| 来源 | 候选数 | 进入 LLM | 最终推荐 |", rendered)
        self.assertIn("| cvf | 1 | 0 | 0 |", rendered)

    def test_plain_text_sections_are_bucketed(self) -> None:
        sections = _sections_from_plain_text(
            "Introduction\nThis is motivation.\n\nMethod\nThis is the model.\n\nExperiments\nThis is evaluation."
        )
        self.assertIn("background", sections)
        self.assertIn("method", sections)
        self.assertIn("experiment", sections)

    def test_image_filter_dedupes_and_uses_local_caption(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            img1 = root / "architecture.png"
            img2 = root / "copy.png"
            tiny = root / "tiny.png"
            Image.new("RGB", (640, 420), "white").save(img1)
            img2.write_bytes(img1.read_bytes())
            Image.new("RGB", (20, 20), "white").save(tiny)
            (root / "index.md").write_text("architecture.png Figure 1: method pipeline framework.", encoding="utf-8")
            ranked = image_utils._rank_images(root, None)
            self.assertEqual(len(ranked), 1)
            self.assertEqual(ranked[0].name, "architecture.png")

    def test_image_rank_prefers_first_when_it_is_architecture_like(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "page1_fig1.png"
            second = root / "page5_result.png"
            Image.new("RGB", (640, 420), "white").save(first)
            Image.new("RGB", (900, 520), "white").save(second)
            (root / "index.md").write_text(
                "page1_fig1.png Figure 1: overview architecture and method pipeline.\n"
                "page5_result.png Figure 5: benchmark result curve.",
                encoding="utf-8",
            )
            ranked = image_utils._rank_images(root, None)
            self.assertEqual(ranked[0].name, "page1_fig1.png")

    def test_image_rank_scans_in_order_until_architecture_match(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_plot = root / "page1_fig1.png"
            arch = root / "page2_fig2.png"
            Image.new("RGB", (640, 420), "white").save(result_plot)
            Image.new("RGB", (640, 420), "gray").save(arch)
            (root / "index.md").write_text(
                "page1_fig1.png Figure 1: result curve and ablation plot.\n"
                "page2_fig2.png Figure 2: architecture overview and method pipeline.",
                encoding="utf-8",
            )
            ranked = image_utils._rank_images(root, None, max_images=1)
            self.assertEqual([p.name for p in ranked], ["page2_fig2.png"])

    def test_image_rank_does_not_treat_model_performance_as_architecture(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_plot = root / "page1_model_performance.png"
            arch = root / "page2_workflow.png"
            Image.new("RGB", (640, 420), "white").save(result_plot)
            Image.new("RGB", (640, 420), "gray").save(arch)
            (root / "index.md").write_text(
                "page1_model_performance.png Figure 1: model performance result curve.\n"
                "page2_workflow.png Figure 2: workflow diagram.",
                encoding="utf-8",
            )
            ranked = image_utils._rank_images(root, None, max_images=1)
            self.assertEqual([p.name for p in ranked], ["page2_workflow.png"])

    def test_is_decorative_image_flags_icons_logos_keeps_diagram(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        import random

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            small = root / "small.png"
            Image.new("RGB", (200, 200), "white").save(small)  # 太小→图标
            dark = root / "dark.png"
            Image.new("RGB", (800, 600), "black").save(dark)  # 黑底→logo/黑图
            diagram = root / "diagram.png"
            img = Image.new("RGB", (520, 470))
            random.seed(0)
            img.putdata([(random.randint(90, 255), random.randint(90, 255), random.randint(90, 255)) for _ in range(520 * 470)])
            img.save(diagram)  # 大、白底、彩色→真实图
            self.assertTrue(image_utils._is_decorative_image(small))
            self.assertTrue(image_utils._is_decorative_image(dark))
            self.assertFalse(image_utils._is_decorative_image(diagram))

    def test_is_decorative_image_flags_flattened_white_background_icon(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            icon = root / "flaticon_person.png"
            img = Image.new("RGB", (512, 512), "white")
            draw = ImageDraw.Draw(img)
            draw.ellipse((190, 90, 322, 222), fill=(70, 145, 230))
            draw.rounded_rectangle((150, 240, 362, 430), radius=40, fill=(70, 145, 230))
            img.save(icon)
            self.assertTrue(image_utils._is_decorative_image(icon))

    def test_is_decorative_image_flags_flattened_chrome_like_icon(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            icon = root / "page1_fig1.jpeg"
            img = Image.new("RGB", (512, 512), "black")
            draw = ImageDraw.Draw(img)
            draw.pieslice((-35, -35, 547, 547), 200, 315, fill=(76, 175, 80))
            draw.pieslice((-35, -35, 547, 547), 315, 20, fill=(251, 188, 5))
            draw.pieslice((-35, -35, 547, 547), 20, 200, fill=(244, 67, 54))
            draw.ellipse((170, 170, 342, 342), fill=(33, 150, 243))
            img.save(icon)
            self.assertTrue(image_utils._is_decorative_image(icon))

    def test_llm_selected_decorative_icon_is_rejected(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            icon = root / "page1_fig1.jpeg"
            img = Image.new("RGB", (512, 512), "black")
            draw = ImageDraw.Draw(img)
            draw.pieslice((-35, -35, 547, 547), 200, 315, fill=(76, 175, 80))
            draw.pieslice((-35, -35, 547, 547), 315, 20, fill=(251, 188, 5))
            draw.pieslice((-35, -35, 547, 547), 20, 200, fill=(244, 67, 54))
            draw.ellipse((170, 170, 342, 342), fill=(33, 150, 243))
            img.save(icon)
            (root / "index.md").write_text("page1_fig1.jpeg Figure 1: browser icon.", encoding="utf-8")
            client = Mock()
            client.chat_json.return_value = {
                "has_architecture": True,
                "items": [{"filename": icon.name, "type": "architecture", "usefulness": "high", "reason": "mistaken"}],
            }
            ranked = image_utils._rank_images(root, None, max_images=1, client=client)
            self.assertEqual(ranked, [])

    def test_image_rank_uses_llm_classification_when_available(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "page1_overview.png"
            second = root / "page2_pipeline.png"
            Image.new("RGB", (640, 420), "white").save(first)
            diagram = Image.new("RGB", (640, 420), "white")
            draw = ImageDraw.Draw(diagram)
            for i, color in enumerate([(70, 130, 180), (46, 139, 87), (218, 165, 32), (205, 92, 92)]):
                x0 = 50 + i * 145
                draw.rectangle((x0, 150, x0 + 95, 230), outline=color, width=5)
                draw.line((x0 + 95, 190, x0 + 135, 190), fill=(40, 40, 40), width=4)
            draw.rectangle((225, 60, 415, 105), outline=(120, 80, 180), width=4)
            diagram.save(second)
            client = Mock()
            client.chat_json.return_value = {
                "has_architecture": True,
                "items": [
                    {"filename": first.name, "type": "example", "usefulness": "low", "reason": "teaser"},
                    {"filename": second.name, "type": "architecture", "usefulness": "high", "reason": "pipeline"},
                ]
            }
            ranked = image_utils._rank_images(root, None, max_images=1, client=client)
            self.assertEqual([p.name for p in ranked], ["page2_pipeline.png"])

    def test_image_rank_returns_empty_when_llm_says_no_architecture(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            photo = root / "page1_garden_photo.png"
            Image.new("RGB", (900, 700), "green").save(photo)
            client = Mock()
            client.chat_json.return_value = {
                "has_architecture": False,
                "items": [{"filename": photo.name, "type": "example", "usefulness": "low", "reason": "photo"}],
            }
            ranked = image_utils._rank_images(root, None, max_images=1, client=client)
            self.assertEqual(ranked, [])

    def test_feedback_subcommand_list_runs(self) -> None:
        with patch("obsidian_paper_radar.cli.load_seen", return_value={}):
            self.assertEqual(feedback_main(["--list"]), 0)

    def test_daily_feedback_resolves_via_wikilink_without_marker(self) -> None:
        from obsidian_paper_radar import feedback as fb

        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            note = vault / "笔记" / "甲方法" / "甲方法.md"
            note.parent.mkdir(parents=True)
            note.write_text('---\npaper_id: "arxiv:1"\n---\n# 甲方法\n', encoding="utf-8")
            daily = vault / "日报"
            daily.mkdir()
            (daily / "06-08 论文日报.md").write_text(
                "## 快速反馈\n\n- [x] **不推荐** · [[笔记/甲方法/甲方法|甲方法]]\n", encoding="utf-8"
            )
            fb_file = vault / "feedback.json"
            with patch.object(fb, "feedback_path", return_value=fb_file), \
                 patch.object(fb, "_seen_tags", return_value=["rag"]), \
                 patch.object(fb, "_seen_title", return_value="T"), \
                 patch.object(fb, "_build_title_index", return_value={}):
                count = fb.import_feedback_from_daily(vault, "日报")
            self.assertEqual(count, 1)
            data = json.loads(fb_file.read_text(encoding="utf-8"))
            self.assertEqual(data["arxiv:1"]["label"], "useless")

    def test_daily_feedback_no_label_scoped_to_section(self) -> None:
        from obsidian_paper_radar import feedback as fb

        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            daily = vault / "日报"
            daily.mkdir(parents=True)
            (daily / "06-08 论文日报.md").write_text(
                "## 推荐列表\n\n- [x] 不该被当成反馈的复选框\n\n## 快速反馈\n\n勾选 = 不想再看到。\n\n- [x] 乙方法\n",
                encoding="utf-8",
            )
            fb_file = vault / "feedback.json"
            with patch.object(fb, "feedback_path", return_value=fb_file), \
                 patch.object(fb, "_seen_tags", return_value=[]), \
                 patch.object(fb, "_seen_title", return_value=""), \
                 patch.object(fb, "_build_title_index", return_value={"乙方法": "arxiv:9"}):
                count = fb.import_feedback_from_daily(vault, "日报")
            self.assertEqual(count, 1)  # 只导入「快速反馈」区块内勾选的「乙方法」
            data = json.loads(fb_file.read_text(encoding="utf-8"))
            self.assertEqual(data["arxiv:9"]["label"], "useless")

    def test_daily_feedback_resolves_via_title_fallback(self) -> None:
        from obsidian_paper_radar import feedback as fb

        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            daily = vault / "日报"
            daily.mkdir(parents=True)
            (daily / "06-08 论文日报.md").write_text(
                "## 快速反馈\n\n- [x] **不推荐** · 乙方法\n- [ ] **不推荐** · 丙方法\n", encoding="utf-8"
            )
            fb_file = vault / "feedback.json"
            with patch.object(fb, "feedback_path", return_value=fb_file), \
                 patch.object(fb, "_seen_tags", return_value=[]), \
                 patch.object(fb, "_seen_title", return_value=""), \
                 patch.object(fb, "_build_title_index", return_value={"乙方法": "arxiv:2", "丙方法": "arxiv:3"}):
                count = fb.import_feedback_from_daily(vault, "日报")
            self.assertEqual(count, 1)  # 只导入勾选的「乙方法」，未勾选的「丙方法」不导入
            data = json.loads(fb_file.read_text(encoding="utf-8"))
            self.assertIn("arxiv:2", data)
            self.assertNotIn("arxiv:3", data)

    def test_normalize_markdown_inserts_blank_after_heading(self) -> None:
        out = _normalize_markdown("## 核心问题\n内容第一句。")
        self.assertEqual(out, "## 核心问题\n\n内容第一句。")

    def test_normalize_markdown_inserts_blank_before_list(self) -> None:
        out = _normalize_markdown("正文段落。\n- 项一\n- 项二")
        self.assertEqual(out, "正文段落。\n\n- 项一\n- 项二")

    def test_normalize_markdown_preserves_frontmatter_tag_list(self) -> None:
        text = "---\ntags:\n  - a\n  - b\n---\n\n## S\nbody"
        out = _normalize_markdown(text)
        self.assertIn("---\ntags:\n  - a\n  - b\n---", out)  # YAML 未被插空行破坏
        self.assertIn("## S\n\nbody", out)

    def test_normalize_markdown_skips_code_block(self) -> None:
        text = "## 代码\n\n```python\n- not a list\nx = 1\n```\n"
        out = _normalize_markdown(text)
        self.assertIn("```python\n- not a list\nx = 1\n```", out)


if __name__ == "__main__":
    unittest.main()
