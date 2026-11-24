"""Virtuelni predsednik skupštine - live YouTube Assembly session -> transcript -> breaches of order -> measures."""
import collections
import datetime
import html
import json
import os
import re
import sys
import threading
import time

from common import SESSIONS, fmt_time, fold_text, load_settings, log, parse_time, save_settings, setup_env, to_latin

setup_env()

# Must come BEFORE PyQt: Qt ships its own msvcp140.dll; if an older one is loaded first, CTranslate2 (built with a
# newer MSVC) crashes with an access violation when the Whisper model is created.
import ctranslate2  # noqa: E402,F401
import faster_whisper  # noqa: E402,F401
import sherpa_onnx  # noqa: E402,F401

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRectF, Qt, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPainterPath  # noqa: E402
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QMenu,  # noqa: E402
                             QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                             QSlider, QSplitter, QStyle, QStyleOptionSlider, QTableWidget, QTableWidgetItem,
                             QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

import names  # noqa: E402
import profiles as profiles_mod  # noqa: E402
import report  # noqa: E402
import local_llm  # noqa: E402
import theme  # noqa: E402
import session_state  # noqa: E402
import stream  # noqa: E402
from rules import KINDS, MEASURES, Discipline  # noqa: E402
from speakers import CHAIR, SpeakerTracker  # noqa: E402
from judge import TECHNIQUES  # noqa: E402

# accusations / blame that deserve a wider-context check (double standards, own record, session record)
CONTEXT_TRIGGERS = {"scapegoating", "ad_hominem", "labeling", "whataboutism", "unsupported_accusation",
                    "conspiracy", "victimhood", "gaslighting", "false_analogy", "dehumanization"}

MEASURE_COLORS = {"upozorenje": "#c98a00", "opomena": "#d35400", "opomena2": "#c0392b",
                  "oduzimanje": "#962d22", "udaljenje": "#5b0f0a", "sumnja": "#777777"}
COOLDOWN_S = 45


def mark_unsure(text, words, fmt):
    """Wrap each word Whisper was unsure of (first occurrence) with fmt, e.g. "{}(?)" for the judge."""
    for w in dict.fromkeys(words):
        text = re.sub(r"(?<![\w])" + re.escape(w) + r"(?![\w])", lambda m: fmt.format(m.group(0)), text, count=1)
    return text


def strip_unsure_marks(x):
    """The judge sometimes copies the "(?)" uncertainty marks into its quotes - not for display."""
    if isinstance(x, str):
        return x.replace("(?)", "")
    if isinstance(x, list):
        return [strip_unsure_marks(v) for v in x]
    if isinstance(x, dict):
        return {k: (v if k.startswith("_") else strip_unsure_marks(v)) for k, v in x.items()}
    return x


class Bridge(QObject):
    segment = pyqtSignal(object)
    result = pyqtSignal(object)
    caption = pyqtSignal(object, float)
    caption_local = pyqtSignal(object, str, float, float)
    face = pyqtSignal(str, float, float)
    timer = pyqtSignal(object, float)
    fact = pyqtSignal(int, object)
    profile = pyqtSignal(str, object)
    status = pyqtSignal(str)
    agenda = pyqtSignal(object)
    resolved = pyqtSignal(object)
    failed = pyqtSignal(str)
    ended = pyqtSignal()


class Video(QWidget):
    def __init__(self):
        super().__init__()
        self.img = None
        self.banner = None          # (lines, color, until)
        self.subtitle = None        # (speaker label, text) - the line being heard now
        self.setMinimumSize(480, 270)
        self.setStyleSheet("background:#000")

    def set_frame(self, data):
        self.img = QImage(data, stream.VW, stream.VH, stream.VW * 3, QImage.Format.Format_RGB888).copy()
        self.update()

    def show_banner(self, lines, color, secs=8):
        self.banner = (lines, color, time.time() + secs)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000"))
        if self.img is not None:
            w, h = self.width(), self.height()
            s = min(w / stream.VW, h / stream.VH)
            dw, dh = stream.VW * s, stream.VH * s
            target = QRectF((w - dw) / 2, (h - dh) / 2, dw, dh)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(target, self.img)
        else:
            p.setPen(QColor("#8b93a7"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Nalepi YouTube link sednice i pritisni Počni")
        if self.subtitle and self.img is not None:
            self._draw_subtitle(p, target, lift=bool(self.banner and time.time() < self.banner[2]))
        if self.banner and time.time() < self.banner[2]:
            lines, color, _ = self.banner
            w = self.width()
            bh = 34 + 24 * (len(lines) - 1)
            r = QRectF(12, self.height() - bh - 16, w - 24, bh)
            path = QPainterPath()
            path.addRoundedRect(r, 8, 8)
            c = QColor(color)
            c.setAlpha(225)
            p.fillPath(path, c)
            p.setPen(Qt.GlobalColor.white)
            f = QFont("Segoe UI", 15, QFont.Weight.Bold)
            p.setFont(f)
            p.drawText(r.adjusted(14, 6, -14, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, lines[0])
            p.setFont(QFont("Segoe UI", 11))
            for i, ln in enumerate(lines[1:]):
                p.drawText(r.adjusted(14, 36 + 24 * i, -14, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, ln)
        p.end()


    def _draw_subtitle(self, p, target, lift=False):
        """Subtitle at the bottom of the picture: speaker (yellow) + what is being said, on a dark band."""
        who, text = self.subtitle
        size = max(11, int(target.height() / 24))
        f_text, f_who = QFont("Segoe UI", size), QFont("Segoe UI", max(9, int(size * 0.75)), QFont.Weight.Bold)
        maxw = target.width() * 0.9
        flags = int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap)
        p.setFont(f_text)
        tb = p.boundingRect(QRectF(0, 0, maxw, 1000), flags, text)
        p.setFont(f_who)
        wb = p.boundingRect(QRectF(0, 0, maxw, 1000), flags, who) if who else QRectF()
        width = max(tb.width(), wb.width()) + 24
        height = tb.height() + wb.height() + 10
        bottom = target.bottom() - (70 if lift else 10)
        r = QRectF(target.center().x() - width / 2, bottom - height, width, height)
        path = QPainterPath()
        path.addRoundedRect(r, 6, 6)
        p.fillPath(path, QColor(0, 0, 0, 175))
        inner = r.adjusted(12, 5, -12, -5)
        if who:                                          # speaker [party] in yellow, then the words
            p.setPen(QColor("#ffd54a"))
            p.drawText(QRectF(inner.left(), inner.top(), inner.width(), wb.height()), flags, who)
        p.setFont(f_text)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(inner.left(), inner.top() + wb.height(), inner.width(), tb.height()), flags, text)


class JumpSlider(QSlider):
    """Timeline: a click on the bar jumps straight to that point (Qt's default only steps a page)."""
    jumped = pyqtSignal(int)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(e.position().toPoint()):
                v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()),
                                                   self.width())
                self.setValue(v)
                self.jumped.emit(v)
                e.accept()
                return
        super().mousePressEvent(e)


HALL = "Iz sale (nepoznati)"


