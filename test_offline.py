"""Offline check: wav -> ASR -> speaker labels (no UI, no API). Usage: python test_offline.py file.wav [start_s]"""
import sys
import threading
import wave

import numpy as np

from common import fmt_time, load_settings, setup_env

setup_env()
sys.stdout.reconfigure(encoding="utf-8")
from asr import ASRWorker  # noqa: E402
from speakers import SpeakerTracker  # noqa: E402

wav, start = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
w = wave.open(wav)
pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
st = load_settings()
tracker = SpeakerTracker(st["voice_change_sim"])
tracker.chair.on_change.append(lambda o, n, t, src: print(f"   >> PREDSEDAVA [{fmt_time(t)}]: {o} -> {n} ({src})", flush=True))
done = threading.Event()


def on_seg(seg):
    lab = tracker.label(seg)
    flags = []
    if lab.voice_change:
        flags.append("PROMENA GLASA")
    if lab.loud_db >= st["shout_db"]:
        flags.append(f"GLASNO +{lab.loud_db:.0f}dB")
    if lab.chair_info.get("floor"):
        flags.append(f"REČ -> {lab.chair_info['floor']}")
    if lab.chair_info.get("measure"):
        flags.append(f"MERA PREDSEDAVAJUĆEG: {lab.chair_info['measure']}")
    print(f"[{fmt_time(seg.t0)}] {lab.who:28s} sim={lab.sim:+.2f} {seg.db:6.1f}dB | {seg.latin}"
          + (f"   <<{', '.join(flags)}>>" if flags else ""), flush=True)


asr = ASRWorker(st["whisper_model"], on_seg, lambda m: print("#", m))
asr.start()
step = 3200
for i in range(0, len(pcm), step):
    asr.push(pcm[i:i + step], start + i / 16000)
asr.push(np.zeros(16000 * 11, dtype=np.float32), start + len(pcm) / 16000)  # flush
asr.stop()
asr.join()
print("# turns:", [(t.name, fmt_time(t.since), t.limit_key) for t in tracker.turns])
