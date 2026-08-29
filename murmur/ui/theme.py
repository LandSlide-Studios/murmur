"""Shared styling for the panels, matching the pill's visual language.

Same near-black ground and single blue accent. No gradients, no glow — the
same discipline the overlay follows.
"""

BG = "#0E0E10"
SURFACE = "#16171B"
SURFACE_2 = "#1E2027"
RULE = "#282B33"
INK = "#E9EBF0"
INK_2 = "#A9AFBD"
INK_3 = "#767D8C"
ACCENT = "#5B8DEF"
GOOD = "#4ADE80"
WARN = "#E8B84B"
BAD = "#EF4444"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {INK};
    font-family: "Segoe UI";
    font-size: 13px;
}}
QLineEdit, QPlainTextEdit {{
    background: {SURFACE};
    border: 1px solid {RULE};
    border-radius: 6px;
    padding: 7px 9px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QListWidget, QTableWidget {{
    background: {SURFACE};
    border: 1px solid {RULE};
    border-radius: 6px;
    outline: none;
}}
QListWidget::item {{
    padding: 7px 9px;
    border-bottom: 1px solid {RULE};
}}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {SURFACE_2};
    color: {INK};
}}
QHeaderView::section {{
    background: {BG};
    color: {INK_3};
    border: none;
    border-bottom: 1px solid {RULE};
    padding: 6px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QTableWidget {{ gridline-color: {RULE}; }}
QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {RULE};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {RULE}; }}
QPushButton:disabled {{ color: {INK_3}; border-color: {RULE}; }}
QLabel[muted="true"] {{ color: {INK_3}; font-size: 12px; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {RULE}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
