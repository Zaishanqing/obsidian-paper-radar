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

from obsidian_paper_radar.search import _fetch_semantic_scholar

PROFILE = {"long_term_interests": ["large language model"]}


def _s2_response() -> Mock:
    resp = Mock(status_code=200, headers={})
    resp.json.return_value = {"data": [{"title": "Cached Paper", "abstract": "abs", "paperId": "x1"}]}
    resp.raise_for_status.return_value = None
    return resp


class SemanticScholarTest(unittest.TestCase):
    def test_caches_query_results_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = {
                "enabled": True,
                "max_results": 5,
                "lookback_days": 30,
                "sleep_between_requests_seconds": 0,
                "cache": {"enabled": True, "dir": temp_dir, "ttl_days": 3},
            }
            with patch("obsidian_paper_radar.net_cache.requests.request", return_value=_s2_response()):
                first = _fetch_semantic_scholar(cfg, PROFILE, date(2026, 6, 7), None)
            # Second run must come from disk cache, never touching the network.
            with patch("obsidian_paper_radar.net_cache.requests.request", side_effect=AssertionError("network should not be used")):
                second = _fetch_semantic_scholar(cfg, PROFILE, date(2026, 6, 7), None)

        self.assertEqual(len(first), 1)
        self.assertEqual(second[0].title, "Cached Paper")

    def test_retries_on_429_then_succeeds(self) -> None:
        resp_429 = Mock(status_code=429, headers={})
        cfg = {
            "enabled": True,
            "max_results": 5,
            "lookback_days": 30,
            "sleep_between_requests_seconds": 0,
            "backoff_seconds": 0,
            "max_retries": 3,
            "cache": {"enabled": False},
        }
        with patch("obsidian_paper_radar.net_cache.time.sleep", lambda _s: None):
            with patch("obsidian_paper_radar.net_cache.requests.request", side_effect=[resp_429, _s2_response()]):
                papers = _fetch_semantic_scholar(cfg, PROFILE, date(2026, 6, 7), None)
        self.assertEqual(len(papers), 1)

    def test_stops_after_persistent_rate_limit(self) -> None:
        resp_429 = Mock(status_code=429, headers={})
        cfg = {
            "enabled": True,
            "max_results": 5,
            "lookback_days": 30,
            "sleep_between_requests_seconds": 0,
            "backoff_seconds": 0,
            "max_retries": 1,
            "stop_on_rate_limit": True,
            "cache": {"enabled": False},
        }
        with patch("obsidian_paper_radar.net_cache.requests.request", return_value=resp_429):
            papers = _fetch_semantic_scholar(cfg, PROFILE, date(2026, 6, 7), None)
        self.assertEqual(papers, [])


if __name__ == "__main__":
    unittest.main()
