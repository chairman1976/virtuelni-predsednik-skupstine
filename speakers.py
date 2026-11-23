"""Who is speaking and since when.

Evidence, strongest first:
  1. the on-screen name caption of the official broadcast (videoocr.py, or Claude vision as a fallback)
  2. the chair giving the floor - "Reč ima narodna poslanica X", "Izvolite, kolega X", "Dajem reč poslaniku X",
     "X ima reč" - matched against all case forms of the 250 MPs' names (names.py); the next voice that is not
     the chair is very probably X
  3. gender: the role words ("poslanica"/"poslanik"), the MP roster, and the speaker's own grammar
     ("rekla sam"/"rekao sam") - an announced woman is not attached to a voice that talks about itself as a man
  4. voiceprints (TitaNet embeddings): recognise people again, notice interruptions and heckling
Voice pitch is NOT used for gender: in the hall an angry man easily reaches 270-300 Hz (measured on 19.11.2026),
higher than female speakers; it is used for "raised voice" instead (pitch well above the person's own normal).
"""
import difflib
from dataclasses import dataclass, field

import numpy as np

import names
from common import to_latin
from rules import LIMITS, limit_key, parse_chair

CHAIR = "Predsedavajući"
ANON = "Glas "          # unnamed voice clusters: "Glas 1", "Glas 2", ...


@dataclass
class Voice:
    name: str
    centroid: object = None
    n: int = 0
    dbs: list = field(default_factory=list)
    f0s: list = field(default_factory=list)
    gm: int = 0                                  # grammar votes: talks about self as a man / woman
    gf: int = 0
    chair_marks: int = 0                         # times this voice said chair phrases
    last_t: float = -1e9                         # when this voice was last heard

    def add(self, emb, db, f0=None, gm=0, gf=0):
        if emb is not None:
            if self.centroid is None:
                self.centroid = emb.copy()
            else:
                w = min(self.n, 30)
                c = (self.centroid * w + emb) / (w + 1)
                self.centroid = c / (np.linalg.norm(c) + 1e-9)
            self.n += 1
        self.dbs = (self.dbs + [db])[-60:]
        if f0:
            self.f0s = (self.f0s + [f0])[-60:]
        self.gm += gm
        self.gf += gf

    def sim(self, emb):
        if self.centroid is None or emb is None or self.n < (1 if self.anon else 2):
            return None
        return float(np.dot(self.centroid, emb))

    @property
    def anon(self):
        return self.name.startswith(ANON) or self.name.startswith("Govornik ")

    @property
    def gender(self):
        """From the person's own grammar, only when it is clear."""
        if self.gm - self.gf >= 2:
            return "M"
        if self.gf - self.gm >= 2:
            return "F"
        return None

    @property
    def baseline_db(self):
        return float(np.median(self.dbs)) if len(self.dbs) >= 4 else None

    @property
    def baseline_f0(self):
        return float(np.median(self.f0s)) if len(self.f0s) >= 4 else None


@dataclass
class Turn:
    name: str
    role: str
    since: float
    limit_key: str = None
    source: str = "chair"        # caption | chair | voice
    party: str = ""
    gender: str = None
    confirmed: bool = False      # the on-screen caption showed this name

    @property
    def limit(self):
        return LIMITS.get(self.limit_key)


@dataclass
class Labeled:
    who: str
    is_chair: bool
    voice_change: bool
    loud_db: float               # dB above this person's normal level (0 if unknown)
    sim: float
    chair_info: dict
    pitch_up: float = 0.0        # % above this person's normal pitch (raised / shouting voice)
    gender_conflict: bool = False


def _compatible(g1, g2):
    return g1 is None or g2 is None or g1 == g2


