"""Fact-checking claims made in the debate, with Claude + web search.

The judge only flags checkable factual claims (numbers, statistics, allegations stated as fact); each one is
checked here, one at a time, with the server-side web_search tool. Lying is not a breach of order in the
Poslovnik, so verdicts are information for the viewer - they never lead to a measure.
Measured 2026-11-23: Haiku 5.5 with web_search_20260209 answered from memory without searching; with
web_search_20250305 it searched (2 queries) - ~17 s and ~$0.02 per claim.
"""
import json
import queue
import threading
import time

from common import fold_text, log
from judge import PRICES, make_client

VERDICTS = ["tačno", "uglavnom tačno", "delimično tačno", "netačno", "neproverljivo", "mišljenje"]
VERDICT_COLORS = {"tačno": "#1e8449", "uglavnom tačno": "#52a447", "delimično tačno": "#d4a017",
                  "netačno": "#c0392b", "neproverljivo": "#7f8c8d", "mišljenje": "#7f8c8d"}
SEARCH_PRICE = 0.01  # $ per web search

LEGAL = {
    "type": "object",
    "properties": {
        "actionable": {"type": "string", "enum": ["ne", "moguće", "verovatno"]},
        "grounds": {"type": "string"},
        "immunity": {"type": "string", "enum": ["štiti ga", "verovatno štiti", "sporno", "ne štiti",
                                                 "nije poslanik"]},
        "explanation": {"type": "string"},
    },
    "required": ["actionable", "grounds", "immunity", "explanation"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "explanation": {"type": "string"},
        "sources": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            "required": ["title", "url"], "additionalProperties": False}},
        "legal": LEGAL,
    },
    "required": ["verdict", "explanation", "sources", "legal"],
    "additionalProperties": False,
}

SYSTEM = """You fact-check claims made by speakers in the National Assembly of Serbia, live, for viewers.
ALWAYS search the web before answering (at least one search); never answer from memory alone.
The claim may be raw speech-to-text with recognition errors: first work out from it and the context what the
speaker actually claimed (who did what, where), check that, and start the explanation with the claim in one
clean sentence.
Prefer primary and official sources: Republički zavod za statistiku (stat.gov.rs), Narodna banka Srbije,
ministries and government sites, parlament.gov.rs, official election commissions, EU/UN/World Bank data; then
reputable media and Serbian fact-checking sites (Istinomer, Raskrikavanje, FakeNews Tragač).
Verdict: "tačno" / "uglavnom tačno" / "delimično tačno" / "netačno"; "neproverljivo" if reliable data can't be
found; "mišljenje" if it is a value judgement, not a checkable fact. Judge the claim as of the session date.
explanation: 2-4 sentences in Serbian, Latin script, neutral and concrete (give the real numbers and dates).
sources: the pages you actually used (title + URL), most important first, max 4.
legal: could the claim (if false, or as stated about a person) ground a lawsuit or a criminal complaint, on what
grounds, and would the speaker's parliamentary immunity protect them. If the claim is true, an opinion, or about
no identifiable person/organisation: actionable "ne", grounds "".
LEGAL NOTE (information for viewers, not legal advice; Serbian law as of the session date):
- Defamation (kleveta) is NOT a criminal offence in Serbia since 2012; a false factual claim that harms someone's
  honour or reputation can ground a CIVIL claim for non-pecuniary damages (Zakon o obligacionim odnosima).
- Insult (uvreda, Krivični zakonik čl. 170) is still a criminal offence (private prosecution, fine).
- Other offences can be committed by words: threats (ugrožavanje sigurnosti, KZ čl. 138), incitement to
  national, racial or religious hatred (KZ čl. 317), spreading false news that causes panic (KZ čl. 343).
- Immunity of MPs (Ustav čl. 103): an MP cannot be held criminally or otherwise liable for an opinion expressed
  or a vote cast in performing the MP's function (imunitet neodgovornosti) - in practice this covers what is
  said in the plenary; whether it covers false factual claims or threats (not "opinions") is disputed. It does
  not protect ministers or other non-MP speakers, nor statements made outside the MP's function.
Fill "legal" only with what the claim itself supports; be careful and attributed, never accuse."""


