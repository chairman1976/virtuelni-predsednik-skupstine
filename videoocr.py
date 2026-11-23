"""Reads the official broadcast's on-screen texts with the built-in Windows OCR (free, offline).

- bottom-left caption: speaker name (Cyrillic) + parliamentary group  -> names.match_caption
- top-centre countdown: remaining speaking time ("6:26")
Windows here only has the Serbian-Latin OCR, so Cyrillic comes out as Latin look-alikes; names.py handles that.
A name is reported once it was read the same in 2 of the last 3 frames (or once with a very good score).
"""
import asyncio
import collections
import re
import threading
import time

import numpy as np

from common import log
from names import match_caption, match_caption_cyr

CAPTION_BOX = (0.0, 0.86, 0.62, 1.0)      # fractions of the frame (x0, y0, x1, y1) - Latin look-alike reading
CYR_BOX = (0.0, 0.78, 0.92, 1.0)          # name line starts at ~0.82 of the height (cut off by CAPTION_BOX)
# (scale, threshold, blur): the caption font is white with a dark outline; a single binarisation leaves hollow
# letters, so each frame is read a few ways and the best roster match wins (tested on "Милован Дрецун")
CYR_VARIANTS = [(3, 170, 1.2), (3, 200, 1.2), (2, 170, 1.2)]
TIMER_BOX = (0.36, 0.0, 0.50, 0.10)
RE_TIMER = re.compile(r"(\d{1,2})\s*[:;.,•]\s*(\d{2})")


def cyrillic_ocr_available():
    try:
        from winrt.windows.media.ocr import OcrEngine
        return any(l.language_tag.lower() == "sr-cyrl-rs" for l in OcrEngine.available_recognizer_languages)
    except Exception:
        return False


class CaptionReader(threading.Thread):
    def __init__(self, get_frame, width, height, on_caption, on_timer, interval=1.5):
        super().__init__(daemon=True)
        self.get_frame, self.w, self.h = get_frame, width, height
        self.on_caption, self.on_timer = on_caption, on_timer
        self.interval = interval
        self.running = True
        self.votes = collections.deque(maxlen=3)
        self.last_emit = (None, 0.0)
        self.last_timer = None
        self.timer_misses = 0
        self.ok = True
        self.cap_lang = "sr-Latn-RS"           # falls back to en-US on PCs without Serbian OCR
        self.cyr = cyrillic_ocr_available()    # Serbian-Cyrillic OCR installed: read the caption as it is

    def stop(self):
        self.running = False

    def _crop(self, img, box, scale, threshold, blur=0):
        from PIL import Image, ImageFilter
        c = img.crop((int(box[0] * self.w), int(box[1] * self.h), int(box[2] * self.w), int(box[3] * self.h)))
        c = c.resize((c.width * scale, c.height * scale), Image.LANCZOS)
        if threshold:
            a = np.asarray(c).astype(np.int16)
            c = Image.fromarray(np.where(a.min(axis=2) > threshold, 0, 255).astype(np.uint8))  # white text only
        if blur:
            c = c.filter(ImageFilter.GaussianBlur(blur))
        return c

    def _read_cyr(self, img):
        """[(text, y centre as fraction of the box height)] per image variant."""
        reads = []
        for scale, thr, blur in CYR_VARIANTS:
            c = self._crop(img, CYR_BOX, scale, thr, blur)
            res = asyncio.run(self._ocr(c, "sr-Cyrl-RS"))
            lines = []
            for ln in res.lines:
                ys = [w.bounding_rect.y + w.bounding_rect.height / 2 for w in ln.words]
                if ys:
                    lines.append((ln.text, sum(ys) / len(ys) / c.height))
            reads.append(lines)
        return reads

    @staticmethod
    async def _ocr(img, lang):
        import winocr
        return await winocr.recognize_pil(img, lang)

    def run(self):
        try:
            from PIL import Image
            import winocr  # noqa: F401  (fails here if the OCR package is missing)
        except Exception as e:
            log("OCR unavailable:", e)
            self.ok = False
            return
        while self.running:
            time.sleep(self.interval)
            got = self.get_frame()
            if not got:
                continue
            frame, t = got
            try:
                img = Image.frombytes("RGB", (self.w, self.h), frame)
                if self.cyr:
                    cyr = self._read_cyr(img)
                else:
                    cap = asyncio.run(self._ocr(self._crop(img, CAPTION_BOX, 3, 190), self.cap_lang))
                tim = asyncio.run(self._ocr(self._crop(img, TIMER_BOX, 4, 0), "en-US"))
            except Exception as e:
                log("ocr:", e)
                if self.cap_lang != "en-US":
                    self.cap_lang = "en-US"     # Serbian OCR language not installed on this PC
                continue
            if self.cyr:
                self._caption(None, t, match_caption_cyr(cyr))
            else:
                self._caption([l.text for l in cap.lines], t)
            self._timer(" ".join(l.text for l in tim.lines), t)

    def _caption(self, lines, t, matched=None):
        mp, score, group = matched or match_caption(lines)
        name = mp["name"] if mp else None
        self.votes.append(name)
        if not name:
            return
        agree = sum(1 for v in self.votes if v == name)
        if agree >= 2 or score >= 0.9:
            last, when = self.last_emit
            if name != last or time.time() - when > 20:
                self.last_emit = (name, time.time())
                self.on_caption(mp, group, score, t)

    def _timer(self, text, t):
        m = RE_TIMER.search(text)
        if not m:
            self.timer_misses += 1
            if self.timer_misses == 3 and self.last_timer is not None:
                self.last_timer = None
                self.on_timer(None, t)
            return
        self.timer_misses = 0
        secs = int(m.group(1)) * 60 + int(m.group(2))
        if secs != self.last_timer:
            self.last_timer = secs
            self.on_timer(secs, t)
