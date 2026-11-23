"""Shared setup: paths, SSL (AVG HTTPS scanning), CUDA DLLs, settings, Cyrillic->Latin."""
import glob
import json
import os
import site
import sys

FROZEN = getattr(sys, "frozen", False)        # running as the PyInstaller .exe
# .exe: data/, models/, sessions/, settings and bin/ (ffmpeg, node, yt-dlp) live next to the .exe
ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
MODELS = os.path.join(ROOT, "models")
SESSIONS = os.path.join(ROOT, "sessions")
SETTINGS_FILE = os.path.join(ROOT, "settings_local.json")  # holds the API key - keep out of git

# Antivirus HTTPS scanning (e.g. AVG Web Shield) re-signs HTTPS; then Python needs certifi + the exported
# antivirus root in one PEM. Used only if the file exists (or PRED_CA_BUNDLE points to one).
CA_BUNDLE = os.environ.get("PRED_CA_BUNDLE") or os.path.expanduser(r"~\.certs\ca-bundle-with-avg.pem")


def setup_env():
    if os.path.exists(CA_BUNDLE):
        os.environ.setdefault("SSL_CERT_FILE", CA_BUNDLE)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", CA_BUNDLE)
    # cuBLAS / cuDNN come from the nvidia-* pip wheels
    roots = [getattr(sys, "_MEIPASS", ROOT)] if FROZEN else site.getsitepackages()
    for sp in roots:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    # pip puts yt-dlp in <python>\Scripts, which is not on PATH when started via pythonw or a .venv
    for tools in (os.path.join(sys.prefix, "Scripts"), os.path.join(ROOT, "bin")):
        if os.path.isdir(tools):
            os.environ["PATH"] = tools + os.pathsep + os.environ.get("PATH", "")
    os.makedirs(SESSIONS, exist_ok=True)


DEFAULTS = {
    "api_key": "",
    "judge_model": "claude-haiku-5-5",
    "judge_effort": "medium",
    "profiles": True,          # build public-record profiles of speakers (~$0.04 per MP, once)
    "context_checks": True,    # wider-context / spin checks of accusations (~$0.02 each)
    "whisper_model": "large-v3-turbo",
    "judge_every_s": 20,
    "min_confidence": 0.6,
    "shout_db": 7.0,
    "voice_change_sim": 0.62,
    "caption_every_s": 60,
    "volume": 0.8,
    "last_url": "",
    "chime": True,
}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    if s.get("voice_change_sim") == 0.55:
        s["voice_change_sim"] = 0.62            # old, more lenient default -> strict matching
    if not s["api_key"]:
        s["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return s


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


_CYR = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E", "Ж": "Ž", "З": "Z", "И": "I",
    "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj", "М": "M", "Н": "N", "Њ": "Nj", "О": "O", "П": "P", "Р": "R",
    "С": "S", "Т": "T", "Ћ": "Ć", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č", "Џ": "Dž", "Ш": "Š",
    # Russian / Macedonian / Ukrainian letters whisper sometimes slips in
    "Ы": "I", "Э": "E", "Ю": "Ju", "Я": "Ja", "Щ": "Š", "Ь": "", "Ъ": "", "Й": "J", "Ё": "E",
    "Ќ": "Ć", "Ѓ": "Đ", "Ѕ": "Dz", "І": "I", "Ї": "Ji", "Є": "Je", "Ў": "U",
}
_TR = {}
for k, v in _CYR.items():
    _TR[ord(k)] = v
    _TR[ord(k.lower())] = v.lower()


def to_latin(text):
    return text.translate(_TR)


def fmt_time(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def parse_time(text):
    """'1:30:00' / '90:00' / '5400' -> seconds."""
    text = (text or "").strip()
    if not text:
        return 0.0
    parts = [float(p) for p in text.split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fold_text(s):
    """lowercase Latin without diacritics or punctuation - for comparing short texts."""
    import re
    import unicodedata
    s = to_latin(s).lower().replace("đ", "dj")
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", "", s).strip()
