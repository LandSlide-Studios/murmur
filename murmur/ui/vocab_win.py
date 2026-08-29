"""Vocabulary panel — the supervision half of the learner.

Every learned term is listed with how it was misheard, how many times it has
been seen, and a switch to turn it off. Nothing enters the active set invisibly,
because a learner that quietly rewrites your words is worse than none at all.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import INK_3, STYLESHEET

TERM, HEARD, HITS, ON = range(4)


class VocabWindow(QWidget):
    def __init__(self, vocab):
        super().__init__()
        self.vocab = vocab
        self.setWindowTitle("Murmur — Vocabulary")
        self.setStyleSheet(STYLESHEET)
        self.resize(620, 460)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Term", "Heard as", "Times", "Active"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(TERM, QHeaderView.Stretch)
        header.setSectionResizeMode(HEARD, QHeaderView.Stretch)
        header.setSectionResizeMode(HITS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(ON, QHeaderView.ResizeToContents)

        self.forget_btn = QPushButton("Forget selected")
        self.hint = QLabel(
            "Learned from your corrections. Untick Active to stop applying one.")
        self.hint.setProperty("muted", True)

        row = QHBoxLayout()
        row.addWidget(self.forget_btn)
        row.addStretch(1)
        row.addWidget(self.hint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self.table)
        layout.addLayout(row)

        self.forget_btn.clicked.connect(self._forget)
        self.table.itemChanged.connect(self._toggled)
        self.reload()

    def reload(self) -> None:
        self.table.blockSignals(True)
        rows = self.vocab.all_terms()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            term = QTableWidgetItem(r["term"])
            heard = QTableWidgetItem(r["wrong_form"])
            hits = QTableWidgetItem(str(r["hit_count"]))
            hits.setTextAlignment(Qt.AlignCenter)

            active = QTableWidgetItem()
            active.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            active.setCheckState(
                Qt.Checked if (r["enabled"] and r["promoted"]) else Qt.Unchecked)
            if not r["promoted"]:
                active.setToolTip(
                    "Seen once. Automatic corrections apply after a second sighting.")
                term.setForeground(Qt.GlobalColor.gray)

            for col, item in ((TERM, term), (HEARD, heard),
                              (HITS, hits), (ON, active)):
                self.table.setItem(i, col, item)
        self.table.blockSignals(False)
        self.hint.setText(
            f"{len(rows)} term{'s' if len(rows) != 1 else ''} learned from your corrections."
            if rows else "Nothing learned yet. Fix a word in History and it lands here.")

    def _toggled(self, item) -> None:
        if item.column() != ON:
            return
        term_item = self.table.item(item.row(), TERM)
        if term_item:
            self.vocab.set_enabled(term_item.text(),
                                   item.checkState() == Qt.Checked)

    def _forget(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        term_item = self.table.item(row, TERM)
        if term_item:
            self.vocab.forget(term_item.text())
            self.reload()
