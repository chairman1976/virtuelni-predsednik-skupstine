"""Claude as the chair's assistant: reads the live transcript and reports breaches of order.

One request every ~20 s of new speech (or at a change of speaker). The Poslovnik articles sit in a
cached system prompt, so each call only pays full price for the new transcript lines.
Also reads the on-screen name caption from a video frame (vision).
"""
import base64
import json
import os
import queue
import ssl
import threading
import time

from common import CA_BUNDLE, fmt_time, log
from rules import KINDS, RULES_TEXT

PRICES = {  # $ per 1M tokens: input, output (cache read = 0.1x input, cache write = 1.25x input)
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
MODELS = [("claude-haiku-5-5", "Haiku 5.5 (jeftin, brz)"),
          ("claude-sonnet-5-5", "Sonnet 5.5"),
          ("claude-opus-5-5", "Opus 5.5 (najtačniji, najskuplji)")]

KIND_DOC = "\n".join(f"- {k}: {v[0]} ({v[1]})" for k, v in KINDS.items())

# persuasion / manipulation techniques (shown to viewers; not breaches of the Poslovnik by themselves)
TECHNIQUES = {
    "ad_hominem": "Napad na ličnost umesto na argument",
    "labeling": "Etiketiranje / obojeni izrazi (izdajnici, lopovi, tajkuni...)",
    "whataboutism": "Relativizacija - „a šta ste vi radili“",
    "strawman": "Slamnati čovek - iskrivljavanje tuđeg stava",
    "false_dilemma": "Lažna dilema - ili-ili",
    "false_analogy": "Lažna analogija / poređenje",
    "appeal_to_fear": "Zastrašivanje",
    "appeal_to_emotion": "Manipulacija emocijama / patetika",
    "scapegoating": "Traženje krivca (žrtveno jagnje)",
    "red_herring": "Skretanje teme",
    "cherry_picking": "Selektivni podaci",
    "unsupported_accusation": "Optužba bez dokaza",
    "conspiracy": "Teorija zavere",
    "mockery": "Ismevanje / sarkazam",
    "victimhood": "Predstavljanje sebe kao žrtve",
    "bandwagon": "Pozivanje na većinu - „svi građani znaju“",
    "dehumanization": "Dehumanizacija protivnika",
    "gaslighting": "Poricanje očiglednog",
}
TONES = ["argumentovan", "smiren", "oštar", "ironičan", "emotivan", "uvredljiv", "agresivan", "manipulativan"]
TECHNIQUE_DOC = "\n".join(f"  - {k}: {v}" for k, v in TECHNIQUES.items())

SYSTEM = f"""You assist the presiding officer (predsedavajući) of the National Assembly of the Republic of Serbia.
You read an automatic, live speech-to-text transcript of the plenary session and detect breaches of order
under the Rules of Procedure (Poslovnik). The app applies the penalty ladder itself (upozorenje -> opomena ->
druga opomena -> oduzimanje reči -> udaljenje); you only report what happened.

THE RULES (verbatim, Poslovnik Narodne skupštine, čl. 96-116):
<poslovnik>
{RULES_TEXT}
</poslovnik>

VIOLATION KINDS you may report (field "kind"):
{KIND_DOC}

HOW TO JUDGE
- direct_address: a speaker turns to another MP (or group of MPs) in the second person - "ti", or "vi" aimed at
  specific MPs ("vi ste lopovi", "šta se smeješ ti", "ti, u trećem redu") - instead of speaking to the chair and
  the Assembly. Addressing the chair ("poštovani predsedavajući"), the citizens, or a member of the Government
  in debate is allowed. Naming an MP in the third person is allowed.
- insult / profanity: offensive words about people or groups (lopovi, kriminalci, izdajnici, nevaspitani,
  "pokrali ste") or swearing. Harsh criticism of policies, laws or the Government's record is NOT an insult.
- private_life: facts or judgements about the private life of other persons (family, health, relationships...).
- off_topic: the speaker clearly and for a sustained stretch talks about something unrelated to the agenda item
  under debate. Passing remarks and political framing do not count.
- heckling (dobacivanje) and no_floor are the most often missed breaches - look for them actively:
  * any line from a label other than TRENUTNI GOVORNIK and other than the chair, while that speaker has the
    floor, is someone talking without the floor: short shouts ("sramota", "lažeš", "sedi dole", "bravo" as
    mockery), chanting, arguing back, comments. A known name on such a line -> offender is that MP (heckling
    if it is an interjection, no_floor if they hold a longer speech). "Glas N" -> offender "Glas N".
  * also infer it from reactions even when the heckler's words were not transcribed: the speaker says
    "ne dobacujte", "pustite me da govorim", "nemojte da vičete", "ne prekidajte me", "čujete li se", or the
    chair says "molim vas da ne dobacujete", "molim poslanika X da ne dobacuje", "tišina u sali" -> report
    heckling; offender = the person the chair names, else "Glas iz sale".
  * a line marked (drugi glas) inside someone else's speech is strong evidence of an interruption.
- no_floor: someone (not the chair) speaks at length without having been given the floor.
- shouting: the line is marked GLASNO (much louder than that person's normal level) AND the words show yelling
  or aggression. Loudness alone is not enough.
- point_of_order_abuse: a speaker given the floor for "povreda Poslovnika" does not name the article violated or
  uses the time to debate instead.
- physical_attack: only when the transcript clearly shows it.
- Never report the chair (label "Predsedavajući") as an offender. Report what the chair does in chair_actions:
  e.g. "izričem vam opomenu" -> opomena; the chair warning someone -> upozorenje; giving the floor -> dao_rec.
- LYING is not a breach of order in the Poslovnik. Put checkable factual claims in fact_checks instead (they are
  checked on the web). Most valuable first:
  1. concrete claims about events and actions - who did what, where, when ("studenti su prekrečili mural
     Kosovo je Srbija u Bulevaru Arsenija Čarnojevića", "vlada je zatvorila 20 bolnica", "on je osuđen za...");
  2. numbers and statistics (plate, budžet, broj glasova, procenti);
  3. allegations of crimes or corruption stated as fact.
  NOT worth checking: the speaker's own personal life or pay, opinions, predictions, rhetorical questions,
  vague generalities. At most 2 per call; never repeat a claim listed under VEĆ POSLATO NA PROVERU.
  "claim": one self-contained sentence in Serbian Latin that makes sense without the transcript (who did what,
  where), cleaned of recognition errors.
- The transcript has recognition errors: judge the evident meaning, but only report what the words support.
- QUOTING is not insulting. Always read the context: when a speaker repeats insulting words that were said TO or
  ABOUT them (or someone else) - "rekli ste mi da sam lopov", "nazvali ste nas izdajnicima", reading a hostile
  headline or post aloud - the speaker is not insulting anyone. Do not report insult/profanity for the quoted
  words; at most a "disorder" with confidence <= 0.5 if the sentence itself is still problematic (e.g. the quote
  is used to provoke). If the quote is WRONG - the person never said it, or it is distorted / taken out of context
  - that is manipulation and a falsehood: put it in rhetoric (strawman or gaslighting, with the quote) and in
  fact_checks ("X je rekao/la ..." as the claim to verify).
- A word marked "(?)" was heard uncertainly by the speech recognition and may be a mishearing of a similar,
  harmless word (e.g. "svinje" for "sve nje", "lopovi" for "lopte"). Never report insult, profanity, private
  life or threats when the offending word is marked (?) or does not fit the sentence and context - then either
  skip it or report it with confidence at most 0.5 (shown as suspicion, no measure). The same applies to an
  unmarked insulting word that breaks the grammar or sense of its sentence - the recognizer often mishears
  confidently: "vi ste lopovi kandidate predložili" ("lopovi" does not fit; probably "loše"/"lepo") ->
  confidence at most 0.5. An insult is certain only when the sentence is coherent with it ("vi ste lopovi i
  to cela Srbija zna").
  Never report a line that is garbled or not real Serbian (e.g. "Ebna sөmde", isolated syllables) - there is
  nothing to judge in it.
- Confidence: 0.9+ the words clearly are the breach (e.g. "lopovi ste", "ti, u trećem redu"); 0.7-0.85 a
  breach a chair would usually sanction; 0.5-0.65 borderline. Do not report anything below 0.4.
  A heated debate is normal; report clear breaches.
- Judge ONLY the lines under NOVO. Lines under KONTEKST were judged before - never report them again.
- "quote" must be copied from the NOVO lines. "offender" is the speaker label exactly as written on that line,
  unless the text reveals the person's real name (e.g. the chair says "molim poslanicu Anu Anić da ne
  dobacuje" -> offender "Ana Anić"; give names in the nominative).
- speaker_names: if the text reveals who is behind a voice label (Glas N / Govornik N), map it. Only from what
  identifies the SPEAKER (the chair giving them the floor, "ja, kao ...", others answering "kolega X je rekao").
  A name the label itself says to someone ("gospodine X", "uvaženi X", "vi, X, ste ...") is the person being
  ADDRESSED - never the speaker. If unsure, leave the label out.

RHETORIC (information for viewers, never a breach by itself) - for every speaker in NOVO (not the chair):
- tone: one overall tone of what they said in NOVO.
- rhetoric: clear uses of persuasion / manipulation techniques, each with the exact quote. Techniques:
{TECHNIQUE_DOC}
  Only clear instances; ordinary political argument is not manipulation. Explanation: one short sentence,
  Serbian Latin, saying why this quote is that technique.
  Mockery also covers imitating someone: deliberately repeating another person's publicised slip,
  mispronunciation, accent or catch-phrase (e.g. stressing "sumLJam" instead of "sumnjam" when an MP in the
  debate is known for saying "sumljam" - see DRUGI POSLANICI and the session so far). Then name the person
  mocked in the explanation ("ismeva poslanicu X imitirajući njen izgovor ..."). Work out who is the target
  from the profiles and the debate - do not assume it is the chair. Deliberate mispronunciations in the
  transcript are kept as spoken; do not treat them as transcription errors when they echo such a slip.
- explanation: one short sentence in Serbian (Latin script), as the chair would say it in the hall.
- legal_note: "" unless the words could also be a criminal offence or ground a lawsuit outside the hall (insult
  - KZ čl. 170; a false factual claim harming a named person's reputation - civil damages, defamation is not a
  crime since 2012; threats - KZ čl. 138; hate speech - KZ čl. 317). Then one short sentence in Serbian naming the
  ground AND whether parliamentary immunity (Ustav čl. 103: no liability for an opinion expressed in performing
  the MP's function) would protect the speaker - for MPs speaking in the plenary usually yes, disputed for
  threats/false facts; ministers and guests are not MPs. Information only, never decides the measure.
- PREDSEDAVAJUĆI is the person running the sitting (the Speaker or a deputy speaker). MPs addressing the chair
  ("gospodine predsedavajući", "predsedniče Čupiću", "potpredsednice Petrović") is NOT direct address to another
  MP and not a breach.
- agenda_item: the agenda item being debated now. If DNEVNI RED SEDNICE is given, return the exact title of
  one of its items - the current one, or the next one once the chair moves on ("prelazimo na ... tačku");
  "" if it is not clear. Without a list: a short title if the text makes it clear, else "".
- chair_person: full name of whoever is chairing if the text makes it clear (MPs address the chair by name, the
  chair says who takes over, "predsedavanje preuzima ..."); otherwise "". Never guess.
- JAVNI PROFIL and ZAPISNIK are background for understanding spin and the situation (who blames whom, who is
  consistent). Use them for tone/rhetoric; measures are decided only by what is said in NOVO under the rules.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "violations": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "offender": {"type": "string"},
                "quote": {"type": "string"},
                "explanation": {"type": "string"},
                "confidence": {"type": "number"},
                "legal_note": {"type": "string"},
            },
            "required": ["kind", "offender", "quote", "explanation", "confidence", "legal_note"],
            "additionalProperties": False}},
        "fact_checks": {"type": "array", "items": {
            "type": "object",
            "properties": {"speaker": {"type": "string"}, "claim": {"type": "string"}, "why": {"type": "string"}},
            "required": ["speaker", "claim", "why"], "additionalProperties": False}},
        "chair_actions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["upozorenje", "opomena", "oduzimanje_reci", "udaljenje", "pauza", "dao_rec"]},
                "target": {"type": "string"},
                "quote": {"type": "string"}},
            "required": ["action", "target", "quote"], "additionalProperties": False}},
        "agenda_item": {"type": "string"},
        "tone": {"type": "array", "items": {
            "type": "object",
            "properties": {"speaker": {"type": "string"}, "tone": {"type": "string", "enum": TONES}},
            "required": ["speaker", "tone"], "additionalProperties": False}},
        "rhetoric": {"type": "array", "items": {
            "type": "object",
            "properties": {"speaker": {"type": "string"},
                           "technique": {"type": "string", "enum": list(TECHNIQUES)},
                           "quote": {"type": "string"}, "explanation": {"type": "string"}},
            "required": ["speaker", "technique", "quote", "explanation"], "additionalProperties": False}},
        "chair_person": {"type": "string"},
        "speaker_names": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "name": {"type": "string"}},
            "required": ["label", "name"], "additionalProperties": False}},
    },
    "required": ["violations", "fact_checks", "chair_actions", "agenda_item", "chair_person", "speaker_names",
                 "tone", "rhetoric"],
    "additionalProperties": False,
}

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "has_caption": {"type": "boolean"},
        "name": {"type": "string"},
        "party": {"type": "string"},
        "role": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["has_caption", "name", "party", "role", "confidence"],
    "additionalProperties": False,
}
CAPTION_PROMPT = ("This is a frame of a Serbian National Assembly broadcast. Is there a name caption that "
                  "identifies the person speaking right now - typically a lower third with just a name, maybe "
                  "with a party / parliamentary group or function under it? If yes, return the name in the "
                  "nominative, Latin script, party/group and role if shown. NOT a speaker caption: news tickers "
                  "and headline banners (e.g. 'ČUPIĆ: PREDSEDNIČKE IZBORE RASPISAĆU...' - a surname followed "
                  "by a quote or a news sentence), channel logos, clocks, text about other people. If there is "
                  "no speaker caption: has_caption=false, empty strings. confidence 0-1 that this really is the "
                  "current speaker's name caption.")


VERIFY_SCHEMA = {"type": "object", "properties": {"reasoning": {"type": "string"}, "is_breach": {"type": "boolean"}},
                 "required": ["reasoning", "is_breach"], "additionalProperties": False}


def make_client(api_key):
    import anthropic
    kw = {}
    if os.path.exists(CA_BUNDLE):
        kw["http_client"] = anthropic.DefaultHttpxClient(verify=ssl.create_default_context(cafile=CA_BUNDLE))
    return anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0, **kw)


class Judge(threading.Thread):
    def __init__(self, settings, on_result, on_caption, on_status):
        super().__init__(daemon=True)
        self.s = settings
        self.on_result, self.on_caption, self.on_status = on_result, on_caption, on_status
        self.local = None
        if settings.get("_judge") == "local":       # Ollama on this PC instead of Claude
            from local_llm import Ollama
            self.local = Ollama(settings["_local_model"])
            self.client = None
        else:
            self.client = make_client(settings["api_key"])
        self.model = settings["judge_model"]
        self.effort = settings.get("judge_effort", "low")
        self.lock = threading.Lock()
        self.pending = []           # lines not yet judged
        self.context = []           # last judged lines
        self.flush_now = False
        self.caption_q = queue.Queue(maxsize=1)
        self.running = True
        self.cost = 0.0
        self.calls = 0
        self.agenda = ""
        self.agenda_list = ""           # the sitting's whole agenda, found on the internet (agenda.py)
        self.speaker_info = ""
        self.chair_info = "nije još prepoznat"
        self.last_call = time.time()
        self.last_caption_name = None
        self.sent_claims = []
        self.speaker_profile = ""
        self.others_profile = ""        # other MPs in this debate: allusions, imitation, mocking
        self.session_record = ""

    # ---- inputs (from the GUI thread) -------------------------------------------------------
    def add_line(self, line):
        with self.lock:
            self.pending.append(line)

    def speaker_changed(self):
        self.flush_now = True

    def request_caption(self, jpeg_bytes, t):
        try:
            self.caption_q.put_nowait((jpeg_bytes, t))
        except queue.Full:
            pass

    def stop(self):
        self.running = False

    def reset(self):
        """After a seek: lines from before the jump are neither judged nor used as context."""
        with self.lock:
            self.pending = []
            self.context = []
            self.flush_now = False

    # ---- loop ---------------------------------------------------------------------------------
    def run(self):
        while self.running:
            time.sleep(0.5)
            try:
                jpeg, t = self.caption_q.get_nowait()
                self._caption(jpeg, t)
            except queue.Empty:
                pass
            with self.lock:
                if not self.pending:
                    continue
                span = self.pending[-1]["t"] - self.pending[0]["t"]
                due = (self.flush_now or span >= self.s["judge_every_s"]
                       or time.time() - self.last_call >= self.s["judge_every_s"] * 1.5)
                if not due:
                    continue
                batch, self.pending, self.flush_now = self.pending, [], False
            self._judge(batch)
            self.context = (self.context + batch)[-14:]

    @staticmethod
    def _fmt(line):
        extra = []
        if line.get("loud_db", 0) >= line.get("shout_db", 99):
            extra.append(f"GLASNO +{line['loud_db']:.0f} dB")
        if line.get("voice_change"):
            extra.append("drugi glas")
        tail = f"  ({', '.join(extra)})" if extra else ""
        return f"[{fmt_time(line['t'])}] {line['who']}: {line['text']}{tail}"

    def _usage(self, u):
        pin, pout = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cost += (u.input_tokens * pin + cr * pin * 0.1 + cw * pin * 1.25 + u.output_tokens * pout) / 1e6
        self.calls += 1
        self.no_credit = False

    def _call(self, content, schema, system=None, max_tokens=4000):
        if self.local:
            if not isinstance(content, str):
                return None                         # caption images: the local OCR does that
            res = self.local.json(system, content, schema, max_tokens=min(max_tokens, 3000))
            self.calls = self.local.calls
            if res is None:
                self.on_status("Lokalni model ne odgovara (radi li Ollama?)")
            return res
        import anthropic
        kw = {}
        if system:
            kw["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=max_tokens,
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content}], **kw)
        except anthropic.AuthenticationError:
            self.on_status("Claude: neispravan API ključ.")
            return None
        except anthropic.RateLimitError:
            self.on_status("Claude: previše zahteva, čekam…")
            time.sleep(10)
            return None
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e):
                # the account ran out of credit: say so plainly (the app shows it in red until it works again)
                self.no_credit = True
                self.on_status("Claude API: nalog nema kredita - dopuni ga na console.anthropic.com "
                               "(Plans & Billing). Sudija i provere ne rade dok se ne dopuni.")
                time.sleep(30)
            else:
                self.on_status(f"Claude: loš zahtev - {e.message}")
            log("bad request:", e)
            return None
        except anthropic.APIStatusError as e:
            self.on_status(f"Claude: greška servera {e.status_code}")
            return None
        except anthropic.APIConnectionError as e:
            self.on_status("Claude: nema veze sa serverom.")
            log("connection:", e)
            return None
        self._usage(r.usage)
        if r.stop_reason == "refusal":
            self.on_status("Claude je odbio ovaj deo transkripta.")
            return None
        text = next((b.text for b in r.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError:
            log("bad json:", text[:300])
            return None

    def _judge(self, batch):
        msg = ((f"DNEVNI RED SEDNICE:\n{self.agenda_list}\n" if self.agenda_list else "")
               + f"TAČKA DNEVNOG REDA: {self.agenda or 'nepoznata'}\n"
               f"PREDSEDAVAJUĆI (ko vodi sednicu): {self.chair_info}\n"
               f"TRENUTNI GOVORNIK: {self.speaker_info or 'nepoznat'}\n\n"
               + (f"JAVNI PROFIL GOVORNIKA (pozadina, sa izvorima):\n{self.speaker_profile}\n\n"
                  if self.speaker_profile else "")
               + (f"DRUGI POSLANICI U OVOJ RASPRAVI (aluzije, imitacija, ismevanje):\n{self.others_profile}\n\n"
                  if self.others_profile else "")
               + (f"ZAPISNIK OVE SEDNICE DO SADA (po grupama):\n{self.session_record}\n\n"
                  if self.session_record else "")
               + "KONTEKST (već ocenjeno, ne prijavljuj ponovo):\n"
               + ("\n".join(self._fmt(l) for l in self.context) or "(nema)")
               + "\n\nVEĆ POSLATO NA PROVERU (ne ponavljaj):\n"
               + ("\n".join(f"- {c}" for c in self.sent_claims[-12:]) or "(ništa)")
               + "\n\nNOVO (oceni samo ovo):\n" + "\n".join(self._fmt(l) for l in batch))
        self.last_call = time.time()
        t0 = time.time()
        res = self._call(msg, SCHEMA, system=SYSTEM)
        if res is not None and self.local:
            res["violations"] = [v for v in res.get("violations", []) if self._verify_local(batch, v)]
        if res is not None:
            self.sent_claims += [f["claim"] for f in res.get("fact_checks", [])]
        if res is not None:
            res["_t"] = batch[-1]["t"]
            res["_batch"] = batch
            res["_src"] = self              # the app drops results of a judge from a stopped session
            self.on_result(res)
            if self.local:
                self.on_status(f"Lokalni sudija ({self.local.model}): ocenjeno {len(batch)} redova za "
                               f"{time.time() - t0:.1f} s ({self.calls} poziva, besplatno)")
            else:
                self.on_status(f"Claude: ocenjeno {len(batch)} redova za {time.time() - t0:.1f} s  "
                               f"(ukupno ${self.cost:.3f}, {self.calls} poziva)")

    def _verify_local(self, batch, v):
        """A small local model reports far too much (tested: 43 reports where Claude had 8, almost all at 0.85).
        A second, narrow question per report keeps only the clear ones (tested: 4 of 43 kept). Same system
        prompt, so Ollama reuses the cached Poslovnik and the check takes ~5 s."""
        kind = KINDS.get(v.get("kind"))
        if not kind:
            return False
        q = ("PROVERA JEDNE PRIJAVE. Transkript:\n" + "\n".join(self._fmt(x) for x in batch)
             + f"\n\nPrijava: {v['offender']} - {v['kind']} ({kind[0]}, {kind[1]})\nCitat: „{v['quote']}“\n\n"
             "Strogo proveri: da li ovaj citat SAM PO SEBI jasno krši baš navedeni član Poslovnika? Oštra kritika, "
             "ironija, političke optužbe, čitanje biografija, najava govornika, obraćanje predsedavajućem, "
             "pozivanje na Poslovnik i greške prepoznavanja govora NISU prekršaj. Ako nisi siguran, odgovor je "
             "false. reasoning: jedna rečenica.")
        r = self.local.json(SYSTEM, q, VERIFY_SCHEMA, max_tokens=200)
        if not (r and r.get("is_breach")):
            log("local judge dropped:", v["kind"], v["offender"], v["quote"][:60])
            return False
        v["confidence"] = min(v.get("confidence", 0.6), 0.75)        # a local verdict is never "certain"
        v["explanation"] = v.get("explanation", "") + " (lokalni model)"
        return True

    def _caption(self, jpeg, t):
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(jpeg).decode()}},
            {"type": "text", "text": CAPTION_PROMPT},
        ]
        res = self._call(content, CAPTION_SCHEMA, max_tokens=1000)
        ok = bool(res and res.get("has_caption") and res.get("name") and res.get("confidence", 0) >= 0.8)
        name = res["name"].strip().lower() if ok else None
        # act only when two reads in a row agree (a single read of a ticker or a cut-away is not enough)
        if ok and name == self.last_caption_name:
            res["_src"] = self
            self.on_caption(res, t)
        self.last_caption_name = name
