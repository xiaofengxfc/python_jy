"""Development entry point: python run.py"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from btrader.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
