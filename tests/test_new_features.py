from __future__ import annotations

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

from obsidian_paper_radar import citation_network as cn
from obsidian_paper_radar import feedback as fb
from obsidian_paper_radar.health import build_run_report, format_report_line
from obsidian_paper_radar.models import Paper, RerankResult
from obsidian_paper_radar.moc import update_mocs
from obsidian_paper_radar.search import rough_score
from obsidian_paper_radar.weekly_digest import build_weekly_digest


def _paper(paper_id: str, source: str, *, abstract: str = "x", extra: dict | None = None) -> Paper:
    return Paper(
        paper_id=paper_id,
        title=paper_id,
        authors=[],
        abstract=abstract,
        published="2026",
        url="",
        pdf_url="",
        source=source,
        extra_context=extra or {},
    )


class HealthReportTest(unittest.TestCase):
    def test_counts_sources_fallback_and_verification(self) -> None:
        candidates = [_paper("arxiv:1", "arxiv"), _paper("arxiv:2", "arxiv"), _paper("dblp:x", "dblp:aaai")]
        llm = [_paper("arxiv:1", "arxiv", extra={"open_source": {"has_verified_code": True}})]
        results = [
            RerankResult(paper_id="arxiv:1", recommend_score=8, decision="keep", action="detailed_note", fallback_used=True),
        ]
        report = build_run_report(candidates, llm, results, results, 2, 1, date(2026, 6, 7))
        self.assertEqual(report["candidates_total"], 3)
        self.assertEqual(report["candidates_by_source"], {"arxiv": 2, "dblp": 1})
        self.assertEqual(report["llm_fallback"], 1)
        self.assertEqual(report["open_source_verified_code"], 1)
        self.assertIn("候选 3", format_report_line(report))


class FeedbackTest(unittest.TestCase):
    def test_derive_and_apply_adjustments(self) -> None:
        feedback = {
            "p1": {"label": "useful", "tags": ["RAG"]},
            "p2": {"label": "useless", "tags": ["benchmark"]},
        }
        deltas = fb.derive_adjustments(feedback, boost=1.5, penalty=2.0)
        self.assertEqual(deltas["rag"], 1.5)
        self.assertEqual(deltas["benchmark"], -2.0)
        self.assertEqual(fb.adjustment_for_text("a new rag method", deltas), 1.5)
        self.assertEqual(fb.adjustment_for_text("yet another benchmark", deltas), -2.0)

    def test_adjustment_is_clamped(self) -> None:
        deltas = {"a": 10.0, "b": 10.0}
        self.assertEqual(fb.adjustment_for_text("a and b", deltas, max_adjust=3.0), 3.0)

    def test_record_feedback_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(fb, "STATE_DIR", Path(temp_dir)):
                fb.record_feedback("arxiv:1", "useful", tags=["rag"], title="T")
                data = fb.load_feedback()
        self.assertEqual(data["arxiv:1"]["label"], "useful")
        self.assertEqual(data["arxiv:1"]["tags"], ["rag"])

    def test_import_daily_feedback_supports_obsidian_comment_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            daily_dir = vault / "日报"
            daily_dir.mkdir()
            (daily_dir / "06-07 论文日报.md").write_text(
                "- [x] **有用** · [[笔记/A|A]] %%pid:arxiv:1%%\n"
                "- [x] **无用** · [[笔记/B|B]] %%pid:arxiv:2%%\n",
                encoding="utf-8",
            )
            with patch.object(fb, "STATE_DIR", vault / "state"):
                count = fb.import_feedback_from_daily(vault, "日报")
                data = fb.load_feedback()
        self.assertEqual(count, 2)
        self.assertEqual(data["arxiv:1"]["label"], "useful")
        self.assertEqual(data["arxiv:2"]["label"], "useless")

    def test_import_daily_feedback_supports_legacy_html_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            daily_dir = vault / "日报"
            daily_dir.mkdir()
            (daily_dir / "06-07 论文日报.md").write_text(
                "- [x] **有用** · [[笔记/A|A]] <!-- pid:arxiv:1 -->\n",
                encoding="utf-8",
            )
            with patch.object(fb, "STATE_DIR", vault / "state"):
                count = fb.import_feedback_from_daily(vault, "日报")
                data = fb.load_feedback()
        self.assertEqual(count, 1)
        self.assertEqual(data["arxiv:1"]["label"], "useful")

    def test_import_daily_feedback_supports_compact_hidden_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            daily_dir = vault / "日报"
            daily_dir.mkdir()
            (daily_dir / "06-07 论文日报.md").write_text(
                "- [x] **不推荐** · [[笔记/A|A]] <!-- feedback:useless pid:arxiv:1 -->\n"
                "- [ ] **不推荐** · [[笔记/B|B]] <!-- feedback:useless pid:arxiv:2 -->\n",
                encoding="utf-8",
            )
            with patch.object(fb, "STATE_DIR", vault / "state"):
                count = fb.import_feedback_from_daily(vault, "日报")
                data = fb.load_feedback()
        self.assertEqual(count, 1)
        self.assertEqual(data["arxiv:1"]["label"], "useless")
        self.assertNotIn("arxiv:2", data)

    def test_rough_score_applies_feedback_boost(self) -> None:
        paper = _paper("arxiv:1", "arxiv", abstract="a retrieval augmented generation method")
        scored = rough_score([paper], {}, date(2026, 6, 7), {}, {"retrieval augmented generation": 2.0}, 3.0)
        baseline = _paper("arxiv:2", "arxiv", abstract="a retrieval augmented generation method")
        plain = rough_score([baseline], {}, date(2026, 6, 7), {})
        self.assertGreater(scored[0].rough_score, plain[0].rough_score)


