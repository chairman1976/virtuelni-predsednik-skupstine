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
from common import log, to_latin
from chair import ChairTracker
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


# Strict matching - mixing two people is worse than splitting one person (a later caption / announcement
# merges the pieces back, and all their statistics move with them).
MARGIN = 0.06         # the best voice must beat the second best by this much, else it's ambiguous
LEARN = 0.08          # a voiceprint learns only from segments matching it by thr + LEARN ...
MIN_LEARN_S = 2.0     # ... that are at least this long (short embeddings are noisy)


def _compatible(g1, g2):
    return g1 is None or g2 is None or g1 == g2


class SpeakerTracker:
    def __init__(self, sim_threshold=0.55):
        self.on_rename = None        # callback(old, new) - the app re-attributes stats
        self.thr = sim_threshold
        self.voices = {CHAIR: Voice(CHAIR)}       # name -> Voice
        self.turn = None
        self.turns = []
        self.anon_count = 0
        self.run = None                           # (voice, since) of an unannounced voice still talking
        self.last_voice = (None, -1e9)            # last non-chair voice heard, and when
        self.last_any = (None, -1e9)              # last voice of anyone, the chair included
        self.chair = ChairTracker()               # WHO is chairing (the role is CHAIR)
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

    def _rename(self, old, new, force=False):
        """Voice `old` turns out to be `new` (merge if `new` already has a voice). Returns False if refused.

        Two well-learned voiceprints that clearly differ are never merged (unless forced by on-screen evidence):
        one wrong guess (e.g. Claude taking the MP someone addresses - "gospodine Krizmaniću" - for the speaker)
        would otherwise give that MP's name to another person's voice for the rest of the session."""
        if old == new or old not in self.voices:
            return False
        if new in self.voices and not force:
            a, b = self.voices[old], self.voices[new]
            if a.centroid is not None and b.centroid is not None and a.n >= 3 and b.n >= 3                     and float(np.dot(a.centroid, b.centroid)) < self.thr:
                log(f"voices: not merging {old} into {new} (different voices, "
                    f"{float(np.dot(a.centroid, b.centroid)):.2f})")
                return False
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
        if self.on_rename:
            self.on_rename(old, new)
        return True

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
        who, when = self.last_any
        if who == CHAIR and t - when < 6 and self.chair.caption(name, t):
            return                                  # the chair's own caption: who is chairing, not a new turn
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
            self._rename(self.turn.name, name, force=True)
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
            # an unnamed voice gets the name. A voice that already has a name is NOT renamed here: that renamed
            # all its lines, back to the start (a caption for Vučić turned the real Krizmanić's earlier speech
            # into Vučić's too) - the app splits such a voice line by line (Main.conflict_evidence)
            if v is not None and v.anon and _compatible(v.gender, gender):
                self._rename(lv, name, force=True)              # this voice is that person
        turn = self.start_turn(name, role, t if not self.turn or self.turn.name != name else self.turn.since,
                               limit=limit, source="caption", party=party, gender=gender)
        turn.confirmed, turn.source = True, "caption"
        turn.party = party or turn.party

    def move_voice(self, old, new, centroid_old=None, n_old=0, centroid_new=None, n_new=0):
        """Voice `old` is really `new` (a correction): the voiceprint goes to `new` - merged if `new` has one.
        No alias: lines labelled `old` before the correction stay `old` unless the app rewrites them.
        With centroid_old/centroid_new (a split): `old` keeps its own part, `new` gets the other part."""
        if old == new or old not in self.voices:
            return False
        v = self.voices[old]
        if centroid_old is not None:                      # split: old keeps the rest of its voice
            v.centroid, v.n = centroid_old, max(1, n_old)
            src = Voice(new)
            src.centroid, src.n = centroid_new, max(1, n_new)
        else:
            src = self.voices.pop(old)
        if new in self.voices:
            t = self.voices[new]
            if src.centroid is not None:
                c = (t.centroid * t.n + src.centroid * src.n) if t.centroid is not None else src.centroid
                t.centroid = c / (np.linalg.norm(c) + 1e-9)
            t.n += src.n
            if centroid_old is None:
                t.dbs, t.f0s = (t.dbs + src.dbs)[-60:], (t.f0s + src.f0s)[-60:]
        else:
            src.name = new
            self.voices[new] = src
        for a in ("last_voice", "last_any"):
            who, when = getattr(self, a)
            if who == old and centroid_old is None:
                setattr(self, a, (new, when))
        if self.run and self.run[0] == old:
            self.run = None
        return True

    def set_face(self, name, t, voice):
        """Face recognition: the face whose lips move on screen at t is MP `name`, while `voice` was talking.

        Weaker than the caption (the director may show someone else): it only names an unnamed voice, or votes
        for who is chairing. A named voice is never renamed by a face. Returns what was done."""
        name = self.canonical(name)
        if not voice:
            return "no voice"
        voice = self.final_name(voice)
        if voice == CHAIR:
            self.chair._vote(name, t, 1, "lice na ekranu")
            return "chair vote"
        if voice == name:
            if self.turn and self.turn.name == name:
                self.turn.face = True
            return "same"
        if self.chair.person and self.canonical(self.chair.person) == name:
            # the face of whoever is chairing: an unnamed voice talking now is the chair's voice, not an MP
            v = self.voices.get(voice)
            if v is not None and v.anon:
                self._rename(voice, CHAIR, force=True)
                return "chair voice"
            return "chair"
        v = self.voices.get(voice)
        if v is None or not v.anon:
            return "named voice"                 # e.g. a reaction shot while someone else talks
        mp = names.roster_lookup(name)
        if mp and not _compatible(v.gender, mp["gender"]):
            return "gender"
        if not self._rename(voice, name):
            return "different voices"
        if self.turn and self.turn.name == name:
            self.turn.face = True
            if self.turn.source == "voice":
                self.turn.source = "face"
            if mp:
                self.turn.gender = self.turn.gender or mp["gender"]
        return "named"

    # ---- labelling one transcript segment ---------------------------------------------------------------
    def _sims(self, emb):
        out = {}
        if emb is not None:
            for nm, v in self.voices.items():
                s = v.sim(emb)
                if s is not None:
                    out[nm] = s
        return out

    def _new_chair_voice(self, t):
        """Someone else took the chair. The previous chair keeps the voiceprint learned while chairing (under
        their own name, if known - so they are recognised when they later speak as an MP); the chair's voice is
        learned anew. Never one voiceprint for two people."""
        old_person = self.chair.voice_changed(t)
        v = self.voices[CHAIR]
        if old_person and old_person not in self.voices and v.centroid is not None:
            v.name, v.chair_marks = old_person, 0
            self.voices[old_person] = v
        self.voices[CHAIR] = Voice(CHAIR)

    def _clear_best(self, sims, exclude=()):
        """(name, sim) of the best matching voice if it is above the threshold AND clearly ahead of the next one."""
        c = sorted(((s, n) for n, s in sims.items() if n not in exclude), reverse=True)
        if not c or c[0][0] < self.thr:
            return None, (c[0][0] if c else -1.0)
        if len(c) > 1 and c[0][0] - c[1][0] < MARGIN:
            return None, c[0][0]                 # two voices fit about equally - don't guess
        return c[0][1], c[0][0]

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
                self._rename(nm, CHAIR, force=True)
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
        prev = getattr(self, "prev_chair", None)
        if not floor and prev and seg.t0 - prev[1] < 2.5:
            # Whisper split the announcement: "... sledeći na redu. Aleksandar." + "Vučić ima reč."
            floor = self._floor(prev[0] + " " + seg.text)
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
            cs = sims.get(CHAIR)
            if strong and chair_v.n >= 5 and cs is not None and cs < self.thr - 0.15:
                # a clearly different voice is running the sitting: the chair changed hands
                self._new_chair_voice(seg.t0)
                chair_v = self.voices[CHAIR]
            ok = strong or chair_v.n < 3 or (cs is not None and cs >= self.thr + LEARN)
            chair_v.add(seg.emb if ok else None, seg.db, seg.f0)   # no grammar votes: the chair talks about others
            chair_v.last_t = seg.t1
            self.last_any = (CHAIR, seg.t1)
            self.prev_chair = (seg.text, seg.t1)
            self.chair.observe(seg.text, True, seg.t0)
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
            best, _bs = self._clear_best(sims, exclude=(CHAIR,))
            if best and (not self.voices[best].anon or self._may_take_name(best, seg.t0 - 5)):
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
            ob, _obs = self._clear_best(others)
            own = float(np.dot(cur.centroid, seg.emb)) if (cur.centroid is not None and seg.emb is not None) \
                else None                                       # vs. the first segments we learned
            if chair_sim >= self.thr and chair_sim >= max(others.values(), default=-1.0):
                who, sim, change = CHAIR, chair_sim, True
            elif ob and self._may_take_name(ob, self.turn.since) \
                    and _compatible(self.voices[ob].gender, turn_g) and _compatible(seg_gender, turn_g):
                # the announced person is a voice we already heard unnamed -> give it the name
                self._rename(ob, cur.name)
                cur = self.voices[cur.name]
                who, sim = cur.name, others[ob]
            elif ob:
                who, sim, change = ob, others[ob], True                # someone else we know (heckler...)
            elif not _compatible(seg_gender, turn_g) or (own is not None and own < self.thr):
                # wrong gender, or a different voice than the one we started learning: not the announced person
                self.anon_count += 1
                who, change = f"{ANON}{self.anon_count}", True
            else:
                who, sim = cur.name, (own if own is not None else 0.0)
        elif sims:
            cur_sim = sims.get(cur.name) if cur else None
            top = max(sims.values())
            best, _bs = self._clear_best(sims)
            if cur_sim is not None and cur_sim >= self.thr and cur_sim >= top - 0.02:
                who, sim = cur.name, cur_sim
            elif best:
                who, sim = best, sims[best]
                change = cur is not None and best != cur.name
            # else: nobody clearly - becomes a new unknown voice below (never glued to the nearest one)
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
                ann = self.turn if (self.turn and self.turn.source != "voice" and self.turn.name != CHAIR) else None
                av, wv = (self.voices.get(ann.name) if ann else None), self.voices.get(who)
                if ann and av is not None and wv is not None and av.n <= 2 and start - ann.since < 40 \
                        and who.startswith(ANON) and not wv.chair_marks \
                        and _compatible(wv.gender, ann.gender):
                    # the announced speaker barely has a voiceprint (seeded from a shout / noise) and one new
                    # voice now talks steadily right after the announcement: that voice IS the announced person
                    av.centroid, av.n = None, 0
                    self._rename(who, ann.name)
                    who = ann.name
                    self.run, change = None, False
                    v = self.voice(who)
                    v.last_t = seg.t1
                    loud, up = self._loud(v, seg.db), self._pitch_up(v, seg.f0)
                    v.add(seg.emb, seg.db, seg.f0, gm, gf)
                    self.last_voice = (who, seg.t1)
                    return Labeled(who, False, False, loud, sim, info, up, False)
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
        self.last_any = (who, seg.t1)
        if who != CHAIR:
            self.chair.observe(seg.text, False, seg.t0)
        loud = self._loud(v, seg.db)
        up = self._pitch_up(v, seg.f0)
        learn = seg.emb is not None and (v.n == 0 or (seg.t1 - seg.t0 >= MIN_LEARN_S
                                                       and (v.n < 3 or sim >= self.thr + LEARN)))
        v.add(seg.emb if learn else None, seg.db, seg.f0, gm, gf)
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
