"""HTML for the Statistika / Retorika / Za proveru tabs and the end-of-session report.

Real chair vs app: a real measure and an app measure are the same event when they are within 90 s of each other,
of the same family (upozorenje / opomena / oduzimanje reči / udaljenje) and for the same person (or the real
target is unknown). Fines follow čl. 114: highest of 10 % (1 opomena), 20 % (2), 40 % (oduzimanje), 50 % (udaljenje).
"""
import collections
import datetime
import html

from factcheck import VERDICT_COLORS
from judge import TECHNIQUES

FAMILY = {"upozorenje": "upozorenje", "opomena": "opomena", "opomena2": "opomena", "oduzimanje": "oduzimanje",
          "oduzimanje_reci": "oduzimanje", "udaljenje": "udaljenje"}
FAMILY_LABEL = {"upozorenje": "Upozorenje", "opomena": "Opomena", "oduzimanje": "Oduzimanje reči",
                "udaljenje": "Udaljenje sa sednice"}
WINDOW = 90
CHAIR_PENALTY = {"upozorenje": 2, "opomena": 5, "oduzimanje": 10, "udaljenje": 15}
TONE_COLORS = {"argumentovan": "#1e8449", "smiren": "#52a447", "oštar": "#d4a017", "ironičan": "#af7ac5",
               "emotivan": "#5dade2", "uvredljiv": "#c0392b", "agresivan": "#922b21", "manipulativan": "#7d3c98"}
CSS = """<style>
body{font-family:'Segoe UI',sans-serif;font-size:10pt;color:#222}
h2{font-size:13pt;margin:10px 0 4px} h3{font-size:11pt;margin:8px 0 2px}
table{border-collapse:collapse;margin:4px 0} td,th{border:1px solid #ccc;padding:3px 8px}
th{background:#eef} .big{font-size:20pt;font-weight:bold} .muted{color:#888}
</style>"""


def esc(x):
    return html.escape(str(x))


def _fine(measures):
    fams = [FAMILY.get(m, "") for m in measures]
    if "udaljenje" in fams:
        return 50
    if "oduzimanje" in fams:
        return 40
    n = fams.count("opomena")
    return 20 if n >= 2 else 10 if n == 1 else 0


def match(real, app, same_person):
    """real: [{t,target,measure,...}], app: [{t,offender,measure,...}] -> (pairs, real_only, app_only)."""
    used, pairs, real_only = set(), [], []
    for r in sorted(real, key=lambda x: x["t"]):
        best, bd = None, None
        for i, a in enumerate(app):
            if i in used or FAMILY.get(a["measure"]) != FAMILY.get(r["measure"]):
                continue
            d = abs(a["t"] - r["t"])
            if d > WINDOW:
                continue
            if r.get("target") and not same_person(r["target"], a["offender"]):
                continue
            if bd is None or d < bd:
                best, bd = i, d
        if best is None:
            real_only.append(r)
        else:
            used.add(best)
            pairs.append((r, app[best]))
    app_only = [a for i, a in enumerate(app) if i not in used]
    return pairs, real_only, app_only


