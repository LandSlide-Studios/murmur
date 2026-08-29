"""History panel.

Every dictation is here, including the ones that failed — so if a paste went
somewhere unexpected, or you forgot to paste at all, the text is still recoverable.

Editing a transcript and saving it is capture path A for vocabulary learning:
the diff is trusted immediately, because the intent is explicit.
"""

import logging
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import BAD, INK_3, STYLESHEET, WARN

log = logging.getLogger(__name__)

ROW_DATA = Qt.UserRole + 1
STATUS_COLOUR = {"error": BAD, "cancelled": WARN, "empty": INK_3}


def _when(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%d %b", time.localtime(ts))


class HistoryWindow(QWidget):
    def __init__(self, history, injector, corrections):
        super().__init__()
        self.history = history
        self.injector = injector
        self.corrections = corrections

        self.setWindowTitle("Murmur — History")
        self.setStyleSheet(STYLESHEET)
        self.resize(760, 560)

        self.search = QLineEdit(placeholderText="Search your dictations…")
        self.list = QListWidget()
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Select a dictation to view or edit it.")

        self.status = QLabel("")
        self.status.setProperty("muted", True)

        self.copy_btn = QPushButton("Copy")
        self.paste_btn = QPushButton("Paste again")
        self.save_btn = QPushButton("Save correction")
        self.save_btn.setToolTip(
            "Fix a word here and Murmur learns it for future dictations.")

        row = QHBoxLayout()
        for b in (self.copy_btn, self.paste_btn, self.save_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self.search)
        layout.addWidget(self.list, 3)
        layout.addWidget(self.editor, 2)
        layout.addLayout(row)

        self.search.textChanged.connect(self.reload)
        self.list.currentItemChanged.connect(self._show)
        self.copy_btn.clicked.connect(self._copy)
        self.paste_btn.clicked.connect(self._paste)
        self.save_btn.clicked.connect(self._save)
        self.reload()

    # --- data -------------------------------------------------------------

    def reload(self) -> None:
        query = self.search.text().strip()
        rows = self.history.search(query) if query else self.history.recent()
        self.list.clear()
        for r in rows:
            text = (r["corrected_text"] or r["final_text"] or r["raw_text"] or "")
            preview = " ".join(text.split())[:96] or "(nothing transcribed)"
            label = f"{_when(r['ts']):>9}   {preview}"
            item = QListWidgetItem(label)
            item.setData(ROW_DATA, r)
            if r["status"] != "ok":
                item.setText(f"{_when(r['ts']):>9}   [{r['status']}] {preview}")
                item.setForeground(Qt.GlobalColor.gray)
            self.list.addItem(item)
        self.status.setText(f"{len(rows)} dictation{'s' if len(rows) != 1 else ''}")

    def _current(self):
        item = self.list.currentItem()
        return item.data(ROW_DATA) if item else None

    def _show(self, current, _previous=None) -> None:
        if current is None:
            self.editor.setPlainText("")
            return
        r = current.data(ROW_DATA)
        self.editor.setPlainText(
            r["corrected_text"] or r["final_text"] or r["raw_text"] or "")

    # --- actions ----------------------------------------------------------

    def _copy(self) -> None:
        text = self.editor.toPlainText()
        if text:
            self.injector._set_clipboard(text)
            self.status.setText("Copied to clipboard")

    def _paste(self) -> None:
        text = self.editor.toPlainText()
        if not text:
            return
        # Hide, then wait before pasting. hide() returns immediately but Windows
        # restores focus to the previous window asynchronously — pasting in the
        # same tick races that, and the keystroke can land nowhere.
        self.hide()
        QTimer.singleShot(180, lambda: self._deferred_paste(text))

    def _deferred_paste(self, text: str) -> None:
        """On a worker thread: inject() waits up to 500ms for modifiers, 60ms
        for the clipboard and 300ms for the restore. That is most of a second
        of frozen UI if it runs on the Qt thread."""
        def run():
            try:
                self.injector.inject(text)
            except Exception:
                log.exception("could not paste from the history panel")

        threading.Thread(target=run, daemon=True, name="murmur-panel-paste").start()

    def _save(self) -> None:
        row = self._current()
        if not row:
            return
        original = row["final_text"] or row["raw_text"] or ""
        edited = self.editor.toPlainText()
        if edited == original:
            self.status.setText("No change to save")
            return
        self.history.set_correction(row["id"], edited)
        learned = self.corrections.learn_from_edit(original, edited)
        if learned:
            self.status.setText(
                f"Saved — learned {learned} new term{'s' if learned != 1 else ''}")
        else:
            self.status.setText("Saved")
        self.reload()
