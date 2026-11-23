"""Poslovnik Narodne skupštine - order-keeping rules (čl. 96, 103-116) as code.

Ladder (čl. 108-111): opomena -> (after 2 opomene) oduzimanje reči -> udaljenje sa sednice.
Fines (čl. 114): 1 opomena 10 %, 2 opomene 20 %, oduzimanje reči 40 %, udaljenje 50 % of the base
MP salary; only the highest applies. Opomena / oduzimanje are counted per day (čl. 113).
"""
import os
import re
from dataclasses import dataclass, field

from common import DATA, to_latin

RULES_TEXT = open(os.path.join(DATA, "poslovnik_red.txt"), encoding="utf-8").read()

# kind -> (label, article reference, needs a prior warning before opomena)
KINDS = {
    "direct_address": ("Neposredno obraćanje drugom poslaniku", "čl. 107 st. 2, čl. 109 al. 5", False),
    "insult": ("Uvredljivi izrazi", "čl. 107, čl. 109 al. 6", False),
    "profanity": ("Psovke", "čl. 109 al. 6", False),
    "private_life": ("Iznošenje privatnog života drugih", "čl. 107 st. 2, čl. 109 al. 7", False),
    "off_topic": ("Govor van tačke dnevnog reda", "čl. 106 st. 1, čl. 109 al. 3", True),
    "heckling": ("Dobacivanje / ometanje govornika", "čl. 106 st. 3, čl. 109 al. 4", False),
    "no_floor": ("Govor bez dobijene reči", "čl. 105 st. 1, čl. 109 al. 2", False),
    "shouting": ("Vikanje / povišen ton - narušavanje dostojanstva", "čl. 107 st. 1, čl. 109 al. 8", True),
    "point_of_order_abuse": ("Zloupotreba povrede Poslovnika", "čl. 103 st. 4 i 7", False),
    "time_exceeded": ("Prekoračeno vreme govora", "čl. 96, 103, 104", True),
    "disorder": ("Drugo narušavanje reda", "čl. 109 al. 8", False),
    "physical_attack": ("Fizički napad", "čl. 111 st. 2", False),
}

MEASURES = {
    "upozorenje": ("UPOZORENJE", 0),
    "opomena": ("OPOMENA", 10),
    "opomena2": ("DRUGA OPOMENA", 20),
    "oduzimanje": ("ODUZIMANJE REČI", 40),
    "udaljenje": ("UDALJENJE SA SEDNICE", 50),
}

# Time limits (seconds) by the kind of turn the chair gives
LIMITS = {
    "replika": 120,          # čl. 104 st. 4
    "povreda": 120,          # čl. 103 st. 4
    "izvestilac": 300,       # čl. 96
    "grupa": 1200,           # čl. 96 - predsednik / predstavnik poslaničke grupe
    "bez_grupe": 300,        # čl. 96 - poslanici koji nisu članovi grupa
}


@dataclass
class MPRecord:
    name: str
    warnings: dict = field(default_factory=dict)   # kind -> count (upozorenja)
    opomene: int = 0
    oduzimanje: bool = False
    udaljenje: bool = False
    events: list = field(default_factory=list)

    @property
    def fine_pct(self):
        if self.udaljenje:
            return 50
        if self.oduzimanje:
            return 40
        return {0: 0, 1: 10}.get(self.opomene, 20)


class Discipline:
    """Turns detected violations into the measure the chair should pronounce."""

    def __init__(self):
        self.mps = {}

    def record(self, name):
        key = name.strip().lower()
        if key not in self.mps:
            self.mps[key] = MPRecord(name.strip())
        return self.mps[key]

    def decide(self, name, kind):
        r = self.record(name)
        label, article, needs_warning = KINDS.get(kind, KINDS["disorder"])
        if kind == "physical_attack":
            m = "udaljenje"
        elif r.oduzimanje or r.udaljenje:
            m = "udaljenje"                       # čl. 111 st. 1
        elif needs_warning and not r.warnings.get(kind) and r.opomene == 0:
            m = "upozorenje"                      # čl. 109 al. 3: "i pored upozorenja"
        elif r.opomene >= 2:
            m = "oduzimanje"                      # čl. 110
        elif r.opomene == 1:
            m = "opomena2"
        else:
            m = "opomena"
        # apply
        if m == "upozorenje":
            r.warnings[kind] = r.warnings.get(kind, 0) + 1
        elif m in ("opomena", "opomena2"):
            r.opomene += 1
        elif m == "oduzimanje":
            r.oduzimanje = True
        elif m == "udaljenje":
            r.udaljenje = True
        return m, label, article, r.fine_pct


# ---- chair phrases (matched on Latin, lowercase) ---------------------------------------------
_ROLE = (r"(?i:narodn[aio]\s+poslani(?:k|ca)|poslani(?:k|ca)|ministar(?:ka)?|predsedni(?:k|ca)\s+poslaničke\s+grupe|"
         r"predstavni(?:k|ca)\s+poslaničke\s+grupe|potpredsedni(?:k|ca)(?:\s+vlade)?|predsedni(?:k|ca)\s+vlade|"
         r"izvestilac|kolega|koleginica|gospodin|gospođa|ovlašćeni\s+predstavnik\s+predlagača)")
