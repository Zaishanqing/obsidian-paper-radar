from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar.config import load_dotenv


class ConfigProxyTest(unittest.TestCase):
    def test_network_proxy_sets_requests_proxy_envs(self) -> None:
        keys = ["NETWORK_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as temp_dir:
                env_path = Path(temp_dir) / ".env"
                env_path.write_text("NETWORK_PROXY=http://127.0.0.1:7890\n", encoding="utf-8")
                load_dotenv(env_path)
            self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:7890")
            self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://127.0.0.1:7890")
            self.assertEqual(os.environ.get("http_proxy"), "http://127.0.0.1:7890")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
