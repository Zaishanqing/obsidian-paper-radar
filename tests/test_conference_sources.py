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

from obsidian_paper_radar.conference_sources import (
    _cached_fetch,
    _openreview_venue_candidates,
    _pmlr_volume_override,
    _write_cache,
    fetch_acl_anthology,
    fetch_cvf,
    fetch_dblp,
    fetch_openreview,
    fetch_pmlr,
)
from obsidian_paper_radar.models import Paper
from obsidian_paper_radar.search import rough_score


def _cache_paper(paper_id: str) -> Paper:
    return Paper(
        paper_id=paper_id,
        title=paper_id,
        authors=[],
        abstract="abstract",
        published="2026",
        url="",
        pdf_url="",
        source="cvf:cvpr",
    )


class ConferenceSourcesTest(unittest.TestCase):
    def test_openreview_source_normalizes_public_notes(self) -> None:
        response = Mock()
        response.json.return_value = {
            "notes": [
                {
                    "id": "abc123",
                    "forum": "abc123",
                    "content": {
                        "title": {"value": "Test OpenReview Paper"},
                        "abstract": {"value": "This paper proposes a useful architecture."},
                        "authors": {"value": ["Alice", "Bob"]},
                        "pdf": {"value": "/pdf?id=abc123"},
                    },
                }
            ]
        }
        response.raise_for_status.return_value = None

        with patch("obsidian_paper_radar.conference_sources.requests.get", return_value=response):
            papers = fetch_openreview(
                {
                    "enabled": True,
                    "conferences": ["iclr"],
                    "years": [2026],
                    "max_results": 1,
                    "cache": {"enabled": False},
                    "sleep_between_requests_seconds": 0,
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, "openreview:iclr")
        self.assertEqual(papers[0].categories[0], "ICLR")
        self.assertEqual(papers[0].pdf_url, "https://openreview.net/pdf?id=abc123")

    def test_cvf_source_fetches_listing_and_detail(self) -> None:
        listing = '<dt class="ptitle"><br><a href="/content/CVPR2026/html/Test.html">Test CVF Paper</a></dt>'
        detail = """
        <div id="authors">Alice, Bob</div>
        <div id="abstract">This paper proposes a transferable vision module.</div>
        <a href="/content/CVPR2026/papers/Test.pdf">pdf</a>
        """

        listing_response = Mock()
        listing_response.text = listing
        listing_response.raise_for_status.return_value = None
        detail_response = Mock()
        detail_response.text = detail
        detail_response.raise_for_status.return_value = None

        with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=[listing_response, detail_response]):
            papers = fetch_cvf(
                {
                    "enabled": True,
                    "conferences": ["cvpr"],
                    "years": [2026],
                    "max_results": 1,
                    "cache": {"enabled": False},
                    "sleep_between_requests_seconds": 0,
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, "cvf:cvpr")
        self.assertEqual(papers[0].categories[0], "CVPR")
        self.assertTrue(papers[0].pdf_url.endswith("/content/CVPR2026/papers/Test.pdf"))

    def test_cvf_source_prefers_main_pdf_over_supplemental(self) -> None:
        listing = '<dt class="ptitle"><br><a href="/content/CVPR2026/html/Test.html">Test CVF Paper</a></dt>'
        detail = """
        <div id="authors">Alice, Bob</div>
        <div id="abstract">This paper proposes a transferable vision module.</div>
        <a href="/content/CVPR2026/supplemental/Test_supplemental.pdf">supp</a>
        <a href="/content/CVPR2026/papers/Test.pdf">pdf</a>
        """
        listing_response = Mock()
        listing_response.text = listing
        listing_response.raise_for_status.return_value = None
        detail_response = Mock()
        detail_response.text = detail
        detail_response.raise_for_status.return_value = None

        with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=[listing_response, detail_response]):
            papers = fetch_cvf(
                {
                    "enabled": True,
                    "conferences": ["cvpr"],
                    "years": [2026],
                    "max_results": 1,
                    "cache": {"enabled": False},
                    "sleep_between_requests_seconds": 0,
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertTrue(papers[0].pdf_url.endswith("/content/CVPR2026/papers/Test.pdf"))
        self.assertNotIn("supplemental", papers[0].pdf_url)

    def test_openreview_source_uses_cache(self) -> None:
        response = Mock()
        response.json.return_value = {
            "notes": [
                {
                    "id": "cached123",
                    "forum": "cached123",
                    "content": {
                        "title": {"value": "Cached OpenReview Paper"},
                        "abstract": {"value": "A cached architecture paper."},
                    },
                }
            ]
        }
        response.raise_for_status.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = {
                "enabled": True,
                "conferences": ["iclr"],
                "years": [2026],
                "max_results": 1,
                "cache": {"enabled": True, "dir": temp_dir, "ttl_days": 30},
                "sleep_between_requests_seconds": 0,
            }
            with patch("obsidian_paper_radar.conference_sources.requests.get", return_value=response):
                first = fetch_openreview(cfg, date(2026, 6, 7))
            with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=AssertionError("network should not be used")):
                second = fetch_openreview(cfg, date(2026, 6, 7))

        self.assertEqual(first[0].title, second[0].title)

    def test_pmlr_source_fetches_index_listing_and_detail(self) -> None:
        index = '<a href="v999/">Volume 999 Proceedings of ICML 2026</a>'
        listing = '<a href="demo26a.html">abs</a>'
        detail = """
        <meta name="citation_title" content="Test PMLR Paper">
        <meta name="citation_author" content="Alice">
        <meta name="citation_pdf_url" content="https://example.com/paper.pdf">
        <meta name="citation_publication_date" content="2026/07/01">
        <h4>Abstract</h4><div id="abstract" class="abstract">This paper proposes a learning mechanism.</div>
        <a href="https://github.com/example/code">Software</a>
        """
        responses = []
        for text in (index, listing, detail):
            response = Mock()
            response.text = text
            response.raise_for_status.return_value = None
            responses.append(response)

        with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=responses):
            papers = fetch_pmlr(
                {
                    "enabled": True,
                    "conferences": ["icml"],
                    "years": [2026],
                    "max_results": 1,
                    "cache": {"enabled": False},
                    "sleep_between_requests_seconds": 0,
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, "pmlr:icml")
        self.assertTrue(papers[0].extra_context["has_software_link"])

    def test_acl_anthology_package_backend_fetches_volume(self) -> None:
        class Name:
            first = "Alice"
            last = "Author"

        class Author:
            name = Name()

        class PackagePaper:
            id = "1"
            title = "Package ACL Paper"
            abstract = "This paper proposes a package backed metadata reader."
            authors = [Author()]
            year = 2026

        class PackageVolume:
            def papers(self) -> list[PackagePaper]:
                return [PackagePaper()]

        class PackageAnthology:
            def get_volume(self, full_id: str) -> PackageVolume | None:
                return PackageVolume() if full_id == "2026.acl-long" else None

        with patch("obsidian_paper_radar.conference_sources._load_acl_anthology", return_value=PackageAnthology()), patch(
            "obsidian_paper_radar.conference_sources.requests.get", side_effect=AssertionError("network should not be used")
        ):
            papers = fetch_acl_anthology(
                {
                    "enabled": True,
                    "backend_order": ["package"],
                    "conferences": ["acl"],
                    "years": [2026],
                    "max_results": 1,
                    "per_volume_limit": 1,
                    "cache": {"enabled": False},
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "Package ACL Paper")
        self.assertEqual(papers[0].source, "acl:acl")
        self.assertEqual(papers[0].extra_context["acl_backend"], "package")

    def test_acl_anthology_falls_back_from_package_to_github_xml(self) -> None:
        xml = """
        <collection id="2026.acl">
          <volume id="long">
            <paper id="1">
              <title>XML ACL Paper</title>
              <author><first>Alice</first><last>Author</last></author>
              <abstract>This paper proposes an XML metadata reader.</abstract>
            </paper>
          </volume>
        </collection>
        """
        xml_response = Mock(status_code=200)
        xml_response.text = xml
        xml_response.raise_for_status.return_value = None

        with patch("obsidian_paper_radar.conference_sources._load_acl_anthology", side_effect=RuntimeError("no git")), patch(
            "obsidian_paper_radar.conference_sources.requests.get", return_value=xml_response
        ):
            papers = fetch_acl_anthology(
                {
                    "enabled": True,
                    "backend_order": ["package", "github_xml"],
                    "conferences": ["acl"],
                    "years": [2026],
                    "max_results": 1,
                    "per_volume_limit": 1,
                    "cache": {"enabled": False},
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].paper_id, "acl:2026.acl-long.1")
        self.assertEqual(papers[0].authors, ["Alice Author"])
        self.assertEqual(papers[0].extra_context["acl_backend"], "github_xml")

    def test_acl_anthology_falls_back_from_xml_to_dblp_and_semantic_scholar(self) -> None:
        xml_response = Mock(status_code=500)
        xml_response.raise_for_status.side_effect = RuntimeError("xml failed")

        with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=[xml_response, _dblp_response()]), patch(
            "obsidian_paper_radar.net_cache.requests.request", return_value=_s2_batch_response()
        ):
            papers = fetch_acl_anthology(
                {
                    "enabled": True,
                    "backend_order": ["github_xml", "dblp_semantic_scholar"],
                    "conferences": ["acl"],
                    "years": [2026],
                    "max_results": 1,
                    "per_venue_limit": 1,
                    "roster_limit": 10,
                    "cache": {"enabled": False},
                    "enrich_cache": False,
                    "keep_without_abstract": False,
                    "backoff_seconds": 0,
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, "acl:acl")
        self.assertEqual(papers[0].abstract, "We propose a retrieval augmented method.")
        self.assertEqual(papers[0].extra_context["acl_backend"], "dblp_semantic_scholar")

    def test_acl_anthology_html_backend_fetches_volume_and_detail(self) -> None:
        volume = '<a href="/2026.acl-long.1/">A paper</a>'
        detail = """
        <meta content="Test ACL Paper" name="citation_title">
        <meta content="Alice" name="citation_author">
        <meta content="2026/7" name="citation_publication_date">
        <meta content="https://aclanthology.org/2026.acl-long.1.pdf" name="citation_pdf_url">
        <meta property="og:description" content="This paper proposes a language model module.">
        """
        volume_response = Mock()
        volume_response.text = volume
        volume_response.raise_for_status.return_value = None
        detail_response = Mock()
        detail_response.text = detail
        detail_response.raise_for_status.return_value = None

        with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=[volume_response, detail_response]):
            papers = fetch_acl_anthology(
                {
                    "enabled": True,
                    "backend_order": ["html"],
                    "conferences": ["acl"],
                    "years": [2026],
                    "max_results": 1,
                    "per_volume_limit": 1,
                    "cache": {"enabled": False},
                    "sleep_between_requests_seconds": 0,
                },
                date(2026, 6, 7),
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, "acl:acl")
        self.assertEqual(papers[0].categories[0], "ACL")

    def test_hotness_strategy_boosts_top_conference_papers(self) -> None:
        paper = Paper(
            paper_id="pmlr:icml-2026-test",
            title="Test",
            authors=[],
            abstract="No explicit interest keyword.",
            published="2026-07-01",
            url="",
            pdf_url="",
            source="pmlr:icml",
            extra_context={"venue": "ICML", "has_software_link": True},
        )
        scored = rough_score([paper], {}, date(2026, 6, 7), {"hotness": {"enabled": True, "top_venues": ["icml"]}})
        self.assertGreaterEqual(scored[0].rough_score, 3.0)


def _dblp_response() -> Mock:
    response = Mock()
    response.json.return_value = {
        "result": {
            "hits": {
                "hit": [
                    {
                        "info": {
                            "title": "A Retrieval Augmented Method.",
                            "authors": {"author": [{"text": "Alice"}, {"text": "Bob 0001"}]},
                            "year": "2026",
                            "doi": "10.1609/aaai.v1.1",
                            "ee": "https://doi.org/10.1609/aaai.v1.1",
                            "url": "https://dblp.org/rec/conf/aaai/x",
                        }
                    }
                ]
            }
        }
    }
    response.raise_for_status.return_value = None
    return response


def _s2_batch_response() -> Mock:
    response = Mock(status_code=200, headers={})
    response.json.return_value = [
        {
            "abstract": "We propose a retrieval augmented method.",
            "citationCount": 5,
            "influentialCitationCount": 1,
            "externalIds": {"ArXiv": "2601.00001"},
        }
    ]
    response.raise_for_status.return_value = None
    return response


class DblpSourceTest(unittest.TestCase):
    BASE_CFG = {
        "enabled": True,
        "conferences": ["aaai"],
        "years": [2026],
        "max_results": 5,
        "per_venue_limit": 5,
        "roster_limit": 10,
        "cache": {"enabled": False},
        "enrich_cache": False,
        "sleep_between_requests_seconds": 0,
        "backoff_seconds": 0,
    }

    def test_fetches_roster_and_enriches_abstract_via_s2_batch(self) -> None:
        cfg = {**self.BASE_CFG, "enrich_abstracts": True}
        with patch("obsidian_paper_radar.conference_sources.requests.get", return_value=_dblp_response()), patch(
            "obsidian_paper_radar.net_cache.requests.request", return_value=_s2_batch_response()
        ):
            papers = fetch_dblp(cfg, date(2026, 6, 7), profile={"long_term_interests": ["retrieval augmented"]})

        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper.source, "dblp:aaai")
        self.assertEqual(paper.abstract, "We propose a retrieval augmented method.")
        self.assertIn("AAAI", paper.categories)
        self.assertIn("cs.AI", paper.categories)
        self.assertEqual(paper.authors, ["Alice", "Bob"])  # 去掉 DBLP 同名消歧编号
        self.assertTrue(paper.pdf_url.endswith("2601.00001"))

    def test_drops_papers_without_abstract_by_default(self) -> None:
        cfg = {**self.BASE_CFG, "enrich_abstracts": False, "keep_without_abstract": False}
        with patch("obsidian_paper_radar.conference_sources.requests.get", return_value=_dblp_response()):
            papers = fetch_dblp(cfg, date(2026, 6, 7), profile=None)
        self.assertEqual(papers, [])

    def test_keyword_prefilter_excludes_non_matching_titles(self) -> None:
        cfg = {**self.BASE_CFG, "enrich_abstracts": False, "keep_without_abstract": True}
        with patch("obsidian_paper_radar.conference_sources.requests.get", return_value=_dblp_response()):
            papers = fetch_dblp(cfg, date(2026, 6, 7), profile={"long_term_interests": ["quantum computing"]})
        self.assertEqual(papers, [])


