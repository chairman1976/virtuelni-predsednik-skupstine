"""Modern look: dark slate shell, accent blue, rounded controls; reading panes stay light 'paper' for contrast."""

BG = "#0f1218"        # window
PANEL = "#171b24"     # cards / bars
LINE = "#262c3a"      # borders
FIELD = "#11151d"     # inputs
TEXT = "#e6e9ef"
MUTED = "#8b93a7"
ACCENT = "#4f8cff"
DANGER = "#ef4444"
PAPER = "#f7f8fb"     # reading panes (HTML content has dark text)

QSS = f"""
QMainWindow, QWidget#root {{ background: {BG}; color: {TEXT}; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#appTitle {{ font-size: 13pt; font-weight: 600; color: {TEXT}; }}
QLabel#appSub {{ color: {MUTED}; }}

QLineEdit, QComboBox {{
    background: {FIELD}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 6px 10px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit::placeholder {{ color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; selection-background-color: {ACCENT};
    outline: none; padding: 4px;
}}

QPushButton {{
    background: #222838; color: {TEXT}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px 14px;
}}
QPushButton:hover {{ background: #2b3246; border-color: #34405a; }}
QPushButton:pressed {{ background: #1b2030; }}
QPushButton:disabled {{ color: #565e72; background: #191d28; border-color: #1f2430; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6a9dff; }}
QPushButton#primary[running="true"] {{ background: {DANGER}; border-color: {DANGER}; }}
QPushButton#primary[running="true"]:hover {{ background: #f26363; }}

QWidget#speakerCard {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
QWidget#speakerCard QLabel {{ background: transparent; }}
QLabel#spName {{ font-size: 20pt; font-weight: 600; color: white; }}
QLabel#spRole {{ color: {MUTED}; }}
QLabel#spVoice {{ color: #8fc1ff; }}
QLabel#spChair {{ color: #f5b971; font-weight: 600; }}

QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 12px; background: {PAPER}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 7px 12px; margin: 0 2px 6px 0; border-radius: 8px;
}}
QTabBar::tab:hover {{ color: {TEXT}; background: #1d2230; }}
QTabBar::tab:selected {{ color: white; background: #263049; }}
QTabBar QToolButton {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 6px; color: {TEXT}; }}

QTextBrowser, QTableWidget {{
    background: {PAPER}; color: #1d2433; border: none; border-radius: 12px; padding: 8px;
    selection-background-color: #cfe0ff; selection-color: #111;
}}
QTableWidget {{ gridline-color: #e3e6ee; }}
QHeaderView::section {{
    background: #eceff5; color: #3a4256; border: none; border-bottom: 1px solid #dde1ea; padding: 6px 8px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: #eceff5; border: none; }}

QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 2px solid {ACCENT};
}}
QSlider::handle:horizontal:disabled {{ background: #3a4152; border-color: #3a4152; }}
QSlider::sub-page:horizontal:disabled {{ background: #3a4152; }}

QProgressBar {{
    background: {FIELD}; border: 1px solid {LINE}; border-radius: 7px; color: {TEXT}; text-align: center;
    height: 14px; font-size: 8.5pt;
}}
QProgressBar::chunk {{
    border-radius: 6px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22c55e, stop:0.6 #f59e0b, stop:1 #ef4444);
}}

QSplitter::handle {{ background: {BG}; width: 8px; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid {LINE}; }}
QStatusBar QLabel {{ color: {MUTED}; }}
QToolTip {{
    background: #1f2533; color: {TEXT}; border: 1px solid #34405a; border-radius: 6px; padding: 6px 8px;
}}
QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QInputDialog, QMessageBox, QFileDialog {{ background: {PANEL}; color: {TEXT}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c4cad6; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #a9b1c1; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #c4cad6; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def apply(app):
    from PyQt6.QtGui import QColor, QFont, QPalette
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))      # not in QSS: that would override the big timer fonts
    pal = app.palette()
    for role, col in ((QPalette.ColorRole.Window, BG), (QPalette.ColorRole.WindowText, TEXT),
                      (QPalette.ColorRole.Base, FIELD), (QPalette.ColorRole.Text, TEXT),
                      (QPalette.ColorRole.Button, "#222838"), (QPalette.ColorRole.ButtonText, TEXT),
                      (QPalette.ColorRole.Highlight, ACCENT), (QPalette.ColorRole.HighlightedText, "#ffffff"),
                      (QPalette.ColorRole.ToolTipBase, "#1f2533"), (QPalette.ColorRole.ToolTipText, TEXT),
                      (QPalette.ColorRole.PlaceholderText, MUTED)):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
