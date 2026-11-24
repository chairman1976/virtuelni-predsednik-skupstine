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


BYTES_PER_S = PLAY_SR * 4          # 48 kHz stereo int16
AHEAD_S = 6.0                      # ffmpeg may run this far ahead of playback (then the readers stop -> it waits)
PREBUF_S = 2.0                     # sound starts once this much is buffered
REBUF_S = 1.5                      # after the buffer ran dry: wait for this much, then continue (one clean pause
                                   # instead of a stream of tiny gaps = "robot voice")


class Player:
    """Runs ffmpeg and plays its picture + sound from a jitter buffer; ASR gets the audio as soon as it arrives.

    The network (YouTube HLS) delivers in bursts and stalls, and the GIL is busy with whisper/OCR/UI. Earlier
    ffmpeg ran with -re (real-time) and the sound was played as it came: ~0.6 s of cushion, so every hiccup was
    an audible gap (measured: 94 empty audio blocks in the first 8 s of a live stream). Now ffmpeg runs freely
    up to AHEAD_S in front, sound plays from that buffer, and the picture is shown by the sound's clock.
    """

    def __init__(self, src, start_s, on_asr_audio, on_frame, on_end):
        self.src, self.start_s = src, (0.0 if src["is_live"] else start_s)
        self.on_asr_audio, self.on_frame, self.on_end = on_asr_audio, on_frame, on_end
        self.play_buf = collections.deque()
        self.play_lock = threading.Lock()
        self.play_queued = 0
        self.played = 0                    # bytes of sound handed to the sound card
        self.buffering = True              # waiting for PREBUF_S / REBUF_S before (re)starting the sound
        self.underruns = 0                 # times the buffer ran dry (diagnostics)
        self.frames = collections.deque()  # (t, frame) decoded ahead of the picture
        self.frame_lock = threading.Lock()
        self.n_frames = 0
        self.eof = False
        self.asr_samples = 0
        self.running = False
        self.proc = None
        self.volume = 0.8
        self.asr_time = self.start_s
        self._out = None

    @property
    def stream_time(self):
        """Position the user sees and hears now (not how far ffmpeg/ASR got)."""
        lat = 0.0
        if self._out is not None:
            try:
                lat = float(self._out.latency)
            except Exception:
                pass
        return self.start_s + max(0.0, self.played / BYTES_PER_S - lat)

    def buffered(self):
        return self.play_queued / BYTES_PER_S

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
            # no -re: YouTube HLS arrives in bursts (live: 15 s at once, then 5 s every 5 s); the jitter buffer
            # absorbs them and the readers hold ffmpeg back once it is AHEAD_S in front of playback.
            if not live and self.start_s > 0:
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
        threading.Thread(target=self._presenter, daemon=True).start()
        # "high" latency: the callback runs in Python and must get the GIL in time - a 20 ms budget ("low")
        # is missed whenever whisper/OCR/the UI hold it, and every miss is a click
        self._out = sd.OutputStream(samplerate=PLAY_SR, channels=2, dtype="int16", blocksize=2048,
                                    callback=self._play_cb, latency="high")
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
            self.asr_time = self.start_s + self.asr_samples / ASR_SR
            self.on_asr_audio(pcm, t0)
        self.eof = True
        while self.running and (self.play_queued > 0 or self.frames):   # let the buffered tail play out
            time.sleep(0.1)
        if self.running:
            self.running = False
            self.on_end()

    def _wait_room(self, ahead):
        """Back-pressure: stop reading while ffmpeg is far enough ahead (ffmpeg then blocks on its output)."""
        while self.running and ahead() > AHEAD_S:
            time.sleep(0.05)

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
            t = self.start_s + self.n_frames / FPS
            self.n_frames += 1
            with self.frame_lock:
                self.frames.append((t, frame))
            # the picture may run a little further ahead than the sound, so the two never wait on each other
            self._wait_room(lambda: t - self.stream_time - 1.5)
        conn.close()

    def _presenter(self):
        """Hands over the frame that belongs to what is being heard now."""
        while self.running:
            now = self.stream_time
            due = None
            with self.frame_lock:
                while self.frames and self.frames[0][0] <= now:
                    due = self.frames.popleft()[1]
                if due is None and self.played == 0 and self.frames:
                    due = self.frames[0][1]          # show the first picture while the sound is buffering
            if due is not None:
                self.on_frame(due)
            time.sleep(1 / (2 * FPS))

    def _audio_reader(self, la):
        try:
            conn = la.accept()
        except OSError:
            return
        while self.running:
            data = conn.recv(16384)
            if not data:
                break
            with self.play_lock:
                self.play_buf.append(data)
                self.play_queued += len(data)
            self._wait_room(self.buffered)
        conn.close()

    def _play_cb(self, outdata, frames, _time, _status):
        need = frames * 4
        out = bytearray()
        with self.play_lock:
            if self.buffering:
                # (re)start only with a cushion - or at the end of a recording, with whatever is left
                if self.play_queued >= (REBUF_S if self.played else PREBUF_S) * BYTES_PER_S or (
                        self.eof and self.play_queued):
                    self.buffering = False
            elif self.play_queued < need and not self.eof:
                self.buffering = True                # ran dry: pause cleanly instead of playing scraps
                self.underruns += 1
            if not self.buffering:
                while len(out) < need and self.play_buf:
                    chunk = self.play_buf.popleft()
                    take = need - len(out)
                    out += chunk[:take]
                    if len(chunk) > take:
                        self.play_buf.appendleft(chunk[take:])
                    self.play_queued -= min(len(chunk), take)
                self.played += len(out)
        if len(out) < need:
            out += bytes(need - len(out))
        a = np.frombuffer(bytes(out), dtype=np.int16).reshape(-1, 2)
        if self.volume < 0.999:
            a = (a.astype(np.float32) * self.volume).astype(np.int16)
        outdata[:] = a
