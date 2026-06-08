from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar import open_source_check as osc
from obsidian_paper_radar.models import Paper, RerankResult


def _paper(abstract: str, extra: dict | None = None) -> Paper:
    return Paper(
        paper_id="arxiv:1",
        title="t",
        authors=[],
        abstract=abstract,
        published="2026",
        url="",
        pdf_url="",
        source="arxiv",
        extra_context=extra or {},
    )


class ExtractCandidateUrlsTest(unittest.TestCase):
    def test_extracts_code_and_dataset_links(self) -> None:
        paper = _paper("Code at https://github.com/foo/bar. Data at https://huggingface.co/datasets/foo.")
        code, dataset = osc.extract_candidate_urls(paper)
        self.assertIn("https://github.com/foo/bar", code)
        self.assertTrue(any("huggingface.co" in url for url in dataset))

    def test_normalizes_github_subpaths(self) -> None:
        paper = _paper("See https://github.com/foo/bar/tree/main/src for details.")
        code, _ = osc.extract_candidate_urls(paper)
        self.assertEqual(code, ["https://github.com/foo/bar"])

    def test_ignores_arxiv_and_doi_links(self) -> None:
        paper = _paper("https://arxiv.org/abs/2606.1 and https://doi.org/10.1/x")
        code, dataset = osc.extract_candidate_urls(paper)
        self.assertEqual(code, [])
        self.assertEqual(dataset, [])

    def test_reads_repo_url_candidates_from_context(self) -> None:
        paper = _paper("No links in abstract.", {"repo_url_candidates": ["https://github.com/owner/repo"]})
        code, _ = osc.extract_candidate_urls(paper)
        self.assertIn("https://github.com/owner/repo", code)


class VerifyAndReconcileTest(unittest.TestCase):
    def test_github_verification_upgrades_code_availability(self) -> None:
        paper = _paper("https://github.com/foo/bar")
        resp = Mock(status_code=200)
        resp.json.return_value = {"stargazers_count": 123, "archived": False, "pushed_at": "2026-01-01"}
        with patch.object(osc, "request_with_retry", return_value=resp):
            evidence = osc.verify_paper_open_source(paper, {"github_api": True, "cache": {"enabled": False}}, None)

        self.assertTrue(evidence["has_verified_code"])
        paper.extra_context["open_source"] = evidence
        result = RerankResult(
            paper_id="arxiv:1", recommend_score=5, decision="keep", action="daily_only", code_availability="mentioned"
        )
        osc.reconcile_result(result, paper)
        self.assertEqual(result.code_availability, "available")
        self.assertTrue(any("已联网核验" in signal and "123" in signal for signal in result.reproducibility_signals))

    def test_dead_code_link_adds_caution_signal(self) -> None:
        paper = _paper("https://github.com/foo/bar")
        resp = Mock(status_code=404)
        with patch.object(osc, "request_with_retry", return_value=resp):
            evidence = osc.verify_paper_open_source(paper, {"github_api": True, "cache": {"enabled": False}}, None)

        self.assertFalse(evidence["has_verified_code"])
        self.assertTrue(evidence["code_link_unverified"])
        paper.extra_context["open_source"] = evidence
        result = RerankResult(paper_id="arxiv:1", recommend_score=5, decision="keep", action="daily_only")
        osc.reconcile_result(result, paper)
        self.assertTrue(any("联网未访问成功" in signal for signal in result.reproducibility_signals))

    def test_no_links_leaves_availability_untouched(self) -> None:
        paper = _paper("This paper has no code or dataset links at all.")
        evidence = osc.verify_paper_open_source(paper, {"cache": {"enabled": False}}, None)
        self.assertFalse(evidence["has_verified_code"])
        paper.extra_context["open_source"] = evidence
        result = RerankResult(
            paper_id="arxiv:1", recommend_score=5, decision="keep", action="daily_only", code_availability="unknown"
        )
        osc.reconcile_result(result, paper)
        self.assertEqual(result.code_availability, "unknown")


if __name__ == "__main__":
    unittest.main()
