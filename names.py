"""Names and gender: the official MP roster, Serbian case forms, OCR look-alike matching, gender cues.

- find_announced(text): the chair's sentence -> which MP gets the floor ("Reč ima narodna poslanica Milena
  Popović", "Izvolite, koleginice Đurđević", "Dajem reč poslaniku Marku Markoviću", "Vučić ima reč"),
  matched against every case form of all 250 MPs' names, with the role words' gender as a tie-breaker.
- match_caption(ocr_lines): the on-screen caption is Cyrillic, Windows OCR only reads Latin, so it returns
  look-alikes ("AneKcaHgap Byqnh" = Александар Вучић); every roster name is turned into the same look-alike
  form and compared.
- speech_gender(text): the speaker's own grammar - "rekao sam" (M) / "rekla sam" (F), "uveren/uverena sam".
"""
import difflib
import json
import os
import re
import unicodedata

from common import DATA, to_latin

# ---------------------------------------------------------------------------------------------------
_FOLD = str.maketrans({"č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "dj"})


def fold(s):
    """lowercase, no diacritics (ASR is not always sure about č/ć/š/ž/đ)."""
    s = s.lower().translate(_FOLD)
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


ROSTER = _load("poslanici.json", [])
GROUPS = _load("grupe.json", [])

# ---- declension ------------------------------------------------------------------------------------
_FLEETING = re.compile(r"^(.*[^aeiou])([ae])([rlkcn])$")      # Petar -> Petr-a, Štimac -> Štimc-a


def forms(word, gender, is_surname):
    """All case forms (Latin) of one name word."""
    w = word.lower()
    out = {w}
    if gender == "F":
        if w.endswith("a"):
            s = w[:-1]
            out |= {s + "e", s + "i", s + "u", s + "om", s + "o"}
            if s.endswith("k"):
                out.add(s[:-1] + "ci")
            if s.endswith("g"):
                out.add(s[:-1] + "zi")
        return out                                  # female surnames on a consonant don't decline
    last = w[-1]
    if last == "o":
        s = w[:-1]
        out |= {s + "a", s + "u", s + "om"}
    elif last == "e":
        s = w[:-1]
        out |= {s + "a", s + "u", s + "em"}
    elif last == "a":
        s = w[:-1]
        out |= {s + "e", s + "i", s + "u", s + "om"}
    elif last in "iu":
        pass
    else:
        soft = w.endswith(("ć", "č", "š", "ž", "đ", "j", "lj", "nj", "c", "dž"))
        out |= {w + "a", w + "u", w + ("em" if soft else "om"), w + "e"}
        m = _FLEETING.match(w)
        if m and not w.endswith("ić"):
            s = m.group(1) + m.group(3)
            out |= {s + "a", s + "u", s + "om", s + "e", s + "em"}
    return out


def _build_index():
    idx = {}
    for i, mp in enumerate(ROSTER):
        words = mp["name"].replace(" - ", " ").replace("-", " ").split()
        first, surnames = words[0], words[1:]
        for f in forms(first, mp["gender"], False):
            idx.setdefault(fold(f), []).append((i, "first"))
        for sn in surnames:
            for f in forms(sn, mp["gender"], True):
                idx.setdefault(fold(f), []).append((i, "sur"))
    return idx


INDEX = _build_index()

# ---- gender cues ------------------------------------------------------------------------------------
_MALE_ROLE = re.compile(r"\b(?:narodn(?:i|og|om)|poslani(?:k|ka|ku|če)|koleg(?:a|e|i|u)|gospodin\w*|"
                        r"ministr?(?:a|e|u|ar)|predsedni(?:k|ka|ku|če)|potpredsedni(?:k|ka|ku|če))\b", re.I)
_FEMALE_ROLE = re.compile(r"\b(?:narodn(?:a|oj|u)|poslanic(?:a|e|i|u)|koleginic(?:a|e|i|u)|gospođ(?:a|o|i|u)|"
                          r"ministark(?:a|e|i|u)|predsednic(?:a|e|i|u)|potpredsednic(?:a|e|i|u))\b", re.I)


def role_gender(text):
    """Gender of the person the chair refers to, from the role words right before/after the name."""
    m, f = len(_MALE_ROLE.findall(text)), len(_FEMALE_ROLE.findall(text))
    return "M" if m > f else "F" if f > m else None


_ADJ = r"(?:uveren|siguran|spreman|zahvalan|ponosan|srećan|svestan|dužan|ubeđen|razočaran|iznenađen|zabrinut|" \
       r"ljut|tužan|zadovoljan|sposoban|kriv|dosledan|prisiljen|primoran|pozvan|izabran)"
_ADJ_F = r"(?:uverena|sigurna|spremna|zahvalna|ponosna|srećna|svesna|dužna|ubeđena|razočarana|iznenađena|" \
         r"zabrinuta|ljuta|tužna|zadovoljna|sposobna|kriva|dosledna|prisiljena|primorana|pozvana|izabrana)"
_SPEECH_M = re.compile(r"\b(?:sam|nisam|bih)\s+(?:\w+\s+){0,2}\w{2,}(?:ao|eo|io|uo)\b|\b\w{2,}(?:ao|eo|io|uo)\s+sam\b|"
                       rf"\b{_ADJ}\s+sam\b|\bsam\s+{_ADJ}\b", re.I)
_SPEECH_F = re.compile(r"\b(?:sam|nisam|bih)\s+(?:\w+\s+){0,2}\w{2,}la\b|\b\w{2,}la\s+sam\b|"
                       rf"\b{_ADJ_F}\s+sam\b|\bsam\s+{_ADJ_F}\b", re.I)
_NOT_PARTICIPLE = re.compile(r"\b(?:sam\s+(?:tamo|dakle|sada|zato|samo|ovo|to|da|sva|cela|mala|bila\s+je))\b", re.I)


def speech_gender(text):
    """(male_votes, female_votes) from the speaker's grammar about themselves."""
    t = _NOT_PARTICIPLE.sub(" ", to_latin(text))
    return len(_SPEECH_M.findall(t)), len(_SPEECH_F.findall(t))


# ---- announced speaker --------------------------------------------------------------------------------
_FLOOR_AFTER = re.compile(r"\b(?:re[cč]\s+ima|dajem\s+re[cč]|da[cć]u\s+re[cč]|re[cč]\s+dajem|sledeć[aei]\s+(?:je|govornik|"
                          r"govornica|na\s+listi)|pravo\s+na\s+repliku\s+ima|replik\w*\s+ima|po\s+poslovniku\s+"
                          r"(?:se\s+)?(?:javio|javila|ima)|povred\w*\s+poslovnika|povred\w*\s+poslanika|"
                          r"reklamacij\w*|reklamira)\b", re.I)
_FLOOR_BEFORE = re.compile(r"\b(?:ima\s+re[cč]|izvolite|izvoli|imate\s+re[cč]|ima\s+pravo\s+na\s+repliku|"
                           r"javio\s+se|javila\s+se)\b", re.I)


FLOOR_AFTER, FLOOR_BEFORE = _FLOOR_AFTER, _FLOOR_BEFORE
ROLE = re.compile(r"\b(?:(?:predsedni|predstavni)\w*\s+poslan+i[čc]k\w*\s+grup\w*|narodn\w+\s+poslani\w+|izvestila\w*|"
                  r"ministr\w*|ministark\w*|potpredsedni\w+\s+vlade|predsedni\w+\s+vlade)", re.I)


_NOT_A_FIRST_NAME = re.compile(r"(?i)^(?:narodn\w*|poslani\w*|koleg\w*|gospo\w*|ministr\w*|ministark\w*|predsedni\w*|"
                               r"potpredsedni\w*|izvolite|izvoli|ima|reč|rec|dajem|hvala|i|a|da|je|sa|za|od|po|"
                               r"replik\w*|uvažen\w*|poštovan\w*|dr|prof)$")
_FIRST_NAMES = {fold(w) for mp in ROSTER for w in [mp["name"].split()[0]]}


def find_names(text):
    """Roster MPs mentioned in text: list of (mp_index, start_char, end_char, score)."""
    words = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Za-zČĆŠŽĐčćšžđ]+", to_latin(text))]
    hits = {}

    def add(i, start, e, score):
        old = hits.get(i)
        if old is None or score > old[3]:
            hits[i] = (i, start, e, score)

    for k, (w, s, e) in enumerate(words):
        if len(w) < 3:
            continue
        prev = words[k - 1][0] if k > 0 else ""
        fprev = fold(prev)
        for i, part in INDEX.get(fold(w), []):
            if part != "sur":
                continue
            score, start = 1.0, s
            if prev and any(j == i and p == "first" for j, p in INDEX.get(fprev, [])):
                score, start = 2.0, words[k - 1][1]              # first name + surname
            elif prev[:1].isupper() and not _NOT_A_FIRST_NAME.match(prev) and (
                    fprev in _FIRST_NAMES or fprev.endswith(("a", "o", "an", "ar", "ko", "ja", "na"))):
                score = 0.2       # "Mirjana Nikolić" is not "Goran Nikolić": a different first name was said
            if w[0].isupper():
                score += 0.3
            add(i, start, e, score)
        # ASR misspelt surname ("Milovan Trecu" = Milovan Drecun), also with a slightly misspelt or cut first
        # name ("Aleksandr Vuć" = Aleksandar Vučić): first name + similar surname
        if prev and w[:1].isupper() and not INDEX.get(fold(w)):
            firsts = [(j, p) for j, p in INDEX.get(fprev, []) if p == "first"]
            if not firsts and prev[:1].isupper() and len(fprev) >= 5:
                for near in difflib.get_close_matches(fprev, _FIRST_NAMES, n=2, cutoff=0.85):
                    firsts += [(j, p) for j, p in INDEX.get(near, []) if p == "first"]
            for j, p in firsts:
                sur = fold(ROSTER[j]["name"].split(" ", 1)[1])
                r = difflib.SequenceMatcher(None, fold(w), sur[:len(fold(w)) + 2]).ratio()
                if r >= 0.6:
                    add(j, words[k - 1][1], e, 1.2 + r)
    return list(hits.values())


def find_announced(text):
    """If text is the chair giving someone the floor: (mp dict, score, gender_hint) else None."""
    lat = to_latin(text)
    after = [m.end() for m in _FLOOR_AFTER.finditer(lat)]
    before = [m.start() for m in _FLOOR_BEFORE.finditer(lat)]
    if not after and not before:
        return None
    names = find_names(lat)
    if not names:
        return None
    best, best_s = None, -1e9
    for i, s, e, score in names:
        d = min([abs(s - a) for a in after] + [abs(b - e) for b in before] + [999])
        sc = score - d / 40.0
        g = role_gender(lat[max(0, s - 40):e + 15])
        if g and g == ROSTER[i]["gender"]:
            sc += 0.6
        elif g and g != ROSTER[i]["gender"]:
            sc -= 1.0
        if sc > best_s:
            best, best_s = i, sc
    if best_s < 0.4:
        return None
    return ROSTER[best], best_s, role_gender(lat)


def roster_lookup(name):
    """Best roster entry for a free-form name (e.g. from Claude or a regex), or None."""
    f = fold(name)
    if " " not in f.strip():
        # just a surname ("Čupić"): fine if exactly one MP has it
        hits = [mp for mp in ROSTER if f.strip() in fold(mp["name"]).replace("-", " ").split()[1:]]
        return hits[0] if len(hits) == 1 else None
    best, sc = None, 0.0
    for mp in ROSTER:
        r = difflib.SequenceMatcher(None, f, fold(mp["name"])).ratio()
        if r > sc:
            best, sc = mp, r
    return best if sc >= 0.82 else None


# ---- on-screen caption (Cyrillic read by a Latin OCR) ------------------------------------------------
_LOOK = {"а": "a", "б": "b", "в": "b", "г": "r", "д": "a", "ђ": "h", "е": "e", "ж": "x", "з": "3", "и": "n",
         "ј": "j", "к": "k", "л": "n", "љ": "rb", "м": "m", "н": "h", "њ": "hb", "о": "o", "п": "n", "р": "p",
         "с": "c", "т": "t", "ћ": "h", "у": "y", "ф": "o", "х": "x", "ц": "n", "ч": "y", "џ": "n", "ш": "w"}


def lookalike(cyr):
    return "".join(_LOOK.get(c, c if c == " " else "") for c in cyr.lower())


_OCR_NORM = str.maketrans({"0": "o", "6": "b", "u": "n", "v": "n", "a": "a", "@": "a", "1": "n", "4": "h",
                           "l": "n", "i": "n", "ł": "n", "ą": "a", "ë": "e", "ô": "o", "â": "a", "ř": "p",
                           "y": "y", "q": "y", "g": "a", "č": "c", "ş": "", "ș": ""})


def _ocr_norm(s):
    s = s.lower().translate(_OCR_NORM)
    s = s.replace("nn", "n").replace("vł", "n")
    return re.sub(r"[^a-z3 ]", "", s)


_LOOK_ROSTER = [(i, _ocr_norm(lookalike(mp["cyr"])), _ocr_norm(lookalike(mp["cyr"].split(" ", 1)[1])))
                for i, mp in enumerate(ROSTER)]
_LOOK_GROUPS = [(g, _ocr_norm(lookalike(g))) for g in GROUPS]


def _partial(needle, hay):
    if not needle or not hay:
        return 0.0
    if len(hay) <= len(needle):
        return difflib.SequenceMatcher(None, needle, hay).ratio()
    best = 0.0
    for k in range(0, len(hay) - len(needle) + 1):
        r = difflib.SequenceMatcher(None, needle, hay[k:k + len(needle)]).ratio()
        if r > best:
            best = r
    return best


def match_caption(lines):
    """OCR lines of the caption -> (mp dict or None, score, group or "")."""
    if not lines:
        return None, 0.0, ""
    normed = [_ocr_norm(l) for l in lines if len(l.strip()) >= 4]
    # the group line ("Александар Вучић - Србија побеђује") can contain a person's name: find it first and
    # keep it out of the name matching
    group, gbest, name_lines = "", 0.0, []
    for ln in normed:
        is_group = False
        for g, gl in _LOOK_GROUPS:
            r = _partial(gl[-min(len(gl), 14):], ln)      # end of the group name is the distinctive part
            if r >= 0.7:
                is_group = True
            if r > gbest:
                group, gbest = g, r
        if not is_group:
            name_lines.append(ln)
    if gbest < 0.6:
        group = ""
    normed = name_lines
    if not normed:
        return None, 0.0, group
    scores = []
    for i, full, sur in _LOOK_ROSTER:
        s = 0.0
        for ln in normed:   # surname counts most: many MPs share a first name ("Aleksandar ...")
            # a short surname ("Ilić") fits somewhere in almost any OCR line - scale its weight by length
            sp = _partial(sur, ln) * min(1.0, len(sur.replace(" ", "")) / 7)
            s = max(s, 0.5 * difflib.SequenceMatcher(None, full, ln).ratio() + 0.5 * sp)
        scores.append((s, i))
    scores.sort(reverse=True)
    top, second = scores[0], scores[1] if len(scores) > 1 else (0, None)
    if top[0] >= 0.62 and top[0] - second[0] >= 0.06:
        return ROSTER[top[1]], top[0], group
    return None, top[0], group


def _cy(s):
    """Cyrillic letters and single spaces only (OCR noise like ';', '•', Latin look-alikes dropped)."""
    return re.sub(r"\s+", " ", re.sub(r"[^а-яђјљњћџ ]", " ", s.lower())).strip()


_CYR_ROSTER = [(i, _cy(mp["cyr"]), _cy(mp["cyr"].split(" ", 1)[-1])) for i, mp in enumerate(ROSTER) if mp.get("cyr")]
_CYR_GROUPS = sorted({mp["group"] for mp in ROSTER if mp.get("group")})


def match_caption_cyr(reads):
    """Captions read with the Serbian-Cyrillic Windows OCR, several image variants of one frame.
    reads: list (one per variant) of [(text, y)] with y = line centre as a fraction of the caption box height.
    The name is the upper line; the lower one is the group ("Александар Вучић - Србија побеђује" - which
    contains a name, so it must never be matched as the speaker). Returns (mp or None, score, group)."""
    best = {}
    group, gbest = "", 0.0
    for lines in reads:
        rows = []
        for text, y in sorted(lines, key=lambda x: x[1]):
            ln = _cy(text)
            if len(ln.replace(" ", "")) < 4:
                continue
            g_r, g_name = max((_partial(_cy(g)[-14:], ln), g) for g in _CYR_GROUPS)
            rows.append((ln, y, g_r >= 0.7))
            if g_r > gbest:
                group, gbest = g_name, g_r
        group_y = min((y for _ln, y, is_g in rows if is_g), default=None)
        for ln, y, is_g in rows:
            # the name is above the group line; with no group line found, the upper half of the box
            if is_g or (group_y is not None and y >= group_y) or (group_y is None and y >= 0.6):
                continue
            for i, full, sur in _CYR_ROSTER:
                sp = _partial(sur, ln) * min(1.0, len(sur.replace(" ", "")) / 7)
                s = 0.5 * difflib.SequenceMatcher(None, full, ln).ratio() + 0.5 * sp
                if s > best.get(i, 0.0):
                    best[i] = s
    if gbest < 0.6:
        group = ""
    if not best:
        return None, 0.0, group
    ranked = sorted(((s, i) for i, s in best.items()), reverse=True)
    top, second = ranked[0], ranked[1] if len(ranked) > 1 else (0.0, None)
    if top[0] >= 0.62 and top[0] - second[0] >= 0.08:
        return ROSTER[top[1]], top[0], group
    return None, top[0], group


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(len(ROSTER), "MPs,", len(INDEX), "name forms,", len(GROUPS), "groups")
    for t in ["Хвала. Реч има народна посланица Ана Брнабић.",
              "Izvolite, koleginice Štimac.",
              "Dajem reč narodnom poslaniku Jovi Bakiću.",
              "Molim vas za red, gospodin Vučić ima reč da završi posle Ade Mavrić povreda poslovnika.",
              "Reč ima Uglješa Mrdić, po Poslovniku.",
              "Sledeći je kolega Petrašinović, izvolite."]:
        r = find_announced(t)
        print(t, "->", (r[0]["name"], round(r[1], 2), r[2]) if r else None)
    for t in ["Ja sam gledao vas dok ste govorili, nisam zaista video.", "Rekla sam vam, uverena sam da je tako.",
              "Ja sam sama to pročitala."]:
        print(t, "->", speech_gender(t))
    for lines in [["AneKcaHgap Byqnh", "AneKcaHgap Byqnh - Cp6nja n06ehyje"], ["AP Yrrbewa Mpgnh"],
                  ["Jogo", "CTygeHTCKa nncTa"], ["au-lhH0BY1h", "AneKčaHna)) Byyh -Qp6Mja n06ehyje"]]:
        mp, sc, g = match_caption(lines)
        print(lines, "->", mp["name"] if mp else None, round(sc, 2), g)