class CacheMergeTest(unittest.TestCase):
    def test_incremental_merge_and_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = {"cache": {"enabled": True, "dir": temp_dir, "ttl_days": 30}}
            calls: list[str] = []

            def fetch_one() -> list[Paper]:
                calls.append("a")
                return [_cache_paper("p1")]

            out1 = _cached_fetch(cfg, "cvf", "cvpr-2026", 1, fetch_one)
            self.assertEqual([p.paper_id for p in out1], ["p1"])

            def fetch_two() -> list[Paper]:
                calls.append("b")
                return [_cache_paper("p2")]

            # needed (2) exceeds the historical fetch size (1) -> refetch and merge by paper_id
            out2 = _cached_fetch(cfg, "cvf", "cvpr-2026", 2, fetch_two)
            self.assertEqual(sorted(p.paper_id for p in out2), ["p1", "p2"])

            def fetch_fail() -> list[Paper]:
                raise AssertionError("cache should have satisfied the request")

            out3 = _cached_fetch(cfg, "cvf", "cvpr-2026", 2, fetch_fail)
            self.assertEqual(sorted(p.paper_id for p in out3), ["p1", "p2"])
            self.assertEqual(calls, ["a", "b"])

    def test_legacy_count_files_merged_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy_dir = Path(temp_dir) / "cvf"
            legacy_dir.mkdir(parents=True)
            legacy_path = legacy_dir / "cvpr-2026-n5.json"
            _write_cache(legacy_path, [_cache_paper("p_legacy")], requested_max=5)

            cfg = {"cache": {"enabled": True, "dir": temp_dir, "ttl_days": 30}}

            def fetch_new() -> list[Paper]:
                raise AssertionError("legacy cache already covers the request")

            out = _cached_fetch(cfg, "cvf", "cvpr-2026", 1, fetch_new)
            self.assertEqual([p.paper_id for p in out], ["p_legacy"])
            self.assertFalse(legacy_path.exists())
            self.assertTrue((legacy_dir / "cvpr-2026.json").exists())

    def test_empty_cache_hit_avoids_refetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = {"cache": {"enabled": True, "dir": temp_dir, "ttl_days": 30}}

            out1 = _cached_fetch(cfg, "pmlr", "icml-2026", 5, lambda: [])
            self.assertEqual(out1, [])

            def fetch_fail() -> list[Paper]:
                raise AssertionError("empty cache should have satisfied the request")

            out2 = _cached_fetch(cfg, "pmlr", "icml-2026", 5, fetch_fail)
            self.assertEqual(out2, [])

    def test_acl_404_volume_is_cached_as_empty(self) -> None:
        response = Mock(status_code=404)
        response.raise_for_status.side_effect = AssertionError("404 should be handled as empty")
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = {
                "enabled": True,
                "backend_order": ["html"],
                "conferences": ["acl"],
                "years": [2026],
                "max_results": 1,
                "per_volume_limit": 1,
                "cache": {"enabled": True, "dir": temp_dir, "ttl_days": 30},
                "sleep_between_requests_seconds": 0,
            }
            with patch("obsidian_paper_radar.conference_sources.requests.get", return_value=response):
                self.assertEqual(fetch_acl_anthology(cfg, date(2026, 6, 7)), [])
            with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=AssertionError("network should not be used")):
                self.assertEqual(fetch_acl_anthology(cfg, date(2026, 6, 7)), [])