class CitationNetworkTest(unittest.TestCase):
    def test_s2_id_resolution(self) -> None:
        self.assertEqual(cn._s2_id_for(_paper("arxiv:2601.00001", "arxiv")), "ARXIV:2601.00001")
        self.assertEqual(cn._s2_id_for(_paper("dblp:x", "dblp:aaai", extra={"doi": "10.1/x"})), "DOI:10.1/x")
        self.assertEqual(cn._s2_id_for(_paper("dblp:y", "dblp:aaai")), "")

    def test_enrich_populates_extra_context(self) -> None:
        paper = _paper("arxiv:2601.00001", "arxiv")
        resp = Mock(status_code=200)
        resp.json.return_value = {
            "references": [{"title": "Ref A", "year": 2024, "externalIds": {"ArXiv": "2400.00001"}, "citationCount": 50}],
            "citations": [{"title": "Cite B", "year": 2026, "externalIds": {"DOI": "10.1/x"}, "citationCount": 3}],
        }
        resp.raise_for_status.return_value = None
        with patch.object(cn, "request_with_retry", return_value=resp):
            cn.enrich_citation_network([paper], {"enabled": True, "cache": {"enabled": False}})
        network = paper.extra_context["citation_network"]
        self.assertEqual(network["references"][0]["title"], "Ref A")
        self.assertTrue(network["references"][0]["url"].endswith("2400.00001"))
        self.assertEqual(network["citations"][0]["title"], "Cite B")


class MocTest(unittest.TestCase):
    def test_writes_moc_for_topics_above_threshold(self) -> None:
        seen = {
            "p1": {"title": "A", "note_path": "笔记/06-07/A/A", "status": "noted", "tags": ["RAG"]},
            "p2": {"title": "B", "note_path": "笔记/06-07/B/B", "status": "noted", "tags": ["RAG"]},
            "p3": {"title": "C", "note_path": "笔记/06-07/C/C", "status": "noted", "tags": ["lonely"]},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            written = update_mocs(Path(temp_dir), "MOC", seen, {"enabled": True, "min_notes_per_topic": 2})
            rag = Path(temp_dir) / "MOC" / "RAG.md"
            self.assertTrue(rag.exists())
            self.assertIn("[[笔记/06-07/A/A|A]]", rag.read_text(encoding="utf-8"))
            self.assertFalse((Path(temp_dir) / "MOC" / "lonely.md").exists())
            self.assertEqual(len(written), 1)


class WeeklyDigestTest(unittest.TestCase):
    ITEMS = [{"title": "P1", "tags": ["rag"], "last_score": 8.0, "note_path": "笔记/x/x"}]

    def test_rule_based_digest_without_client(self) -> None:
        out = build_weekly_digest(self.ITEMS, date(2026, 6, 7), client=None)
        self.assertIn("本周推荐清单", out)
        self.assertIn("[[笔记/x/x|P1]]", out)

    def test_llm_digest_includes_hot_directions(self) -> None:
        client = Mock()
        client.chat_json.return_value = {
            "overview": "概览",
            "hot_directions": ["RAG"],
            "must_read": [{"title": "P1", "reason": "R"}],
            "backlog_note": "积压",
        }
        out = build_weekly_digest(self.ITEMS, date(2026, 6, 7), client=client)
        self.assertIn("本周热点方向", out)
        self.assertIn("RAG", out)

    def test_empty_items_returns_none(self) -> None:
        self.assertIsNone(build_weekly_digest([], date(2026, 6, 7)))


if __name__ == "__main__":
    unittest.main()
