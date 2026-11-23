"""Public-record profiles of MPs (and other speakers), built once with Claude + web search and cached on disk.

Used as background so the judge and the context check can see spin: a speaker attacking others for something
their own side does too, contradicting their own earlier statements, leaving out context.
Rules for the content: neutral, every item attributed to a named source with a date, no labels of our own -
"according to X, he said Y" - and the same treatment for every party.
"""
import json
import os
import re
import threading
import queue

from common import DATA, fold_text, log
from judge import PRICES, make_client
from factcheck import SEARCH_PRICE, web_tool

PROFILE_DIR = os.path.join(DATA, "profiles")

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "party_and_group": {"type": "string"},
        "roles": {"type": "array", "items": {"type": "string"}},
        "positions": {"type": "array", "items": {
            "type": "object",
            "properties": {"topic": {"type": "string"}, "position": {"type": "string"},
                           "source_title": {"type": "string"}, "url": {"type": "string"}, "date": {"type": "string"}},
            "required": ["topic", "position", "source_title", "url", "date"], "additionalProperties": False}},
        "notable_statements": {"type": "array", "items": {
            "type": "object",
            "properties": {"statement": {"type": "string"}, "context": {"type": "string"},
                           "source_title": {"type": "string"}, "url": {"type": "string"}, "date": {"type": "string"}},
            "required": ["statement", "context", "source_title", "url", "date"], "additionalProperties": False}},
        "controversies": {"type": "array", "items": {
            "type": "object",
            "properties": {"what": {"type": "string"}, "reported_by": {"type": "string"},
                           "url": {"type": "string"}, "date": {"type": "string"}},
            "required": ["what", "reported_by", "url", "date"], "additionalProperties": False}},
    },
    "required": ["name", "summary", "party_and_group", "roles", "positions", "notable_statements", "controversies"],
    "additionalProperties": False,
}

SYSTEM = """You compile a short public-record profile of a Serbian politician for viewers of a live parliament
broadcast, so they can understand context and spin. Search the web (several searches: biography, party,
notable public statements, positions, controversies). Use reputable sources (official sites, Wikipedia,
established media from different sides, fact-checkers such as Istinomer, Raskrikavanje, FakeNews Tragač).
Rules:
- Neutral and factual. Every position, statement and controversy must be attributed to its source with a date
  ("prema pisanju N1, 2023: ..."). Quote or closely paraphrase what the person actually said.
- No labels or judgements of your own (do not call anyone a liar, conspiracy theorist, extremist...). If such a
  description is common, report it as "X ga je opisao kao ..." with the source.
- Include things relevant to judging consistency in debate: notable public claims (including ones fact-checkers
  rated), positions on key topics, past statements, conflicts with own party line.
- Only this person; if you are not sure a source is about the same person, leave it out.
- Everything in Serbian, Latin script. summary: 2-3 sentences. Max 6 positions, 6 statements, 5 controversies."""


def _path(name):
    return os.path.join(PROFILE_DIR, re.sub(r"[^a-z0-9]+", "_", fold_text(name)) + ".json")


def load(name):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def short(profile, limit=900):
    """Compact text for prompts."""
    if not profile:
        return ""
    bits = [f"{profile['name']} ({profile.get('party_and_group', '')}): {profile.get('summary', '')}"]
    for s in profile.get("notable_statements", [])[:4]:
        bits.append(f"- izjava ({s['date']}, {s['source_title']}): {s['statement']}")
    for c in profile.get("controversies", [])[:3]:
        bits.append(f"- prema {c['reported_by']} ({c['date']}): {c['what']}")
    return "\n".join(bits)[:limit]


class ProfileBuilder(threading.Thread):
    """Builds missing profiles in the background, one at a time; cached forever (delete the file to refresh)."""

    def __init__(self, settings, on_done, on_status):
        super().__init__(daemon=True)
        self.client = make_client(settings["api_key"])
        self.model = settings.get("fact_model") or settings["judge_model"]
        self.on_done, self.on_status = on_done, on_status
        self.q = queue.Queue()
        self.asked = set()
        self.cost = 0.0
        self.running = True

    def request(self, name, hint=""):
        key = fold_text(name)
        if key in self.asked:
            return
        self.asked.add(key)
        if load(name):
            self.on_done(name, load(name))
            return
        self.q.put((name, hint))

    def stop(self):
        self.running = False
        self.q.put(None)

    def run(self):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        while self.running:
            item = self.q.get()
            if item is None:
                break
            name, hint = item
            prof = self.build(name, hint)
            if prof:
                with open(_path(name), "w", encoding="utf-8") as f:
                    json.dump(prof, f, ensure_ascii=False, indent=1)
                self.on_done(name, prof)

    def build(self, name, hint=""):
        import anthropic
        msgs = [{"role": "user", "content": f"Napravi profil: {name}. {hint}".strip()}]
        try:
            for _ in range(3):
                r = self.client.messages.create(
                    model=self.model, max_tokens=16000, system=SYSTEM, tools=[web_tool(self.model, uses=6)],
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                    messages=msgs)
                pin, pout = PRICES.get(self.model, PRICES["claude-opus-5-5"])
                self.cost += (r.usage.input_tokens * pin + r.usage.output_tokens * pout) / 1e6
                stu = getattr(r.usage, "server_tool_use", None)
                self.cost += ((getattr(stu, "web_search_requests", 0) or 0) if stu else 0) * SEARCH_PRICE
                if r.stop_reason != "pause_turn":
                    break
                msgs.append({"role": "assistant", "content": r.content})
        except anthropic.APIError as e:
            log("profile:", e)
            self.on_status(f"Profil {name}: greška")
            return None
        if r.stop_reason == "max_tokens":
            log("profile: output cut off for", name)
            return None
        texts = [b.text for b in r.content if b.type == "text"]
        try:
            return json.loads(texts[-1])
        except (IndexError, ValueError):
            return None
