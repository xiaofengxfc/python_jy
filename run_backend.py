"""Packaging entry point for the local backend.

The Electron shell spawns this executable and talks to it over HTTP/WebSocket.
It is a thin wrapper for the same reason ``run.py`` exists: a frozen build needs
``src/`` on ``sys.path`` before importing the package absolutely.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from btrader.web.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