class SpeakerTracker:
    def __init__(self, sim_threshold=0.55):
        self.thr = sim_threshold
        self.voices = {CHAIR: Voice(CHAIR)}       # name -> Voice
        self.turn = None
        self.turns = []
        self.anon_count = 0
        self.run = None                           # (voice, since) of an unannounced voice still talking
        self.last_voice = (None, -1e9)            # last non-chair voice heard, and when
        self.pending = None                       # floor given without a name: (role, limit, gender, t)
        self.aliases = {}                         # old label -> new name (Govornik 2 -> Jovo Bakić)

    # ---- names --------------------------------------------------------------------------------------
    def canonical(self, name):
        """Roster spelling if it is an MP, else merge ASR spelling variants of names already seen."""
        mp = names.roster_lookup(name)
        if mp:
            return mp["name"]
        best, score = None, 0.0
        for k, v in self.voices.items():
            if k == CHAIR or v.anon:
                continue
            r = difflib.SequenceMatcher(None, k.lower(), name.lower()).ratio()
            if r > score:
                best, score = k, r
        return best if score >= 0.84 else name

    def voice(self, name):
        if name not in self.voices:
            self.voices[name] = Voice(name)
        return self.voices[name]

    def _rename(self, old, new):
        """Voice `old` turns out to be `new` (merge if `new` already has a voice)."""
        if old == new or old not in self.voices:
            return
        self.aliases[old] = new
        v = self.voices.pop(old)
        if new in self.voices:
            t = self.voices[new]
            if v.centroid is not None:
                c = (t.centroid * t.n + v.centroid * v.n) if t.centroid is not None else v.centroid
                t.centroid = c / (np.linalg.norm(c) + 1e-9)
            t.n += v.n
            t.dbs, t.f0s = (t.dbs + v.dbs)[-60:], (t.f0s + v.f0s)[-60:]
            t.gm, t.gf = t.gm + v.gm, t.gf + v.gf
        else:
            v.name = new
            self.voices[new] = v
        for tr in self.turns:
            if tr.name == old:
                tr.name = new
        if self.last_voice[0] == old:
            self.last_voice = (new, self.last_voice[1])

    def start_turn(self, name, role, t, limit=None, source="chair", party="", gender=None):
        name = self.canonical(name)
        mp = names.roster_lookup(name)
        if mp:
            gender = gender or mp["gender"]
        if self.turn and self.turn.name == name and t - self.turn.since < 20:
            if limit:
                self.turn.limit_key = limit
            return self.turn
        self.turn = Turn(name, role, t, limit, source, party, gender)
        self.turns.append(self.turn)
        self.voice(name)
        return self.turn

    def set_caption(self, name, party, role, t, gender=None):
        """Name shown on screen = the person speaking now. Strongest evidence."""
        if not name:
            return
        name = self.canonical(name)
        mp = names.roster_lookup(name)
        gender = gender or (mp["gender"] if mp else None)
        lv, lt = self.last_voice
        if self.turn and self.turn.name == name:
            self.turn.confirmed, self.turn.source = True, "caption"
            self.turn.party = party or self.turn.party
            return
        if self.turn and self.turn.source == "chair" and t - self.turn.since < 12 \
                and not self.turn.name.startswith("Govornik "):
            return          # the chair just named someone; the caption may still show the previous speaker
        if self.turn and self.turn.name.startswith("Govornik ") and self.turn.name in self.voices \
                and t - self.turn.since < 60:
            # floor was given without a name we understood - the caption tells who it is
            self._rename(self.turn.name, name)
            self.turn.confirmed, self.turn.source = True, "caption"
            self.turn.party = party or self.turn.party
            self.turn.gender = gender or self.turn.gender
            return
        if self.pending and t - self.pending[3] < 30:
            role = role or self.pending[0]
            limit = self.pending[1]
            self.pending = None
        else:
            limit = None
        if lv and lv != CHAIR and t - lt < 10:
            v = self.voices.get(lv)
            if v is not None and (v.anon or not (self.turn and self.turn.confirmed)) \
                    and _compatible(v.gender, gender):
                self._rename(lv, name)                          # this voice is that person
        turn = self.start_turn(name, role, t if not self.turn or self.turn.name != name else self.turn.since,
                               limit=limit, source="caption", party=party, gender=gender)
        turn.confirmed, turn.source = True, "caption"
        turn.party = party or turn.party

    # ---- labelling one transcript segment ---------------------------------------------------------------
    def _sims(self, emb):
        out = {}
        if emb is not None:
            for nm, v in self.voices.items():
                s = v.sim(emb)
                if s is not None:
                    out[nm] = s
        return out

    def _merge_into_chair(self, emb):
        """A voice that says chair phrases is the chair: unnamed clusters ("Glas N", "Govornik N") that match are
        folded into the chair's voice; named voices only get a mark (never renamed to someone else later)."""
        for nm in [n for n in self.voices if n != CHAIR]:
            v = self.voices[nm]
            s = v.sim(emb)
            if s is None or s < self.thr:
                continue
            v.chair_marks += 1
            if v.name.startswith(ANON) or (v.name.startswith("Govornik ") and s >= self.thr + 0.05):
                self._rename(nm, CHAIR)
                if self.turn and self.turn.name == CHAIR:
                    self.turn = None             # that "speaker" was the chair all along

    def _may_take_name(self, nm, since):
        """Can unnamed voice `nm` be the person the chair just announced (turn starting at `since`)?"""
        v = self.voices.get(nm)
        if v is None or not v.anon or v.chair_marks:
            return False                         # it spoke like the chair
        ch = self.voices[CHAIR]
        if ch.centroid is not None and v.centroid is not None and float(np.dot(ch.centroid, v.centroid)) >= self.thr - 0.1:
            return False                         # it sounds like the chair
        if v.last_t >= since - 4:
            return False                         # it was talking right up to the announcement (chair / previous)
        return True

    def _floor(self, text):
        """The chair giving someone the floor: (name, role, limit, gender) or None."""
        lat = to_latin(text)
        ann = names.find_announced(text)
        info = parse_chair(text)
        role = info.get("floor", ("", "", None))[0]
        if ann:
            mp, _score, g = ann
            return mp["name"], role or ("narodna poslanica" if mp["gender"] == "F" else "narodni poslanik"), \
                limit_key(lat, role), mp["gender"]
        if "floor" in info:                    # not an MP (minister, guest...) - regex name
            role, nm, lim = info["floor"]
            return nm, role, lim, names.role_gender(lat)
        return None

    def label(self, seg):
        info = parse_chair(seg.text)
        floor = self._floor(seg.text)
        if floor:
            info["floor"] = floor[:3]
            info["chair"] = True
        sims = self._sims(seg.emb)
        chair_v = self.voices[CHAIR]
        cur = self.voices.get(self.turn.name) if self.turn else None
        strong = "floor" in info or "measure" in info
        if info.get("chair") and not strong and chair_v.n >= 3 and cur is not None:
            # weak marker ("zahvaljujem", "nastavljamo"...) - trust the voice over the words
            cs, ms = sims.get(CHAIR), sims.get(cur.name)
            if cs is not None and ms is not None and ms >= self.thr and ms > cs:
                info = {k: v for k, v in info.items() if k != "chair"}
        gm, gf = names.speech_gender(seg.text)
        if info.get("chair"):
            loud = self._loud(chair_v, seg.db)
            if seg.emb is not None:
                self._merge_into_chair(seg.emb)
            chair_v.add(seg.emb, seg.db, seg.f0)           # no grammar votes: the chair talks about others
            chair_v.last_t = seg.t1
            lat = to_latin(seg.text)
            if floor:
                nm, role, lim, g = floor
                self.start_turn(nm, role, seg.t1, lim, gender=g)
                self.pending = None
            elif names.FLOOR_AFTER.search(lat) or names.FLOOR_BEFORE.search(lat):
                # floor given but no name understood: the next new voice gets this turn
                self.pending = (names.ROLE.search(lat).group(0) if names.ROLE.search(lat) else "",
                                limit_key(lat), names.role_gender(lat), seg.t1)
            return Labeled(CHAIR, True, False, loud, 1.0, info, self._pitch_up(chair_v, seg.f0))

        seg_gender = "M" if gm > gf else "F" if gf > gm else None
        who, change, sim = None, False, 0.0
        chair_sim = sims.get(CHAIR) or -1.0
        if self.pending and seg.t0 - self.pending[3] < 20 and _compatible(seg_gender, self.pending[2]) \
                and not (chair_sim >= self.thr and chair_sim >= max(sims.values(), default=-1.0)):
            role, lim, g, _ = self.pending
            self.pending = None
            others = {k: s for k, s in sims.items() if k != CHAIR}
            best = max(others, key=others.get) if others else None
            if best and others[best] >= self.thr and (not self.voices[best].anon
                                                      or self._may_take_name(best, seg.t0 - 5)):
                nm = best
                if best.startswith(ANON):
                    nm = f"Govornik {len(self.turns) + 1}"
                    self._rename(best, nm)
            else:
                nm = f"Govornik {len(self.turns) + 1}"
            self.start_turn(nm, role, seg.t0, lim, source="chair", gender=g)
            cur = self.voices.get(self.turn.name)
        turn_g = self.turn.gender if self.turn else None
        if cur is not None and cur.n < 3:
            # still learning the announced speaker's voice: keep the chair's (and wrong-gender) voices out
            others = {k: s for k, s in sims.items() if k not in (CHAIR, cur.name)}
            ob = max(others, key=others.get) if others else None
            if chair_sim >= self.thr and chair_sim >= others.get(ob, -1.0):
                who, sim, change = CHAIR, chair_sim, True
            elif ob and others[ob] >= self.thr and self._may_take_name(ob, self.turn.since) \
                    and _compatible(self.voices[ob].gender, turn_g) and _compatible(seg_gender, turn_g):
                # the announced person is a voice we already heard unnamed -> give it the name
                self._rename(ob, cur.name)
                cur = self.voices[cur.name]
                who, sim = cur.name, others[ob]
            elif ob and others[ob] >= self.thr:
                who, sim, change = ob, others[ob], True                # someone else we know (heckler...)
            elif not _compatible(seg_gender, turn_g):
                self.anon_count += 1                                    # wrong gender for the announced name
                who, change = f"{ANON}{self.anon_count}", True
            else:
                who = cur.name
        elif sims:
            cur_sim = sims.get(cur.name) if cur else None
            best = max(sims, key=sims.get)
            if cur_sim is not None and cur_sim >= self.thr and cur_sim >= sims[best] - 0.05:
                who, sim = cur.name, cur_sim
            elif sims[best] >= self.thr:
                who, sim = best, sims[best]
                change = cur is not None and best != cur.name
        if who is None and seg.emb is not None and (cur is not None and cur.n >= 3):
            # a voice nobody knows: heckler, unannounced speaker, or the chair before the first "reč ima"
            self.anon_count += 1
            who, change = f"{ANON}{self.anon_count}", True
        if who is None and cur is not None:
            who = cur.name                                       # too short to judge the voice
        if who is None:                                          # nobody named yet
            who = f"Govornik {len(self.turns) + 1}"
            self.start_turn(who, "", seg.t0, source="voice")
        # a different (non-chair) voice that keeps talking is a new, unannounced turn
        if change and who != CHAIR:
            if self.run is None or self.run[0] != who:
                self.run = (who, seg.t0)
            elif seg.t1 - self.run[1] >= 12:
                start = self.run[1]
                if who.startswith(ANON):
                    newname = f"Govornik {len(self.turns) + 1}"
                    self._rename(who, newname)
                    who = newname
                self.start_turn(who, "", start, source="voice")
                self.run, change = None, False
        elif not change:
            self.run = None
        v = self.voice(who)
        v.last_t = seg.t1
        loud = self._loud(v, seg.db)
        up = self._pitch_up(v, seg.f0)
        v.add(seg.emb, seg.db, seg.f0, gm, gf)
        if who != CHAIR:
            self.last_voice = (who, seg.t1)
        mp = names.roster_lookup(who) if not v.anon and who != CHAIR else None
        conflict = bool(mp and v.gender and v.gender != mp["gender"])
        return Labeled(who, who == CHAIR, change, loud, sim, info, up, conflict)

    @staticmethod
    def _loud(v, db):
        b = v.baseline_db if v else None
        return 0.0 if b is None else max(0.0, db - b)

    @staticmethod
    def _pitch_up(v, f0):
        b = v.baseline_f0 if v else None
        return 0.0 if (b is None or not f0) else max(0.0, (f0 / b - 1) * 100)

    def final_name(self, label):
        """Name a label finally turned into (follows renames: Glas 3 -> Govornik 2 -> Jovo Bakić)."""
        seen = set()
        while label in self.aliases and label not in seen:
            seen.add(label)
            label = self.aliases[label]
        return label

    def elapsed(self, t):
        return 0.0 if self.turn is None else max(0.0, t - self.turn.since)