class NumItem(QTableWidgetItem):
    """Table cell that sorts by its number (UserRole), not by its text."""

    def __lt__(self, other):
        a, b = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        if isinstance(a, int) and isinstance(b, int):
            return a < b
        return super().__lt__(other)


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.s = load_settings()
        self.setWindowTitle("Virtuelni predsednik skupštine – nadzor reda na sednici (Poslovnik NS RS)")
        self.resize(1500, 880)
        self.b = Bridge()
        self.b.segment.connect(self.on_segment)
        self.b.result.connect(self.on_result)
        self.b.caption.connect(self.on_caption)
        self.b.caption_local.connect(self.on_caption_local)
        self.b.face.connect(self.on_face)
        self.b.timer.connect(self.on_timer)
        self.b.fact.connect(self.on_fact)
        self.b.profile.connect(self.on_profile)
        self.profiler = None
        self.facter = None
        self.profiler = None
        self.ocr = None
        self.ocr_frame = None
        self.facew = None
        self.face_frame = None
        self.face_seen = None           # (name, similarity, wall time) - shown in the speaker panel
        self.b.status.connect(self.set_status)
        self.b.agenda.connect(self.on_agenda)
        self.b.resolved.connect(self.on_resolved)
        self.b.failed.connect(self.on_failed)
        self.b.ended.connect(lambda: self.set_status("Kraj snimka."))
        self.player = self.asr = self.judge = None
        self.latest_frame = None
        self.running = False
        self._build()
        self.frame_timer = QTimer(self, interval=40, timeout=self.tick_video)
        self.frame_timer.start()
        self.ui_timer = QTimer(self, interval=500, timeout=self.tick_ui)
        self.ui_timer.start()

    # ---- layout --------------------------------------------------------------------------------
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        v = QVBoxLayout(root)
        v.setContentsMargins(14, 10, 14, 6)
        v.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel("Virtuelni predsednik skupštine")
        title.setObjectName("appTitle")
        sub = QLabel("nadzor reda na sednici po Poslovniku Narodne skupštine")
        sub.setObjectName("appSub")
        head.addWidget(title)
        head.addSpacing(10)
        head.addWidget(sub)
        head.addStretch(1)
        v.addLayout(head)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.url = QLineEdit(self.s.get("last_url", ""))
        self.url.setPlaceholderText("YouTube link (uživo ili snimak sednice)")
        self.start_at = QLineEdit()
        self.start_at.setPlaceholderText("od 0:00:00")
        self.start_at.setFixedWidth(90)
        self.start_at.setToolTip("Samo za snimke: odakle da krene (npr. 1:30:00)")
        self.go = QPushButton("▶ Počni")
        self.go.setObjectName("primary")
        self.go.setMinimumWidth(96)
        self.go.clicked.connect(self.toggle)
        self.model = QComboBox()
        from judge import MODELS
        for mid, label in MODELS:
            self.model.addItem(label, mid)
        self.model.insertSeparator(self.model.count())
        for mid, label in local_llm.MODELS:
            self.model.addItem(f"Lokalno: {label} – besplatno", f"local:{mid}")
        self.model.addItem("Hibrid: lokalni sudija + Claude provere", f"hybrid:{local_llm.MODELS[0][0]}")
        i = self.model.findData(self.s["judge_model"])
        self.model.setCurrentIndex(max(0, i))
        keyb = QPushButton("API ključ…")
        keyb.clicked.connect(self.ask_key)
        keyb.setToolTip("Tvoj Claude API ključ (console.anthropic.com → API Keys). Troškovi idu na taj ključ.\n"
                        "Bez ključa sudi lokalni model (Ollama) na ovom računaru - besplatno, ali slabije.")
        self.report_btn = QPushButton("Izveštaj")
        self.report_btn.setToolTip("Sačuvaj i otvori HTML izveštaj sednice (statistika, mere, retorika, tvrdnje)")
        self.report_btn.clicked.connect(lambda: self.save_report(open_it=True))
        self.tr_btn = QPushButton("Transkript…")
        self.tr_btn.setToolTip("Sačuvaj transkript: tekst (.txt), titlovi (.srt) ili stranica za Word (.html)")
        self.tr_btn.clicked.connect(self.save_transcript_dialog)
        self.posl_btn = QPushButton("Poslovnik")
        self.posl_btn.clicked.connect(self.toggle_poslovnik)
        self.sub_btn = QPushButton("Titl")
        self.sub_btn.setCheckable(True)
        self.sub_btn.setChecked(self.s.get("subtitles", True))
        self.sub_btn.toggled.connect(self.toggle_subtitles)
        self.poslovnik = None
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(self.s["volume"] * 100))
        self.vol.setFixedWidth(90)
        self.vol.valueChanged.connect(self.set_volume)
        lbl_link, lbl_judge = QLabel("Link"), QLabel("Sudija")
        for lb in (lbl_link, lbl_judge):
            lb.setProperty("muted", True)
        for w in (lbl_link, self.url, self.start_at, self.go, lbl_judge, self.model, keyb,
                  self.report_btn, self.tr_btn, self.posl_btn, self.sub_btn,
                  QLabel("🔊"), self.vol):
            top.addWidget(w)
        top.setStretch(1, 1)
        v.addLayout(top)
        ag = QHBoxLayout()
        self.agenda = QLineEdit()
        self.agenda.setPlaceholderText("Tačka dnevnog reda (popuni ili će je aplikacija prepoznati iz govora)")
        lbl_ag = QLabel("Tačka")
        lbl_ag.setProperty("muted", True)
        ag.addWidget(lbl_ag)
        ag.addWidget(self.agenda, 1)
        self.agenda_box = QComboBox()
        self.agenda_box.setMinimumWidth(320)
        self.agenda_box.setVisible(False)
        self.agenda_box.activated.connect(self.pick_agenda)
        ag.addWidget(self.agenda_box)
        v.addLayout(ag)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.video = Video()
        self.video.setFocusPolicy(Qt.FocusPolicy.ClickFocus)       # click the video, then ← → / space work
        lv.addWidget(self.video, 1)
        seek_row = QHBoxLayout()
        seek_row.setContentsMargins(0, 0, 8, 0)
        self.back_btn = QPushButton("⏪ 30 s")
        self.back_btn.clicked.connect(lambda: self.skip(-30))
        self.pause_btn = QPushButton("⏸")
        self.pause_btn.setFixedWidth(40)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.fwd_btn = QPushButton("30 s ⏩")
        self.fwd_btn.clicked.connect(lambda: self.skip(30))
        self.timeline = JumpSlider(Qt.Orientation.Horizontal)
        self.timeline.sliderPressed.connect(lambda: setattr(self, "dragging", True))
        self.timeline.sliderMoved.connect(lambda v: self.time_lbl.setText(f"→ {fmt_time(v)}"))
        self.timeline.sliderReleased.connect(self._timeline_released)
        self.timeline.jumped.connect(self.seek)
        self.time_lbl = QLabel("0:00:00 / 0:00:00")
        self.time_lbl.setFont(QFont("Consolas", 10))
        for w in (self.back_btn, self.pause_btn, self.fwd_btn):
            w.setEnabled(False)
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            seek_row.addWidget(w)
        self.timeline.setEnabled(False)
        self.timeline.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.timeline.setToolTip("Prevuci ili klikni da skočiš na deo snimka (← → 10 s, razmak = pauza)")
        seek_row.addWidget(self.timeline, 1)
        seek_row.addWidget(self.time_lbl)
        lv.addLayout(seek_row)
        self.dragging = False
        panel = QWidget()
        panel.setObjectName("speakerCard")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(16, 12, 16, 12)
        self.sp_name = QLabel("—")
        self.sp_name.setObjectName("spName")
        self.sp_role = QLabel("")
        self.sp_role.setObjectName("spRole")
        row = QHBoxLayout()
        self.sp_timer = QLabel("0:00")
        self.sp_timer.setFont(QFont("Consolas", 22, QFont.Weight.Bold))
        self.sp_official = QLabel("")
        self.sp_official.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        self.sp_official.setToolTip("Zvanični tajmer sa ekrana prenosa (preostalo vreme)")
        self.sp_voice = QLabel("")
        self.sp_voice.setObjectName("spVoice")
        self.loud = QProgressBar()
        self.loud.setRange(0, 20)
        self.loud.setTextVisible(True)
        self.loud.setFormat("glasnoća +%v dB")
        self.loud.setFixedWidth(170)
        row.addWidget(self.sp_timer)
        row.addSpacing(16)
        row.addWidget(self.sp_official)
        row.addSpacing(16)
        row.addWidget(self.sp_voice, 1)
        row.addWidget(self.loud)
        self.sp_chair = QLabel("Predsedava: nije još prepoznat")
        self.sp_chair.setObjectName("spChair")
        pv.addWidget(self.sp_chair)
        pv.addWidget(self.sp_name)
        pv.addWidget(self.sp_role)
        pv.addLayout(row)
        lv.addWidget(panel)
        split.addWidget(left)

        self.tabs = QTabWidget()
        self.cards = QTextBrowser()
        self.cards.setOpenExternalLinks(False)
        self.transcript = QTextBrowser()
        self.table = QTableWidget(0, len(report.RANK_COLS))
        self.table.setHorizontalHeaderLabels([c[1] for c in report.RANK_COLS])
        for j, c in enumerate(report.RANK_COLS):
            self.table.horizontalHeaderItem(j).setToolTip(c[2])
        hh = self.table.horizontalHeader()
        hh.setMinimumSectionSize(30)
        hh.setStretchLastSection(False)
        self.table.setStyleSheet("QHeaderView::section { padding: 3px 4px; }")
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(True)            # row number = place on the list
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self.facts = QTextBrowser()
        self.chair = QTextBrowser()
        self.stats = QTextBrowser()
        self.rhet = QTextBrowser()
        self.groups = QTextBrowser()
        self.profs = QTextBrowser()
        for tb in (self.cards, self.transcript, self.facts, self.chair, self.stats, self.rhet, self.groups,
                   self.profs):
            tb.setOpenLinks(False)                     # [time] links jump the video there
            tb.anchorClicked.connect(self.on_link)
        self.transcript.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.transcript.customContextMenuRequested.connect(self.transcript_menu)
        self.transcript.setToolTip("Desni klik na red: proveri tvrdnju na internetu")
        self.tabs.addTab(self.cards, "Mere")
        self.tabs.addTab(self.transcript, "Transkript")
        self.rank_detail = QTextBrowser()
        self.rank_detail.setOpenLinks(False)
        self.rank_detail.anchorClicked.connect(self.on_link)
        self.rank_detail.setHtml("<p style='color:#888'>Klik na ćeliju: rečenice na koje se broj odnosi "
                                 "(klik na vreme vraća snimak na taj trenutak).</p>")
        self.table.cellClicked.connect(self.show_rank_detail)
        rank_split = QSplitter(Qt.Orientation.Vertical)
        rank_split.addWidget(self.table)
        rank_split.addWidget(self.rank_detail)
        rank_split.setSizes([300, 260])
        self.tabs.addTab(rank_split, "Rang lista")
        self.tabs.addTab(self.facts, "Za proveru (tvrdnje)")
        self.tabs.addTab(self.chair, "Stvarni predsedavajući")
        self.tabs.addTab(self.stats, "Statistika")
        self.tabs.addTab(self.rhet, "Retorika")
        self.tabs.addTab(self.groups, "Po grupama")
        self.tabs.addTab(self.profs, "Profili")
        split.addWidget(self.tabs)
        split.setSizes([820, 680])
        v.addWidget(split, 1)
        self.setCentralWidget(root)
        self.cost_lbl = QLabel("")
        self.statusBar().addPermanentWidget(self.cost_lbl)
        self.add_tooltips()
        self.set_status("Spremno." + ("" if self.s["api_key"] else
                                      "  Bez API ključa sudi lokalni model (Ollama) - besplatno, ali slabije."))

    def _go_state(self, running):
        self.go.setProperty("running", running)
        self.go.style().unpolish(self.go)
        self.go.style().polish(self.go)

    def add_tooltips(self):
        tips = {
            self.url: "Link YouTube prenosa ili snimka sednice Narodne skupštine (npr. kanal ParlamentSrbija).",
            self.go: "Počni / zaustavi praćenje. Na zaustavljanje se izveštaj i transkript snimaju automatski.",
            self.sub_btn: "Transkript kao titl preko slike: ko govori (i stranka) i šta upravo govori.",
            self.posl_btn: "Otvori ceo Poslovnik Narodne skupštine u bočnom panelu (pretraga, sadržaj, skok na član).\n"
                           "Klik na član na kartici mere (npr. čl. 107) otvara baš taj član.",
            self.model: "Ko sudi po Poslovniku i proverava činjenice.\n"
                        "Claude (treba API ključ): Haiku je najjeftiniji (~10 ¢ na sat sednice); Sonnet i Opus su\n"
                        "pažljiviji, ali skuplji.\n"
                        "Lokalno: model na ovom računaru preko Ollame - bez ključa i besplatno, sve opcije rade, ali\n"
                        "slabije razume srpski i Poslovnik (više promašaja i lažnih prijava). Pretraga interneta\n"
                        "ide preko DuckDuckGo-a. Prvi put se model preuzima (3-5 GB).\n"
                        "Hibrid: lokalni sudija (često, besplatno) + Claude za proveru činjenica i profile.\n"
                        "Claude model bez ključa automatski prelazi na lokalni.",
            self.vol: "Jačina zvuka prenosa (ne utiče na prepoznavanje govora).",
            self.agenda: "Tačka dnevnog reda. Koristi se da se proceni da li govornik govori van teme (čl. 106).\n"
                         "Na početku sesije aplikacija nađe dnevni red na internetu (desno); sudija prati koja se\n"
                         "tačka raspravlja i prelazak na sledeću.",
            self.back_btn: "Nazad 30 sekundi (strelica ← = 10 s). Već ocenjeni delovi se ne kažnjavaju ponovo.",
            self.pause_btn: "Pauza / nastavak (razmak).",
            self.fwd_btn: "Napred 30 sekundi (strelica → = 10 s).",
            self.time_lbl: "Trenutno vreme snimka / ukupno trajanje.",
            self.sp_name: "Ko trenutno govori. ✓ = potvrđeno (natpis na ekranu ili najava predsedavajućeg);\n"
                          "♂/♀ = pol iz spiska poslanika ili iz govora. Kad se kasnije prepozna ko je bio\n"
                          "'Govornik N' ili 'Glas N', sav njegov raniji govor, mere i statistika prelaze na to ime.",
            self.sp_role: "Uloga i poslanička grupa govornika, i odakle aplikacija zna ko je to.",
            self.sp_timer: "Koliko dugo govori od kada je dobio reč (prema rokovima iz Poslovnika).",
            self.sp_voice: "Glas: poklapanje sa glasovnim otiskom govornika i promena glasa.",
            self.loud: "Glasnoća u odnosu na govornikov uobičajeni glas - visoko = povišen ton/vikanje.",
            self.video: "Prenos. Klikni na video, pa ← → za premotavanje i razmak za pauzu.",
            self.cost_lbl: "Potrošnja Claude API-ja u ovoj sesiji (sudija + provere na internetu + profili).",
            self.sp_chair: "Ko predsedava: predsednik ili potpredsednik Narodne skupštine. Prepoznaje se po natpisu\n"
                           "na ekranu, po tome kako mu se poslanici obraćaju ('predsedniče Čupiću'), po najavi\n"
                           "promene i po glasu - kad drugi glas počne da vodi sednicu, predsedavajući se promenio.",
        }
        for w, t in tips.items():
            w.setToolTip(t)
        tab_tips = [
            "Mere koje bi predsedavajući trebalo da izrekne po Poslovniku (upozorenje → opomena →\n"
            "oduzimanje reči → udaljenje) i novčane kazne po čl. 114.",
            "Sve izgovoreno, sa vremenom i govornikom. Klik na vreme = skok u snimku.\n"
            "Desni klik na red: proveri tvrdnju na internetu ili je sam označi kao tačnu/netačnu.",
            "Zbir po poslaniku: upozorenja, opomene, oduzimanje reči, udaljenje i kazna u % plate.",
            "Tvrdnje proverene na internetu sa ocenom (tačno … netačno) i izvorima. Laž nije povreda reda,\n"
            "pa za nju nema mere - samo ocena.",
            "Šta je stvarni predsedavajući uradio: davanje reči, opomene, oduzimanje reči.",
            "Stvarni predsedavajući naspram aplikacije: poklapanja, propuštene mere i ocena predsedavajućeg\n"
            "(100 minus bodovi za svaku neizrečenu meru).",
            "Ton govora i tehnike manipulacije (napad na ličnost, etiketiranje, relativizacija…) sa citatima,\n"
            "plus provere šireg konteksta (dvostruki aršini, raniji stavovi). Samo informacija - mere su samo\n"
            "po Poslovniku.",
            "Isto po poslaničkim grupama: vreme govora, mere na 10 minuta, kazne, tehnike, netačne tvrdnje.",
            "Javni profili poslanika koji govore (sa izvorima i datumima) - pozadina za razumevanje konteksta.",
        ]
        for i, t in enumerate(tab_tips[:self.tabs.count()]):
            self.tabs.setTabToolTip(i, t)

    # ---- controls --------------------------------------------------------------------------------
    def ask_key(self):
        k, ok = QInputDialog.getText(self, "Claude API ključ",
                                     "Anthropic API ključ (console.anthropic.com → API Keys).\n"
                                     "Čuva se u settings_local.json pored aplikacije.",
                                     QLineEdit.EchoMode.Password, self.s.get("api_key", ""))
        if ok:
            self.s["api_key"] = k.strip()
            save_settings(self.s)
            self.set_status("API ključ sačuvan." if k.strip() else "API ključ obrisan.")

    def set_volume(self, v):
        self.s["volume"] = v / 100
        if self.player:
            self.player.volume = v / 100

    def set_status(self, text):
        self.statusBar().showMessage(text)

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        url = self.url.text().strip()
        if not url:
            return
        self.s["last_url"] = url
        self.s["judge_model"] = self.model.currentData()
        save_settings(self.s)
        self.go.setEnabled(False)
        self._local_ok = self.check_local()
        need = self._local_ok and "local" in local_llm.plan(self.s, True)[:2]
        model = local_llm.plan(self.s, True)[2]
        self.set_status("Tražim stream (yt-dlp)…")

        def work():
            try:
                if need:
                    st, _msg = local_llm.status(model)
                    if st == "not_running":
                        self.b.status.emit("Pokrećem Ollamu…")
                        local_llm.start_server()
                        st, _msg = local_llm.status(model)
                    if st == "no_model":
                        st = "ok" if local_llm.pull(model, self.b.status.emit) else st
                    if st != "ok":
                        self._local_ok = False
                        self.b.status.emit(f"Lokalni model nije spreman ({_msg}) - nastavljam bez sudije.")
                self.b.resolved.emit(stream.resolve(url))
            except Exception as e:
                self.b.failed.emit(str(e))
        threading.Thread(target=work, daemon=True).start()

    def check_local(self):
        """Is a local model usable / wanted for this run? Asks before downloading a model."""
        judge, research, model = local_llm.plan(self.s, True)
        if "local" not in (judge, research):
            return True
        st, msg = local_llm.status(model)
        if st == "no_ollama":
            QMessageBox.information(
                self, "Lokalni model",
                "Za rad bez API ključa potrebna je Ollama (besplatna): https://ollama.com/download\n\n"
                "Bez nje (i bez ključa) radi samo prepoznavanje govora, govornika i vremena.")
            return False
        if st == "no_model":
            size = next((lb for m, lb in local_llm.MODELS if m == model), model)
            ans = QMessageBox.question(self, "Lokalni model",
                                       f"Preuzeti lokalni model {size}?\nJednom, posle radi bez interneta i "
                                       "besplatno. Preuzimanje traje nekoliko minuta.")
            return ans == QMessageBox.StandardButton.Yes
        return True

    def ask_resume(self):
        """The saved state of this video's earlier session, if the user wants to continue it."""
        ent = session_state.find(self.s["last_url"])
        if not ent:
            return None
        env = os.environ.get("PRED_RESUME")              # tests: "1" continue, "0" fresh
        if env is None and os.environ.get("PRED_AUTOTEST"):
            env = "0"
        if env is None:
            ago = time.time() - ent["saved"]
            ago_s = f"{ago / 60:.0f} min" if ago < 5400 else f"{ago / 3600:.1f} h"
            box = QMessageBox(QMessageBox.Icon.Question, "Ista sednica",
                              f"Ovaj video je već praćen (pre {ago_s}): {ent['lines']} redova transkripta, "
                              f"{ent['measures']} mera, zaustavljeno na {fmt_time(ent['pos'])}.\n\n"
                              "Nastaviti tu sesiju? Transkript, mere, statistika, glasovi govornika i provere "
                              "ostaju" + (", a snimak se nastavlja odatle." if not ent["live"] else "."),
                              parent=self)
            cont = box.addButton("Nastavi sesiju", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Počni iznova", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            env = "1" if box.clickedButton() is cont else "0"
        if env != "1":
            return None
        try:
            return session_state.load(ent["path"])
        except Exception as e:
            log("session load:", e)
            QMessageBox.warning(self, "Ista sednica", f"Ne mogu da učitam prethodnu sesiju: {e}")
            return None

    def restore_session(self, st, start_s):
        """Put a saved session back: data first, then every pane is rebuilt from it."""
        for k, v in st["fields"].items():
            setattr(self, k, v)
        self.tracker = st["tracker"]
        self.tracker.on_rename = self.on_speaker_renamed
        self.tracker.chair.on_change = [self.on_chair_change]
        self.tracker.run, self.last_turn_obj = None, None     # the speaker on the floor stays (panel, timer)
        turns = self.tracker.turns
        self.turn_flagged = {("official", id(turns[x[1]])) if isinstance(x, (tuple, list)) else id(turns[x])
                             for x in st["flagged"]}
        self.profiles = dict(st["profiles"])
        self.chair.setHtml(st["chair_html"])
        self.agenda.setText(st["agenda"])
        if st["agenda_items"]:
            for i, title in enumerate(st["agenda_items"]):
                self.agenda_box.addItem(f"{i + 1}. {title}", title)
            self.agenda_box.setToolTip(st["agenda_tip"])
            self.agenda_box.setVisible(True)
            if st["agenda"] in st["agenda_items"]:
                self.agenda_box.setCurrentIndex(st["agenda_items"].index(st["agenda"]))
            if self.judge:
                self.judge.agenda_list = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(st["agenda_items"]))
        if self.judge:
            self.judge.cost, self.judge.calls = st["cost"], st["calls"]
        if self.facter:
            self.facter.cost, self.facter.checked = st["fact_cost"], st["checked"]
            for c in self.claims:                        # checked ones are not checked again; unfinished go on
                if c["status"] == "done":
                    self.facter.seen.append(fold_text(c["claim"]))
                elif c["status"] in ("queued", "checking"):
                    c["status"] = "queued"
                    self.facter.submit(c)
        self.tx_items.append({"resume": start_s})
        self.render(self.transcript, "".join(f"<p style='margin:0'>{self.tx_html(it)}</p>" for it in self.tx_items))
        self.rebuild_measures()
        self.render_rhetoric()
        self.render_facts()
        self.render(self.profs, report.profiles_html(self.profiles))
        self.groups_dirty = True
        self.update_speaker_panel()
        self.set_status(f"Nastavljena sesija: {len(self.segments)} redova, {len(self.app_measures)} mera.")

    def on_failed(self, msg):
        self.go.setEnabled(True)
        QMessageBox.warning(self, "Greška", msg)
        self.set_status(msg)

    def on_resolved(self, src):
        from asr import ASRWorker
        self.src = src
        self.discipline = Discipline()
        self.tracker = SpeakerTracker(self.s["voice_change_sim"])
        self.tracker.on_rename = self.on_speaker_renamed
        self.tracker.chair.on_change.append(self.on_chair_change)
        self.sp_chair.setText("Predsedava: nije još prepoznat")
        self.incidents = []             # every violation the judge reported; measures are derived from these
        self.tx_items = []              # transcript lines (rebuilt with final names after a rename)
        self.relabel_pending = False
        self.last_measure = {}          # offender -> (time, card index)
        self.app_measures = []          # (t, offender, measure)
        self.turn_flagged = set()
        self.last_caption_req = 0.0
        self.last_turn_obj = None
        for w in (self.cards, self.transcript, self.facts, self.chair, self.stats, self.rhet, self.groups,
                  self.profs):
            w.clear()
        self.profiles = {}              # name -> profile dict (None while being built)
        self.last_context = {}          # speaker -> time of the last context check (rate limit)
        self.groups_dirty = False
        self.real_measures = []         # what the real chair did: {t, target, measure, quote}
        self.claims = []                # fact-check items
        self.rhetoric = []              # {t, speaker, technique, quote, explanation}
        self.tones = []                 # {t, speaker, tone}
        self.recent_lines = []
        self.segments = []              # for transcript export
        self.report_path = None
        self.table.setRowCount(0)
        start_s = 0.0 if src["is_live"] else parse_time(self.start_at.text())
        resume = self.ask_resume()
        if resume:
            # recording: back where it stopped (a few seconds early); live: the clock goes on by the time away
            start_s = (resume["pos"] + (time.time() - resume["saved"]) if src["is_live"]
                       else max(0.0, resume["pos"] - 5))
            self.start_at.setText("0" if src["is_live"] else fmt_time(start_s))
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        safe = re.sub(r"[^\w\- ]", "", src["title"])[:60].strip() or "sednica"
        self.log_path = os.path.join(SESSIONS, f"{stamp}_{safe}.jsonl")
        if resume and os.path.exists(resume["log_path"]):
            self.log_path = resume["log_path"]        # one session = one log, also across restarts
        self.logf = open(self.log_path, "a", encoding="utf-8")
        self._log({"type": "resume" if resume else "start", "title": src["title"], "live": src["is_live"],
                   "start_s": start_s, "url": self.s["last_url"], "model": self.s["judge_model"]})
        self.last_state_save = time.time()
        self.asr = ASRWorker(self.s["whisper_model"], self.b.segment.emit, self.b.status.emit)
        self.asr.start()
        jb, rb, lm = local_llm.plan(self.s, self._local_ok)
        self.s["_judge"], self.s["_research"], self.s["_local_model"] = jb, rb, lm
        if jb:
            from judge import Judge
            from factcheck import FactChecker
            self.judge = Judge(self.s, self.b.result.emit, self.b.caption.emit, self.b.status.emit)
            self.judge.start()
            m = re.search(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d\d)", src["title"])
            d = src.get("date", "")
            date = (f"{m.group(1)}.{m.group(2)}.{m.group(3)}." if m
                    else f"{d[6:8]}.{d[4:6]}.{d[:4]}." if len(d) == 8
                    else datetime.date.today().strftime("%d.%m.%Y."))
            self.facter = FactChecker(self.s, self.b.fact.emit, self.b.status.emit, date)
            self.facter.start()
            import agenda as agenda_mod
            self.agenda_hits = []
            self.agenda_box.clear()
            self.agenda_box.setVisible(False)
            if not (resume and resume["agenda_items"]):
                self.set_status("Tražim dnevni red sednice na internetu…")
                agenda_mod.find_async(dict(self.s), src["title"], date, self.b.agenda.emit)
            if self.s.get("profiles", True):
                from profiles import ProfileBuilder
                self.profiler = ProfileBuilder(self.s, self.b.profile.emit, self.b.status.emit)
                self.profiler.start()
        else:
            self.judge = None
            self.facter = None
        self.resolved_at = time.time()
        self.paused = False
        self.official_timer = None          # seconds left on the broadcast's countdown, if shown
        self.timer_zero_at = None
        self.last_local_caption = 0.0
        self.sp_official.setText("")
        from videoocr import CaptionReader, cyrillic_ocr_available
        if not cyrillic_ocr_available():
            log("Serbian-Cyrillic OCR not installed - captions read as Latin look-alikes")
        self.ocr = CaptionReader(self._ocr_frame, stream.VW, stream.VH, self.b.caption_local.emit,
                                 self.b.timer.emit)
        self.ocr.start()
        if self.s.get("faces", True):
            from faces import FaceWatcher
            self.facew = FaceWatcher(self._face_frame, self.b.face.emit, self.b.status.emit)
            self.facew.start()
        self.covered = []               # [from, to] stretches already listened to and judged
        if resume:
            self.restore_session(resume, start_s)
        self._new_stretch(start_s)
        if not self._start_player(start_s):
            return
        self.running = True
        self.go.setEnabled(True)
        self.go.setText("■ Stani")
        self._go_state(True)
        kind = "UŽIVO" if src["is_live"] else "snimak"
        live = src["is_live"]
        dur = int(src["duration"] or 0)
        self.timeline.setEnabled(not live and dur > 0)
        self.timeline.setRange(0, max(1, dur))
        for b in (self.back_btn, self.pause_btn, self.fwd_btn):
            b.setEnabled(not live)
        self.setWindowTitle(f"Virtuelni predsednik skupštine – {src['title']} ({kind})")
        self.set_status(f"Puštam: {src['title']} ({kind}). Model govora se učitava…")

    def _start_player(self, start_s):
        if not self.src["is_live"] and time.time() - self.resolved_at > 5 * 3600:
            # YouTube stream links expire after ~6 h - fetch fresh ones
            self.set_status("Obnavljam link ka snimku…")
            QApplication.processEvents()
            try:
                self.src = stream.resolve(self.s["last_url"])
                self.resolved_at = time.time()
            except Exception as e:
                self.on_failed(str(e))
                return False
        self.player = stream.Player(self.src, start_s, self.asr.push, self._frame, self.b.ended.emit)
        self.player.volume = self.s["volume"]
        try:
            self.player.start()
        except Exception as e:
            self.on_failed(f"Ne mogu da pokrenem reprodukciju: {e}")
            return False
        return True

    # ---- scrolling through a recording -----------------------------------------------------------
    def seek(self, t):
        if not self.running or self.src["is_live"]:
            return
        dur = self.src["duration"] or 0
        t = max(0.0, min(float(t), dur - 3 if dur else float(t)))
        if self.player:
            self.player.stop()
        self.asr.reset()
        if self.judge:
            self.judge.reset()
        self._new_stretch(t)
        self.tracker.turn, self.tracker.run, self.last_turn_obj = None, None, None
        self.sp_name.setText("—")
        self.sp_role.setText("čekam da predsedavajući najavi govornika ili prepoznam glas…")
        self.paused = False
        self.pause_btn.setText("⏸")
        self.timeline.setValue(int(t))
        self.tx_items.append({"seek": t})
        self.transcript.append(self.tx_html(self.tx_items[-1]))
        self._log({"type": "seek", "t": t})
        self._start_player(t)

    def _new_stretch(self, t):
        self.cur_stretch = [t, t]
        self.covered.append(self.cur_stretch)

    def already_judged(self, t):
        """True if moment t was already covered before the latest seek (don't judge or penalize twice)."""
        return any(iv is not self.cur_stretch and iv[0] <= t < iv[1] for iv in self.covered)

    def skip(self, delta):
        if self.running and self.player:
            base = self.paused_at if self.paused else self.player.stream_time
            self.seek(base + delta)

    def toggle_pause(self):
        if not self.running or self.src["is_live"]:
            return
        if self.paused:
            # resume a bit early: speech still buffered for transcription at pause time was dropped
            self.seek(min(self.paused_at, self.asr.last_end or self.paused_at))
        else:
            self.paused_at = self.player.stream_time
            self.player.stop()
            self.paused = True
            self.pause_btn.setText("▶")
            self.set_status(f"Pauza na {fmt_time(self.paused_at)}")

    def toggle_subtitles(self, on):
        self.s["subtitles"] = bool(on)
        save_settings(self.s)
        if not on:
            self.video.subtitle = None
            self.video.update()

    def toggle_poslovnik(self):
        if self.poslovnik is None:
            from poslovnik_view import PoslovnikDock
            self.poslovnik = PoslovnikDock(self)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.poslovnik)
            QTimer.singleShot(0, lambda: self.resizeDocks([self.poslovnik], [500], Qt.Orientation.Horizontal))
            return
        self.poslovnik.setVisible(not self.poslovnik.isVisible())

    def open_article(self, no):
        if self.poslovnik is None:
            self.toggle_poslovnik()
        self.poslovnik.show_article(no)

    def dismiss_incident(self, i, dismissed=True):
        """'✕ nije prekršaj' on a card: the user says it was not a breach (e.g. a misheard word). The measure
        is withdrawn, the ladder replayed without it, and the chair's score no longer counts it as missed."""
        if not (0 <= i < len(self.incidents)):
            return
        inc = self.incidents[i]
        inc["dismissed"] = dismissed
        self._log({"type": "dismiss" if dismissed else "undismiss", "t": inc["t"], "offender": inc["offender"],
                   "kind": inc["kind"], "quote": inc["quote"]})
        self.rebuild_measures()
        self.set_status(("Mera poništena: " if dismissed else "Mera vraćena: ") + f"{inc['offender']} – "
                        f"„{inc['quote'][:60]}“")

    def on_link(self, url):
        s = url.toString()
        if s.startswith("posl:"):
            self.open_article(int(s[5:]))
        elif s.startswith(("dis:", "undis:")):
            self.dismiss_incident(int(s.split(":")[1]), s.startswith("dis:"))
        elif s.startswith("http"):
            QDesktopServices.openUrl(url)
        elif s.startswith("t:"):
            self.seek(float(s[2:]) - 3)        # land just before the moment

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Left:
            self.skip(-10)
        elif k == Qt.Key.Key_Right:
            self.skip(10)
        elif k == Qt.Key.Key_Space:
            self.toggle_pause()
        else:
            super().keyPressEvent(e)

    @staticmethod
    def tlink(t):
        return f"<a href='t:{t:.1f}' style='color:#888;text-decoration:none'>[{fmt_time(t)}]</a>"

    def stop(self):
        self.running = False
        if self.player:
            self.player.stop()
        if self.asr:
            self.asr.stop(abort=True)
        if self.judge:
            self.judge.stop()
        if self.ocr:
            self.ocr.stop()
            self.ocr = None
        if self.facew:
            self.facew.stop()
            self.facew = None
        if self.facter:
            self.facter.stop()
        if self.profiler:
            self.profiler.stop()
        self._log({"type": "stop"})
        session_state.save(self)
        try:
            self.save_report()
            self.save_transcript(self.log_path[:-6] + "_transkript.txt")
        except Exception as e:
            log("report:", e)
        self.facter = None
        self.logf.close()
        self.player = self.asr = self.judge = None
        self.go.setText("▶ Počni")
        self._go_state(False)
        self.timeline.setEnabled(False)
        for b in (self.back_btn, self.pause_btn, self.fwd_btn):
            b.setEnabled(False)
        self.set_status(f"Zaustavljeno. Izveštaj: {self.report_path or self.log_path}")

    def closeEvent(self, e):
        if self.running:
            self.stop()
        save_settings(self.s)
        e.accept()

    def _frame(self, data):
        self.latest_frame = data           # painted by tick_video in the GUI thread
        self.ocr_frame = data              # read by the caption OCR thread
        self.face_frame = data             # read by the face watcher

    def _face_frame(self):
        """(RGB picture, time) for the face watcher, or None (nothing new, paused)."""
        f, self.face_frame = self.face_frame, None
        if f is None or not self.player or self.paused:
            return None
        import numpy as np
        return np.frombuffer(f, np.uint8).reshape(stream.VH, stream.VW, 3), self.player.stream_time

    def voice_at(self, t):
        """Who was talking at moment t (transcript segments)."""
        for g in reversed(self.segments[-60:]):
            if g["t0"] - 0.5 <= t <= g["t1"] + 1.0:
                return g["who"]
            if g["t1"] < t - 10:
                break
        return None

    def on_face(self, name, sim, t):
        """The face whose lips move on screen is `name` (faces.FaceWatcher)."""
        if not self.running or not self.tracker:
            return
        who = self.voice_at(t)
        what = self.tracker.set_face(name, t, who)
        if what == "named voice":
            self.conflict_evidence(name, t, 1, "lice")
        self.face_seen = (name, sim, time.time())
        self._log({"type": "face", "t": t, "name": name, "sim": round(sim, 2), "voice": who, "result": what})
        self.update_speaker_panel()

    def _ocr_frame(self):
        f, self.ocr_frame = self.ocr_frame, None
        if f is None or not self.player or self.paused:
            return None
        return f, self.player.stream_time

    def _log(self, obj):
        try:
            self.logf.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.logf.flush()
        except (AttributeError, ValueError):
            pass

    # ---- periodic ---------------------------------------------------------------------------------
    def tick_video(self):
        f, self.latest_frame = self.latest_frame, None
        if self.s.get("subtitles", True) and self.running and self.player:
            sub = self.subtitle_at(self.player.stream_time)
            if sub != self.video.subtitle:
                self.video.subtitle = sub
                if f is None:
                    self.video.update()
        if f is not None:
            self.video.set_frame(f)
        elif self.video.banner and time.time() > self.video.banner[2]:
            self.video.banner = None
            self.video.update()

    def subtitle_at(self, t):
        """(speaker with party, text) of the transcript line being heard at t, or None. The transcript runs a
        few seconds ahead of the sound (jitter buffer), so lines are ready when they are heard."""
        for g in reversed(getattr(self, "segments", [])[-40:]):
            if g["t0"] - 0.2 <= t <= g["t1"] + 0.8:
                who = self.tracker.final_name(g["who"])
                label = self.chair_label(g["t0"]) if who == CHAIR else who
                tag = self.party_tag(who)
                return (f"{label} [{tag}]" if tag else label), g["text"]
            if g["t1"] < t - 5:
                break
        return None

    def tick_ui(self):
        if not self.running or not self.player:
            return
        t = self.paused_at if self.paused else self.player.stream_time
        if not self.dragging:
            dur = self.src["duration"] or 0
            if dur and not self.src["is_live"]:
                self.timeline.blockSignals(True)
                self.timeline.setValue(int(t))
                self.timeline.blockSignals(False)
                self.time_lbl.setText(f"{fmt_time(t)} / {fmt_time(dur)}")
            else:
                self.time_lbl.setText(f"UŽIVO · {fmt_time(t)}")
        turn = self.tracker.turn
        if turn:
            el = self.tracker.elapsed(t)
            lim = turn.limit
            txt = fmt_time(el)[2:] if el < 3600 else fmt_time(el)
            if lim:
                txt += f" / {fmt_time(lim)[2:]}"
                over = el > lim
                self.sp_timer.setStyleSheet("color:%s" % ("#ff5c5c" if over else "#7fdc7f"))
                if over and el > lim + 10 and id(turn) not in self.turn_flagged:
                    self.turn_flagged.add(id(turn))
                    self.apply_violation(turn.name, "time_exceeded",
                                         f"Govori {fmt_time(el)[2:]}, a dozvoljeno je {fmt_time(lim)[2:]}.",
                                         "", 1.0, t)
            else:
                self.sp_timer.setStyleSheet("color:#e6e9ef")
            self.sp_timer.setText(txt)
            if turn is not self.last_turn_obj:
                self.last_turn_obj = turn
                self.ask_profile(turn.name)
                if self.judge:
                    prof = self.profiles.get(turn.name)
                    self.judge.speaker_profile = profiles_mod.short(prof, 700) if prof else ""
                    self.judge.others_profile = self.others_profile(turn.name)
                    self.judge.session_record = self.session_record()
                self.request_caption(t, force=True)
                if self.judge:
                    self.judge.speaker_changed()
        if (self.timer_zero_at is not None and turn and t - self.timer_zero_at > 10
                and self.tracker.last_voice[0] == turn.name and t - self.tracker.last_voice[1] < 4
                and ("official", id(turn)) not in self.turn_flagged):
            self.turn_flagged.add(("official", id(turn)))
            self.apply_violation(turn.name, "time_exceeded",
                                 "Zvanični tajmer je istekao, a govornik nastavlja da govori.", "", 1.0, t)
        if self.judge:
            fc = self.facter.cost if self.facter else 0.0
            n = self.facter.checked if self.facter else 0
            who = f"Lokalno ({self.judge.local.model})" if self.judge.local else "Claude"
            self.cost_lbl.setText(f"{who}: {self.judge.calls} poziva · {n} provera · ${self.judge.cost + fc:.3f}")
            if getattr(self.judge, "no_credit", False):
                self.cost_lbl.setText("⚠ Claude API: nema kredita na nalogu – sudija ne radi")
                self.cost_lbl.setStyleSheet("color:#ff5c5c;font-weight:600")
            else:
                self.cost_lbl.setStyleSheet("")
            self.judge.agenda = self.agenda.text().strip()
            if turn:
                self.judge.speaker_info = (f"{turn.name} ({turn.role or 'uloga nepoznata'}"
                                           + (f", {turn.party}" if turn.party else "")
                                           + (", žena" if turn.gender == "F" else ", muškarac" if turn.gender == "M"
                                              else "")
                                           + f"), govori od {fmt_time(turn.since)}"
                                           + (f", vremensko ograničenje {turn.limit} s" if turn.limit else "")
                                           + (f", zvanični tajmer pokazuje {fmt_time(self.official_timer)[2:]}"
                                              if self.official_timer is not None else ""))
        if self.asr:
            hint = [turn.name] if turn and not re.match(r"(Govornik|Glas) \d", turn.name) else []
            hint += [self.tracker.chair.person] if self.tracker.chair.person else []
            self.asr.hint_names = hint
        if self.asr and self.asr.last_end and t - self.asr.last_end > 25:
            self.set_status(f"{fmt_time(t)} · transkript kasni {t - self.asr.last_end:.0f} s (model se učitava "
                            "ili GPU ne stiže)")
        self.request_caption(t)
        if time.time() - getattr(self, "last_state_save", 0) > 60:
            self.last_state_save = time.time()
            session_state.save(self)
        if getattr(self, "groups_dirty", False) and int(time.time()) % 5 == 0:
            self.groups_dirty = False
            self.render(self.groups, report.groups_html(self.group_stats()))

    def _timeline_released(self):
        self.dragging = False
        self.seek(self.timeline.value())

    def request_caption(self, t, force=False):
        if not self.judge or self.judge.local or self.video.img is None or self.paused:
            return                          # the local model has no eyes: the local OCR reads captions
        if time.time() - self.last_local_caption < 90:
            return                          # the local OCR is reading the caption - no need to pay Claude
        gap = time.time() - self.last_caption_req
        if (force and gap > 12) or gap > self.s["caption_every_s"]:
            self.last_caption_req = time.time()
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            self.video.img.save(buf, "JPG", 80)
            self.judge.request_caption(bytes(ba), t)

    # ---- transcript ---------------------------------------------------------------------------------
    def on_segment(self, seg):
        if not self.asr or seg.gen != self.asr.gen:
            return                                  # from before a seek
        lab = self.tracker.label(seg)
        unsure = seg.unsure or []
        line = {"t": seg.t0, "who": lab.who, "text": mark_unsure(seg.latin, unsure, "{}(?)"),
                "loud_db": round(lab.loud_db, 1),
                "shout_db": self.s["shout_db"], "voice_change": lab.voice_change,
                "pitch_up": round(lab.pitch_up)}
        self._log({"type": "seg", "t0": seg.t0, "t1": seg.t1, "who": lab.who, "text": seg.text,
                   "db": round(seg.db, 1), "loud": round(lab.loud_db, 1), "sim": round(lab.sim, 2),
                   "f0": seg.f0 and round(seg.f0)})
        self.recent_lines = (self.recent_lines + [f"{lab.who}: {seg.latin}"])[-8:]
        loud_mark = lab.loud_db if lab.loud_db >= self.s["shout_db"] else 0
        self.segments.append({"emb": None if seg.emb is None else seg.emb.astype("float16"),
                              "t0": seg.t0, "t1": seg.t1, "who": lab.who, "text": seg.latin,
                              "loud": round(loud_mark), "chair": lab.is_chair})
        self.groups_dirty = True
        replay = self.already_judged(seg.t0)
        if self.judge and not replay:              # already judged before seeking back
            self.judge.add_line(line)
        self.cur_stretch[1] = max(self.cur_stretch[1], seg.t1)
        loud = lab.loud_db >= self.s["shout_db"] or (lab.loud_db >= self.s["shout_db"] - 3 and lab.pitch_up >= 25)
        txt = (html.escape(mark_unsure(seg.latin, unsure, "{}"))
               .replace("", "<span style='color:#999' title='nesigurno prepoznato - možda pogrešno čuto'>")
               .replace("", "</span>"))
        if loud:
            txt = (f"<b>{txt}</b> <span style='color:#e74c3c'>(GLASNO +{lab.loud_db:.0f} dB"
                   + (f", ton +{lab.pitch_up:.0f} %" if lab.pitch_up >= 15 else "") + ")</span>")
        self.tx_items.append({"t": seg.t0, "who": lab.who, "txt": txt, "replay": replay,
                              "voice_change": lab.voice_change})
        self.transcript.append(self.tx_html(self.tx_items[-1]))
        info = lab.chair_info
        if info.get("floor"):
            role, name, limit = info["floor"]
            self.chair.append(f"{self.tlink(seg.t0)} <b>daje reč:</b> {html.escape(name)}"
                              + (f" ({html.escape(role)})" if role else "")
                              + (f" – ograničenje {limit}" if limit else ""))
        if info.get("measure"):
            self.real_chair_measure(info["measure"], "", seg.latin, seg.t0)
        self.update_speaker_panel()
        self.sp_voice.setText(f"sada govori: {lab.who}" + (" (drugi glas)" if lab.voice_change else "")
                              + ("  ⚠ glas se po gramatici ne slaže s polom" if lab.gender_conflict else ""))
        self.loud.setValue(int(min(20, lab.loud_db)))

    def transcript_menu(self, pos):
        cur = self.transcript.cursorForPosition(pos)
        line = cur.block().text().strip()
        sel = self.transcript.textCursor().selectedText().strip()
        menu = QMenu(self)
        act_check = menu.addAction("Proveri tvrdnju na internetu" + (" (označeni tekst)" if sel else ""))
        act_false = menu.addAction("Označi kao netačno (moja ocena)")
        act_true = menu.addAction("Označi kao tačno (moja ocena)")
        menu.addSeparator()
        m_line = re.match(r"\[(\d+:\d\d:\d\d)\]\s+(.+?)(?:\s\[[^\]]*\])?:\s", line)
        act_who = act_who_one = None
        if m_line and self.running:
            act_who = menu.addAction("Ovo je govorio… (ispravi govornika od ovog reda nadalje)")
            act_who_one = menu.addAction("Ovo je govorio… (samo ovaj red)")
            menu.addSeparator()
        act_copy = menu.addAction("Kopiraj red")
        chosen = menu.exec(self.transcript.viewport().mapToGlobal(pos))
        if chosen is not None and chosen in (act_who, act_who_one):
            self.correct_speaker(parse_time(m_line.group(1)), m_line.group(2), only_line=chosen == act_who_one)
        elif chosen == act_copy:
            QApplication.clipboard().setText(sel or line)
        elif chosen == act_check:
            self.check_claim_manually(line, sel, cur.block())
        elif chosen in (act_false, act_true):
            self.check_claim_manually(line, sel, cur.block(),
                                      user_verdict="netačno" if chosen == act_false else "tačno")

    def correct_speaker(self, t, shown, only_line=False):
        """The user says the line at t (shown as `shown`) was said by someone else."""
        seg = next((g for g in self.segments if abs(g["t0"] - t) < 1.0), None)
        if not seg:
            return
        old = self.tracker.final_name(seg["who"])
        known = sorted({self.tracker.final_name(g["who"]) for g in self.segments} - {CHAIR, old})
        roster = sorted(mp["name"] for mp in names.ROSTER)
        items = [x for x in known if not x.startswith(("Glas ", "Govornik "))] + [CHAIR] + \
            [x for x in roster if x not in known]
        new, ok = QInputDialog.getItem(
            self, "Ispravi govornika",
            f"Red u {fmt_time(t)} je pripisan: {old}.\nKo je to stvarno govorio?"
            + ("" if only_line else f"\n\nIspravka važi za sve redove glasa „{old}“ od ovog trenutka nadalje "
                                    "(i unazad prerađuje mere, retoriku, tvrdnje i rang listu)."),
            items, 0, True)
        new = self.tracker.canonical(new.strip()) if ok and new.strip() else ""
        if not new or new == old:
            return
        if only_line:
            self.reassign_lines(old, new, lambda tt: abs(tt - seg["t0"]) < 0.5, why="ručno (jedan red)")
        else:
            self.reassign_lines(old, new, lambda tt: tt >= seg["t0"] - 0.1, why="ručno", move_voice=True)

    def reassign_lines(self, old, new, moved, why="", move_voice=False, split=None):
        """Lines of voice `old` for which moved(t) is true were said by `new`: transcript, measures, rhetoric,
        claims and turns are rewritten (retroactively) and every pane rebuilt."""
        fn = self.tracker.final_name

        def hit(who, t):
            return who is not None and fn(who) == old and moved(t)
        n = 0
        for g in self.segments:
            if hit(g["who"], g["t0"]):
                g["who"] = new
                n += 1
        for it in self.tx_items:
            if "who" in it and hit(it["who"], it["t"]):
                it["who"] = new
        for inc in self.incidents:
            if hit(inc["offender"], inc["t"]):
                inc["offender"] = new
        for lst in (self.rhetoric, self.tones, self.claims):
            for x in lst:
                if hit(x.get("speaker"), x["t"]):
                    x["speaker"] = new
        for tr in self.tracker.turns:
            if hit(tr.name, tr.since):
                tr.name = new
        if move_voice:
            self.tracker.move_voice(old, new)
        elif split:
            self.tracker.move_voice(old, new, *split)
        self._log({"type": "reassign", "old": old, "new": new, "lines": n, "why": why})
        self.relabel_all()
        self.set_status(f"Ispravljeno: {n} redova „{old}“ → {new} ({why}). Mere i statistika preračunate.")
        session_state.save(self)

    def conflict_evidence(self, name, t, weight, source):
        """Caption or face says `name` is talking while a voice NAMED someone else is talking. Enough evidence
        (caption + face, two captions, or a face for ~30 s) means that voice was given the wrong name: it is
        split (by the voiceprints of its lines) and the lines that sound like `name` are moved, retroactively."""
        who = self.voice_at(t)
        if not who:
            return
        who = self.tracker.final_name(who)
        if who in (CHAIR, name) or who.startswith(("Glas ", "Govornik ")) or self.same_person(who, name):
            return
        ev = getattr(self, "conflicts", {})
        self.conflicts = ev
        lst = [e for e in ev.get((who, name), []) if t - e[0] < 180] + [(t, weight, source)]
        ev[(who, name)] = lst
        w = sum(e[1] for e in lst)
        srcs = {e[2] for e in lst}
        if (w >= 3 and len(srcs) >= 2) or (w >= 4 and "natpis" in srcs) or w >= 6:
            ev.pop((who, name), None)
            self.split_voice(who, name, [e[0] for e in lst])

    def split_voice(self, old, new, times):
        """Voice `old` mixes two people; around `times` it is `new`. Two-way clustering of the voiceprints of all
        its lines decides, line by line, who said what (all of it, back to the start of the session)."""
        import numpy as np
        segs = [g for g in self.segments if self.tracker.final_name(g["who"]) == old]
        with_emb = [g for g in segs if g.get("emb") is not None]
        near = [g for g in with_emb if any(abs(g["t0"] - x) < 20 for x in times)]
        split = None
        if len(with_emb) >= 6 and near:
            E = np.array([g["emb"] for g in with_emb], dtype=np.float32)
            E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
            c_new = np.mean([g["emb"] for g in near], axis=0).astype(np.float32)
            c_new /= np.linalg.norm(c_new) + 1e-9
            c_old = E[int(np.argmin(E @ c_new))].copy()
            for _ in range(10):                          # 2-means, seeded by the evidence and its opposite
                lab = (E @ c_new) >= (E @ c_old)
                if lab.all() or not lab.any():
                    break
                c_new, c_old = E[lab].mean(0), E[~lab].mean(0)
                c_new /= np.linalg.norm(c_new) + 1e-9
                c_old /= np.linalg.norm(c_old) + 1e-9
            lab = (E @ c_new) >= (E @ c_old)
            if lab.all() or not lab.any() or float(c_new @ c_old) >= self.tracker.thr or (~lab).sum() < 3:
                moved_t = None                           # one voice after all: the whole voice is `new`
            else:
                moved_t = sorted(g["t0"] for g, k in zip(with_emb, lab) if k)
                kept_t = sorted(g["t0"] for g, k in zip(with_emb, lab) if not k)
                split = (c_old, int((~lab).sum()), c_new, int(lab.sum()))
        else:
            moved_t = None
        if moved_t is None:
            self.reassign_lines(old, new, lambda tt: True, why="natpis/lice: ceo glas", move_voice=True)
            return
        import bisect

        def moved(tt):
            # a line (or measure, technique...) at tt belongs to the nearest line with a voiceprint
            i, j = bisect.bisect_right(moved_t, tt + 0.5), bisect.bisect_right(kept_t, tt + 0.5)
            a = moved_t[i - 1] if i else -1e9
            b = kept_t[j - 1] if j else -1e9
            return a >= b
        self.reassign_lines(old, new, moved, why="natpis/lice: razdvojen pomešan glas", split=split)

    def check_claim_manually(self, line, selected, block, user_verdict=None):
        m = re.match(r"\[(\d+:\d\d:\d\d)\]\s+(.+?):\s(.*)", line)
        if not m and not selected:
            return
        t = parse_time(m.group(1)) if m else (self.player.stream_time if self.player else 0.0)
        speaker = self.tracker.final_name(m.group(2)) if m else "nepoznat"
        claim = selected or (m.group(3) if m else line)
        claim = re.sub(r"\(GLASNO.*?\)|\(već ocenjeno\)", "", claim).strip()
        ctx, b = [], block.previous()
        for _ in range(4):                       # the lines before it help to understand a garbled sentence
            if not b.isValid():
                break
            ctx.insert(0, b.text())
            b = b.previous()
        nxt = block.next()
        if nxt.isValid():
            ctx.append(nxt.text())
        item = {"id": len(self.claims), "t": t, "speaker": speaker, "claim": claim,
                "why": "provera na zahtev korisnika", "context": " / ".join(ctx)[-700:], "manual": True,
                "status": "queued"}
        self.claims.append(item)
        if user_verdict:
            item["status"] = "done"
            item["result"] = {"verdict": user_verdict, "explanation": "Ocena korisnika.", "sources": [],
                              "searches": 0, "user": True}
            self._log({"type": "fact_result", "t": t, "claim": claim, **item["result"]})
        elif self.facter:
            self.facter.submit(item)
            self.set_status("Šaljem tvrdnju na proveru na internetu…")
        self.render_facts()
        self.tabs.setCurrentWidget(self.facts)

    def update_speaker_panel(self):
        turn = self.tracker.turn
        if not turn:
            return
        sym = {"F": "♀ ", "M": "♂ "}.get(turn.gender, "")
        self.sp_name.setText(sym + turn.name + ("  ✓" if turn.confirmed else ""))
        bits = [b for b in (turn.role, turn.party) if b]
        src = {"chair": "najavio predsedavajući", "caption": "ime sa ekrana (potvrđeno)", "voice": "po glasu",
               "face": "prepoznat po licu"}
        extra = []
        fs = self.face_seen
        if fs and time.time() - fs[2] < 8:
            extra.append(f"na ekranu govori: {fs[0]}" + (" ✓" if self.same_person(fs[0], turn.name) else ""))
        self.sp_role.setText(" · ".join(bits + [src.get(turn.source, "")] + extra))

    def on_caption(self, res, t):
        """Claude vision read the caption (fallback when the local OCR finds nothing)."""
        if res.pop("_src", None) is not self.judge:
            return                                  # late answer from a stopped session
        self.tracker.set_caption(res.get("name", ""), res.get("party", ""), res.get("role", ""), t)
        self._log({"type": "caption", "src": "claude", "t": t, **res})
        self.update_speaker_panel()

    def on_caption_local(self, mp, group, score, t):
        self.last_local_caption = time.time()
        self.conflict_evidence(mp["name"], t, 2, "natpis")
        self.tracker.set_caption(mp["name"], group, "", t, mp["gender"])
        self._log({"type": "caption", "src": "ocr", "t": t, "name": mp["name"], "group": group,
                   "score": round(score, 2)})
        self.update_speaker_panel()

    def on_timer(self, secs, t):
        self.official_timer = secs
        if secs is None:
            self.sp_official.setText("")
            self.timer_zero_at = None
            return
        self.sp_official.setText(f"⏱ {secs // 60}:{secs % 60:02d}")
        self.sp_official.setStyleSheet("color:%s" % ("#ff5c5c" if secs <= 10 else "#f5d76e"))
        if secs == 0:
            if self.timer_zero_at is None:
                self.timer_zero_at = t
        else:
            self.timer_zero_at = None

    # ---- verdicts ---------------------------------------------------------------------------------
    def on_result(self, res):
        if res.pop("_src", None) is not self.judge:
            return                                  # late answer from a stopped session (a slow local model)
        res = strip_unsure_marks(res)               # "genije(?)" -> "genije" in quotes, claims, rhetoric
        t = res["_t"]
        for m in res.get("speaker_names", []):
            lab, name = m["label"], m["name"].strip()
            if re.match(r"(?i)(nepoznat|unknown|glas\b|govornik\b|predsedavaj)", name) or len(name) < 4:
                continue                            # not a real name
            if len(name.split()) < 2 and not names.roster_lookup(name):
                continue                            # a bare first name ("Aleksandar") identifies nobody
            if name and lab in self.tracker.voices and lab != CHAIR and self.tracker.voices[lab].anon:
                self.tracker._rename(lab, self.tracker.canonical(name))
        cp = (res.get("chair_person") or "").strip()
        if cp and not re.match(r"(?i)(nepoznat|unknown|predsedavaj)", cp):
            self.tracker.chair.from_judge(cp, t)
        for v in res.get("violations", []):
            if v["offender"].strip() in ("", CHAIR):
                continue
            note = (v.get("legal_note") or "").strip()
            expl = v["explanation"] + (f" ⚖ {note}" if note else "")
            self.apply_violation(v["offender"].strip(), v["kind"], expl, v["quote"],
                                 float(v.get("confidence", 0)), t)
        for f in res.get("fact_checks", []):
            item = {"id": len(self.claims), "t": t, "speaker": self.person(f["speaker"]),
                    "claim": f["claim"], "why": f["why"], "status": "queued",
                    "context": " / ".join(self.recent_lines)[-600:]}
            if self.facter and self.facter.submit(item):
                self.claims.append(item)
            elif not self.facter:
                self.claims.append(item)
            self._log({"type": "fact", "t": t, **f})
        if res.get("fact_checks"):
            self.render_facts()
        for x in res.get("rhetoric", []):
            if x["speaker"] != CHAIR:
                self.rhetoric.append({"t": t, **x})
        for x in res.get("tone", []):
            if x["speaker"] != CHAIR:
                self.tones.append({"t": t, **x})
        for x in res.get("rhetoric", []):
            if x["technique"] in CONTEXT_TRIGGERS and x["speaker"] != CHAIR:
                self.queue_context_check(x, t)
        if res.get("rhetoric") or res.get("tone"):
            self._log({"type": "rhetoric", "t": t, "tone": res.get("tone"), "rhetoric": res.get("rhetoric")})
            self.render_rhetoric()
        for a in res.get("chair_actions", []):
            act = a["action"]
            if act == "dao_rec":
                continue
            mk = {"oduzimanje_reci": "oduzimanje"}.get(act, act)
            self.real_chair_measure(mk, a.get("target", ""), a.get("quote", ""), t)
        ag = (res.get("agenda_item") or "").strip()
        items = [self.agenda_box.itemData(i) for i in range(self.agenda_box.count())]
        if ag and ag in items:
            # the debate moved to another item: switch after two answers in a row agree (no flip-flopping)
            self.agenda_hits = (getattr(self, "agenda_hits", []) + [ag])[-2:]
            if ag != self.agenda.text().strip() and (not self.agenda.text().strip() or self.agenda_hits == [ag, ag]):
                self.agenda.setText(ag)
                self.agenda_box.setCurrentIndex(items.index(ag))
                self._log({"type": "agenda_item", "t": t, "item": ag})
        elif ag and not self.agenda.text().strip() and not items:
            self.agenda.setText(ag)

    def on_agenda(self, res):
        """The sitting's agenda, found on the internet: a pick list next to the Tačka field + context for the judge."""
        if not res or not self.running:
            self.set_status("Dnevni red nije pronađen na internetu - upiši tačku ili će je sudija prepoznati.")
            return
        items = sorted(res.get("items", []), key=lambda x: x.get("no", 0))
        self.agenda_box.clear()
        for it in items:
            self.agenda_box.addItem(f"{it['no']}. {it['title']}", it["title"])
        src = "\n".join(f"{s['title']} – {s['url']}" for s in res.get("sources", [])[:4])
        self.agenda_box.setToolTip(f"Dnevni red: {res.get('session', '')}\nIzvori:\n{src}\n\n"
                                   "Izaberi tačku koja se sada raspravlja.")
        self.agenda_box.setVisible(True)
        if self.judge:
            self.judge.agenda_list = "\n".join(f"{it['no']}. {it['title']}" for it in items)
        self._log({"type": "agenda", "session": res.get("session", ""), "items": items,
                   "sources": res.get("sources", [])})
        self.set_status(f"Dnevni red pronađen: {len(items)} tačaka ({res.get('session', '')}).")

    def pick_agenda(self, i):
        title = self.agenda_box.itemData(i)
        if title:
            self.agenda.setText(title)

    def person(self, name):
        """Final identity of a label or spelling: 'Govornik 3' -> 'Jovo Bakić' after he is recognised."""
        name = self.tracker.final_name((name or "").strip())
        if name.startswith(("Glas ", "Govornik ")) or name == CHAIR:
            return name
        return self.tracker.final_name(self.tracker.canonical(name))

    def apply_violation(self, offender, kind, explanation, quote, conf, t):
        if self.already_judged(t):
            return                                  # replaying a part that was already judged
        inc = {"t": t, "offender": self.person(offender), "kind": kind, "explanation": explanation,
               "quote": quote, "conf": conf, "id": len(self.incidents)}
        self.incidents.append(inc)
        fresh = self.rebuild_measures(new=inc)
        if not fresh:
            return
        m, title, label, article, fine = fresh
        offender = inc["offender"]
        self.video.show_banner([f"{title} – {offender}", f"{label} ({article})",
                                (f"„{quote[:110]}“" if quote else explanation[:120])],
                               MEASURE_COLORS[m])
        if self.s.get("chime"):
            import winsound
            threading.Thread(target=winsound.MessageBeep, args=(winsound.MB_ICONEXCLAMATION,), daemon=True).start()
        self._log({"type": "measure", "t": t, "offender": offender, "kind": kind, "measure": m,
                   "article": article, "quote": quote, "explanation": explanation, "confidence": conf,
                   "fine_pct": fine})

    def rebuild_measures(self, new=None):
        """Cards, the measure ladder and statistics, replayed from all incidents in time order. Re-run when a
        speaker is identified, so what 'Govornik 3' / 'Glas 2' did counts for the real MP (his warnings,
        opomene and fines merge in the right order). Returns the measure of incident `new`, if any."""
        self.discipline = Discipline()
        self.app_measures, self.last_measure, cards, fresh = [], {}, [], None
        self.counted = []               # breaches that count for the ranking (judged sure enough, not withdrawn)
        min_conf = self.s["min_confidence"]
        for inc in sorted(self.incidents, key=lambda x: x["t"]):
            inc["offender"] = off = self.person(inc["offender"])
            t, kind, quote, expl, conf = inc["t"], inc["kind"], inc["quote"], inc["explanation"], inc["conf"]
            label, article, _ = KINDS.get(kind, KINDS["disorder"])
            if not off or off == CHAIR:
                continue
            if inc.get("dismissed"):
                # the user heard it differently (e.g. a misheard word): no measure, ladder not advanced
                cards.append(f"<div style='margin:4px 0 4px 12px;color:#999'>✕ {self.tlink(t)} poništeno: "
                             f"{html.escape(off)} – {html.escape(label)} – „{html.escape(quote)}“ "
                             f"<a href='undis:{inc['id']}'>vrati</a></div>")
                continue
            if kind in ("heckling", "no_floor") and off == self.floor_holder(t):
                # judged as a voice from the hall, later identified as the speaker himself: nobody heckles
                # or speaks without the floor in his own turn
                continue
            if conf >= min_conf:
                self.counted.append(inc)
            if off.startswith("Glas "):
                # an unidentified voice from the hall: the chair can't sanction someone unknown either
                if conf >= min_conf:
                    cards.append(self.card_html("sumnja", "IZ SALE (nepoznat)", off, label, article, quote, expl,
                                                t, 0, inc.get("id")))
                continue
            if conf < min_conf:
                if conf >= 0.5:
                    cards.append(self.card_html("sumnja", "SUMNJA (bez mere)", off, label, article, quote, expl,
                                                t, 0, inc.get("id")))
                continue
            last = self.last_measure.get(off.lower())
            if last is not None and t - last < COOLDOWN_S and kind != "physical_attack":
                # same burst: the chair pronounces one measure, not one per sentence
                cards.append(f"<div style='margin-left:24px;color:#999'>↳ {self.tlink(t)} i dalje: "
                             f"{html.escape(label)} – „{html.escape(quote)}“</div>")
                continue
            m, label, article, fine = self.discipline.decide(off, kind)
            self.last_measure[off.lower()] = t
            title = MEASURES[m][0]
            cards.append(self.card_html(m, title, off, label, article, quote, expl, t, fine, inc.get("id")))
            self.app_measures.append({"t": t, "offender": off, "measure": m, "kind": kind, "label": label,
                                      "quote": quote})
            if inc is new:
                fresh = (m, title, label, article, fine)
        self.render(self.cards, "".join(cards))
        self.refresh_stats()
        self.refresh_table()
        return fresh

    def floor_holder(self, t):
        """Final name of who had the floor at moment t (the last turn that started before t)."""
        tr = None
        trk = getattr(self, "tracker", None)
        for x in (trk.turns if trk else []):
            if x.since <= t + 1:
                tr = x
        return self.person(tr.name) if tr else None

    @staticmethod
    def article_links(article):
        """'čl. 107 st. 2, čl. 109 al. 5' -> each article a link that opens it in the Poslovnik panel."""
        return re.sub(r"čl\. (\d+)", lambda m: f"<a href='posl:{m.group(1)}'>čl. {m.group(1)}</a>",
                      html.escape(article))

    def card_html(self, m, title, offender, label, article, quote, explanation, t, fine, inc_id=None):
        col = MEASURE_COLORS[m]
        undo = (f" &nbsp; <a href='dis:{inc_id}' style='color:#999;font-size:9pt'>✕ nije prekršaj</a>"
                if inc_id is not None else "")
        fine_txt = f" · kazna {fine} % osnovne plate" if fine else ""
        q = f"<br><i>„{html.escape(quote)}“</i>" if quote else ""
        return (f"<table width='100%' cellspacing='0' cellpadding='6' style='margin:4px 0'><tr>"
                f"<td width='6' bgcolor='{col}'></td><td>"
                f"<b style='color:{col};font-size:13pt'>{html.escape(title)}</b> "
                f"<b>{html.escape(offender)}</b> {self.tlink(t)}{undo}<br>"
                f"{html.escape(label)} – <span style='color:#666'>{self.article_links(article)}</span>{fine_txt}{q}"
                f"<br><span style='color:#444'>{html.escape(explanation)}</span></td></tr></table>")

    # ---- speaker identified later: move everything to the real name -----------------------------------
    def tx_html(self, it):
        if "seek" in it:
            return f"<p style='color:#888'><i>── skok na {fmt_time(it['seek'])} ──</i></p>"
        if "resume" in it:
            return (f"<p style='color:#888'><i>── aplikacija ponovo otvorena – nastavak sesije od "
                    f"{fmt_time(it['resume'])} ──</i></p>")
        who = self.tracker.final_name(it["who"])
        color = "#e67e22" if who == CHAIR else ("#5dade2" if not it["voice_change"] else "#bb8fce")
        shown = self.chair_label(it["t"]) if who == CHAIR else who
        tag = self.party_tag(who)
        tag = f" <span style='color:#999;font-weight:normal'>[{html.escape(tag)}]</span>" if tag else ""
        return (f"{self.tlink(it['t'])} <b style='color:{color}'>{html.escape(shown)}</b>{tag}<b "
                f"style='color:{color}'>:</b> {it['txt']}"
                + (" <span style='color:#aaa'>(već ocenjeno)</span>" if it["replay"] else ""))

    def chair_label(self, t):
        p = self.tracker.chair.at(t)
        return f"{CHAIR} ({p})" if p else CHAIR

    def on_chair_change(self, old, new, t, source):
        """The person chairing became known or changed (context, caption, voice, Claude)."""
        ch = self.tracker.chair
        self._log({"type": "chair_person", "t": t, "old": old, "new": new, "source": source})
        if new:
            txt = f"Predsedava: {new} – {ch.role(new)}"
            self.set_status(f"{txt} (prepoznato: {source}).")
        else:
            txt = "Predsedava: promena – čekam ko je preuzeo" if old else "Predsedava: nije još prepoznat"
            self.set_status(f"Predsedavajući se promenio ({source}): {old or '?'} više ne vodi sednicu.")
        self.sp_chair.setText(txt)
        self.chair.append(f"<p style='color:#2c3e50'><b>{self.tlink(t)} "
                          f"{html.escape(txt)}</b> <span style='color:#888'>({html.escape(source)})</span></p>")
        if self.judge:
            self.judge.chair_info = ch.label() if new else "promenio se, nije još prepoznat"
        if not self.relabel_pending:            # earlier chair lines of this stretch now get the name
            self.relabel_pending = True
            QTimer.singleShot(0, self.relabel_all)

    def on_speaker_renamed(self, old, new):
        self._log({"type": "rename", "old": old, "new": new})
        self.set_status(f"Prepoznat govornik: {old} → {new} – njegov raniji govor, mere i statistika prebačeni.")
        if not self.relabel_pending:                # the tracker is mid-update; do it once, right after
            self.relabel_pending = True
            QTimer.singleShot(0, self.relabel_all)

    def relabel_all(self):
        self.relabel_pending = False
        for lst in (self.rhetoric, self.tones):
            for x in lst:
                x["speaker"] = self.person(x["speaker"])
            lst[:] = [x for x in lst if x["speaker"] != CHAIR]
        for c in self.claims:
            c["speaker"] = self.person(c.get("speaker", ""))
        self.last_context = {self.person(k): v for k, v in self.last_context.items()}
        for sg in self.segments:
            sg["who"] = self.tracker.final_name(sg["who"])
            sg["chair"] = sg["who"] == CHAIR
        self.rebuild_measures()
        self.refresh_stats()
        self.render(self.transcript, "".join(f"<p style='margin:0'>{self.tx_html(it)}</p>" for it in self.tx_items))
        self.render_rhetoric()
        self.render_facts()
        self.groups_dirty = True
        if self.tracker.turn:
            self.ask_profile(self.tracker.turn.name)

    # ---- statistics / report ------------------------------------------------------------------------
    @staticmethod
    def render(browser, html_text):
        sb = browser.verticalScrollBar()
        v = sb.value()
        browser.setHtml(html_text)
        sb.setValue(v)

    def same_person(self, a, b):
        ca, cb = self.person(a), self.person(b)
        if ca == cb:
            return True
        sa, sb = ca.split()[-1:], cb.split()[-1:]
        return bool(sa and sb and sa[0].lower() == sb[0].lower())

    def refresh_stats(self):
        self.render(self.stats, report.stats_html(self.real_measures, self.app_measures, self.same_person,
                                                  self.tlink, self.person, self.tracker.chair.at))

    def render_rhetoric(self):
        self.render(self.rhet, report.rhetoric_html(self.rhetoric, self.tones, self.tlink, self.person)
                    + report.context_html(self.claims, self.tlink))
        self.refresh_table()

    # ---- profiles and wider context ------------------------------------------------------------------
    def ask_profile(self, name):
        if not self.profiler or name in self.profiles:
            return
        mp = names.roster_lookup(name)
        if not mp:
            return                              # only MPs we can identify (no "Govornik 3")
        self.profiles[mp["name"]] = profiles_mod.load(mp["name"])
        hint = f"Narodni poslanik, {mp['party']}, {mp['city']}, rođen {mp['year']}, poslanička grupa " \
               f"{to_latin(mp.get('group', ''))}."
        self.profiler.request(mp["name"], hint)
        self.render(self.profs, report.profiles_html(self.profiles))

    def on_profile(self, name, prof):
        self.profiles[name] = prof
        self._log({"type": "profile", "name": name})
        self.render(self.profs, report.profiles_html(self.profiles))
        if self.judge and self.tracker.turn and self.tracker.turn.name == name:
            self.judge.speaker_profile = profiles_mod.short(prof, 700)
        if self.judge and self.tracker.turn:
            self.judge.others_profile = self.others_profile(self.tracker.turn.name)

    def others_profile(self, current, n=5):
        """Short profiles of the other MPs who spoke recently in this debate (newest first), so the judge can
        see allusions to them - e.g. imitating an MP's publicised slip."""
        seen, out = {current}, []
        for tr in reversed(self.tracker.turns):
            nm = self.person(tr.name)
            if nm in seen or not names.roster_lookup(nm):
                continue
            seen.add(nm)
            if self.profiles.get(nm):
                out.append(profiles_mod.brief(self.profiles[nm]))
            if len(out) >= n:
                break
        return "\n".join(out)

    def session_record(self):
        """What happened in this session so far, per group - evidence for 'who brought bad blood' claims."""
        import collections
        lines = []
        for g, r in sorted(self.group_stats().items(), key=lambda kv: -kv[1]["secs"]):
            if g == "Nepoznato":
                continue
            kinds = collections.Counter(a.get("kind", "") for a in self.app_measures
                                        if self.group_of(a["offender"]) == g)
            false = r["verdicts"].get("netačno", 0) + r["verdicts"].get("delimično tačno", 0)
            lines.append(f"{g}: govorili {r['secs'] / 60:.0f} min; mere aplikacije {len(r['app'])} "
                         f"({', '.join(f'{k} {v}' for k, v in kinds.most_common(4)) or '-'}); mere predsedavajućeg "
                         f"{len(r['real'])}; retoričke tehnike {sum(r['tech'].values())} "
                         f"({', '.join(f'{k} {v}' for k, v in r['tech'].most_common(3)) or '-'}); ton "
                         f"{', '.join(f'{k} {v}' for k, v in r['tone'].most_common(2)) or '-'}; netačne/delimično "
                         f"tačne tvrdnje {false}")
        return "\n".join(lines)

    def own_side_profiles(self, group, exclude):
        out = []
        for name, p in self.profiles.items():
            if p and name != exclude and self.group_of(name) == group:
                out.append(profiles_mod.short(p, 350))
        return "\n".join(out[:6])

    def queue_context_check(self, x, t):
        if not self.facter or not self.s.get("context_checks", True):
            return
        speaker = self.person(x["speaker"])
        if t - self.last_context.get(speaker, -1e9) < 180:
            return                              # at most one context check per speaker per 3 minutes
        self.last_context[speaker] = t
        group = self.group_of(speaker) or ""
        prof = self.profiles.get(speaker)
        record = self.session_record()
        own = self.own_side_profiles(group, speaker)
        if own:
            record += "\nProfili drugih poslanika iz grupe govornika (javni zapis):\n" + own
        item = {"id": len(self.claims), "kind": "context", "t": t, "speaker": speaker, "group": group,
                "claim": x["quote"], "why": TECHNIQUES.get(x["technique"], x["technique"]),
                "profile": profiles_mod.short(prof, 900) if prof else "", "record": record,
                "context": " / ".join(self.recent_lines)[-600:], "status": "queued"}
        if self.facter.submit(item):
            self.claims.append(item)
            self.render_rhetoric()

    def render_facts(self):
        for c in self.claims:
            if not self.facter and c["status"] == "queued":
                c["status"], c["why"] = "nokey", "provera na internetu radi uz Claude API ključ"
        if self.facter and not any(c["status"] == "checking" for c in self.claims):
            nxt = next((c for c in self.claims if c["status"] == "queued"), None)
            if nxt:
                nxt["status"] = "checking"
        self.render(self.facts, report.facts_html(self.claims, self.tlink))
        self.refresh_table()

    def on_fact(self, cid, res):
        if res.pop("_src", None) is not self.facter:
            return                                  # late answer from a stopped session
        for c in self.claims:
            if c["id"] == cid:
                c["result"], c["status"] = res, "done"
                self._log({"type": "context_result" if c.get("kind") == "context" else "fact_result",
                           "t": c["t"], "claim": c["claim"], **res})
        self.render_facts()
        self.render_rhetoric()

    def _transcript_meta(self):
        return {"title": self.src["title"], "url": self.s["last_url"],
                "made": datetime.datetime.now().strftime("%d.%m.%Y %H:%M")}

    def save_transcript(self, path):
        segs = [dict(sg, who=self.chair_label(sg["t0"])) if self.tracker.final_name(sg["who"]) == CHAIR else sg
                for sg in getattr(self, "segments", [])]
        name = self.tracker.final_name
        if path.lower().endswith(".srt"):
            text = report.transcript_srt(segs, name)
        elif path.lower().endswith((".html", ".htm")):
            text = report.transcript_html(self._transcript_meta(), segs, name)
        else:
            text = report.transcript_txt(self._transcript_meta(), segs, name)
        with open(path, "w", encoding="utf-8-sig") as f:      # BOM: Notepad/Word show č ć š ž đ right
            f.write(text)
        return path

    def save_transcript_dialog(self):
        if not getattr(self, "segments", None):
            QMessageBox.information(self, "Transkript", "Još nema transkripta - pokreni sednicu.")
            return
        base = getattr(self, "log_path", os.path.join(SESSIONS, "sednica.jsonl"))[:-6] + "_transkript"
        path, flt = QFileDialog.getSaveFileName(
            self, "Sačuvaj transkript", base + ".txt",
            "Tekst (*.txt);;Titlovi za video (*.srt);;Stranica / Word (*.html)")
        if not path:
            return
        ext = {"Titlovi": ".srt", "Stranica": ".html"}
        for k, e in ext.items():
            if flt.startswith(k) and not path.lower().endswith(e):
                path = os.path.splitext(path)[0] + e
        try:
            self.save_transcript(path)
        except OSError as e:
            QMessageBox.warning(self, "Transkript", f"Ne mogu da sačuvam: {e}")
            return
        self.set_status(f"Transkript sačuvan: {path} ({len(self.segments)} redova)")

    GROUP_SHORT = {"studentska lista": "SL", "aleksandar vučić - srbija pobeđuje": "SNS-SP",
                   "nisu članovi poslaničkih grupa": "nezavisni"}

    def party_tag(self, name):
        """Short party / group of a speaker for the transcript: the party (SNS, SVM...) if the roster has
        one, else the parliamentary group (SL = Studentska lista...). '' for unknown voices."""
        if not name or name == CHAIR or name.startswith(("Glas ", "Govornik ")):
            return ""
        mp = names.roster_lookup(name)
        if mp and mp.get("party") and mp["party"] not in ("-", ""):
            return mp["party"]
        g = self.group_of(name)
        if not g or g == "Nepoznato":
            return ""
        return self.GROUP_SHORT.get(g.lower(), "".join(w[0] for w in g.split() if len(w) > 2).upper()[:6])

    def group_of(self, name):
        """Parliamentary group of a speaker: roster first, then what the on-screen caption said."""
        name = self.tracker.final_name(name)
        if name == CHAIR:
            return None
        mp = names.roster_lookup(name)
        if mp and mp.get("group"):
            return to_latin(mp["group"])
        for tr in reversed(self.tracker.turns):
            if tr.name == name and tr.party:
                return to_latin(tr.party)
        return "Nepoznato"

    def group_stats(self):
        import collections
        rows = {}

        def row(g):
            return rows.setdefault(g, {"secs": 0.0, "app": [], "real": [], "fined": set(), "speakers": set(),
                                       "tech": collections.Counter(), "tone": collections.Counter(),
                                       "verdicts": collections.Counter()})
        for g in getattr(self, "segments", []):
            grp = self.group_of(g["who"])
            if grp and not g["who"].startswith("Glas "):
                r = row(grp)
                r["secs"] += max(0.0, g["t1"] - g["t0"])
                r["speakers"].add(self.tracker.final_name(g["who"]))
        for a in self.app_measures:
            grp = self.group_of(a["offender"])
            if grp:
                row(grp)["app"].append(a["measure"])
                if a["measure"] != "upozorenje":
                    row(grp)["fined"].add(self.tracker.final_name(a["offender"]))
        for r_ in self.real_measures:
            if r_["target"]:
                grp = self.group_of(r_["target"])
                if grp:
                    row(grp)["real"].append(r_["measure"])
        for x in self.rhetoric:
            grp = self.group_of(x["speaker"])
            if grp:
                row(grp)["tech"][x["technique"]] += 1
        for x in self.tones:
            grp = self.group_of(x["speaker"])
            if grp:
                row(grp)["tone"][x["tone"]] += 1
        for c in self.claims:
            if c.get("result"):
                grp = self.group_of(c["speaker"])
                if grp:
                    row(grp)["verdicts"][c["result"]["verdict"]] += 1
        return rows

    def save_report(self, open_it=False):
        if not getattr(self, "log_path", None):
            return
        path = self.log_path[:-6] + "_izvestaj.html"
        tl = lambda t: f"[{fmt_time(t)}]"   # noqa: E731 - plain times in the file
        covered = ", ".join(f"{fmt_time(a)}–{fmt_time(b)}" for a, b in self.covered if b > a)
        fc = self.facter.cost if self.facter else 0.0
        jc = self.judge.cost if self.judge else 0.0
        doc = report.full_report(
            {"title": self.src["title"], "url": self.s["last_url"], "covered": covered or "–"},
            self.cards.toHtml(),
            report.stats_html(self.real_measures, self.app_measures, self.same_person, tl, self.person,
                              self.tracker.chair.at),
            report.rhetoric_html(self.rhetoric, self.tones, tl, self.person)
            + report.context_html(self.claims, tl),
            report.ranking_html(self.ranking())
            + report.facts_html(self.claims, tl) + report.groups_html(self.group_stats())
            + report.profiles_html({k: v for k, v in self.profiles.items() if v}),
            f"Claude trošak ${jc + fc:.3f}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        self.report_path = path
        if open_it:
            os.startfile(path)

    def real_chair_measure(self, measure, target, quote, t):
        fam = report.FAMILY.get(measure)
        target = self.tracker.canonical(target) if target and target != CHAIR else ""
        for r in self.real_measures:            # the same event can come from the regex and from Claude
            if report.FAMILY.get(r["measure"]) == fam and abs(r["t"] - t) < 30:
                if target and not r["target"]:
                    r["target"] = target
                    self.refresh_stats()
                return
        self.real_measures.append({"t": t, "target": target, "measure": measure, "quote": quote})
        self.refresh_stats()
        match = any(abs(t - a["t"]) < 90 and report.FAMILY.get(a["measure"]) == fam for a in self.app_measures)
        mark = "<span style='color:#27ae60'>✓ i aplikacija</span>" if match else \
            "<span style='color:#c0392b'>aplikacija nije izrekla</span>"
        self.chair.append(f"{self.tlink(t)} <b>{html.escape(measure.upper())}</b> "
                          f"{html.escape(target)} – „{html.escape(quote[:140])}“ {mark}")
        self._log({"type": "real_chair", "t": t, "measure": measure, "target": target, "quote": quote})

    def ranking(self):
        """One row per person: breaches, measures, manipulation, false claims, tone (report.RANK_COLS)."""
        rows = {}

        def row(name):
            name = self.person(name)
            if not name or name == CHAIR:
                return None
            if name.startswith("Glas "):
                name = HALL                      # unidentified voices from the hall: one row, not one per voice
            return rows.setdefault(name, {"name": name, **{c[0]: 0 for c in report.RANK_COLS[2:]},
                                          "_tech": collections.Counter(), "_kinds": collections.Counter(),
                                          "_items": collections.defaultdict(list)})
        for inc in getattr(self, "counted", []):
            r = row(inc["offender"])
            if r:
                r["breaches"] += 1
                r["_kinds"][inc["kind"]] += 1
                item = (inc["t"], inc["quote"], KINDS.get(inc["kind"], (inc["kind"],))[0], inc["explanation"])
                r["_items"]["breaches"].append(item)
                if inc["kind"] in ("insult", "profanity", "private_life"):
                    r["insults"] += 1
                    r["_items"]["insults"].append(item)
                if inc["kind"] in ("heckling", "no_floor"):
                    r["heckling"] += 1
                    r["_items"]["heckling"].append(item)
        for a in self.app_measures:
            r = row(a["offender"])
            if r:
                key = {"upozorenje": "warnings", "opomena": "opomene", "opomena2": "opomene",
                       "oduzimanje": "oduzimanje", "udaljenje": "udaljenje"}.get(a["measure"])
                item = (a["t"], a["quote"], f"{MEASURES[a['measure']][0]} – {a['label']}", "")
                if key:
                    r["_items"][key].append(item)
                if a["measure"] != "upozorenje":
                    r["_items"]["fine"].append(item)
        for rec in self.discipline.mps.values():
            r = row(rec.name)
            if r:
                r["warnings"] = sum(rec.warnings.values())
                r["opomene"] = rec.opomene
                r["oduzimanje"] = int(rec.oduzimanje)
                r["udaljenje"] = int(rec.udaljenje)
                r["fine"] = rec.fine_pct
        for x in self.rhetoric:
            r = row(x["speaker"])
            if r:
                r["manipulation"] += 1
                r["_tech"][x["technique"]] += 1
                r["_items"]["manipulation"].append((x["t"], x.get("quote", ""), TECHNIQUES.get(
                    x["technique"], x["technique"]), x.get("explanation", "")))
        for c in self.claims:
            res = c.get("result") or {}
            if res.get("verdict") in report.FALSE_VERDICTS:
                r = row(c.get("speaker", ""))
                if r:
                    r["false"] += 1
                    r["_items"]["false"].append((c["t"], c["claim"], res.get("verdict", "").upper(),
                                                 res.get("explanation", "")))
        for x in self.tones:
            if x["tone"] in ("uvredljiv", "agresivan"):
                r = row(x["speaker"])
                if r:
                    r["aggressive"] += 1
                    # the tone is judged per batch of lines (~20 s): the speaker's lines of that batch
                    lines = [g for g in self.segments if self.same_person(g["who"], x["speaker"])
                             and x["t"] - self.s["judge_every_s"] - 10 <= g["t0"] <= x["t"] + 1]
                    for g in lines or [{"t0": x["t"], "text": ""}]:
                        r["_items"]["aggressive"].append((g["t0"], g["text"], f"ton: {x['tone']}", ""))
        out = [r for r in rows.values() if any(r[c[0]] for c in report.RANK_COLS[2:])]
        self._rank_rows = {r["name"]: r for r in out}
        for r in out:
            g = "" if r["name"] == HALL else self.group_of(r["name"]) or ""
            r["group"] = "" if g == "Nepoznato" else g
        return out

    def show_rank_detail(self, row, col):
        """Click on a ranking cell: the sentences behind that number (name/group column: everything)."""
        it = self.table.item(row, 0)
        r = getattr(self, "_rank_rows", {}).get(it.text() if it else "")
        if not r:
            return
        key, head, _tip = report.RANK_COLS[col]
        keys = [k for k, _h, _t in report.RANK_COLS[2:]] if col < 2 else [key]
        out = [f"<h3 style='margin:4px 0'>{html.escape(r['name'])}"
               + (f" – {html.escape(report.col_title(head))}" if col >= 2 else "") + "</h3>"]
        for k in keys:
            items = r["_items"].get(k, [])
            if not items:
                continue
            if len(keys) > 1:
                h = report.col_title(next(h for kk, h, _t in report.RANK_COLS if kk == k))
                out.append(f"<p style='margin:8px 0 2px 0'><b>{html.escape(h)}</b> ({len(items)})</p>")
            seen = set()
            for t, quote, label, expl in sorted(items, key=lambda x: x[0]):
                if (t, quote) in seen:
                    continue
                seen.add((t, quote))
                out.append(f"<p style='margin:2px 0 2px 8px'>{self.tlink(t)} <i>„{html.escape(quote)}“</i>"
                           f" <span style='color:#666'>– {html.escape(label)}"
                           + (f": {html.escape(expl[:220])}" if expl else "") + "</span></p>")
        if len(out) == 1:
            out.append("<p style='color:#888'>Nema zabeleženih rečenica za ovu kolonu.</p>")
        self.rank_detail.setHtml("".join(out))

    def refresh_table(self):
        rows = self.ranking()
        self.table.setSortingEnabled(False)                  # filling a sorted table scrambles the rows
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            tips = []
            if r["_kinds"]:
                tips.append("Prekršaji: " + ", ".join(f"{KINDS.get(k, (k,))[0]} ×{n}"
                                                       for k, n in r["_kinds"].most_common()))
            if r["_tech"]:
                tips.append("Tehnike: " + ", ".join(f"{TECHNIQUES.get(k, k)} ×{n}"
                                                     for k, n in r["_tech"].most_common()))
            if r["group"]:
                tips.insert(0, r["group"])                   # the group column is cut when long
            tip = "\n".join(tips)
            for j, (key, _h, _t) in enumerate(report.RANK_COLS):
                v = r.get(key, "")
                if isinstance(v, int):
                    # numbers sort as numbers; 0 stays blank so the list reads at a glance
                    it = NumItem((f"{v} %" if key == "fine" else str(v)) if v else "")
                    it.setData(Qt.ItemDataRole.UserRole, v)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                else:
                    it = QTableWidgetItem(str(v))
                if tip:
                    it.setToolTip(tip)
                self.table.setItem(i, j, it)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(False)   # the arrow would widen every column
        self.table.resizeColumnsToContents()
        if self.table.columnWidth(1) > 130:              # long group names: cut, full name in the tooltip
            self.table.setColumnWidth(1, 130)


def main():
    app = QApplication(sys.argv)
    theme.apply(app)
    w = Main()
    w.show()
    auto = os.environ.get("PRED_AUTOTEST")   # "url|start|seconds|screenshot.png" - for testing
    if auto:
        url, start, secs, shot = auto.split("|")
        w.url.setText(url)
        w.start_at.setText(start)
        QTimer.singleShot(500, w.start)

        def finish():
            w.tabs.setCurrentIndex(1)
            w.grab().save(shot.replace(".png", "_transcript.png"))
            w.tabs.setCurrentIndex(0)
            w.grab().save(shot)
            w.close()
        QTimer.singleShot(int(float(secs) * 1000), finish)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
