"""Who is chairing right now: the Speaker, a deputy speaker (or the oldest MP at a constitutive sitting).

Evidence, strongest first:
  * a handover sentence ("predsedavanje preuzima potpredsednica Jelena Petrović")
  * the broadcast caption showing a leadership member while the chair's voice is talking
  * MPs addressing the chair by title + name ("predsedniče Čupiću", "potpredsednice Petrović")
  * Claude reading the context
  * weak: "gospodine Čupiću" without a title (2 needed), and "predsedavajuća/predsedavajući" for the gender
A clearly different voice giving the floor means the chair changed (speakers.py); the name then comes from
context again. Every change is kept in a timeline, so each moment of the session has its chair.
"""
import json
import os
import re

import names
from common import DATA, to_latin


def _load():
    try:
        with open(os.path.join(DATA, "rukovodstvo.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


LEAD = _load()
ROLES = {}
if LEAD.get("predsednik"):
    ROLES[LEAD["predsednik"]] = "predsednik Narodne skupštine"
for _n in LEAD.get("potpredsednici", []):
    _mp = names.roster_lookup(_n)
    ROLES[_n] = "potpredsednica Narodne skupštine" if _mp and _mp["gender"] == "F" \
        else "potpredsednik Narodne skupštine"
if LEAD.get("konstitutivna_sednica"):
    ROLES.setdefault(LEAD["konstitutivna_sednica"], "predsedavajući konstitutivne sednice (najstariji poslanik)")
LEAD_IDX = {i for i, mp in enumerate(names.ROSTER) if mp["name"] in ROLES}

_W = r"[a-zčćšžđ]"
TITLE = re.compile(r"\b(?:pot)?predsedni(?:če|ce|ku|ci|k|ca)\b|\bpredsedavaju(?:ći|ća|ćem|ćoj|ćeg|će|ci|ca)\b")
ADDR = re.compile(r"\b(?:gospodine|gospođo|gospodjo|poštovani|poštovana|uvaženi|uvažena|kolega|koleginice)\b")
HANDOVER = re.compile(r"predsedavanj\w*\s+(?:\w+\s+){0,2}(?:preuzim|preuzm|nastavl)\w*|"
                      r"preuz(?:im|m)\w*\s+(?:\w+\s+){0,2}predsedavanj\w*|predsedavać\w*|"
                      r"sednic\w*\s+(?:će\s+)?(?:dalje\s+)?predsedava\w*")
FEMALE = re.compile(r"\b(?:predsedavajuća|predsednice|potpredsednice)\b")
MALE = re.compile(r"\b(?:predsedniče|potpredsedniče)\b")      # "predsedavajući" is also used generically


class ChairTracker:
    def __init__(self):
        self.person = None          # name of whoever is chairing now (None = not known yet)
        self.source = ""
        self.timeline = []          # {"t", "name" (or None), "source"}; source "glas" = the voice changed
        self.votes = []             # (t, name, weight)
        self.gender_cues = []       # (t, "M"/"F")
        self.on_change = []         # callbacks(old, new, t, source)

    # ---- queries -------------------------------------------------------------------------------------
    @staticmethod
    def role(name):
        if name in ROLES:
            return ROLES[name]
        return "narodni poslanik" if name else ""

    def at(self, t):
        """Who chaired at moment t. Between two detected changes it is one person: if the name became known
        only later in that stretch, it applies from the stretch's start."""
        if not self.timeline:
            return None
        start = 0
        for k, e in enumerate(self.timeline):
            if e["name"] is None and e["t"] <= t:       # the chair changed (voice / gender): new stretch
                start = k
        end = next((k for k in range(start + 1, len(self.timeline)) if self.timeline[k]["name"] is None),
                   len(self.timeline))
        epoch = self.timeline[start:end]
        before = [e for e in epoch if e["t"] <= t and e["name"]]
        if before:
            return before[-1]["name"]
        later = [e for e in epoch if e["name"]]
        return later[0]["name"] if later else None

    def label(self, t=None):
        name = self.person if t is None else self.at(t)
        return f"{name} ({self.role(name)})" if name else "nije još prepoznat"

    # ---- evidence ------------------------------------------------------------------------------------
    def set(self, name, t, source):
        mp = names.roster_lookup(name)
        name = mp["name"] if mp else name
        if not name or name == self.person:
            return
        old, self.person, self.source = self.person, name, source
        self.timeline.append({"t": t, "name": name, "source": source})
        self.votes = []
        for cb in self.on_change:
            cb(old, name, t, source)

    def voice_changed(self, t):
        """A different voice took over the chair: the person is unknown until context names them."""
        old = self.person
        self.person, self.source = None, ""
        self.timeline.append({"t": t, "name": None, "source": "glas"})
        self.votes, self.gender_cues = [], []
        for cb in self.on_change:
            cb(old, None, t, "glas")
        return old

    def _vote(self, name, t, weight, source):
        self.votes = [v for v in self.votes if t - v[0] < 180] + [(t, name, weight)]
        total = sum(w for _t, n, w in self.votes if n == name)
        need = 2 if self.person is None else 4          # replacing a known chair needs more evidence
        g = self._gender()
        mp = names.roster_lookup(name)
        if g and mp and mp["gender"] != g and self.person is None:
            need += 1                                   # MPs talk to a woman/man in the chair - doesn't fit
        if total >= need:
            self.set(name, t, source)

    def _gender(self):
        f = sum(1 for _t, g in self.gender_cues if g == "F")
        m = sum(1 for _t, g in self.gender_cues if g == "M")
        if f >= 2 and f > 2 * m:
            return "F"
        if m >= 2 and m > 2 * f:
            return "M"
        return None

    def observe(self, text, chair_speaking, t):
        """One transcript segment. chair_speaking = it was labelled as the chair's voice."""
        lat = to_latin(text)
        low = lat.lower()
        hits = [(i, s, e, sc) for i, s, e, sc in names.find_names(lat) if i in LEAD_IDX and sc >= 1.0]
        if HANDOVER.search(low) and hits:
            i = max(hits, key=lambda h: h[3])[0]
            self.set(names.ROSTER[i]["name"], t, "najava promene predsedavajućeg")
            return
        if not chair_speaking:
            for i, s, _e, _sc in hits:
                before = low[max(0, s - 40):s]
                name = names.ROSTER[i]["name"]
                if TITLE.search(before):
                    self._vote(name, t, 2, "obraćanje predsedavajućem")       # "predsedniče Čupiću"
                elif ADDR.search(before[-14:]):
                    self._vote(name, t, 1, "obraćanje predsedavajućem")       # "gospodine Čupiću"
            for rx, g in ((FEMALE, "F"), (MALE, "M")):
                for _m in rx.finditer(low):
                    self.gender_cues = [c for c in self.gender_cues if t - c[0] < 180] + [(t, g)]
            g = self._gender()
            mp = names.roster_lookup(self.person) if self.person else None
            if g and mp and mp["gender"] != g:
                # MPs keep addressing a chair of the other gender: someone else took over
                self.person, self.source = None, ""
                self.timeline.append({"t": t, "name": None, "source": "obraćanje (drugi pol)"})
                for cb in self.on_change:
                    cb(mp["name"], None, t, "obraćanje (drugi pol)")

    def caption(self, name, t):
        """The broadcast caption shows `name` while the chair's voice is talking."""
        if name in ROLES:
            self.set(name, t, "natpis na ekranu")
            return True
        return False

    def from_judge(self, name, t):
        mp = names.roster_lookup(name)
        if mp:
            self._vote(mp["name"], t, 2 if mp["name"] in ROLES else 1, "Claude (kontekst)")
