"""Generate ``packaging/bitrader.ico`` for the Windows build.

Run once after changing the artwork:

    .venv\\Scripts\\python.exe packaging\\make_icon.py

The icon is drawn with Qt and written as a multi-resolution PNG-in-ICO file
(Vista and newer understand PNG-compressed entries).
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
)

ROOT = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64, 128, 256)
GOLD = QColor("#f0b90b")


def render(size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(size)

    radius = s * 0.22
    gradient = QLinearGradient(0, 0, s, s)
    gradient.setColorAt(0.0, QColor("#1b1e28"))
    gradient.setColorAt(1.0, QColor("#0b0d12"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(QRectF(0, 0, s, s), radius, radius)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(240, 185, 11, 80), max(1.0, s * 0.02)))
    inset = s * 0.012
    painter.drawRoundedRect(
        QRectF(inset, inset, s - 2 * inset, s - 2 * inset), radius, radius
    )

    # Three candles climbing left to right.
    body_w = s * 0.105
    candles = (
        (0.27, 0.56, 0.20, QColor("#8a6d0b")),
        (0.50, 0.42, 0.30, GOLD),
        (0.73, 0.26, 0.42, GOLD),
    )
    for cx, top, height, color in candles:
        x = s * cx
        painter.setPen(QPen(color, max(1.0, s * 0.024)))
        painter.drawLine(
            QPointF(x, s * (top - 0.07)),
            QPointF(x, s * (top + height + 0.06)),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(
            QRectF(x - body_w / 2, s * top, body_w, s * height), s * 0.015, s * 0.015
        )

    painter.end()

    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def write_ico(path: Path, sizes=SIZES) -> None:
    images = [render(size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    payload = b""
    for size, data in zip(sizes, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset
        )
        payload += data
        offset += len(data)
    path.write_bytes(header + entries + payload)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    target = ROOT / "bitrader.ico"
    write_ico(target)
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(SIZES)} sizes)")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
