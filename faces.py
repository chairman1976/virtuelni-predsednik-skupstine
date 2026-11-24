"""Who is on screen: face recognition against the official MP photos (parlament.gov.rs).

OpenCV's YuNet finds faces (+5 landmarks), SFace turns an aligned face into a 128-number face print; a print is
compared with the prints of all 250 MPs' official photos (data/faces.npz, built by build_gallery()). Both models
are from opencv_zoo (Apache-2.0 / MIT), small and fast on the CPU.

The camera does not always show who is talking (the chair, reaction shots, wide shots), so a face is only
evidence: the app uses it while a voice is talking and the same face stays on screen.
"""
import os
import re
import shutil
import threading
import ssl
import urllib.parse
import urllib.request

import numpy as np

from common import CA_BUNDLE, DATA, MODELS, log

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
YUNET = ("face_detection_yunet_2023mar.onnx", ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx")
SFACE = ("face_recognition_sface_2021dec.onnx", ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx")
GALLERY = os.path.join(DATA, "faces.npz")
SITE = "https://www.parlament.gov.rs"

MATCH = 0.48          # cosine similarity (SFace: same person ~0.5-0.75 here; Vučić's face scored 0.43-0.47 as
                      # Brnabić - too close)
MARGIN = 0.06         # ...and clearly better than the second-best MP
MIN_FACE = 40         # px (face height in the 640x360 frame) - smaller faces are too blurry to tell


def _ctx():
    import certifi
    return ssl.create_default_context(cafile=CA_BUNDLE if os.path.exists(CA_BUNDLE) else certifi.where())


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
        return r.read()


def _model(name, url):
    path = os.path.join(MODELS, name)
    if not os.path.exists(path):
        os.makedirs(MODELS, exist_ok=True)
        log("downloading face model", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=_ctx(), timeout=120) as r, open(path + ".part", "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(path + ".part", path)
    return path


class FaceID:
    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(_model(*YUNET), "", (320, 320), 0.8, 0.3, 50)
        self.rec = cv2.FaceRecognizerSF.create(_model(*SFACE), "")
        self.names, self.gal = [], np.zeros((0, 128), np.float32)
        if os.path.exists(GALLERY):
            z = np.load(GALLERY, allow_pickle=False)
            self.names, self.gal = list(z["names"]), z["emb"].astype(np.float32)

    def faces(self, bgr):
        """[(box x,y,w,h, face print)] for every face in a BGR image, biggest first."""
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        _n, found = self.det.detect(bgr)
        out = []
        for f in (found if found is not None else []):
            crop = self.rec.alignCrop(bgr, f)
            e = self.rec.feature(crop).flatten().astype(np.float32)
            out.append((tuple(int(v) for v in f[:4]), e / (np.linalg.norm(e) + 1e-9)))
        out.sort(key=lambda x: -x[0][2] * x[0][3])
        return out

    def match(self, emb):
        """(name, similarity, margin over the second-best) against the MP gallery, or (None, s, m)."""
        if not len(self.names):
            return None, 0.0, 0.0
        sims = self.gal @ emb
        order = np.argsort(-sims)
        best, second = float(sims[order[0]]), float(sims[order[1]]) if len(order) > 1 else 0.0
        ok = best >= MATCH and best - second >= MARGIN
        return (self.names[order[0]] if ok else None), best, best - second

    def who(self, rgb):
        """The MP whose face fills the picture now: (name, similarity, box) or None.

        Only the biggest face counts, and only if it is big enough and clearly bigger than any other face
        (a close-up of one person, as the director shows the speaker)."""
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        fs = self.faces(bgr)
        if not fs or fs[0][0][3] < MIN_FACE:
            return None
        if len(fs) > 1 and fs[1][0][3] > 0.7 * fs[0][0][3]:
            return None                         # several people of similar size: no single subject
        name, sim, _m = self.match(fs[0][1])
        return (name, sim, fs[0][0]) if name else None


TALK = 0.28           # mouth movement (aligned lips, frame to frame) of someone talking; listeners ~0.1-0.24
TALK_LEAD = 1.25      # ...and this much more than anyone else in the picture
FPS = 5
MAX_FACES = 6         # more faces = a wide shot of the hall: nobody in particular
CUT = 25.0            # mean grey-level change of a 64x36 copy of the picture between frames = a camera cut


class _Track:
    def __init__(self, box):
        import collections
        self.box, self.prev, self.n = box, None, 0
        self.acts = collections.deque(maxlen=3 * FPS)     # the last 3 s of mouth movement
        self.votes = collections.Counter()                # MP name -> times recognised
        self.sims = collections.defaultdict(list)

    def centre(self):
        x, y, w, h = self.box
        return x + w / 2, y + h / 2

    def talk(self):
        return float(np.mean(self.acts)) if len(self.acts) >= 8 else 0.0

    def name(self):
        if not self.votes:
            return None, 0.0
        nm, k = self.votes.most_common(1)[0]
        return (nm, float(np.mean(self.sims[nm]))) if k >= 2 and k >= 0.6 * sum(self.votes.values()) else (None, 0.0)


class FaceWatcher(threading.Thread):
    """Watches the picture: whose lips move, and who that is. Calls on_face(name, similarity, t) once the same
    MP has been the talking face for 3 s (then every 5 s while it lasts)."""

    def __init__(self, get_frame, on_face, on_status):
        super().__init__(daemon=True)
        self.get_frame, self.on_face, self.on_status = get_frame, on_face, on_status
        self.running = True
        self.tracks = []
        self.streak = (None, 0)
        self.thumb = None                                # tiny grey copy of the last frame (cut detection)

    def stop(self):
        self.running = False

    def run(self):
        import time
        try:
            if not os.path.exists(GALLERY):
                self.on_status("Preuzimam fotografije poslanika za prepoznavanje lica (jednom)…")
                build_gallery(self.on_status)
            self.fid = FaceID()
        except Exception as e:
            log("faces:", e)
            self.on_status(f"Prepoznavanje lica nije dostupno: {e}")
            return
        import cv2
        self.cv2 = cv2
        last_decide = 0.0
        while self.running:
            t_start = time.time()
            fr = self.get_frame()
            if fr is None:
                time.sleep(0.05)
                continue
            rgb, t = fr
            try:
                self._step(np.ascontiguousarray(rgb[:, :, ::-1]))
            except Exception as e:                       # never let one odd frame stop the watcher
                log("faces step:", e)
            if time.time() - last_decide >= 1.0:
                last_decide = time.time()
                self._decide(t)
            time.sleep(max(0.0, 1 / FPS - (time.time() - t_start)))

    def _step(self, bgr):
        cv2 = self.cv2
        h, w = bgr.shape[:2]
        thumb = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)
        thumb = thumb.astype(np.float32)
        # the director cut to another camera (measured: ~40-48 at a cut, < 14 within a shot): a face at the
        # same place in the picture is someone else now
        if self.thumb is not None and float(np.abs(thumb - self.thumb).mean()) > CUT:
            self.tracks, self.streak = [], (None, 0)
        self.thumb = thumb
        self.fid.det.setInputSize((w, h))
        _n, found = self.fid.det.detect(bgr)
        found = [] if found is None else list(found)
        if not found or len(found) > MAX_FACES:
            self.tracks = []                             # cut to a wide shot / no faces: start over
            return
        new = []
        for f in found:
            box = tuple(float(v) for v in f[:4])
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
            tr = min(self.tracks, key=lambda x: (x.centre()[0] - cx) ** 2 + (x.centre()[1] - cy) ** 2, default=None)
            if tr is None or ((tr.centre()[0] - cx) ** 2 + (tr.centre()[1] - cy) ** 2) ** 0.5 > 0.6 * box[2] \
                    or tr in new or not 0.7 < box[3] / tr.box[3] < 1.4:
                tr = _Track(box)
            tr.box = box
            new.append(tr)
            if box[3] < MIN_FACE:
                continue
            aligned = self.fid.rec.alignCrop(bgr, f)
            g = cv2.GaussianBlur(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY).astype(np.float32), (3, 3), 0)
            lips = g[70:102, 30:82]                       # the mouth in SFace's aligned 112x112 face
            lips = (lips - lips.mean()) / (lips.std() + 1e-6)
            if tr.prev is not None:
                tr.acts.append(float(np.abs(lips - tr.prev).mean()))
            tr.prev = lips
            if tr.n % FPS == 0:                           # who it is: once a second is plenty
                e = self.fid.rec.feature(aligned).flatten().astype(np.float32)
                name, sim, _m = self.fid.match(e / (np.linalg.norm(e) + 1e-9))
                if name:
                    tr.votes[name] += 1
                    tr.sims[name].append(sim)
            tr.n += 1
        self.tracks = new                                # faces gone from the picture are forgotten

    def _decide(self, t):
        big = max((tr.box[3] for tr in self.tracks), default=0)
        cand = sorted((tr for tr in self.tracks if tr.box[3] >= max(MIN_FACE, 0.6 * big)),
                      key=lambda x: -x.talk())
        who = None
        if cand and cand[0].talk() >= TALK and (len(cand) == 1 or cand[0].talk() >= TALK_LEAD * cand[1].talk()):
            who, sim = cand[0].name()
        k = self.streak[1] + 1 if who and who == self.streak[0] else (1 if who else 0)
        self.streak = (who, k)
        if who and (k == 3 or (k > 3 and k % 5 == 0)):
            self.on_face(who, sim, t)


def build_gallery(on_status=print):
    """Download the official photos of all MPs and store their face prints in data/faces.npz."""
    import cv2
    import update_roster
    page = update_roster.fetch()
    fid = FaceID()
    names, embs, missing = [], [], []
    blocks = page.split('class="grid_single"')[1:]
    for i, blk in enumerate(blocks):
        mps = update_roster.parse(blk)
        m = re.search(r'<img src="([^"]+)"', blk)
        if not mps or not m:
            continue
        name = mps[0]["name"]
        try:
            img = cv2.imdecode(np.frombuffer(_get(SITE + urllib.parse.quote(m.group(1))), np.uint8), cv2.IMREAD_COLOR)
            # the photos are small (150x209): enlarge so the detector sees a normal-size face
            img = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            fs = fid.faces(img)
        except Exception as e:
            log("face photo", name, e)
            fs = []
        if fs:
            names.append(name)
            embs.append(fs[0][1])
        else:
            missing.append(name)
        if i % 25 == 0:
            on_status(f"Lica poslanika: {i + 1}/{len(blocks)}")
    np.savez_compressed(GALLERY, names=np.array(names), emb=np.array(embs, np.float32))
    on_status(f"Lica poslanika: {len(names)} sačuvano, bez lica na fotografiji: {len(missing)} {missing[:5]}")
    return names, missing


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    build_gallery()