def stats_html(real, app, same_person, tlink, canon, chair_at=None):
    pairs, real_only, app_only = match(real, app, same_person)
    out = [CSS, "<h2>Stvarni predsedavajući ↔ aplikacija</h2>"]
    if not real and not app:
        return "".join(out) + "<p class='muted'>Još nema izrečenih mera.</p>"
    n_real, n_app, n_pair = len(real), len(app), len(pairs)
    recall = f"{100 * n_pair / n_real:.0f} %" if n_real else "–"
    precision = f"{100 * n_pair / n_app:.0f} %" if n_app else "–"
    out.append("<table><tr>"
               f"<td><div class='big'>{n_real}</div>mera predsedavajućeg</td>"
               f"<td><div class='big'>{n_app}</div>mera aplikacije</td>"
               f"<td><div class='big'>{n_pair}</div>poklapanja</td>"
               f"<td><div class='big'>{recall}</div>stvarnih mera koje je i aplikacija izrekla</td>"
               f"<td><div class='big'>{precision}</div>mera aplikacije koje je izrekao i predsedavajući</td>"
               "</tr></table>")
    # chair score: every measure the app would pronounce and the chair did not is a minus for the chair
    minus = sum(CHAIR_PENALTY.get(FAMILY.get(a["measure"], ""), 0) for a in app_only)
    score = max(0, 100 - minus)
    col = "#1e8449" if score >= 85 else "#d4a017" if score >= 60 else "#c0392b"
    out.append(f"<h3>Ocena predsedavajućeg</h3><table><tr><td><div class='big' style='color:{col}'>{score}/100"
               f"</div></td><td>{len(app_only)} propuštenih mera = −{minus} bodova<br>"
               "<span class='muted'>−2 upozorenje, −5 opomena, −10 oduzimanje reči, −15 udaljenje: mere koje bi "
               "aplikacija izrekla po Poslovniku, a predsedavajući nije. "
               f"{len(real_only)} mera je predsedavajući izrekao, a aplikacija nije (ne oduzima bodove). "
               "Ocena je mišljenje aplikacije i zavisi od tačnosti transkripta.</span></td></tr></table>")
    if chair_at:
        people = {}
        for x in real:
            people.setdefault(chair_at(x["t"]) or "nije prepoznat", {"real": 0, "missed": 0, "minus": 0})["real"] += 1
        for x in app_only:
            r = people.setdefault(chair_at(x["t"]) or "nije prepoznat", {"real": 0, "missed": 0, "minus": 0})
            r["missed"] += 1
            r["minus"] += CHAIR_PENALTY.get(FAMILY.get(x["measure"], ""), 0)
        if people and set(people) != {"nije prepoznat"}:
            out.append("<h3>Po predsedavajućem</h3><table><tr><th>Predsedavao</th><th>Izrekao mera</th>"
                       "<th>Propustio</th><th>Ocena</th></tr>")
            for who, r in sorted(people.items(), key=lambda kv: kv[1]["minus"]):
                sc = max(0, 100 - r["minus"])
                c = "#1e8449" if sc >= 85 else "#d4a017" if sc >= 60 else "#c0392b"
                out.append(f"<tr><td>{esc(who)}</td><td>{r['real']}</td><td>{r['missed']}</td>"
                           f"<td style='color:{c}'><b>{sc}/100</b></td></tr>")
            out.append("</table>")
    # by type
    out.append("<h3>Po vrsti mere</h3><table><tr><th>Mera</th><th>Predsedavajući</th><th>Aplikacija</th>"
               "<th>Poklapanja</th></tr>")
    for fam, label in FAMILY_LABEL.items():
        r = sum(1 for x in real if FAMILY.get(x["measure"]) == fam)
        a = sum(1 for x in app if FAMILY.get(x["measure"]) == fam)
        p = sum(1 for x, _ in pairs if FAMILY.get(x["measure"]) == fam)
        if r or a:
            out.append(f"<tr><td>{label}</td><td>{r}</td><td>{a}</td><td>{p}</td></tr>")
    out.append("</table>")
    # by person, with fines
    people = collections.OrderedDict()
    for x in real:
        people.setdefault(canon(x.get("target") or "(nepoznat)"), [[], []])[0].append(x["measure"])
    for x in app:
        people.setdefault(canon(x["offender"]), [[], []])[1].append(x["measure"])
    out.append("<h3>Po poslaniku</h3><table><tr><th>Poslanik</th><th>Predsedavajući</th><th>Kazna</th>"
               "<th>Aplikacija</th><th>Kazna</th></tr>")
    for name, (r, a) in sorted(people.items(), key=lambda kv: -(len(kv[1][0]) + len(kv[1][1]))):
        def fmt(ms):
            c = collections.Counter(FAMILY_LABEL.get(FAMILY.get(m, ""), m) for m in ms)
            return ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in c.items()) or "–"
        fr, fa = _fine(r), _fine(a)
        out.append(f"<tr><td>{esc(name)}</td><td>{esc(fmt(r))}</td><td>{f'{fr} %' if fr else ''}</td>"
                   f"<td>{esc(fmt(a))}</td><td>{f'{fa} %' if fa else ''}</td></tr>")
    out.append("</table>")
    if real_only:
        out.append("<h3>Izrekao predsedavajući, aplikacija nije</h3><ul>")
        for x in real_only:
            out.append(f"<li>{tlink(x['t'])} <b>{esc(FAMILY_LABEL.get(FAMILY.get(x['measure'], ''), x['measure']))}"
                       f"</b> {esc(x.get('target') or '')} – „{esc(x.get('quote', '')[:120])}“</li>")
        out.append("</ul>")
    if app_only:
        out.append("<h3>Propusti predsedavajućeg (izrekla aplikacija, predsedavajući nije)</h3><ul>")
        for x in app_only:
            out.append(f"<li>{tlink(x['t'])} <b>{esc(FAMILY_LABEL.get(FAMILY.get(x['measure'], ''), x['measure']))}"
                       f"</b> {esc(x['offender'])} – {esc(x.get('label', ''))}: „{esc(x.get('quote', '')[:120])}“</li>")
        out.append("</ul>")
    if pairs:
        out.append("<h3>Poklapanja</h3><ul>")
        for r, a in pairs:
            out.append(f"<li>{tlink(r['t'])} {esc(FAMILY_LABEL.get(FAMILY.get(r['measure'], ''), ''))} – "
                       f"{esc(a['offender'])} ({esc(a.get('label', ''))})</li>")
        out.append("</ul>")
    return "".join(out)


