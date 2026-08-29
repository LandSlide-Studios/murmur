"""Boot the real app object, open both panels, and screenshot them."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from murmur.app import MurmurApp
from murmur.config import Config
from murmur.ui.history_win import HistoryWindow
from murmur.ui.vocab_win import VocabWindow

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

cfg = Config.load(Path(__file__).resolve().parent.parent / "settings.json")
app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
mu = MurmurApp(cfg)

# Seed realistic data so the panels are judged with content, not empty state.
samples = [
    ("um so send the proposal over to dana tomorrow morning",
     "Send the proposal over to Dana tomorrow morning.", "hold", "ok"),
    ("check the dns records for the northgate site",
     "Check the DNS records for the Northgate site.", "toggle", "ok"),
    ("follow up with priya about the photos",
     "Follow up with Priya about the photos.", "hold", "ok"),
    ("this one got cancelled halfway", None, "toggle", "cancelled"),
    ("the model fell over here", None, "hold", "error"),
]
for raw, final, mode, status in samples:
    mu.history.add(raw=raw, polished=final, final=final, mode=mode,
                   duration_ms=3400, app="Code.exe", title="murmur - VS Code",
                   status=status)

mu.vocab.observe("halvorsen", "Halvorsen", source="manual")
mu.vocab.observe("north gate", "Northgate", source="manual")
mu.vocab.observe("land slide", "Landslide", source="auto")
mu.vocab.observe("land slide", "Landslide", source="auto")
mu.vocab.observe("priyah", "Priya", source="auto")   # seen once, not yet active

h = HistoryWindow(mu.history, mu.injector, mu.corrections)
h.show(); h.resize(760, 560)
v = VocabWindow(mu.vocab)
v.show(); v.resize(620, 420)

def capture():
    h.list.setCurrentRow(1)
    app.processEvents()
    h.grab().save(str(OUT / "panel-history.png"))
    v.grab().save(str(OUT / "panel-vocabulary.png"))
    print("history rows :", h.list.count())
    print("vocab rows   :", v.table.rowCount())
    print("saved panel-history.png, panel-vocabulary.png")
    mu.stop()
    app.quit()

QTimer.singleShot(700, capture)
sys.exit(app.exec())
