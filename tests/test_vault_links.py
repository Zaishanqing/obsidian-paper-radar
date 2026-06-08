from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar.vault_links import build_note_index, link_text, make_link_processor, _compile_pattern


def _write_note(base: Path, day: str, title: str, *, note_title: str | None = None, aliases: list[str] | None = None) -> None:
    note_dir = base / day / title
    note_dir.mkdir(parents=True, exist_ok=True)
    front = ["---"]
    if note_title:
        front.append(f'note_title: "{note_title}"')
    if aliases:
        front.append("aliases:")
        front.extend(f"  - {alias}" for alias in aliases)
    front.append("---")
    (note_dir / f"{title}.md").write_text("\n".join(front) + f"\n\n# {title}\n", encoding="utf-8")


class BuildNoteIndexTest(unittest.TestCase):
    def test_indexes_filename_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            paper_dir = "笔记"
            _write_note(vault / paper_dir, "06-07", "渐进式人机文本转换基准", aliases=["RAG检索增强"])

            index = build_note_index(vault, [paper_dir])

            self.assertIn("渐进式人机文本转换基准", index)
            self.assertIn("rag检索增强", index)
            self.assertEqual(index["渐进式人机文本转换基准"], "笔记/06-07/渐进式人机文本转换基准/渐进式人机文本转换基准")

    def test_skips_generic_stopwords(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            _write_note(vault / "笔记", "06-07", "方法")  # 通用词应被过滤
            index = build_note_index(vault, ["笔记"])
            self.assertNotIn("方法", index)


class LinkTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = {
            "渐进式人机文本转换基准": "笔记/06-07/渐进式人机文本转换基准/渐进式人机文本转换基准",
            "rag": "笔记/06-01/RAG/RAG",
        }
        self.pattern = _compile_pattern(self.index.keys())

    def test_links_known_title_inside_chinese_text(self) -> None:
        text = "今天复现了渐进式人机文本转换基准的实验。"
        out = link_text(text, self.index, self.pattern, set())
        self.assertIn("[[笔记/06-07/渐进式人机文本转换基准/渐进式人机文本转换基准|渐进式人机文本转换基准]]", out)

    def test_protects_code_and_existing_links_and_frontmatter(self) -> None:
        text = (
            "---\nnote_title: RAG\n---\n"
            "正文提到 RAG 一次。\n"
            "`RAG` 在行内代码里不应被链接。\n"
            "```\nRAG in code block\n```\n"
            "[[已有链接 RAG]] 也不应被改。\n"
        )
        out = link_text(text, self.index, self.pattern, set(), per_key=5)
        # frontmatter 原样保留
        self.assertTrue(out.startswith("---\nnote_title: RAG\n---\n"))
        # 行内代码与代码块原样保留
        self.assertIn("`RAG` 在行内代码", out)
        self.assertIn("```\nRAG in code block\n```", out)
        self.assertIn("[[已有链接 RAG]]", out)
        # 正文中的裸 RAG 被链接
        self.assertIn("[[笔记/06-01/RAG/RAG|RAG]]", out)

    def test_skip_paths_avoids_self_link(self) -> None:
        text = "本文即渐进式人机文本转换基准。"
        skip = {"笔记/06-07/渐进式人机文本转换基准/渐进式人机文本转换基准"}
        out = link_text(text, self.index, self.pattern, skip)
        self.assertNotIn("[[", out)

    def test_max_links_per_term_caps_repeats(self) -> None:
        text = "RAG 又 RAG 再 RAG。"
        out = link_text(text, self.index, self.pattern, set(), per_key=1)
        self.assertEqual(out.count("[[笔记/06-01/RAG/RAG|"), 1)


class MakeLinkProcessorTest(unittest.TestCase):
    def test_returns_none_for_empty_index(self) -> None:
        self.assertIsNone(make_link_processor({}, {}))

    def test_processor_links_text(self) -> None:
        processor = make_link_processor({"rag": "笔记/RAG/RAG"}, {"max_links_per_doc": 5, "max_links_per_term": 1})
        assert processor is not None
        out = processor("我们用 RAG 做检索。", set())
        self.assertIn("[[笔记/RAG/RAG|RAG]]", out)


if __name__ == "__main__":
    unittest.main()
