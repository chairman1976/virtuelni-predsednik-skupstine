"""Side panel with the full Poslovnik Narodne skupštine (Latin script), searchable, jumps to an article.

Opened with the "Poslovnik" button, or by clicking an article ("čl. 107 st. 2") on a measure card.
Text: data/poslovnik_full.txt (pročišćeni tekst from parlament.gov.rs, Cyrillic) - the page's navigation
and footer around it are cut off.
"""
import html
import os
import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QComboBox, QDockWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser,
                             QVBoxLayout, QWidget)

from common import DATA, to_latin

ORDER_ARTICLES = range(96, 117)      # the articles the app enforces (speaking, order in the hall, measures)
_RE_ART = re.compile(r"^Члан\s+(\d+)\.?\s*$")
_RE_CHAPTER = re.compile(r"^([IVXL]+)\.\s+\S")
_RE_SUB = re.compile(r"^\d+\.\s+\S|^\d+\.\d+\.?\s+\S|^[а-я]\)\s")


def _parse():
    """[(kind, text, art_no)] with kind in chapter | sub | article | para; Latin script."""
    try:
        raw = open(os.path.join(DATA, "poslovnik_full.txt"), encoding="utf-8").read().splitlines()
    except OSError:
        return []
    start = next((i for i, ln in enumerate(raw) if ln.startswith("ПОСЛОВНИК НАРОДНЕ СКУПШТИНЕ")), 0)
    out = []
    for ln in raw[start:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("Народна скупштинаСастав", "©", "Донатор", "Претходни месец")):
            break                                   # footer of the web page
        m = _RE_ART.match(s)
        if m:
            out.append(("article", to_latin(s), int(m.group(1))))
        elif _RE_CHAPTER.match(s) and len(s) < 160:
            out.append(("chapter", to_latin(s).upper(), None))     # "KONSTITUISANjE" -> "KONSTITUISANJE"
        elif _RE_SUB.match(s) and len(s) < 120 and not s.endswith((".", ";", ":")):
            out.append(("sub", to_latin(s), None))
        else:
            out.append(("para", to_latin(s), None))
    return out


def _html(items):
    parts = ["<style>p{margin:2px 0 6px 0} h2{margin:18px 0 6px 0;color:#1f3a5f} "
             "h3{margin:12px 0 4px 0;color:#34495e} .art{font-weight:bold;margin-top:10px;color:#1f3a5f} "
             ".red{background:#fff6e5}</style>"]
    in_order = False
    for kind, text, no in items:
        t = html.escape(text)
        if kind == "chapter":
            parts.append(f"<h2>{t}</h2>")
        elif kind == "sub":
            parts.append(f"<h3>{t}</h3>")
        elif kind == "article":
            in_order = no in ORDER_ARTICLES
            tag = " <span style='color:#b9770e;font-weight:normal'>(red na sednici)</span>" if in_order else ""
            parts.append(f"<a name='cl{no}'></a><p class='art'>{t}{tag}</p>")
        else:
            cls = " class='red'" if in_order else ""
            parts.append(f"<p{cls}>{t}</p>")
    return "".join(parts)


class PoslovnikDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Poslovnik Narodne skupštine", parent)
        self.setObjectName("poslovnikDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.items = _parse()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Traži reč ili broj člana (npr. 107)…")
        self.search.setToolTip("Enter = sledeći pogodak. Broj (npr. 107) = skok na taj član.")
        self.search.returnPressed.connect(self.find_next)
        nxt = QPushButton("Dalje")
        nxt.clicked.connect(self.find_next)
        self.hits = QLabel("")
        self.hits.setProperty("muted", True)
        top.addWidget(self.search, 1)
        top.addWidget(nxt)
        top.addWidget(self.hits)
        v.addLayout(top)
        self.toc = QComboBox()
        self.toc.addItem("Sadržaj – izaberi glavu…", None)
        for kind, text, _no in self.items:
            if kind == "chapter":
                self.toc.addItem(text[:90], text)
        self.toc.addItem("Red na sednici (čl. 96–116)", "cl96")
        self.toc.currentIndexChanged.connect(self._toc_jump)
        v.addWidget(self.toc)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setHtml(_html(self.items) if self.items else
                          "<p>Tekst Poslovnika nije pronađen (data/poslovnik_full.txt).</p>")
        v.addWidget(self.view, 1)
        self.setWidget(w)

    def show_article(self, no):
        self.show()
        self.raise_()
        self.view.scrollToAnchor(f"cl{no}")
        self.search.setText(str(no))
        self._mark(f"Član {no}.")

    def _toc_jump(self, i):
        key = self.toc.itemData(i)
        if not key:
            return
        if key.startswith("cl"):
            self.view.scrollToAnchor(key)
        else:
            self.view.moveCursor(QTextCursor.MoveOperation.Start)
            self.view.find(key)
            self._scroll_top()

    def _scroll_top(self):
        """Put the found text at the top of the view instead of the bottom edge."""
        sb = self.view.verticalScrollBar()
        rect = self.view.cursorRect()
        sb.setValue(sb.value() + rect.top() - 10)

    def _mark(self, text):
        self.view.moveCursor(QTextCursor.MoveOperation.Start)
        if self.view.find(text):
            self._scroll_top()

    def find_next(self):
        q = self.search.text().strip()
        if not q:
            return
        if q.isdigit():
            self.view.scrollToAnchor(f"cl{int(q)}")
            self._mark(f"Član {int(q)}.")
            self.hits.setText(f"čl. {q}")
            return
        q = to_latin(q)
        if not self.view.find(q):                   # wrap around
            self.view.moveCursor(QTextCursor.MoveOperation.Start)
            if not self.view.find(q):
                self.hits.setText("nema")
                return
        self._scroll_top()
        doc = self.view.document()
        n, c = 0, doc.find(q)
        while not c.isNull() and n < 999:
            n += 1
            c = doc.find(q, c)
        self.hits.setText(f"{n} pogodaka")
