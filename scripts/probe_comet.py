"""Render the comet's flight as a filmstrip so the motion can be judged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from murmur.ui.comet import BURST_MS, FLIGHT_MS, PULL_MS, Comet

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
c = Comet(colour="#4ADE80")

W, H = 900, 380
start = QPointF(W - 60, H / 2)          # right bezel, where the pill lives
aim = QPointF(180, 120)                 # the cursor

c._start, c._aim = start, aim
c._origin = QPointF(0, 0)
c.resize(W, H)

frames = []
for phase, dur, step in (("pull", PULL_MS, 36), ("flight", FLIGHT_MS, 13),
                         ("burst", BURST_MS, 110)):
    c._phase = phase
    t = 0
    while t <= dur:
        c._elapsed = t
        img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)          # only the comet, no ground to add up
        p = QPainter(img)
        c.render(p, c.rect().topLeft())
        p.end()
        frames.append((phase, t, img))
        t += step

# Composite every frame onto one canvas: the trail IS the motion.
trail = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
trail.fill(QColor("#14161A"))
tp = QPainter(trail)
# Additive: a motion trail is light accumulating, not layers of translucency.
tp.setCompositionMode(QPainter.CompositionMode_Plus)
for phase, t, img in frames:
    if phase == "burst":
        continue
    tp.drawImage(0, 0, img)
tp.end()
trail.save(str(OUT / "comet-trail.png"))
print(f"  comet-trail.png   {len(frames)} frames composited")

# A few individual moments, side by side.
picks = [("pull", 72), ("flight", 26), ("flight", 130), ("flight", 234), ("burst", 110)]
strip = QImage(W, H // 2 * len(picks), QImage.Format_ARGB32_Premultiplied)
strip.fill(QColor("#14161A"))
sp = QPainter(strip)
for i, (phase, t) in enumerate(picks):
    c._phase, c._elapsed = phase, t
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#14161A"))
    p = QPainter(img)
    c.render(p, c.rect().topLeft())
    p.end()
    sp.drawImage(0, i * (H // 2), img.copy(0, H // 4, W, H // 2))
sp.end()
strip.scaled(W, strip.height(), Qt.KeepAspectRatio,
             Qt.SmoothTransformation).save(str(OUT / "comet-frames.png"))
print(f"  comet-frames.png  {len(picks)} moments")
print(f"\ntiming: {PULL_MS}ms pull + {FLIGHT_MS}ms flight = {PULL_MS + FLIGHT_MS}ms to land")
