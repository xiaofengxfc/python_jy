"""Local web UI entry point: python run_web.py

Development helper for iterating on the interface without rebuilding the
desktop executable. Serves http://127.0.0.1:8765.

Frontend files are read from disk on every request, so editing CSS/JS/HTML
only needs a browser refresh. Backend changes need a restart of this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main() -> None:
    parser = argparse.ArgumentParser(description="biTrader local web UI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    import uvicorn

    from btrader.web.app import create_app

    # Pass the app object rather than an import string: uvicorn's reloader
    # spawns a fresh interpreter that would not inherit the sys.path fix above.
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
