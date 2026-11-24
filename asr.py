"""Streaming Serbian speech-to-text (faster-whisper on the GPU) + loudness + voice embeddings."""
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

import numpy as np

import asr_fix
from common import MODELS, fold_text, log, to_latin

SR = 16000
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "nemo_en_titanet_small.onnx")      # sic: "recongition" is the real release tag
HALLUCINATIONS = re.compile(r"titlov|prevod|subtit|hvala (?:na|što ste) gled|hvala što pratite|продолжение|"
                            r"sledeći video|pretplat|lajk|amara\.org", re.I)


UNSURE = 0.6            # word probability below which a word counts as possibly misheard (~2 % of words)


@dataclass
class Segment:
    t0: float
    t1: float
    text: str
    latin: str
    db: float           # loudness, dBFS RMS
    emb: object = None  # np.ndarray voice embedding or None
    gen: int = 0        # seek generation it belongs to
    f0: float = None    # median voice pitch, Hz
    unsure: list = None  # words Whisper was unsure of (probability < UNSURE) - may be misheard


class VoiceEmbedder:
    def __init__(self):
        import sherpa_onnx
        path = os.path.join(MODELS, "nemo_en_titanet_small.onnx")
        if not os.path.exists(path):
            import shutil
            import ssl
            import urllib.request
            import certifi
            from common import CA_BUNDLE
            # explicit CA file: some Windows cert stores hold a cert that breaks load_default_certs
            ctx = ssl.create_default_context(cafile=CA_BUNDLE if os.path.exists(CA_BUNDLE) else certifi.where())
            os.makedirs(MODELS, exist_ok=True)
            log("downloading voice model", MODEL_URL)
            with urllib.request.urlopen(MODEL_URL, context=ctx, timeout=60) as r, open(path + ".part", "wb") as f:
                shutil.copyfileobj(r, f)
            os.replace(path + ".part", path)
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=path, num_threads=2, provider="cpu")
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def __call__(self, audio):
        s = self.ex.create_stream()
        s.accept_waveform(SR, audio)
        s.input_finished()
        e = np.array(self.ex.compute(s), dtype=np.float32)
        return e / (np.linalg.norm(e) + 1e-9)


def pitch_hz(a):
    """Median voice pitch (F0) of a segment by frame autocorrelation; None if too little voiced speech.
    Typical: men ~85-160 Hz, women ~165-255 Hz."""
    # YIN (de Cheveigné & Kawahara): cumulative-mean-normalised difference, first dip under 0.15
    w, hop = 800, 320                                   # 50 ms window (>= 2 periods at 70 Hz)
    tmax, tmin = SR // 70, SR // 400                    # 70-400 Hz
    if len(a) < w + tmax + hop * 5:
        return None
    idx = np.arange(0, len(a) - w - tmax, hop)
    fr = np.stack([a[i:i + w + tmax] for i in idx])
    e = np.sqrt(np.mean(fr[:, :w] ** 2, axis=1))
    fr = fr[e > max(1e-4, np.percentile(e, 50) * 0.5)]     # skip quiet frames
    if len(fr) < 5:
        return None
    nfft = 1 << int(np.ceil(np.log2(2 * (w + tmax))))
    x = fr
    xw = x[:, :w]
    # r(tau) = sum_j x[j] x[j+tau] for j < w
    r = np.fft.irfft(np.fft.rfft(xw[:, ::-1], nfft) * np.fft.rfft(x, nfft), nfft)[:, w - 1:w - 1 + tmax + 1]
    sq = np.cumsum(np.concatenate([np.zeros((len(x), 1)), x ** 2], axis=1), axis=1)
    e0 = sq[:, w] - sq[:, 0]
    et = sq[:, w:w + tmax + 1] - sq[:, :tmax + 1]       # energy of x[tau : tau+w]
    d = e0[:, None] + et - 2 * r
    d[:, 0] = 0
    cm = np.cumsum(d[:, 1:], axis=1) / np.arange(1, tmax + 1)
    dn = np.ones_like(d)
    dn[:, 1:] = d[:, 1:] / (cm + 1e-12)
    f0 = []
    for row in dn:
        below = np.where(row[tmin:tmax] < 0.15)[0]
        if not len(below):
            continue
        t = tmin + below[0]
        while t + 1 < tmax and row[t + 1] < row[t]:     # walk down to the local minimum
            t += 1
        f0.append(SR / t)
    if len(f0) < 5:
        return None
    return float(np.median(f0))


def rms_db(a):
    return float(20 * np.log10(np.sqrt(np.mean(a * a)) + 1e-9))