def rhetoric_html(rhetoric, tones, tlink, canon):
    out = [CSS, "<h2>Ton i retoričke tehnike</h2>"]
    if not rhetoric and not tones:
        return "".join(out) + "<p class='muted'>Još nema podataka (potreban je Claude API ključ).</p>"
    speakers = collections.OrderedDict()
    for x in tones:
        speakers.setdefault(canon(x["speaker"]), [collections.Counter(), collections.Counter(), []])[0][x["tone"]] += 1
    for x in rhetoric:
        s = speakers.setdefault(canon(x["speaker"]), [collections.Counter(), collections.Counter(), []])
        s[1][x["technique"]] += 1
        s[2].append(x)
    tot = collections.Counter(x["technique"] for x in rhetoric)
    if tot:
        out.append("<h3>Najčešće tehnike u sednici</h3><table>")
        for k, v in tot.most_common():
            out.append(f"<tr><td>{esc(TECHNIQUES.get(k, k))}</td><td>{v}</td></tr>")
        out.append("</table>")
    out.append("<h3>Po govorniku</h3><table><tr><th>Govornik</th><th>Ton (koliko puta)</th><th>Tehnike</th></tr>")
    for name, (tc, kc, _ex) in sorted(speakers.items(), key=lambda kv: -sum(kv[1][1].values())):
        tone = " ".join(f"<span style='color:{TONE_COLORS.get(k, '#444')}'>{esc(k)} ×{v}</span>"
                        for k, v in tc.most_common())
        tech = "<br>".join(f"{esc(TECHNIQUES.get(k, k))} ×{v}" for k, v in kc.most_common())
        out.append(f"<tr><td><b>{esc(name)}</b></td><td>{tone}</td><td>{tech}</td></tr>")
    out.append("</table><h3>Primeri</h3>")
    for x in sorted(rhetoric, key=lambda r: r["t"], reverse=True)[:80]:
        out.append(f"<p>{tlink(x['t'])} <b>{esc(canon(x['speaker']))}</b> – "
                   f"<span style='color:#7d3c98'>{esc(TECHNIQUES.get(x['technique'], x['technique']))}</span>: "
                   f"„{esc(x['quote'][:160])}“<br><span class='muted'>{esc(x['explanation'])}</span></p>")
    return "".join(out)


def facts_html(claims, tlink):
    claims = [c for c in claims if c.get("kind") != "context"]
    out = [CSS]
    if not claims:
        return CSS + "<p class='muted'>Još nema tvrdnji za proveru.</p>"
    for c in sorted(claims, key=lambda x: x["t"], reverse=True):
        res = c.get("result")
        out.append(f"<p>{tlink(c['t'])} <b>{esc(c['speaker'])}</b>: „{esc(c['claim'])}“<br>")
        if res:
            col = VERDICT_COLORS.get(res["verdict"], "#444")
            note = (" <span class='muted'>(ocena korisnika)</span>" if res.get("user") else ""
                    if res.get("searches") else " <span class='muted'>(nije provereno na internetu)</span>")
            out.append(f"<b style='color:{col}'>{esc(res['verdict'].upper())}</b>{note} – {esc(res['explanation'])}")
            if res.get("sources"):
                out.append("<br><span class='muted'>Izvori:</span> " + " · ".join(
                    f"<a href='{esc(s['url'])}'>{esc(s['title'][:70])}</a>" for s in res["sources"][:4]))
        elif c.get("status") == "checking":
            out.append("<i class='muted'>proveravam na internetu…</i>")
        elif c.get("status") == "nokey":
            out.append(f"<i class='muted'>{esc(c.get('why', ''))}</i>")
        else:
            out.append(f"<i class='muted'>čeka proveru · {esc(c.get('why', ''))}</i>")
        out.append("</p>")
    return "".join(out)