class VenueMappingTest(unittest.TestCase):
    def test_openreview_override_takes_priority(self) -> None:
        candidates = _openreview_venue_candidates("iclr", 2026, {"venue_id_overrides": {"iclr-2026": "Custom/2026/Conference"}})
        self.assertEqual(candidates[0], "Custom/2026/Conference")
        self.assertIn("ICLR.cc/2026/Conference", candidates)

    def test_openreview_neurips_includes_datasets_track(self) -> None:
        candidates = _openreview_venue_candidates("neurips", 2025, {})
        self.assertIn("NeurIPS.cc/2025/Conference", candidates)
        self.assertTrue(any("Datasets_and_Benchmarks" in candidate for candidate in candidates))

    def test_pmlr_volume_override_builds_url(self) -> None:
        url = _pmlr_volume_override("icml", 2024, {"volume_overrides": {"icml-2024": "v235"}})
        self.assertTrue(url.endswith("/v235/"))

    def test_openreview_falls_back_to_second_candidate(self) -> None:
        empty = Mock()
        empty.json.return_value = {"notes": []}
        empty.raise_for_status.return_value = None
        full = Mock()
        full.json.return_value = {
            "notes": [
                {
                    "id": "x",
                    "forum": "x",
                    "content": {"title": {"value": "T"}, "abstract": {"value": "A useful method."}},
                }
            ]
        }
        full.raise_for_status.return_value = None

        cfg = {
            "enabled": True,
            "conferences": ["iclr"],
            "years": [2026],
            "max_results": 1,
            "cache": {"enabled": False},
            "sleep_between_requests_seconds": 0,
            "venue_id_overrides": {"iclr-2026": ["First/2026/Conference", "Second/2026/Conference"]},
        }
        with patch("obsidian_paper_radar.conference_sources.requests.get", side_effect=[empty, full]):
            papers = fetch_openreview(cfg, date(2026, 6, 7))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "T")


if __name__ == "__main__":
    unittest.main()