class ASRWorker(threading.Thread):
    """Feed audio with push(pcm, t0); segments come out through on_segment(Segment)."""

    def __init__(self, model_name, on_segment, on_status):
        super().__init__(daemon=True)
        self.model_name, self.on_segment, self.on_status = model_name, on_segment, on_status
        self.q = queue.Queue()
        self.buf = np.zeros(0, dtype=np.float32)
        self.buf_t0 = None
        self.prev_text = ""
        self.hint_names = []  # names likely said now (speaker, chair) - set by the app, steer Whisper's spelling
        self.running = True
        self.abort = False
        self.lag = 0.0
        self.last_end = 0.0
        self.buf_gen = 0
        self.gen = 0          # bumped on every seek: older audio/segments are thrown away

    def push(self, pcm, t0):
        self.q.put((pcm, t0, self.gen))

    def pusher(self):
        """push() bound to the current seek generation: a player stopped by a seek may still deliver one last
        chunk after reset() - with its old generation it is dropped instead of shifting the new stretch's clock."""
        gen = self.gen
        return lambda pcm, t0: self.q.put((pcm, t0, gen))

    def reset(self):
        """Seek: forget buffered audio; segments still in flight from before are dropped."""
        self.gen += 1
        self.prev_text = ""
        self.last_end = 0.0

    def stop(self, abort=False):
        """abort=True: drop what is still buffered (Stop button); False: finish it (end of file)."""
        self.running = False
        self.abort = abort
        self.q.put(None)

    def _load(self):
        from faster_whisper import WhisperModel
        self.on_status(f"Učitavam model govora ({self.model_name})…")
        try:
            self.model = WhisperModel(self.model_name, device="cuda", compute_type="int8")
            dev = "GPU"
        except Exception as e:  # no CUDA -> CPU fallback
            log("CUDA failed, CPU:", e)
            self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            dev = "CPU"
        try:
            self.embed = VoiceEmbedder()
        except Exception as e:
            log("voice embedder unavailable:", e)
            self.embed = None
        self.on_status(f"Model govora spreman ({dev}).")

    def _cut_point(self):
        """Index to cut the buffer at: quietest 0.3 s in the 4-10 s range, hard cut at 10 s."""
        n = len(self.buf)
        if n < 6 * SR:
            return None
        lo, hi = 4 * SR, min(n, 10 * SR)
        win = int(0.3 * SR)
        hop = int(0.1 * SR)
        best, best_e = hi, None
        for i in range(lo, hi - win, hop):
            e = float(np.mean(self.buf[i:i + win] ** 2))
            if best_e is None or e < best_e:
                best, best_e = i + win // 2, e
        loud = float(np.mean(self.buf[:hi] ** 2)) + 1e-12
        if n < 10 * SR and best_e is not None and best_e > loud * 0.25:
            return None  # no real pause yet - wait for more audio (up to 10 s)
        return best

    def run(self):
        self._load()
        stopping = False
        while not stopping:
            item = self.q.get()
            parts = []
            while True:                       # take everything already queued
                if item is None:
                    stopping = True
                    break
                if item[2] != self.buf_gen:   # first audio after a seek: start a fresh buffer
                    if item[2] != self.gen:
                        item = self.q.get() if not self.q.empty() else False
                        if item is False:
                            break
                        continue
                    self.buf_gen, self.buf, self.buf_t0, parts = item[2], np.zeros(0, dtype=np.float32), None, []
                if self.buf_t0 is None:
                    self.buf_t0 = item[1]
                parts.append(item[0])
                if len(parts) > 25 or self.q.empty():   # ~5 s max per pass keeps cuts small
                    break
                item = self.q.get()
            if parts:
                self.buf = np.concatenate([self.buf] + parts)
            if self.buf_gen != self.gen:               # audio from before a seek - don't transcribe it
                self.buf, self.buf_t0 = np.zeros(0, dtype=np.float32), None
                continue
            while True:
                cut = self._cut_point()
                if cut is None:
                    if stopping and len(self.buf) > SR // 2:
                        cut = len(self.buf)
                    else:
                        break
                chunk, self.buf = self.buf[:cut], self.buf[cut:]
                c_t0 = self.buf_t0
                self.buf_t0 = c_t0 + cut / SR
                self._transcribe(chunk, c_t0, self.buf_gen)
                if self.abort:
                    return

    @staticmethod
    def _pieces(s, gap=0.45, min_len=1.2):
        """Split a whisper segment at pauses between words, so an interjection from the hall gets its own line
        (and its own voice check) instead of being glued to the speaker's sentence. Pieces shorter than
        min_len are merged into a neighbour - too short to tell voices apart."""
        words = [w for w in (s.words or []) if w.word.strip()]
        if len(words) < 2:
            return [None]
        groups = [[words[0]]]
        for prev, w in zip(words, words[1:]):
            if w.start - prev.end >= gap:
                groups.append([w])
            else:
                groups[-1].append(w)
        merged = []
        for g in groups:
            if merged and (g[-1].end - g[0].start < min_len or merged[-1][-1].end - merged[-1][0].start < min_len):
                merged[-1] = merged[-1] + g
            else:
                merged.append(g)
        return merged

    def _voice_split(self, chunk, g, depth=0, min_len=1.2, differ=0.45):
        """Whisper sometimes glues two people into one sentence with no pause ("Pa ima lista govornika,
        genij. Reč ima predlagač ..." = a heckler + the chair; voiceprints 0.20 vs 0.87). Try splitting at a few
        word boundaries (sentence ends and the longest gaps first); split where the two sides clearly sound
        like different people. Same-person halves score 0.75-0.9 here, different people below 0.47."""
        if self.embed is None or len(g) < 4 or g[-1].end - g[0].start < 2 * min_len or depth > 1:
            return [g]
        cands = []
        for k in range(1, len(g)):
            if g[k - 1].end - g[0].start < min_len or g[-1].end - g[k].start < min_len:
                continue
            punct = g[k - 1].word.strip()[-1:] in ".?!,"
            cands.append((punct, g[k].start - g[k - 1].end, k))
        best = None
        for _p, _gap, k in sorted(cands, reverse=True)[:4]:
            try:
                left = self.embed(chunk[int(g[0].start * SR):int(g[k - 1].end * SR)])
                right = self.embed(chunk[int(g[k].start * SR):int(g[-1].end * SR)])
            except Exception as e:
                log("embed:", e)
                return [g]
            sim = float(np.dot(left, right))
            if best is None or sim < best[0]:
                best = (sim, k)
        if best is None or best[0] >= differ:
            return [g]
        k = best[1]
        return self._voice_split(chunk, g[:k], depth + 1) + self._voice_split(chunk, g[k:], depth + 1)

    def _transcribe(self, chunk, c_t0, gen):
        t_start = time.time()
        prompt = asr_fix.prompt(self.hint_names, self.prev_text)
        segs, _ = self.model.transcribe(
            chunk, language="sr", beam_size=2, vad_filter=True, condition_on_previous_text=False,
            initial_prompt=prompt, word_timestamps=True,
            # threshold below the 0.5 default: quieter voices from the hall (heckling) are kept
            vad_parameters={"min_silence_duration_ms": 400, "threshold": 0.35})
        out = []
        for s in segs:
            text = s.text.strip()
            if not text or HALLUCINATIONS.search(to_latin(text)):
                continue
            ft = fold_text(text)
            if len(ft) > 12 and ft in fold_text(prompt):
                continue                      # Whisper repeated its prompt over silence
            if s.no_speech_prob > 0.6 and s.avg_logprob < -0.9:
                continue
            pieces = []
            for g in self._pieces(s):
                if g is None:
                    pieces.append((s.start, s.end, text, []))
                else:
                    pieces += [(h[0].start, h[-1].end, "".join(w.word for w in h).strip(),
                                [w.word.strip() for w in h if (w.probability or 0) < UNSURE])
                               for h in self._voice_split(chunk, g)]
            for p0, p1, ptext, unsure in pieces:
                a = chunk[int(p0 * SR):int(p1 * SR)]
                if len(a) < SR // 4:
                    continue
                emb = None
                if self.embed is not None and len(a) >= int(1.2 * SR):
                    try:
                        emb = self.embed(a)
                    except Exception as e:
                        log("embed:", e)
                f0 = pitch_hz(a) if len(a) >= SR else None
                lat = asr_fix.fix(to_latin(ptext))          # "imate rečo" -> "imate reč", "poslannik" ...
                unsure = [u for u in (asr_fix.fix(to_latin(w)).strip(".,!?;:…\"'") for w in unsure)
                          if len(u) >= 4]
                out.append(Segment(c_t0 + p0, c_t0 + p1, lat, lat, rms_db(a), emb, f0=f0, unsure=unsure))
        if gen != self.gen:
            return                            # user seeked while this chunk was being transcribed
        for seg in out:
            seg.gen = gen
            self.prev_text = (self.prev_text + " " + seg.text)[-400:]
            try:
                self.on_segment(seg)
            except (AttributeError, RuntimeError):
                return                        # window already closed
        self.last_end = c_t0 + len(chunk) / SR
        self.lag = time.time() - t_start
