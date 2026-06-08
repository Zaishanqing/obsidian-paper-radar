from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar.models import Paper
from obsidian_paper_radar.models import RerankResult
from obsidian_paper_radar.obsidian_exporter import ObsidianExporter
from obsidian_paper_radar.rerank import fallback_result
from obsidian_paper_radar.utils import fallback_note_title, safe_filename, safe_paper_id
from obsidian_paper_radar.daily import _generate_daily_cards
from obsidian_paper_radar.deepseek_client import _parse_json_payload


class CoreTest(unittest.TestCase):
    def test_safe_filename_removes_windows_invalid_chars(self) -> None:
        self.assertEqual(safe_filename('A/B:C*D?"E<>|'), "A_B_C_D_E")

    def test_safe_paper_id_normalizes_colon(self) -> None:
        self.assertEqual(safe_paper_id("arxiv:2606.12345"), "arxiv_2606.12345")

    def test_fallback_result_uses_rough_score(self) -> None:
        paper = Paper(
            paper_id="arxiv:2606.12345",
            title="Test Paper",
            authors=[],
            abstract="A useful paper",
            published="2026-06-07",
            url="https://arxiv.org/abs/2606.12345",
            pdf_url="https://arxiv.org/pdf/2606.12345",
            source="arxiv",
            rough_score=7.2,
        )
        result = fallback_result(paper)
        self.assertEqual(result.decision, "keep")
        self.assertEqual(result.action, "detailed_note")

    def test_fallback_note_title_stays_short(self) -> None:
        title = fallback_note_title("离线零样本强化学习要求智能体在无显式奖励的情况下解决新任务。")
        self.assertLessEqual(len(title), 18)

    def test_daily_render_handles_empty_results(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        content = exporter.render_daily([], [], date(2026, 6, 7), {})
        self.assertIn("今天没有符合筛选条件的论文。", content)
        self.assertNotIn("# 06-07 论文日报", content)

    def test_daily_render_strips_duplicate_overview_heading(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        content = exporter.render_daily([], [], date(2026, 6, 7), {}, daily_overview="## 今日概览\n\n概览正文")
        self.assertEqual(content.count("## 今日概览"), 1)
        self.assertIn("概览正文", content)

    def test_daily_render_embeds_first_paper_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            image = vault / "每日科研论文" / "笔记" / "06-07" / "方法" / "assets" / "fig1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            exporter = ObsidianExporter(vault, "每日科研论文/日报", "每日科研论文/笔记")
            paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
            result = RerankResult("p1", 8, "keep", "detailed_note", summary_zh="摘要", note_title_zh="方法")
            content = exporter.render_daily([paper], [result], date(2026, 6, 7), {"p1": image.parent.parent / "方法.md"}, image_paths={"p1": [image]})
            self.assertIn("![[每日科研论文/笔记/06-07/方法/assets/fig1.png|600]]", content)

    def test_paper_note_embeds_image_in_method_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            image = vault / "每日科研论文" / "笔记" / "06-07" / "方法" / "assets" / "fig1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            exporter = ObsidianExporter(vault, "每日科研论文/日报", "每日科研论文/笔记")
            paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
            result = RerankResult(
                "p1",
                8,
                "keep",
                "detailed_note",
                note_title_zh="方法",
                method_overview="方法文字。",
                image_guidance={"selection_reason": "架构图。"},
            )
            content = exporter.render_paper_note(paper, result, date(2026, 6, 7), [image])
            self.assertIn("## 方法概览\n\n方法文字。\n\n![[assets/fig1.png|600]]", content)
            self.assertNotIn("## 图片", content)
            self.assertNotIn("\n# 方法\n", content)

    def test_paper_note_localizes_schema_keys_and_enum_values(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult(
            "p1",
            8,
            "keep",
            "detailed_note",
            note_title_zh="方法",
            evidence_strength="medium",
            implementation_feasibility="medium",
            code_availability="unknown",
            dataset_availability="mentioned",
            transferable_modules=[
                {
                    "name": "模块A",
                    "role": "解决问题",
                    "input": "输入数据",
                    "output": "输出结果",
                    "why_it_works": "因为有效",
                    "possible_transfer": "迁移方向",
                    "how_to_adapt": "改造方式",
                }
            ],
            image_guidance={
                "should_use_images": True,
                "preferred_types": "architecture, pipeline",
                "avoid_types": "benchmark_table",
                "selection_reason": "帮助理解。",
            },
            technical_terms=[
                {
                    "term": "术语A",
                    "plain_explanation": "通俗解释",
                    "role_in_paper": "论文中的作用",
                    "need_to_remember": False,
                }
            ],
        )
        content = exporter.render_paper_note(paper, result, date(2026, 6, 7), [])
        self.assertIn("**实现可行性**：中", content)
        self.assertIn("**代码开源情况**：未知", content)
        self.assertIn("**数据集开放情况**：论文提到", content)
        self.assertIn("**作用**：解决问题", content)
        self.assertIn("**输入**：输入数据", content)
        self.assertIn("**为什么有效**：因为有效", content)
        self.assertIn("**通俗解释**：通俗解释", content)
        self.assertIn("**在论文中的作用**：论文中的作用", content)
        self.assertIn("**是否需要记住**：否", content)
        self.assertNotIn("图示选择指导", content)
        self.assertNotIn("是否建议用图", content)
        self.assertNotIn("优先图片类型", content)
        self.assertNotIn("避免图片类型", content)
        self.assertNotIn("role:", content)
        self.assertNotIn("plain_explanation", content)
        self.assertNotIn("need_to_remember", content)
        self.assertNotIn("why_it_works", content)
        self.assertNotIn("medium", content)
        self.assertNotIn("unknown", content)
        self.assertNotIn("architecture, pipeline", content)
        self.assertNotIn("False", content)

    def test_daily_renders_core_points_and_key_results(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        result = RerankResult(
            "p1", 8, "keep", "detailed_note",
            note_title_zh="方法",
            daily_one_sentence_zh="一句话总结。",
            daily_core_points=["时序持续学习：双月窗口", "性能提升：F1 提升 0.016"],
            daily_key_results="Macro-F1 0.667（基线 0.651），训练时间减少 17%。",
        )
        paper = Paper("p1", "T", [], "abstract", "2026-06-07", "", "", "arxiv")
        content = exporter.render_daily([paper], [result], date(2026, 6, 7), {})
        self.assertIn("**核心贡献/观点**", content)
        self.assertIn("- 时序持续学习：双月窗口", content)
        self.assertIn("**关键结果**：Macro-F1 0.667", content)

    def test_daily_render_compacts_deep_note_fields(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult(
            "p1",
            8,
            "keep",
            "detailed_note",
            summary_zh="摘要",
            note_title_zh="短名",
            method_overview="### 总体思路\n\n这是一段非常长的方法解释。" * 20,
            why_read="值得读。",
        )
        content = exporter.render_daily([paper], [result], date(2026, 6, 7), {})
        self.assertIn("**核心贡献**：\n\n总体思路", content)
        self.assertNotIn("**核心贡献**：###", content)

    def test_daily_render_does_not_truncate_body_fields(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        long_tail = "这是结尾的完整判断，不能被截断。"
        result = RerankResult(
            "p1",
            8,
            "keep",
            "daily_only",
            summary_zh="摘要前半段。" + "补充解释。" * 80 + long_tail,
            why_read="看点前半段。" + "继续说明。" * 60 + long_tail,
            project_inspiration="启发前半段。" + "落到项目。" * 60 + long_tail,
            results="结果前半段。" + "指标解释。" * 60 + long_tail,
            key_innovation="创新前半段。" + "机制说明。" * 60 + long_tail,
        )
        content = exporter.render_daily([paper], [result], date(2026, 6, 7), {})
        self.assertGreaterEqual(content.count(long_tail), 5)

    def test_daily_render_preserves_structured_field_lists(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult(
            "p1",
            8,
            "keep",
            "daily_only",
            summary_zh="这是总述。\n- 第一条\n- 第二条",
            why_read="主要看三点： 1. 任务定义清楚 2. 指标有数字 3. 可以迁移",
            results="结果包括：\n1. Macro-F1 提升\n2. 训练更快",
        )
        content = exporter.render_daily([paper], [result], date(2026, 6, 7), {})
        self.assertIn("**一句话总结**：\n\n这是总述。\n\n- 第一条\n- 第二条", content)
        self.assertIn("**看点**：\n\n主要看三点：\n\n1. 任务定义清楚\n2. 指标有数字\n3. 可以迁移", content)
        self.assertIn("**关键结果**：\n\n结果包括：\n\n1. Macro-F1 提升\n2. 训练更快", content)

    def test_daily_render_prefers_daily_card_pass_fields(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper("p1", "Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult(
            "p1",
            8,
            "keep",
            "daily_only",
            summary_zh="旧摘要",
            daily_one_sentence_zh="短总结",
            daily_core_contribution_zh="核心贡献",
        )
        content = exporter.render_daily([paper], [result], date(2026, 6, 7), {})
        self.assertIn("**一句话总结**：短总结", content)
        self.assertIn("**核心贡献**：核心贡献", content)

    def test_daily_render_omits_empty_or_unknown_fields(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper("p1", "Paper", [], "", "", "", "", "arxiv")
        result = RerankResult(
            "p1",
            8,
            "keep",
            "daily_only",
            paper_type="--",
            reading_decision="--",
            code_availability="unknown",
            dataset_availability="not_found",
            summary_zh="短总结",
        )
        content = exporter.render_daily([paper], [result], date(2026, 6, 7), {})
        self.assertIn("**一句话总结**：短总结", content)
        self.assertNotIn("论文类型", content)
        self.assertNotIn("阅读建议", content)
        self.assertNotIn("决策", content)
        self.assertNotIn("开源/数据", content)
        self.assertNotIn("：--", content)
        self.assertNotIn("unknown", content)

    def test_generate_daily_cards_updates_short_fields_and_safe_title(self) -> None:
        paper = Paper("p1", "Long Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult(
            "p1",
            8,
            "keep",
            "daily_only",
            note_title_zh="这是一段非常长的摘要式标题，应该被替换掉",
        )
        client = Mock()
        client.chat_json.return_value = {
            "items": [
                {
                    "paper_id": "p1",
                    "short_title_zh": "证据规划抽取",
                    "one_sentence_summary_zh": "一句话说明。",
                    "core_contribution_zh": "核心贡献说明。",
                }
            ]
        }
        _generate_daily_cards([paper], [result], client)
        self.assertEqual(result.daily_one_sentence_zh, "一句话说明。")
        self.assertEqual(result.daily_core_contribution_zh, "核心贡献说明。")
        self.assertEqual(result.note_title_zh, "证据规划抽取")

    def test_generate_daily_cards_does_not_truncate_model_output(self) -> None:
        paper = Paper("p1", "Long Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult("p1", 8, "keep", "daily_only")
        long_tail = "这是模型输出的完整结尾。"
        long_text = "前半段。" + "持续解释。" * 60 + long_tail
        client = Mock()
        client.chat_json.return_value = {
            "items": [
                {
                    "paper_id": "p1",
                    "one_sentence_summary_zh": long_text,
                    "core_contribution_zh": long_text,
                    "core_points_zh": [long_text],
                    "key_results_zh": long_text,
                }
            ]
        }
        _generate_daily_cards([paper], [result], client)
        self.assertTrue(result.daily_one_sentence_zh.endswith(long_tail))
        self.assertTrue(result.daily_core_contribution_zh.endswith(long_tail))
        self.assertTrue(result.daily_core_points[0].endswith(long_tail))
        self.assertTrue(result.daily_key_results.endswith(long_tail))

    def test_generate_daily_cards_preserves_markdown_list_breaks(self) -> None:
        paper = Paper("p1", "Long Paper", [], "abstract", "2026-06-07", "", "", "arxiv")
        result = RerankResult("p1", 8, "keep", "daily_only")
        client = Mock()
        client.chat_json.return_value = {
            "items": [
                {
                    "paper_id": "p1",
                    "one_sentence_summary_zh": "这是总述。\n- 第一条\n- 第二条",
                    "key_results_zh": "关键结果： 1. Macro-F1 提升 2. 训练更快",
                }
            ]
        }
        _generate_daily_cards([paper], [result], client)
        self.assertEqual(result.daily_one_sentence_zh, "这是总述。\n- 第一条\n- 第二条")
        self.assertEqual(result.daily_key_results, "关键结果：\n1. Macro-F1 提升\n2. 训练更快")

    def test_folder_style_note_path_uses_chinese_title(self) -> None:
        exporter = ObsidianExporter(Path("Vault"), "每日科研论文/日报", "每日科研论文/笔记")
        paper = Paper(
            paper_id="arxiv:2606.06481",
            title="Operation-Guided Progressive Human-to-AI Text Transformation Benchmark",
            authors=[],
            abstract="",
            published="2026-06-07",
            url="",
            pdf_url="",
            source="arxiv",
        )
        result = RerankResult(
            paper_id=paper.paper_id,
            recommend_score=8,
            decision="keep",
            action="detailed_note",
            note_title_zh="渐进式人机文本转换基准",
        )
        path = exporter.note_path(paper, result, date(2026, 6, 7))
        self.assertEqual(path.as_posix(), "Vault/每日科研论文/笔记/06-07/渐进式人机文本转换基准/渐进式人机文本转换基准.md")


class JsonPayloadParseTest(unittest.TestCase):
    def test_single_object(self) -> None:
        self.assertEqual(_parse_json_payload('{"items": [1, 2]}'), {"items": [1, 2]})

    def test_strips_code_fence(self) -> None:
        self.assertEqual(_parse_json_payload('```json\n{"a": 1}\n```'), {"a": 1})

    def test_trailing_prose_after_object(self) -> None:
        # 合法对象后跟解释文字：不再 Extra data 报错，取第一个对象。
        self.assertEqual(_parse_json_payload('{"a": 1}\n\n以上是结果。'), {"a": 1})

    def test_multiple_bare_objects_wrapped_as_items(self) -> None:
        # flash 没套 items 外壳、逐个吐对象（线上 Extra data 的真实形态）→ 聚合成 items。
        content = (
            '{\n  "filename": "fig1.png",\n  "type": "architecture",\n  "usefulness": "high"\n}\n'
            '{\n  "filename": "fig2.png",\n  "type": "result_chart",\n  "usefulness": "low"\n}'
        )
        payload = _parse_json_payload(content)
        self.assertEqual([item["filename"] for item in payload["items"]], ["fig1.png", "fig2.png"])

    def test_prefers_object_with_items_wrapper(self) -> None:
        content = '{"items": [{"filename": "fig1.png"}]}\n{"note": "duplicate"}'
        self.assertEqual(_parse_json_payload(content), {"items": [{"filename": "fig1.png"}]})

    def test_no_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            _parse_json_payload("抱歉，我无法完成。")


if __name__ == "__main__":
    unittest.main()