CONTEXT_VERDICTS = ["dvostruki aršini", "nedosledno ranijim izjavama", "suprotno zapisniku sednice",
                    "nedostaje kontekst", "potkrepljeno", "nema dovoljno podataka"]
CONTEXT_COLORS = {"dvostruki aršini": "#c0392b", "nedosledno ranijim izjavama": "#c0392b",
                  "suprotno zapisniku sednice": "#c0392b", "nedostaje kontekst": "#d4a017",
                  "potkrepljeno": "#1e8449", "nema dovoljno podataka": "#7f8c8d"}
CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": CONTEXT_VERDICTS},
        "explanation": {"type": "string"},
        "sources": SCHEMA["properties"]["sources"],
        "legal": LEGAL,
    },
    "required": ["verdict", "explanation", "sources", "legal"],
    "additionalProperties": False,
}
CONTEXT_SYSTEM = """You give viewers of a live Serbian parliament broadcast the wider context of an accusation or
blame a speaker just made about others (spin analysis). This is information only - it never decides measures;
order in the hall is judged strictly by the Rules of Procedure.
Check, with web searches and the material given:
- double standard: does the speaker's own party / group / camp do or have what they accuse others of
  (e.g. accusing opponents of conspiracy theorists while prominent figures on their own side are widely
  documented as spreading conspiracy theories)?
- inconsistency: did the speaker say or do the opposite before?
- the session record: does what happened in THIS session (measures, insults, false claims per group, given
  below) support or contradict the blame (e.g. "you brought bad blood into the Assembly")?
- missing context that changes the meaning.
Be strictly even-handed: the same standard for every party; if the accusation is supported, say
"potkrepljeno". Every factual point needs a source (or "zapisnik sednice" for the session record). No labels of
your own - attribute ("prema Istinomeru..."). explanation: 2-4 sentences, Serbian Latin, concrete.
legal: could the accusation ground a lawsuit (e.g. a false factual claim about a named person) and does immunity
protect the speaker.
LEGAL NOTE (information for viewers, not legal advice; Serbian law as of the session date):
- Defamation (kleveta) is NOT a criminal offence in Serbia since 2012; a false factual claim that harms someone's
  honour or reputation can ground a CIVIL claim for non-pecuniary damages (Zakon o obligacionim odnosima).
- Insult (uvreda, Krivični zakonik čl. 170) is still a criminal offence (private prosecution, fine).
- Other offences can be committed by words: threats (ugrožavanje sigurnosti, KZ čl. 138), incitement to
  national, racial or religious hatred (KZ čl. 317), spreading false news that causes panic (KZ čl. 343).
- Immunity of MPs (Ustav čl. 103): an MP cannot be held criminally or otherwise liable for an opinion expressed
  or a vote cast in performing the MP's function (imunitet neodgovornosti) - in practice this covers what is
  said in the plenary; whether it covers false factual claims or threats (not "opinions") is disputed. It does
  not protect ministers or other non-MP speakers, nor statements made outside the MP's function.
Fill "legal" only with what the claim itself supports; be careful and attributed, never accuse."""


def web_tool(model, uses=4):
    # Haiku did not search with the newer dynamic-filtering version (tested); bigger models use it
    if "haiku" in model:
        return {"type": "web_search_20250305", "name": "web_search", "max_uses": uses}
    return {"type": "web_search_20260209", "name": "web_search", "max_uses": uses}


