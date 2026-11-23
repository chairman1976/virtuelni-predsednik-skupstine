"""Download the current list of MPs from parlament.gov.rs -> data/poslanici.json.

Run again after the composition changes:  python update_roster.py
Each entry: name (Latin, "Ime Prezime"), cyr, party, city, year, gender (M/F guessed from the first name).
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from common import CA_BUNDLE, DATA, to_latin

URL = "https://www.parlament.gov.rs" + urllib.parse.quote("/народна-скупштина/састав/народни-посланици/актуелни-сазив.11.html")
# male first names that end in -a / -o and female ones that don't
MALE_A = {"nikola", "luka", "saša", "andrija", "ilija", "kosta", "jovica", "mića", "sava", "toma", "boža", "pera",
          "đura", "mirča", "nebojša", "aleksa", "feđa", "mikica", "milija", "mirza", "uglješa", "đoka", "jakša",
          "ljubiša", "dragiša", "radiša", "dimitrija", "mija", "nikica", "perica", "mića", "zoltan", "mihaj", "vanja", "nemanja", "vukašin", "aca", "raša", "miša", "gruja",
          "stanoja", "ljuba", "joca", "jova", "zdravko", "danko", "marko", "branko", "darko", "janko"}
FEMALE_OTHER = {"ivon", "elizabet", "edit", "erzsébet", "agnes", "agneš", "ruth", "nives", "karmen",
                "dolores", "mirjam", "jasmin", "ines"}


def gender_of(first):
    f = first.lower()
    if f in FEMALE_OTHER:
        return "F"
    if f in MALE_A:
        return "M"
    return "F" if f.endswith("a") else "M"


def title(s):
    return " ".join(p[:1].upper() + p[1:].lower() for p in s.split())


def fetch(query=""):
    import ssl
    ctx = ssl.create_default_context(cafile=CA_BUNDLE) if os.path.exists(CA_BUNDLE) else None
    req = urllib.request.Request(URL + query, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, context=ctx, timeout=60).read().decode("utf-8", "replace")


def group_options(page):
    """[(id, short group name)] from the filter form ('group' select)."""
    m = re.search(r'<select[^>]*name="group"[^>]*>(.*?)</select>', page, re.S)
    out = []
    for gid, label in re.findall(r'<option value="(\d+)"[^>]*>(.*?)</option>', m.group(1) if m else "", re.S):
        if gid == "0":
            continue
        g = html.unescape(re.sub(r"\s+", " ", label)).strip()
        g = re.sub(r"^Посланичка група\s*", "", g).strip("„“\"” ")
        if "нису чланови" in g:
            g = "Нису чланови посланичких група"
        out.append((gid, g))
    return out


def add_groups(mps, page):
    """Fill mp['group'] (Cyrillic, as the TV caption shows it) by fetching the list filtered per group."""
    by_cyr = {mp["cyr"]: mp for mp in mps}
    for gid, gname in group_options(page):
        for mp in parse(fetch(f"?submitted=true&group={gid}")):
            if mp["cyr"] in by_cyr:
                by_cyr[mp["cyr"]]["group"] = gname
    for mp in mps:
        mp.setdefault("group", "")


def parse(page):
    out = []
    for name, other in re.findall(r'class="grid_name">\s*(.*?)\s*<span[^>]*>.*?</span>\s*</span>\s*'
                                  r'<span class="grid_other">(.*?)</span>', page, re.S):
        name = html.unescape(re.sub(r"\s+", " ", name)).strip()
        name = re.sub(r"\(.*?\)", " ", name)                       # "(PÁSZTOR BÁLINT)" alt spelling
        name = re.sub(r"(?i)(?<!\w)(?:проф|др|доц|мр|дипл|инг)\.?(?=\s|$)", " ", name)   # "prof. dr" titles
        name = re.sub(r"\s+", " ", name).strip(" -")
        other = html.unescape(re.sub(r"\s+", " ", other)).strip()
        parts = name.split(" ")
        if len(parts) < 2:
            continue
        # site lists "PREZIME IME" (surname may have 2 words, first name is the last word)
        first, surname = parts[-1], " ".join(parts[:-1])
        cyr = f"{title(first)} {title(surname)}"
        lat = to_latin(cyr)
        bits = [b.strip() for b in other.split(",")]
        year = bits[-1].rstrip(".") if bits and bits[-1].rstrip(".").isdigit() else ""
        out.append({"name": lat, "cyr": cyr, "party": to_latin(bits[0]) if bits else "",
                    "city": to_latin(", ".join(bits[1:-1] if year else bits[1:])), "year": year,
                    "gender": gender_of(to_latin(first))})
    return out


def parse_groups(page):
    """Parliamentary groups, as the TV caption shows them (no 'Посланичка група', no quotes), Cyrillic."""
    i = page.find("Изаберите посланичку групу<")
    j = page.find("</select>", i)
    out = []
    for o in re.findall(r"<option[^>]*>(.*?)</option>", page[i:j], re.S):
        g = html.unescape(re.sub(r"\s+", " ", o)).strip()
        g = re.sub(r"^Посланичка група\s*", "", g).strip("„“\"” ")
        if g and "нису чланови" not in g:
            out.append(g)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    page = fetch()
    mps = parse(page)
    add_groups(mps, page)
    path = os.path.join(DATA, "poslanici.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mps, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "grupe.json"), "w", encoding="utf-8") as f:
        json.dump(parse_groups(page), f, ensure_ascii=False, indent=1)
    from collections import Counter
    print(len(mps), "poslanika ->", path)
    print("pol:", Counter(m["gender"] for m in mps), " stranke:", Counter(m["party"] for m in mps).most_common(8))
    for m in mps[:5]:
        print(m)
