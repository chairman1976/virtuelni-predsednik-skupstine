"""Keeps a session's state on disk, so closing the app (or a crash) does not lose the transcript, measures,
statistics, voices and checks: opening the same video again offers to continue where it stopped.

The state is a pickle next to the session log (sessions/<stamp>_<title>.state.pkl), written every minute and
on stop; sessions/resume.json maps a video id to its newest state file. Only files the app wrote itself are
loaded.
"""
import json
import os
import pickle
import re
import time

from common import SESSIONS, log

VERSION = 1
INDEX = os.path.join(SESSIONS, "resume.json")

# app attributes that make up a session (everything else is rebuilt from these)
FIELDS = ["incidents", "tx_items", "real_measures", "claims", "rhetoric", "tones", "recent_lines", "segments",
          "last_context", "covered", "cur_stretch", "official_timer"]


def video_key(url):
    """YouTube video id (the same for a whole live broadcast), else the URL itself."""
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})", url or "")
    return m.group(1) if m else (url or "").strip()


def _index():
    try:
        with open(INDEX, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def find(url):
    """Info about the saved session of this video, or None: dict(path, pos, live, saved, lines, measures)."""
    ent = _index().get(video_key(url))
    if not ent or not os.path.exists(ent.get("path", "")):
        return None
    return ent


def save(w):
    """Write the session of app window w (called in the GUI thread; a few ms)."""
    tr = w.tracker
    on_rename, on_change = tr.on_rename, tr.chair.on_change
    tr.on_rename, tr.chair.on_change = None, []          # bound methods of the window: not part of the state
    pos = w.paused_at if w.paused else (w.player.stream_time if w.player else w.cur_stretch[1])
    turns = {id(x): i for i, x in enumerate(tr.turns)}
    st = {
        "version": VERSION, "url": w.s["last_url"], "title": w.src["title"], "live": w.src["is_live"],
        "saved": time.time(), "pos": pos, "log_path": w.log_path, "tracker": tr,
        "clock": "broadcast" if w.src["is_live"] else "media",   # live: times are broadcast time
        "fields": {k: getattr(w, k) for k in FIELDS},
        # turns are flagged by id() - store their index instead
        "flagged": [("official", turns[x[1]]) if isinstance(x, tuple) else turns[x]
                    for x in w.turn_flagged if (x[1] if isinstance(x, tuple) else x) in turns],
        "profiles": {k: v for k, v in w.profiles.items() if v},
        "chair_html": w.chair.toHtml(),
        "agenda": w.agenda.text(),
        "agenda_items": [w.agenda_box.itemData(i) for i in range(w.agenda_box.count())],
        "agenda_tip": w.agenda_box.toolTip(),
        "cost": (w.judge.cost if w.judge else 0.0), "calls": (w.judge.calls if w.judge else 0),
        "fact_cost": (w.facter.cost if w.facter else 0.0), "checked": (w.facter.checked if w.facter else 0),
    }
    path = w.log_path[:-6] + ".state.pkl"
    try:
        with open(path + ".tmp", "wb") as f:
            pickle.dump(st, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(path + ".tmp", path)
        idx = _index()
        idx[video_key(st["url"])] = {"path": path, "pos": pos, "live": st["live"], "saved": st["saved"],
                                     "title": st["title"], "lines": len(w.segments),
                                     "measures": len(w.app_measures)}
        with open(INDEX + ".tmp", "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        os.replace(INDEX + ".tmp", INDEX)
    except (OSError, pickle.PicklingError, TypeError, AttributeError) as e:
        log("session save:", e)
    finally:
        tr.on_rename, tr.chair.on_change = on_rename, on_change


def shift_times(st, dt):
    """Move every time in a saved session by dt seconds (an older live session timed from when the app
    started -> broadcast time)."""
    f = st["fields"]
    for g in f["segments"]:
        g["t0"] += dt
        g["t1"] += dt
    for it in f["tx_items"]:
        for k in ("t", "seek", "resume"):
            if k in it:
                it[k] += dt
    for k in ("incidents", "real_measures", "claims", "rhetoric", "tones"):
        for x in f[k]:
            if "t" in x:
                x["t"] += dt
    f["last_context"] = {k: v + dt for k, v in f["last_context"].items()}
    for iv in f["covered"]:                 # cur_stretch is one of these lists (same object): shifted once
        iv[0] += dt
        iv[1] += dt
    tr = st["tracker"]
    for t in tr.turns:
        t.since += dt
    for v in tr.voices.values():
        if getattr(v, "last_t", None) is not None:
            v.last_t += dt
    tr.last_voice = (tr.last_voice[0], tr.last_voice[1] + dt)
    tr.last_any = (tr.last_any[0], tr.last_any[1] + dt)
    tr.run = (tr.run[0], tr.run[1] + dt) if tr.run else None
    tr.pending = (*tr.pending[:3], tr.pending[3] + dt) if tr.pending else None
    ch = tr.chair
    for e in ch.timeline:
        e["t"] += dt
    ch.votes = [(t + dt, n, w) for t, n, w in ch.votes]
    ch.gender_cues = [(t + dt, g) for t, g in ch.gender_cues]
    st["pos"] += dt
    st["clock"] = "broadcast"


def load(path):
    with open(path, "rb") as f:
        st = pickle.load(f)
    if st.get("version") != VERSION:
        raise ValueError("stara verzija zapisa sesije")
    return st
