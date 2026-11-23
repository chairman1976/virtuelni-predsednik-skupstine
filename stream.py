"""YouTube link -> one ffmpeg process -> video frames + playback audio + 16 kHz mono audio for ASR.

All three come out of the same ffmpeg, so what you see/hear is exactly what is being judged.
HLS (m3u8) formats are used for both live and recorded streams: they seek instantly, while
ffmpeg on YouTube's DASH files crawls when seeking.
"""
import collections
import json
import socket
import subprocess
import threading
import time

import numpy as np

from common import log

VW, VH, FPS = 640, 360, 25
PLAY_SR = 48000
ASR_SR = 16000
FORMAT = ("bv*[protocol^=m3u8][vcodec^=avc1][height<=480]+ba[protocol^=m3u8]/"   # H.264: VP9 HLS gives decode errors
          "bv*[protocol^=m3u8][height<=480]+ba[protocol^=m3u8]/b[protocol^=m3u8][height<=480]/"
          "b[protocol^=m3u8]/bv*[height<=480]+ba/b")
NOWIN = 0x08000000  # CREATE_NO_WINDOW


def resolve(url):
    """Return dict(title, is_live, duration, video_url, audio_url, headers)."""
    cmd = ["yt-dlp", "--js-runtimes", "node", "--no-warnings", "-f", FORMAT, "-J", "--no-playlist", url]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", creationflags=NOWIN, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("yt-dlp: " + (p.stderr.strip().splitlines() or ["nepoznata greška"])[-1])
    info = json.loads(p.stdout)
    req = info.get("requested_formats")
    if req:
        v = next((f for f in req if f.get("vcodec") not in (None, "none")), req[0])
        a = next((f for f in req if f.get("acodec") not in (None, "none")), req[-1])
        vurl, aurl, headers = v["url"], a["url"], v.get("http_headers") or {}
    else:
        vurl = aurl = info["url"]
        headers = info.get("http_headers") or {}
    return {
        "title": info.get("title", ""),
        "is_live": bool(info.get("is_live")),
        "duration": info.get("duration") or 0,
        "video_url": vurl,
        "audio_url": aurl,
        "headers": headers,
    }


class _Listener:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(60)
        self.port = self.sock.getsockname()[1]

    def accept(self):
        conn, _ = self.sock.accept()
        self.sock.close()
        return conn


def _read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class Player:
    """Runs ffmpeg and exposes: latest video frame, an audio playback buffer, and ASR callbacks."""

    def __init__(self, src, start_s, on_asr_audio, on_frame, on_end):
        self.src, self.start_s = src, (0.0 if src["is_live"] else start_s)
        self.on_asr_audio, self.on_frame, self.on_end = on_asr_audio, on_frame, on_end
        self.play_buf = collections.deque()
        self.play_lock = threading.Lock()
        self.play_queued = 0
        self.asr_samples = 0
        self.running = False
        self.proc = None
        self.volume = 0.8
        self.stream_time = self.start_s
        self._out = None

    # ---------------------------------------------------------------------------------------
    def start(self):
        import sounddevice as sd
        lv, la = _Listener(), _Listener()
        live = self.src["is_live"]
        ua = self.src["headers"].get("User-Agent")
        inputs = []
        urls = [self.src["video_url"]]
        if self.src["audio_url"] != self.src["video_url"]:
            urls.append(self.src["audio_url"])
        for u in urls:
            if not live:
                inputs += ["-re"]
                if self.start_s > 0:
                    inputs += ["-ss", f"{self.start_s:.2f}"]
            if ua:
                inputs += ["-user_agent", ua]
            inputs += ["-i", u]
        amap = "1:a:0" if len(urls) == 2 else "0:a:0"
        vf = (f"scale={VW}:{VH}:force_original_aspect_ratio=decrease,"
              f"pad={VW}:{VH}:(ow-iw)/2:(oh-ih)/2,fps={FPS}")
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + inputs + [
            "-map", "0:v:0", "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", f"tcp://127.0.0.1:{lv.port}",
            "-map", amap, "-ac", "2", "-ar", str(PLAY_SR), "-f", "s16le", f"tcp://127.0.0.1:{la.port}",
            "-map", amap, "-ac", "1", "-ar", str(ASR_SR), "-f", "s16le", "pipe:1",
        ])
        self.running = True
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
        threading.Thread(target=self._stderr, daemon=True).start()
        threading.Thread(target=self._asr_reader, daemon=True).start()
        threading.Thread(target=self._video_reader, args=(lv,), daemon=True).start()
        threading.Thread(target=self._audio_reader, args=(la,), daemon=True).start()
        self._out = sd.OutputStream(samplerate=PLAY_SR, channels=2, dtype="int16", blocksize=1024,
                                    callback=self._play_cb, latency="low")
        self._out.start()

    def stop(self):
        self.running = False
        if self._out:
            try:
                self._out.stop()
                self._out.close()
            except Exception:
                pass
            self._out = None
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    # ---------------------------------------------------------------------------------------
    def _stderr(self):
        for line in self.proc.stderr:
            log("ffmpeg:", line.decode("utf-8", "replace").rstrip())

    def _asr_reader(self):
        f = self.proc.stdout
        block = ASR_SR // 5 * 2  # 0.2 s of int16
        while self.running:
            data = f.read(block)
            if not data:
                break
            pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            t0 = self.start_s + self.asr_samples / ASR_SR
            self.asr_samples += len(pcm)
            self.stream_time = self.start_s + self.asr_samples / ASR_SR
            self.on_asr_audio(pcm, t0)
        if self.running:
            self.running = False
            self.on_end()

    def _video_reader(self, lv):
        try:
            conn = lv.accept()
        except OSError:
            return
        size = VW * VH * 3
        rf = conn.makefile("rb", buffering=size * 2).read
        while self.running:
            frame = _read_exact(rf, size)
            if frame is None:
                break
            self.on_frame(frame)
        conn.close()

    def _audio_reader(self, la):
        try:
            conn = la.accept()
        except OSError:
            return
        while self.running:
            data = conn.recv(8192)
            if not data:
                break
            with self.play_lock:
                self.play_buf.append(data)
                self.play_queued += len(data)
                while self.play_queued > PLAY_SR * 4 * 2:   # > 2 s backlog: drop oldest
                    self.play_queued -= len(self.play_buf.popleft())
        conn.close()

    def _play_cb(self, outdata, frames, _time, _status):
        need = frames * 4
        out = bytearray()
        with self.play_lock:
            while len(out) < need and self.play_buf:
                chunk = self.play_buf.popleft()
                take = need - len(out)
                out += chunk[:take]
                if len(chunk) > take:
                    self.play_buf.appendleft(chunk[take:])
                self.play_queued -= min(len(chunk), take)
        if len(out) < need:
            out += bytes(need - len(out))
        a = np.frombuffer(bytes(out), dtype=np.int16).reshape(-1, 2)
        if self.volume < 0.999:
            a = (a.astype(np.float32) * self.volume).astype(np.int16)
        outdata[:] = a
