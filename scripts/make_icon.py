"""Generate assets/murmur.ico from the same drawing code the tray uses,
so the desktop icon and the tray icon are the same mark."""
import struct
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

ACCENT = "#5B8DEF"
BODY = QColor(14, 14, 16)
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded-square ground so the mark reads on any wallpaper.
    pad = size * 0.06
    p.setPen(Qt.NoPen)
    p.setBrush(BODY)
    p.drawRoundedRect(QRectF(pad, pad, size - 2 * pad, size - 2 * pad),
                      size * 0.22, size * 0.22)

    heights = (0.30, 0.56, 0.40)
    bar_w = size * 0.11
    gap = size * 0.09
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (size - total) / 2
    p.setBrush(QColor(ACCENT))
    for h in heights:
        bh = size * h
        p.drawRoundedRect(QRectF(x, (size - bh) / 2, bar_w, bh),
                          bar_w / 2, bar_w / 2)
        x += bar_w + gap
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def main():
    app = QApplication(sys.argv)
    pngs = [(s, png_bytes(render(s))) for s in SIZES]

    out = BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(pngs)))     # ICONDIR
    offset = 6 + 16 * len(pngs)
    for size, data in pngs:
        dim = 0 if size >= 256 else size
        out.write(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                              len(data), offset))
        offset += len(data)
    for _size, data in pngs:
        out.write(data)

    dest = Path(__file__).resolve().parent.parent / "assets" / "murmur.ico"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out.getvalue())
    render(256).save(str(dest.with_suffix(".png")))
    print(f"wrote {dest} ({dest.stat().st_size} bytes, {len(pngs)} sizes)")


if __name__ == "__main__":
    main()
