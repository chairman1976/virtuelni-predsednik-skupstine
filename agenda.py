"""Finds the agenda (dnevni red) of the sitting on the internet: parlament.gov.rs publishes it with the
convocation, and the media report it ("konstitutivna sednica ima šest tačaka").

With a Claude key: Claude + web search (~1-2 cents, once per session). Without: the local model + DuckDuckGo.
The YouTube descriptions of the Assembly's channel carry no agenda (checked), so the title + date are the
search clues.
"""
import json
import threading

from common import log

SCHEMA = {
    "type": "object",
    "properties": {
        "session": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"no": {"type": "integer"}, "title": {"type": "string"}},
            "required": ["no", "title"], "additionalProperties": False}},
        "sources": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            "required": ["title", "url"], "additionalProperties": False}},
    },
    "required": ["session", "items", "sources"],
    "additionalProperties": False,
}

SYSTEM = """You find the agenda (dnevni red) of a sitting of the National Assembly of Serbia (Narodna skupština
Republike Srbije). Search the web: parlament.gov.rs (saziv sednice, "dnevni red", stenografske beleške) first, then
news reports of the convocation ("predsednik Narodne skupštine sazvao je ... sednicu ... sa dnevnim redom").
A "nastavak" (continuation) sitting has the agenda of the sitting it continues. Items that were already finished on
earlier days still belong to the agenda - list them all, in order.
session: one line, e.g. "Prva sednica 15. saziva (konstitutivna), 17-24. novembar 2026".
items: every agenda item in order, title in Serbian Latin script, short but complete
("Izbor potpredsednika Narodne skupštine"). If you cannot find the agenda reliably, return items [] - never guess.
sources: the pages you used (title + URL), max 4."""


def _prompt(title, date):
    return (f"Sednica sa YouTube prenosa: „{title}“. Datum: {date or 'nepoznat'}.\n"
            "Nađi dnevni red ove sednice Narodne skupštine Srbije.")


def find(settings, title, date):
    """dict(session, items, sources) or None. Blocking - call from a thread."""
    if settings.get("_research") == "local":
        from local_llm import Ollama, research
        out, _n = research(Ollama(settings["_local_model"]), SYSTEM, _prompt(title, date), SCHEMA,
                           n_queries=3, read_pages=3)
        return out if out and out.get("items") else None
    import anthropic
    from factcheck import web_tool
    from judge import make_client
    client = make_client(settings["api_key"])
    model = settings.get("fact_model") or settings["judge_model"]
    msgs = [{"role": "user", "content": _prompt(title, date)}]
    try:
        for _ in range(3):
            r = client.messages.create(
                model=model, max_tokens=8000, system=SYSTEM, tools=[web_tool(model, uses=5)],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=msgs)
            if r.stop_reason != "pause_turn":
                break
            msgs.append({"role": "assistant", "content": r.content})
    except anthropic.APIError as e:
        log("agenda:", e)
        return None
    texts = [b.text for b in r.content if b.type == "text"]
    try:
        out = json.loads(texts[-1])
    except (IndexError, ValueError):
        return None
    return out if out.get("items") else None


def find_async(settings, title, date, on_done):
    threading.Thread(target=lambda: on_done(find(settings, title, date)), daemon=True).start()