def full_report(meta, cards_html, stats, rhetoric, facts, costs):
    when = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    return (f"<html><head><meta charset='utf-8'><title>Izveštaj – {esc(meta['title'])}</title>{CSS}</head><body>"
            f"<h1 style='font-size:16pt'>Izveštaj sa sednice</h1>"
            f"<p><b>{esc(meta['title'])}</b><br><a href='{esc(meta['url'])}'>{esc(meta['url'])}</a><br>"
            f"Praćeno: {esc(meta['covered'])} · napravljeno {when} (Virtuelni predsednik skupštine) · {esc(costs)}</p>"
            f"<p class='muted'>Automatska analiza (prepoznavanje govora + Claude). Moguće su greške u transkriptu, "
            f"imenima i ocenama; mere su predlog po Poslovniku, a ne zvanične odluke.</p>"
            f"{stats}<h2>Mere aplikacije</h2>{cards_html}{rhetoric}<h2>Provera tvrdnji</h2>{facts}"
            "</body></html>")


# ---- transcript export --------------------------------------------------------------------------------
def _hms(t):
    t = int(max(0, t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def transcript_txt(meta, segs, name):
    """segs: [{t0, t1, who, text, loud}] -> plain text, one line per segment, speaker shown when it changes."""
    out = [meta["title"], meta["url"], f"Transkript (automatski, moguće greške) – {meta['made']}", ""]
    prev = None
    for g in sorted(segs, key=lambda x: x["t0"]):
        who = name(g["who"])
        mark = f"  [GLASNO +{g['loud']:.0f} dB]" if g.get("loud") else ""
        if who != prev:
            out.append("")
            out.append(f"[{_hms(g['t0'])}] {who}:")
            prev = who
        out.append(f"    {g['text']}{mark}")
    return "\n".join(out) + "\n"


def transcript_srt(segs, name):
    out = []
    for i, g in enumerate(sorted(segs, key=lambda x: x["t0"]), 1):
        out += [str(i), f"{_srt_time(g['t0'])} --> {_srt_time(max(g['t1'], g['t0'] + 1))}",
                f"{name(g['who'])}: {g['text']}", ""]
    return "\n".join(out)


def transcript_html(meta, segs, name):
    rows = []
    prev = None
    for g in sorted(segs, key=lambda x: x["t0"]):
        who = name(g["who"])
        color = "#e67e22" if g.get("chair") else "#2471a3"
        txt = esc(g["text"])
        if g.get("loud"):
            txt = f"<b>{txt}</b> <span style='color:#c0392b'>[GLASNO +{g['loud']:.0f} dB]</span>"
        head = f"<b style='color:{color}'>{esc(who)}:</b> " if who != prev else ""
        prev = who
        rows.append(f"<p style='margin:2px 0'><span class='muted'>[{_hms(g['t0'])}]</span> {head}{txt}</p>")
    return (f"<html><head><meta charset='utf-8'><title>Transkript – {esc(meta['title'])}</title>{CSS}</head><body>"
            f"<h1 style='font-size:15pt'>{esc(meta['title'])}</h1><p><a href='{esc(meta['url'])}'>{esc(meta['url'])}"
            f"</a><br><span class='muted'>Automatski transkript (moguće greške u rečima i imenima) – "
            f"{esc(meta['made'])}</span></p>{''.join(rows)}</body></html>")


# ---- per parliamentary group -------------------------------------------------------------------------------
def groups_html(rows):
    """rows: {group: {secs, app:[measures], real:[measures], fined:set, tech:Counter, tone:Counter,
    verdicts:Counter, speakers:set}}"""
    out = [CSS, "<h2>Po poslaničkim grupama</h2>"]
    if not rows:
        return "".join(out) + "<p class='muted'>Još nema podataka.</p>"
    out.append("<table><tr><th>Grupa</th><th>Govorilo</th><th>Mere aplikacije</th><th>Mere predsedavajućeg</th>"
               "<th>Kažnjeni poslanici</th><th>Retoričke tehnike</th><th>Najčešći ton</th><th>Provera tvrdnji</th></tr>")
    for g, r in sorted(rows.items(), key=lambda kv: -kv[1]["secs"]):
        mins = r["secs"] / 60
        per10 = (lambda n: f" <span class='muted'>({10 * n / mins:.1f} / 10 min)</span>" if mins >= 1 else "")
        app_c = collections.Counter(FAMILY_LABEL.get(FAMILY.get(m, ""), m) for m in r["app"])
        real_c = collections.Counter(FAMILY_LABEL.get(FAMILY.get(m, ""), m) for m in r["real"])
        fmt = (lambda c: "<br>".join(f"{k} ×{v}" for k, v in c.most_common()) or "–")
        tech_n = sum(r["tech"].values())
        tech = (f"{tech_n}{per10(tech_n)}<br>" + "<br>".join(f"{esc(TECHNIQUES.get(k, k))} ×{v}"
                                                            for k, v in r["tech"].most_common(3))) if tech_n else "–"
        tone = " ".join(f"<span style='color:{TONE_COLORS.get(k, '#444')}'>{esc(k)} ×{v}</span>"
                        for k, v in r["tone"].most_common(2)) or "–"
        verd = " · ".join(f"<span style='color:{VERDICT_COLORS.get(k, '#444')}'>{esc(k)} ×{v}</span>"
                          for k, v in r["verdicts"].most_common()) or "–"
        out.append(f"<tr><td><b>{esc(g)}</b><br><span class='muted'>{len(r['speakers'])} govornika</span></td>"
                   f"<td>{mins:.0f} min</td><td>{len(r['app'])}{per10(len(r['app']))}<br>{fmt(app_c)}</td>"
                   f"<td>{fmt(real_c)}</td><td>{len(r['fined'])}</td><td>{tech}</td><td>{tone}</td>"
                   f"<td>{verd}</td></tr>")
    out.append("</table><p class='muted'>Grupa govornika: iz zvaničnog spiska poslanika (parlament.gov.rs) ili iz "
               "natpisa na ekranu. Neprepoznati govornici su pod „Nepoznato“. „/ 10 min“ = na 10 minuta govora "
               "te grupe, da duže govorenje ne izgleda samo po sebi gore.</p>")
    return "".join(out)


# ---- context / spin checks and profiles ----------------------------------------------------------------------
def context_html(items, tlink):
    from factcheck import CONTEXT_COLORS
    out = ["<h2>Širi kontekst (spin)</h2>"]
    items = [c for c in items if c.get("kind") == "context"]
    if not items:
        return "".join(out) + ("<p class='muted'>Kada govornik optuži ili etiketira drugu stranu, ovde se pojavi "
                               "provera konteksta (dvostruki aršini, ranije izjave, zapisnik sednice).</p>")
    for c in sorted(items, key=lambda x: x["t"], reverse=True):
        res = c.get("result")
        out.append(f"<p>{tlink(c['t'])} <b>{esc(c['speaker'])}</b> <span class='muted'>({esc(c.get('why', ''))})"
                   f"</span>: „{esc(c['claim'][:200])}“<br>")
        if res:
            col = CONTEXT_COLORS.get(res["verdict"], "#444")
            out.append(f"<b style='color:{col}'>{esc(res['verdict'].upper())}</b> – {esc(res['explanation'])}")
            if res.get("sources"):
                out.append("<br><span class='muted'>Izvori:</span> " + " · ".join(
                    f"<a href='{esc(s['url'])}'>{esc(s['title'][:70])}</a>" for s in res["sources"][:5]))
        else:
            out.append("<i class='muted'>proveravam kontekst…</i>")
        out.append("</p>")
    out.append("<p class='muted'>Kontekst je informacija za gledaoce – mere se izriču isključivo po Poslovniku.</p>")
    return "".join(out)


def profiles_html(profs):
    out = [CSS, "<h2>Profili govornika (javni zapis, sa izvorima)</h2>"]
    if not profs:
        return "".join(out) + "<p class='muted'>Profil se pravi automatski kad poslanik dobije reč (Claude + " \
                              "pretraga interneta, jednom po poslaniku, čuva se u data/profiles).</p>"
    for name, p in profs.items():
        if p is None:
            out.append(f"<h3>{esc(name)}</h3><p class='muted'>pravim profil…</p>")
            continue
        out.append(f"<h3>{esc(p['name'])}</h3><p><span class='muted'>{esc(p.get('party_and_group', ''))}</span><br>"
                   f"{esc(p.get('summary', ''))}</p>")
        if p.get("roles"):
            out.append("<p><b>Funkcije:</b> " + "; ".join(esc(r) for r in p["roles"][:5]) + "</p>")
        for title, key, fmt in (
                ("Stavovi", "positions", lambda x: f"<b>{esc(x['topic'])}</b>: {esc(x['position'])}"),
                ("Izjave", "notable_statements", lambda x: f"„{esc(x['statement'])}“ – {esc(x['context'])}"),
                ("Kontroverze", "controversies", lambda x: f"{esc(x['what'])} <span class='muted'>"
                                                          f"({esc(x['reported_by'])})</span>")):
            if p.get(key):
                out.append(f"<p><b>{title}</b></p><ul>")
                for x in p[key]:
                    out.append(f"<li>{fmt(x)} <span class='muted'>{esc(x.get('date', ''))}</span> "
                               f"<a href='{esc(x['url'])}'>izvor</a></li>")
                out.append("</ul>")
    out.append("<p class='muted'>Profil sastavlja Claude iz javnih izvora; sve tvrdnje su pripisane izvoru. "
               "Proverite izvore pre daljeg korišćenja.</p>")
    return "".join(out)