NAME = r"([A-ZČĆŠĐŽ][a-zčćšđž]+(?:[ -][A-ZČĆŠĐŽ][a-zčćšđž]+){1,2})"
RE_FLOOR = re.compile(r"\b(?i:reč|riječ)\s+(?i:ima)[,:]?\s+(?:(" + _ROLE + r")\s+)?" + NAME)
RE_FLOOR_ALT = re.compile(r"\b(?i:izvolite|izvoli)[,\s]+(?:(" + _ROLE + r")\s+)?" + NAME)
# "... narodni poslanik Ime Prezime ima reč" / "gospodin Prezime ima reč"
RE_FLOOR_POST = re.compile(r"(?:(" + _ROLE + r")\s+)?" + NAME + r"\s+(?i:ima\s+reč)")
RE_FLOOR_POST1 = re.compile(r"(" + _ROLE + r")\s+([A-ZČĆŠĐŽ][a-zčćšđž]+)\s+(?i:ima\s+reč)")
RE_CHAIR_OPOMENA = re.compile(r"izričem\s+(?:vam\s+)?(?:meru\s+)?opomen|dobijate\s+opomenu|opominjem\s+vas", re.I)
RE_CHAIR_ODUZ = re.compile(r"oduzimam\s+(?:vam\s+)?reč|izričem\s+(?:vam\s+)?(?:meru\s+)?oduzimanj", re.I)
RE_CHAIR_UDALJ = re.compile(r"udaljujem\s+vas|izričem\s+(?:vam\s+)?(?:meru\s+)?udaljenj", re.I)
RE_REPLIKA = re.compile(r"replik", re.I)
RE_POVREDA = re.compile(r"povred\w*\s+poslovnika|po\s+poslovniku|reklamir", re.I)
RE_GRUPA = re.compile(r"(?:predsedni|predstavni)\w*\s+poslan+i[čc]k\w*\s+grup\w*|ime\s+poslan+i[čc]k\w*\s+grup\w*",
                      re.I)
RE_IZVESTILAC = re.compile(r"izvestila", re.I)
RE_CHAIR_MARKERS = re.compile(r"\b(?:reč\s+ima|ima\s+reč|dajem\s+reč|idemo\s+dalje|izvolite|zahvaljujem|hvala\s+(?:vam|puno)?\s*[,.]?\s*(?:reč|nastavljamo)|"
                              r"nastavljamo|određujem\s+pauzu|molim\s+(?:vas\s+)?(?:za\s+)?mir|"
                              r"izričem|prelazimo\s+na|zaključujem)", re.I)


def limit_key(lat, role=""):
    """Which time limit applies to the turn the chair is giving (replika / povreda Poslovnika / ...)."""
    if RE_REPLIKA.search(lat):
        return "replika"
    if RE_POVREDA.search(lat):
        return "povreda"
    if RE_GRUPA.search(lat) or "poslaničke grupe" in role:
        return "grupa"
    if RE_IZVESTILAC.search(lat) or role == "izvestilac":
        return "izvestilac"
    return None


def parse_chair(text):
    """Look for the chair's procedural phrases in one transcript segment.

    Returns dict with optional keys: floor=(role, name, limit_key), measure=kind, chair=True.
    """
    lat = to_latin(text)
    out = {}
    m = (RE_FLOOR.search(lat) or RE_FLOOR_ALT.search(lat) or RE_FLOOR_POST.search(lat)
         or RE_FLOOR_POST1.search(lat))
    if m:
        role = (m.group(1) or "").lower()
        name = m.group(2)
        limit = None
        if RE_REPLIKA.search(lat):
            limit = "replika"
        elif RE_POVREDA.search(lat):
            limit = "povreda"
        elif RE_GRUPA.search(lat) or "poslaničke grupe" in role:
            limit = "grupa"
        elif RE_IZVESTILAC.search(lat) or role == "izvestilac":
            limit = "izvestilac"
        out["floor"] = (role, name, limit)
        out["chair"] = True
    low = lat.lower()
    if RE_CHAIR_UDALJ.search(low):
        out["measure"] = "udaljenje"
    elif RE_CHAIR_ODUZ.search(low):
        out["measure"] = "oduzimanje"
    elif RE_CHAIR_OPOMENA.search(low):
        out["measure"] = "opomena"
    if out.get("measure") or RE_CHAIR_MARKERS.search(low):
        out["chair"] = True
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        "Хвала. Реч има народни посланик Милица Ђурђевић Стаменковски.",
        "Pravo na repliku, reč ima Marko Marković.",
        "Po Poslovniku, reč ima narodni poslanik Petar Petrović.",
        "Izričem vam opomenu, narodni poslaniče.",
        "Реч има председник посланичке групе Ана Анић.",
        "Dakle, dobio sam opomenu, poštovani građani Srbije",
    ]
    for t in tests:
        print(t, "->", parse_chair(t))
    d = Discipline()
    for k in ["off_topic", "off_topic", "insult", "direct_address", "heckling", "insult"]:
        print(k, d.decide("Pera Perić", k))
