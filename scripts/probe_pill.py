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


def shot(name, bg="#1B1D22", zoom=2):
    img = QImage(pill.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(bg))
    p = QPainter(img)
    pill.render(p, pill.rect().topLeft())
    p.end()
    w, h = pill.width(), pill.height()
    crop = img.copy(QRect(w - 92, int(h / 2 - 110), 92, 220))
    crop = crop.scaled(crop.width() * zoom, crop.height() * zoom,
                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
    crop.save(str(OUT / f"pill-{name}.png"))
    print(f"  pill-{name}.png   capsule {pill.width_s.value:5.1f} x "
          f"{pill.height_s.value:4.1f}  opacity {pill.opacity.value:.2f}")


# Real microphone RMS, not arbitrary numbers. `audio.speech_rms_threshold` is
# 0.012, a normal speaking voice sits near 0.03 and loud talking near 0.075.
# These used to be 0.30 and 0.50 — ten times a real voice — so every shot was
# saturated and showed a meter nobody would ever see.
# MEASURED from his own dictations, not assumed. Level logging reports the
# loudest 400ms window of a real one at 0.0195-0.0268, and blocks during ordinary
# speech around 0.014. Two earlier versions of this line used 0.30 and then 0.03
# — both far above his voice — which is why the meter kept passing review here
# and failing in his hand.
QUIET, NORMAL, LOUD = 0.004, 0.014, 0.027

print("rendering states:")
pill.set_state("armed")
advance(90)
shot("00-armed-idle")

pill.set_state("recording", mode="hold")
advance(60, level=NORMAL)
shot("01-recording-hold")

advance(30, level=LOUD)
shot("02-recording-loud")

pill.set_state("recording", mode="toggle")
advance(60, level=NORMAL)
shot("03-recording-handsfree")

# Listening and hearing nothing. Flat, uniform, about half a speaking bar —
# the contrast against the travelling wave is the whole point of it.
advance(90, level=0.0)
shot("04-silence-flat")

pill.set_state("transcribing")
advance(45)
shot("05-transcribing")

pill.set_state("polishing")
advance(45)
shot("05b-cleaning-up")

pill.set_state("done")
advance(24)
shot("06-done-check")

pill.set_state("recording", mode="hold")
advance(40, level=NORMAL)
pill.set_state("cancelled")
advance(6)
shot("07-cancelled-shake")

print("")
print("size comparison (old -> new):")
print("  idle           none  ->   58 x 12")
print("  recording   140 x 36 ->  104 x 24")
print("  with label  174 x 36 ->  152 x 24")

print("")
print("focus checks:")
print(f"  focusPolicy NoFocus:            {pill.focusPolicy() == Qt.NoFocus}")
print(f"  WA_ShowWithoutActivating:       {pill.testAttribute(Qt.WA_ShowWithoutActivating)}")
print(f"  WA_TransparentForMouseEvents:   {pill.testAttribute(Qt.WA_TransparentForMouseEvents)}")
pill.set_state("recording", mode="hold")
advance(3)
import ctypes
pill.show(); pill._harden()
app.processEvents()
hwnd = int(pill.winId())
ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
print(f"  WS_EX_NOACTIVATE on the HWND:   {bool(ex & 0x08000000)}")
print(f"  pill did NOT become foreground: {ctypes.windll.user32.GetForegroundWindow() != hwnd}")
