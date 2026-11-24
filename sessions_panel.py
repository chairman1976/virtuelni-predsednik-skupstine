"""Left side panel: the official channel's (ParlamentSrbija) live broadcast and past sessions to pick from.

yt-dlp lists the channel's "streams" tab (what is live now / upcoming) and "videos" tab (recordings: sessions in
parts I, II, III..., press conferences) without downloading anything (--flat-playlist, ~2 s each).
"""
import json
import re
import subprocess
import threading
import urllib.request

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

from common import log, to_latin
from stream import NOWIN

CHANNEL = "https://www.youtube.com/channel/UCghngrfQwzJ4aaPZ-UgNIZQ"
SESSION_WORDS = ("sednic", "zasedanj")
SAZIV = {"Petnaestom": 15, "Šesnaestom": 16, "Četrnaestom": 14, "Trinaestom": 13}


def _list(tab, n):
    cmd = ["yt-dlp", "--js-runtimes", "node", "--no-warnings", "--flat-playlist", "-J", "--playlist-end", str(n),
           f"{CHANNEL}/{tab}"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", creationflags=NOWIN, timeout=90)
    if p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or ["yt-dlp"])[-1])
    return json.loads(p.stdout).get("entries") or []


def _hms(s):
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d} h" if s >= 3600 else f"{s // 60} min"


def describe(e, live=False):
    """dict(url, title, line1, line2, live, upcoming, session) for one channel entry."""
    title = to_latin(e.get("title") or "")
    m = re.search(r"(\d{1,2}\.\d{1,2}\.20\d\d)\.?", title)
    date = m.group(1) + "." if m else ""
    part = re.search(r"\((I{1,3}|IV|VI{0,3}|IX|X)\)", title)
    name = re.sub(r"\s*\d{1,2}\.\d{1,2}\.20\d\d\.?.*$", "", title).strip() or title
    # "... Narodne skupštine Republike Srbije u Petnaestom sazivu" -> "... (15. saziv)"
    name = re.sub(r"\s*(Narodne skupštine Republike Srbije|NSRS|NCPC|НСРС)\b", "", name)
    for word, no in SAZIV.items():
        name = re.sub(rf"\s*u {word} sazivu", f" ({no}. saziv)", name, flags=re.I)
    status = e.get("live_status") or ""
    is_live = live or status == "is_live"
    upcoming = status == "is_upcoming"
    tag = "🔴 UŽIVO" if is_live else ("⏳ uskoro" if upcoming else _hms(e.get("duration")))
    line2 = " · ".join(x for x in (date, f"deo {part.group(1)}" if part else "", tag) if x)
    dur = e.get("duration") or 0
    return {"url": f"https://www.youtube.com/watch?v={e['id']}", "id": e["id"], "title": title,
            "line1": name[:1].upper() + name[1:], "line2": line2, "live": is_live, "upcoming": upcoming,
            # a sitting, not a 25-second "consultations about the sitting" clip
            "session": any(w in title.lower() for w in SESSION_WORDS) and (is_live or upcoming or dur >= 600)}


def fetch(n=60):
    """Live/upcoming broadcasts first, then recordings (newest first)."""
    out, seen = [], set()
    for tab, live in (("streams", False), ("videos", False)):
        try:
            entries = _list(tab, 10 if tab == "streams" else n)
        except Exception as ex:
            log("sessions list", tab, ex)
            continue
        for e in entries:
            if not e.get("id") or e["id"] in seen:
                continue
            d = describe(e, live)
            if tab == "streams" and not (d["live"] or d["upcoming"]):
                continue                                     # finished broadcasts come again under "videos"
            seen.add(e["id"])
            out.append(d)
    return out


class SessionsPanel(QWidget):
    """The list; emits chosen(url, title, live) on a click."""
    chosen = pyqtSignal(str, str, bool)
    _loaded = pyqtSignal(object)
    _thumb = pyqtSignal(str, bytes)

    def __init__(self, ca_context=None):
        super().__init__()
        self.ctx = ca_context
        self.items = []
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 6, 0)
        head = QHBoxLayout()
        title = QLabel("<b>Sednice</b> <span style='color:#8b93a7'>· kanal ParlamentSrbija</span>")
        self.refresh_btn = QPushButton("Osveži")
        self.refresh_btn.setFixedWidth(64)
        self.refresh_btn.setToolTip("Osveži spisak sa YouTube kanala Narodne skupštine")
        self.refresh_btn.clicked.connect(self.load)
        head.addWidget(title, 1)
        head.addWidget(self.refresh_btn)
        v.addLayout(head)
        self.only = QCheckBox("samo sednice (bez konferencija za novinare)")
        self.only.setChecked(True)
        self.only.toggled.connect(self.fill)
        v.addWidget(self.only)
        self.status = QLabel("")
        self.status.setProperty("muted", True)
        v.addWidget(self.status)
        self.list = QListWidget()
        self.list.setIconSize(QSize(96, 54))
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.list.itemClicked.connect(self._clicked)
        v.addWidget(self.list, 1)
        self._loaded.connect(self._on_loaded)
        self._thumb.connect(self._on_thumb)
        self.icons = {}

    def load(self):
        self.status.setText("Učitavam spisak sa kanala…")
        self.refresh_btn.setEnabled(False)
        threading.Thread(target=lambda: self._loaded.emit(fetch()), daemon=True).start()

    def _on_loaded(self, items):
        self.refresh_btn.setEnabled(True)
        self.items = items
        live = sum(1 for x in items if x["live"])
        self.status.setText(f"{len(items)} snimaka" + (f" · {live} uživo" if live else " · trenutno nema prenosa")
                            if items else "Spisak nije učitan (internet / yt-dlp).")
        self.fill()
        threading.Thread(target=self._load_thumbs, args=([x["id"] for x in items],), daemon=True).start()

    def fill(self):
        self.list.clear()
        for x in self.items:
            if self.only.isChecked() and not (x["session"] or x["live"]):
                continue
            it = QListWidgetItem(f"{x['line2']}\n{x['line1']}")         # date · part · length, then what
            it.setData(Qt.ItemDataRole.UserRole, x)
            it.setToolTip(x["title"] + ("\n\nKlik: prati prenos uživo." if x["live"] else
                                        "\n\nKlik: pusti i oceni ovaj snimak."))
            if x["live"]:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            if x["upcoming"]:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            if x["id"] in self.icons:
                it.setIcon(self.icons[x["id"]])
            self.list.addItem(it)

    def _load_thumbs(self, ids):
        for vid in ids:
            try:
                req = urllib.request.Request(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                                             headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, context=self.ctx, timeout=15) as r:
                    self._thumb.emit(vid, r.read())
            except Exception:
                pass

    def _on_thumb(self, vid, data):
        img = QImage.fromData(data)
        if img.isNull():
            return
        icon = QIcon(QPixmap.fromImage(img.scaled(96, 54, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)))
        self.icons[vid] = icon
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.ItemDataRole.UserRole)["id"] == vid:
                it.setIcon(icon)

    def _clicked(self, it):
        x = it.data(Qt.ItemDataRole.UserRole)
        if x and not x["upcoming"]:
            self.chosen.emit(x["url"], x["title"], x["live"])
