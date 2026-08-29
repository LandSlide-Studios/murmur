"""Render the pill in each state to PNGs so the composition can be looked at,
and verify it never takes focus."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from murmur.ui.pill import Pill

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
pill = Pill()


def advance(frames, level=0.0):
    for _ in range(frames):
        pill.level = level
        pill._tick()


def shot(name, bg="#1B1D22"):
    """Composite over a desktop-ish ground; the pill is translucent."""
    img = QImage(pill.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(bg))
    p = QPainter(img)
    pill.render(p, pill.rect().topLeft())
    p.end()
    # crop to the pill's neighbourhood so the PNG is not mostly empty
    w, h = pill.width(), pill.height()
    crop = img.copy(QRect(int(w/2 - 110), int(h/2 - 34), 220, 68))
    crop = crop.scaled(crop.width()*2, crop.height()*2,
                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
    path = OUT / f"pill-{name}.png"
    crop.save(str(path))
    print(f"  {path.name}  ({crop.width()}x{crop.height()})")


print("rendering states:")
pill.set_state("recording", mode="hold")
advance(60, level=0.22)
shot("01-recording-hold")

advance(30, level=0.45)
shot("02-recording-loud")

pill.set_state("recording", mode="toggle")
advance(45, level=0.25)
shot("03-recording-handsfree")

advance(120, level=0.0)          # silence -> breathing
shot("04-silence-breathing")

pill.set_state("transcribing")
advance(40)
shot("05-transcribing")

pill.set_state("done")
advance(22)
shot("06-done-check")

pill.set_state("recording", mode="hold")
advance(40, level=0.3)
pill.set_state("cancelled")
advance(6)
shot("07-cancelled-shake")

print("\nfocus checks:")
print(f"  focusPolicy is NoFocus:            {pill.focusPolicy() == Qt.NoFocus}")
print(f"  WA_ShowWithoutActivating:          {pill.testAttribute(Qt.WA_ShowWithoutActivating)}")
print(f"  WA_TransparentForMouseEvents:      {pill.testAttribute(Qt.WA_TransparentForMouseEvents)}")
print(f"  WA_TranslucentBackground:          {pill.testAttribute(Qt.WA_TranslucentBackground)}")
flags = pill.windowFlags()
print(f"  Qt.Tool:                           {bool(flags & Qt.Tool)}")
print(f"  Qt.WindowStaysOnTopHint:           {bool(flags & Qt.WindowStaysOnTopHint)}")
print(f"  Qt.WindowTransparentForInput:      {bool(flags & Qt.WindowTransparentForInput)}")

pill.set_state("recording", mode="hold")
advance(3)
import ctypes
pill.show(); pill._harden()
app.processEvents()
hwnd = int(pill.winId())
ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
print(f"  WS_EX_NOACTIVATE set on the HWND:  {bool(ex & 0x08000000)}")
print(f"  WS_EX_TOOLWINDOW set on the HWND:  {bool(ex & 0x00000080)}")
fg = ctypes.windll.user32.GetForegroundWindow()
print(f"  pill did NOT become foreground:    {fg != hwnd}")
