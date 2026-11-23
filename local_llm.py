"""Local model instead of the Claude API: Ollama on this PC + a free web search.

Everything the app asks Claude for is "text in, JSON out" (judge, rhetoric, chair, fact checks, profiles,
context checks). Ollama does the same with structured output (the JSON schema is forced by a grammar), so the
rest of the app does not change. No key, no cost, nothing leaves the PC except the web searches.

Weaker than Claude: a small model understands Serbian and the Poslovnik worse, and it cannot search the web
itself - here we search (DuckDuckGo, Bing as fallback), read the top pages and hand the text to the model.
"""
import html as htmllib
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from common import CA_BUNDLE, log

HOST = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
# (model, label) - the first one is the default. Sizes are the download / VRAM need at 4-bit.
MODELS = [("qwen2.5:7b", "Qwen 2.5 7B (4,7 GB)"),
          ("gemma3:4b", "Gemma 3 4B (3,3 GB, brži, slabiji)"),
          ("llama3.1:8b", "Llama 3.1 8B (4,9 GB)")]
NUM_CTX = 16384             # the judge's system prompt (Poslovnik) alone is ~9k tokens
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"


def _local(path, body=None, timeout=600):
    """HTTP to the Ollama server on this PC (never through a proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HOST + path, data=data, headers={"Content-Type": "application/json"})
    return opener.open(req, timeout=timeout)


def ollama_exe():
    p = shutil.which("ollama")
    if p:
        return p
    p = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    return p if os.path.exists(p) else None


def status(model):
    """('ok' | 'no_ollama' | 'not_running' | 'no_model', message)."""
    try:
        with _local("/api/tags", timeout=3) as r:
            tags = json.load(r)
    except OSError:
        if not ollama_exe():
            return "no_ollama", "Ollama nije instalirana (ollama.com/download)."
        return "not_running", "Ollama je instalirana, ali ne radi."
    have = {m["name"] for m in tags.get("models", [])} | {m["model"] for m in tags.get("models", [])}
    if model in have or f"{model}:latest" in have:
        return "ok", f"Lokalni model {model} spreman."
    return "no_model", f"Model {model} nije preuzet."


def start_server(vulkan=False):
    """Start `ollama serve` in the background if Ollama is installed but not running.
    vulkan=True: run on the GPU through Vulkan instead of CUDA (an older NVIDIA driver can't run Ollama's CUDA
    build: "the provided PTX was compiled with an unsupported toolchain")."""
    exe = ollama_exe()
    if not exe:
        return False
    env = dict(os.environ)
    if vulkan:
        env.update(OLLAMA_VULKAN="1", CUDA_VISIBLE_DEVICES="-1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([exe, "serve"], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env=env)
    for _ in range(30):
        time.sleep(0.5)
        try:
            with _local("/api/tags", timeout=2):
                return True
        except OSError:
            pass
    return False


def restart_on_vulkan():
    """Stop the running Ollama (tray app + server) and start the server again on Vulkan."""
    subprocess.run(["taskkill", "/F", "/IM", "ollama app.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True)
    time.sleep(1.5)
    return start_server(vulkan=True)


def pull(model, on_progress):
    """Download a model; on_progress(text). True when done."""
    try:
        with _local("/api/pull", {"model": model, "stream": True}, timeout=3600) as r:
            last = 0.0
            for raw in r:
                ev = json.loads(raw)
                if ev.get("error"):
                    on_progress(f"Preuzimanje modela: {ev['error']}")
                    return False
                tot, done = ev.get("total"), ev.get("completed")
                if tot and done and time.time() - last > 1:
                    last = time.time()
                    on_progress(f"Preuzimam {model}: {done / 1e9:.1f} / {tot / 1e9:.1f} GB")
                if ev.get("status") == "success":
                    on_progress(f"Model {model} preuzet.")
                    return True
    except OSError as e:
        on_progress(f"Preuzimanje modela nije uspelo: {e}")
    return False


class Ollama:
    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.seconds = 0.0

    def json(self, system, user, schema, max_tokens=2500, images=None):
        """One structured answer (dict) or None."""
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        m = {"role": "user", "content": user + "\n\nOdgovori isključivo JSON objektom po zadatoj šemi."}
        if images:
            import base64
            m["images"] = [base64.standard_b64encode(b).decode() for b in images]
        msgs.append(m)
        body = {"model": self.model, "messages": msgs, "stream": False, "format": schema, "keep_alive": "30m",
                "options": {"temperature": 0.1, "repeat_penalty": 1.15, "num_ctx": NUM_CTX,
                            "num_predict": max_tokens}}
        t0 = time.time()
        out, started, on_vulkan = None, False, False
        for _attempt in range(3):
            try:
                with _local("/api/chat", body, timeout=600) as r:
                    out = json.load(r)
                break
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")
                log("ollama:", e.code, err[:300])
                if not on_vulkan and "CUDA error" in err:
                    on_vulkan = True
                    log("ollama: CUDA failed (old driver?) - restarting on Vulkan")
                    restart_on_vulkan()
                    continue
                return None
            except OSError as e:
                log("ollama:", e)
                if not started and ollama_exe():
                    started = True
                    start_server()              # installed but not running (e.g. the tray app was closed)
                    continue
                return None
        if out is None:
            return None
        self.calls += 1
        self.seconds += time.time() - t0
        text = out.get("message", {}).get("content", "")
        try:
            return json.loads(text)
        except ValueError:
            log("ollama bad json:", text[:300])
            return None


# ---- free web search -----------------------------------------------------------------------------------
def _get(url, data=None, timeout=15):
    ctx = ssl.create_default_context(cafile=CA_BUNDLE) if os.path.exists(CA_BUNDLE) else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "sr,en;q=0.7"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read(1_500_000)
        cs = r.headers.get_content_charset() or "utf-8"
    return raw.decode(cs, "replace")


def _clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _ddg(q):
    # the "lite" page; the html.duckduckgo.com form answers scripts with a captcha
    page = _get("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": q, "kl": "rs-sr"}))
    out = []
    for m in re.finditer(r"<a[^>]*href=\"([^\"]+)\"[^>]*class='result-link'>(.*?)</a>(.*?)"
                         r"(?:class='result-snippet'>(.*?)</td>|(?=class='result-link'))", page, re.S):
        href = htmllib.unescape(m.group(1))
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        if "duckduckgo.com/y.js" in u or "bing.com/aclick" in u:
            continue                                    # ads
        out.append({"title": _clean(m.group(2)), "url": u, "snippet": _clean(m.group(4) or "")})
    return out


def _bing(q):
    page = _get("https://www.bing.com/search?" + urllib.parse.urlencode({"q": q, "setlang": "sr"}))
    out = []
    for blk in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        a = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        p = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        if a:
            u = htmllib.unescape(a.group(1))
            enc = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("u", [""])[0]
            if enc.startswith("a1"):                    # bing.com/ck/a redirect: base64url of the real link
                import base64
                try:
                    u = base64.urlsafe_b64decode(enc[2:] + "=" * (-len(enc[2:]) % 4)).decode()
                except ValueError:
                    pass
            out.append({"title": _clean(a.group(2)), "url": u,
                        "snippet": _clean(p.group(1)) if p else ""})
    return out


def search(q, n=5):
    for eng in (_ddg, _bing):
        try:
            res = eng(q)
        except OSError as e:
            log("search", eng.__name__, e)
            continue
        if res:
            return res[:n]
    return []


def page_text(url, limit=2500):
    try:
        page = _get(url, timeout=10)
    except (OSError, ValueError):
        return ""
    page = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)\b.*?</\1>", " ", page)
    paras = [_clean(p) for p in re.findall(r"(?is)<(?:p|h1|h2|h3|li)\b[^>]*>(.*?)</(?:p|h1|h2|h3|li)>", page)]
    text = " ".join(p for p in paras if len(p) > 40)
    return text[:limit]


def plan(s, local_ok):
    """Who does what: (judge, research) - each 'claude', 'local' or None, and the local model's name.
    judge_model: a Claude model id, 'local:<model>' (everything local) or 'hybrid:<model>' (local judge,
    Claude for fact checks and profiles when there is a key). A Claude model without a key falls back to local."""
    jm, key = s.get("judge_model", ""), bool(s.get("api_key"))
    model = jm.split(":", 1)[1] if jm.startswith(("local:", "hybrid:")) else s.get("local_model") or MODELS[0][0]
    if jm.startswith("local:"):
        return "local", "local", model
    if jm.startswith("hybrid:"):
        return "local", ("claude" if key else "local"), model
    if key:
        return "claude", "claude", model
    return ("local", "local", model) if local_ok else (None, None, model)


QUERY_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
                "required": ["queries"], "additionalProperties": False}


def research(llm, system, prompt, schema, n_queries=3, read_pages=3, max_tokens=3000):
    """Claude's 'search the web, then answer' for a local model: the model writes queries, we search and read
    the top pages, the model answers from that material only. Returns (dict or None, number of searches)."""
    q = llm.json("Ti pišeš upite za internet pretragu (Google/DuckDuckGo) na srpskom, latinicom. Kratki, "
                  "konkretni upiti sa imenima, brojevima i godinama.",
                  f"Zadatak:\n{prompt}\n\nNapiši do {n_queries} upita za pretragu koji bi našli izvore za ovo.",
                  QUERY_SCHEMA, max_tokens=300)
    queries = [s for s in (q or {}).get("queries", []) if s.strip()][:n_queries] or [prompt[:120]]
    seen, results = set(), []
    for s in queries:
        for r in search(s):
            if r["url"] not in seen:
                seen.add(r["url"])
                results.append(r)
    block = []
    for i, r in enumerate(results[:10]):
        body = page_text(r["url"]) if i < read_pages else ""
        block.append(f"[{i + 1}] {r['title']}\nURL: {r['url']}\n{r['snippet']}" + (f"\nTEKST: {body}" if body else ""))
    material = "\n\n".join(block) or "(pretraga nije vratila ništa)"
    sysx = (system + "\n\nIMPORTANT: you cannot search yourself. The web search was already done for you - use "
            "ONLY the SEARCH RESULTS below (and the material in the request). sources must be URLs taken from "
            "those results; if they do not settle it, say so (e.g. verdict 'neproverljivo').")
    out = llm.json(sysx, f"{prompt}\n\nREZULTATI PRETRAGE:\n{material}", schema, max_tokens=max_tokens)
    if out and "sources" in out:
        ok = {r["url"] for r in results}
        out["sources"] = [s for s in out["sources"] if s.get("url") in ok]      # no invented links
    return out, len(queries)
