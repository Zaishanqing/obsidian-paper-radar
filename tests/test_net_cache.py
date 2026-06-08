from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar.net_cache import JsonDiskCache, request_with_retry


class JsonDiskCacheTest(unittest.TestCase):
    def test_set_get_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = JsonDiskCache(temp_dir, ttl_days=7)
            cache.set("query-key", {"data": [1, 2, 3]})
            self.assertEqual(cache.get("query-key"), {"data": [1, 2, 3]})

    def test_missing_key_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = JsonDiskCache(temp_dir, ttl_days=7)
            self.assertIsNone(cache.get("never-written"))

    def test_expired_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = JsonDiskCache(temp_dir, ttl_days=1)
            cache.set("stale", {"x": 1})
            path = cache._path_for("stale")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(cache.get("stale"))

    def test_version_mismatch_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = JsonDiskCache(temp_dir, ttl_days=7, version=2)
            cache.set("k", {"x": 1})
            path = cache._path_for("k")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["version"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(cache.get("k"))


class RequestWithRetryTest(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self) -> None:
        resp_429 = Mock(status_code=429, headers={})
        resp_200 = Mock(status_code=200, headers={})
        sleeps: list[float] = []
        with patch("obsidian_paper_radar.net_cache.requests.request", side_effect=[resp_429, resp_200]) as mock_request:
            out = request_with_retry("GET", "https://x", max_retries=3, backoff_base=2, sleep=sleeps.append)
        self.assertIs(out, resp_200)
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(len(sleeps), 1)

    def test_respects_retry_after_header(self) -> None:
        resp_429 = Mock(status_code=429, headers={"Retry-After": "5"})
        resp_200 = Mock(status_code=200, headers={})
        sleeps: list[float] = []
        with patch("obsidian_paper_radar.net_cache.requests.request", side_effect=[resp_429, resp_200]):
            request_with_retry("GET", "https://x", max_retries=3, backoff_base=2, sleep=sleeps.append)
        self.assertEqual(sleeps[0], 5.0)

    def test_returns_last_response_when_retries_exhausted(self) -> None:
        resp_429 = Mock(status_code=429, headers={})
        with patch("obsidian_paper_radar.net_cache.requests.request", side_effect=[resp_429, resp_429]):
            out = request_with_retry("GET", "https://x", max_retries=2, backoff_base=2, sleep=lambda _s: None)
        self.assertEqual(out.status_code, 429)


if __name__ == "__main__":
    unittest.main()
