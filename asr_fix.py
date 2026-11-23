"""Clean-up of systematic Whisper errors in Serbian (Latin text), and the prompt that steers Whisper.

Found in the saved sessions (2026-11-23): Whisper writes Russian-style double consonants ("poslannika" 28x,
"organizovanno"), a stray "o"/"ć" on short words ("imate rečo", "ako je taćo"), "ć" for "č"
("predlagaći"), and splits the negation ("ni je", "ne mojte"). Each rule here only touches forms that are
never correct Serbian, so a right word is not changed.
"""
import re

_WORD = [
    # (pattern, replacement) on whole words, case kept for the first letter
    (r"re[čć]o", "reč"),
    (r"re[čć]u", "reč"),                          # "imate reču" - also never correct after "imate/ima"
    (r"ta[ćč]o", "tačno"),
    (r"šlan(a|u|om|ovi|ova)?", r"član\1"),
    (r"predlaga[ćc](a|i|u|em|ima|e)?", r"predlagač\1"),
    (r"gospodj(a|e|i|u|om)", r"gospođ\1"),
    (r"vredj(a|ati|ate|aju|anje)", r"vređ\1"),
    (r"pobedj(uje|ujemo|ujete|uju|ivati)", r"pobeđ\1"),
    (r"medju", "među"),
    (r"rodjen(a|i|o)?", r"rođen\1"),
    (r"prisutstv(o|a|u|om)", r"prisustv\1"),
]
_WORD = [(re.compile(r"\b" + p + r"\b", re.I), r) for p, r in _WORD]
# negation written apart: "ni je" -> "nije", "ne mojte" -> "nemojte" (the split forms are never correct)
_NEG = re.compile(r"\b([Nn])i\s+(je|sam|si|smo|ste|su)\b")
_NEMOJ = re.compile(r"\b([Nn])e\s+(moj|mojte|mojmo)\b")
# double letters Serbian never has inside a word ("poslannik", "organizovanno", "unapređđenje")
_DOUBLE = re.compile(r"([nmlrtsđćčšž])\1", re.I)
_KEEP_DOUBLE = re.compile(r"(?i)\b(?:\w*(?:najj|vašš|nadd|odd|podd|predd|izz|razz|bezz|uzz|ss)\w*)\b")


def _case(src, rep):
    return rep[:1].upper() + rep[1:] if src[:1].isupper() else rep


def fix(text):
    """Latin Whisper text -> the same text with the known systematic errors corrected."""
    if not text:
        return text

    def dbl(m):
        w = m.group(0)
        return w if _KEEP_DOUBLE.fullmatch(w) else _DOUBLE.sub(r"\1", w)
    text = re.sub(r"[A-Za-zČĆŠŽĐčćšžđ]+", dbl, text)
    text = re.sub(r"d+đ+|đ+d+", "đ", text)
    for rx, rep in _WORD:
        text = rx.sub(lambda m: _case(m.group(0), m.expand(rep)), text)
    text = _NEG.sub(lambda m: m.group(1) + "i" + m.group(2), text)
    text = _NEMOJ.sub(lambda m: m.group(1) + "e" + m.group(2), text)
    return text


# ---- the prompt: Whisper copies spelling and vocabulary from it ----------------------------------------
BASE_PROMPT = ("Narodna skupština Republike Srbije. Reč ima narodni poslanik. Imate reč. Hvala, predsedavajući. "
               "Povreda Poslovnika, replika, opomena, oduzimanje reči, predlagač, poslanička grupa, amandman, "
               "dnevni red, glasanje.")


def prompt(names=(), prev=""):
    """Prompt for one chunk: the base vocabulary + names likely to be said now (speaker, chair, announced)
    + the end of the previous chunk. Kept short: Whisper uses at most ~220 prompt tokens."""
    who = ", ".join(n for n in dict.fromkeys(names) if n)[:160]
    return BASE_PROMPT + (f" Govore: {who}." if who else "") + (" " + prev[-160:] if prev else "")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for s in ["Imate rečo.", "Ako ni je taćo, opomene će ostati.", "Dali predlagaći, ima je rečo?",
              "Mi do sada od poslannika SNSa nismo čuli", "Ne mojte se ljutite", "reklamiram šlan 16",
              "najjači, odders, Anna", "Gospodja Petrović", "Ja sam svemu i svačemu u životu prisutstvo",
              "ni jednu reč", "unapredđennjem", "Dali ste mi reč"]:
        print(f"{s!r:50} -> {fix(s)!r}")