class FactChecker(threading.Thread):
    def __init__(self, settings, on_result, on_status, session_date=""):
        super().__init__(daemon=True)
        self.local = None
        if settings.get("_research") == "local":
            from local_llm import Ollama
            self.local = Ollama(settings["_local_model"])
        else:
            self.client = make_client(settings["api_key"])
        self.model = settings.get("fact_model") or settings["judge_model"]
        self.on_result, self.on_status = on_result, on_status
        self.session_date = session_date
        self.q = queue.Queue()
        self.seen = []
        self.running = True
        self.cost = 0.0
        self.checked = 0

    def stop(self):
        self.running = False
        self.q.put(None)

    def submit(self, item):
        """item: dict(id, t, speaker, claim, why, context). False if the same claim was already queued."""
        import difflib
        key = fold_text(item["claim"])
        words = {w for w in key.split() if len(w) > 3}
        if not item.get("manual"):                 # what the user asks for is always checked
            for k in self.seen:
                kw = {w for w in k.split() if len(w) > 3}
                overlap = len(words & kw) / max(1, min(len(words), len(kw)))
                if difflib.SequenceMatcher(None, k, key).ratio() > 0.7 or overlap >= 0.6:
                    return False
        self.seen.append(key)
        self.q.put(item)
        return True

    def run(self):
        while self.running:
            item = self.q.get()
            if item is None:
                break
            t0 = time.time()
            res = self._check(item)
            if res is not None:
                res["seconds"] = round(time.time() - t0, 1)
                res["_src"] = self
                self.on_result(item["id"], res)

    def _check(self, item):
        if item.get("kind") == "context":
            return self._run(item, CONTEXT_SYSTEM, CONTEXT_SCHEMA, (
                f"Datum sednice: {self.session_date or 'nepoznat'}.\n"
                f"Govornik: {item['speaker']} ({item.get('group') or 'grupa nepoznata'})\n"
                f"Šta je rekao (automatski transkript): „{item['claim']}“\n"
                f"Tehnika koju je sudija prepoznao: {item.get('why', '')}\n\n"
                f"Javni profil govornika (sa izvorima):\n{item.get('profile') or '(nema)'}\n\n"
                f"Zapisnik ove sednice po grupama:\n{item.get('record') or '(nema)'}\n\n"
                f"Kontekst iz transkripta: {item.get('context', '')}\n\nDaj širi kontekst ove izjave."))
        prompt = (f"Datum sednice: {self.session_date or 'nepoznat'}.\n"
                  f"Govornik: {item['speaker']}\n"
                  f"Tvrdnja: „{item['claim']}“\n"
                  f"Zašto je sporna: {item.get('why', '')}\n"
                  f"Kontekst iz transkripta (automatski, sa greškama): {item.get('context', '')}\n\n"
                  "Proveri tvrdnju na internetu i oceni je.")
        return self._run(item, SYSTEM, SCHEMA, prompt)

    def _run(self, item, system, schema, prompt):
        if self.local:
            from local_llm import research
            out, searches = research(self.local, system, prompt, schema)
            if out is None:
                self.on_status("Provera činjenica (lokalno): model nije odgovorio.")
                return None
            out["searches"] = searches
            out["explanation"] = out.get("explanation", "") + " (lokalni model)"
            self.checked += 1
            return out
        import anthropic
        msgs = [{"role": "user", "content": prompt}]
        searches = 0
        try:
            for _ in range(3):
                r = self.client.messages.create(
                    model=self.model, max_tokens=12000, system=system, tools=[web_tool(self.model)],
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                    messages=msgs)
                self._usage(r.usage)
                stu = getattr(r.usage, "server_tool_use", None)
                searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
                if r.stop_reason != "pause_turn":
                    break
                msgs.append({"role": "assistant", "content": r.content})
        except anthropic.RateLimitError:
            self.on_status("Provera činjenica: previše zahteva, kasnije…")
            return None
        except anthropic.APIStatusError as e:
            self.on_status(f"Provera činjenica: greška {e.status_code}")
            log("factcheck:", e)
            return None
        except anthropic.APIConnectionError:
            self.on_status("Provera činjenica: nema veze.")
            return None
        if r.stop_reason == "max_tokens":
            log("check: output cut off")
            return None
        if r.stop_reason == "refusal":
            return {"verdict": "neproverljivo", "explanation": "Model je odbio da proveri ovu tvrdnju.",
                    "sources": [], "searches": searches}
        texts = [b.text for b in r.content if b.type == "text"]
        try:
            out = json.loads(texts[-1])
        except (IndexError, ValueError):
            log("factcheck bad json:", texts[-1:])
            return None
        out["searches"] = searches
        self.cost += searches * SEARCH_PRICE
        self.checked += 1
        return out

    def _usage(self, u):
        pin, pout = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        self.cost += (u.input_tokens * pin + u.output_tokens * pout) / 1e6
