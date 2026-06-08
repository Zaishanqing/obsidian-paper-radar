#!/usr/bin/env python3
"""Compatibility entry point – delegates to the package CLI.
安装后请直接使用 ``obsidian-paper-radar`` 命令。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_paper_radar.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
