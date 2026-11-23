# Virtuelni predsednik skupštine

Watches a session of the National Assembly of Serbia on YouTube (live or recorded) and acts like the chair
(predsedavajući): it detects breaches of order under the Poslovnik and shows the measure the chair should pronounce.

## Download (Windows 10/11) - easiest

Open **Releases** (right side of this page), download `VirtuelniPredsednik-windows.zip`, unzip it anywhere and
double-click **`VirtuelniPredsednik.exe`**. Everything is inside (Python, GPU speech recognition, ffmpeg, node,
yt-dlp) - nothing to install. Press **API ključ…** and paste your own Claude API key (see step 3 below).
Windows may warn about an unknown publisher (the .exe is not code-signed): *More info → Run anyway*.
If YouTube stops working after a while, run `bin\yt-dlp.exe -U` in the app folder to update yt-dlp.

## Install from source (Windows 10/11)

1. Download this repository (green **Code** button → *Download ZIP*) and unzip it, or `git clone` it.
2. Double-click **`setup.bat`**. It installs, if missing, Python 3.11, ffmpeg and Node.js (via `winget`), creates
   a `.venv` in the folder and installs the Python packages (~2.5 GB, mostly the NVIDIA GPU libraries).
3. Double-click **`start.bat`**, press **API ključ…** and paste your own Claude API key
   (https://console.anthropic.com → API keys; usage is billed to that key, ~10 ¢ per hour of session with Haiku
   plus ~1-2 ¢ per web fact-check). The key is stored only in `settings_local.json` on your PC.

On the first session the speech model (Whisper large-v3-turbo, ~1.6 GB) and the voice model (40 MB) download
automatically. Requirements: an NVIDIA graphics card for live speed (without one it runs on the CPU - fine for
short recordings, too slow for live; pick a smaller `whisper_model` in `settings_local.json`, e.g. `small`).
Name captions (Cyrillic) are read with Windows OCR. Best: add the Serbian-Cyrillic OCR once - PowerShell **as administrator**: `Add-WindowsCapability -Online -Name "Language.OCR~~~sr-CYRL-RS~0.0.1.0"` (small download). Without it the app falls back to Serbian Latin / English OCR and matches Latin look-alikes, which misses more names. If your antivirus scans
HTTPS and Python reports certificate errors, export its root certificate, append it to certifi's bundle and point
the environment variable `PRED_CA_BUNDLE` at that file.

### Without an API key: local model (free)

Every option also works without a Claude key, with a model running on your own PC:

1. Install **Ollama** (free): https://ollama.com/download (or `winget install Ollama.Ollama`).
2. In the app pick **Lokalno: Qwen 2.5 7B** in the *Sudija* list (a Claude model without a key switches to
   the local one automatically). On the first start the app offers to download the model (3-5 GB, once).

It judges breaches, analyses rhetoric and tone, recognises the chair from context, fact-checks claims (it searches
DuckDuckGo itself and reads the top pages) and builds MP profiles - for free, with nothing sent to Anthropic.
It is weaker than Claude: a small model understands Serbian and the Poslovnik worse, so expect more missed and
more false reports, and fact checks / profiles are less reliable. Name captions on screen are read by Windows OCR
only (no Claude vision fallback). It needs ~5 GB of graphics memory next to Whisper; with less, Ollama moves part
of the model to the CPU and gets slower. **Hibrid** = the local judge for the frequent, cheap part plus Claude
(with a key) for fact checks and profiles.

## Use

Start: double-click `start.bat`, paste a YouTube link, press **Počni**. For recordings you can set a start
time (e.g. `1:30:00`). Set the Claude API key once with **API ključ…** (saved in `settings_local.json`).

Recordings: drag or click the timeline under the video, ⏪/⏩ 30 s, ⏸ pause (click the video, then ← → = 10 s,
space = pause). Every `[time]` in the transcript, measure cards and other tabs is a link that jumps the video
there. Stretches already judged are not judged or penalized again when you scroll back.

## How it works

| Step | Module | What |
|---|---|---|
| Stream | `stream.py` | yt-dlp finds the HLS stream; one ffmpeg feeds video, sound and 16 kHz audio, so the picture and the judged audio are in sync |
| Speech | `asr.py` | faster-whisper `large-v3-turbo` on the GPU (~6x real time), cut at pauses, loudness per segment |
| Who speaks | `speakers.py`, `names.py`, `videoocr.py` | 1) official broadcast caption (bottom left, Cyrillic) read by Windows OCR - only Serbian-Latin OCR is installed, so names are matched as Latin look-alikes against the roster; Claude vision only as fallback. 2) the chair giving the floor ("Reč ima…", "Izvolite, koleginice X", "Dajem reč poslaniku Marku Markoviću", "X ima reč") matched against all case forms of the 250 MPs (`data/poslanici.json`, refresh with `python update_roster.py`); without a recognisable name the next new voice gets the turn and the caption names it. 3) gender from roster / role words / the speaker's grammar ("rekla sam") - pitch is NOT used for gender (angry men reach 270-300 Hz in the hall), only for "raised voice". 4) TitaNet voiceprints. The official countdown (top centre) is read too: 0:00 + still talking -> time exceeded |
| Judge | `judge.py` | Claude reads new transcript lines every ~20 s with the Poslovnik articles 96-116 in a cached system prompt; structured JSON: violations, fact-check claims, real chair actions, agenda item |
| Measures | `rules.py` | ladder čl. 109-111 (upozorenje -> opomena -> druga opomena -> oduzimanje reči -> udaljenje), fines čl. 114 (10/20/40/50 %, highest only), off-topic and shouting need a warning first, 45 s cooldown per MP |

Hover over any button, field or tab for a short explanation.

**Who is chairing** (`chair.py`, `data/rukovodstvo.json` - the Speaker and the six deputy speakers elected on
19.11.2026, with links to the decisions): recognised from the broadcast caption while the chair talks, from MPs
addressing the chair ("predsedniče Čupiću", "potpredsednice Petrović"; a plain "gospodine X" needs two), from
handover sentences ("predsedavanje preuzima…"), from Claude, and from the voice - when a clearly different voice
starts giving the floor, the chair has changed (the previous chair keeps their own voiceprint). Shown in the
speaker card and transcript ("Predsedavajući (Aleksandar Čupić)"); the chair score is also given per person.
Addressing the chair by name is not counted as addressing another MP. Update the JSON if the leadership changes.

**Speakers identified later:** an unannounced voice starts as "Govornik N" (or "Glas N" for heckling from the
hall). As soon as it is recognised - the on-screen caption, the chair naming them, or Claude from context - all
of it moves to the real MP: transcript lines, measures (the warning ladder is replayed in time order, so an
earlier opomena counts toward the next one, and hall heckling becomes a measure), rhetoric, claims and the
per-group statistics.

Tabs: **Mere** (measures), **Transkript**, **Poslanici** (per-MP tally and fine), **Za proveru** (claims the
judge flags are checked on the web by `factcheck.py` - Claude + web_search, verdict tačno / uglavnom tačno /
delimično tačno / netačno / neproverljivo / mišljenje with sources; lying is not a breach of order, so no
penalty), **Stvarni predsedavajući** (what the real chair did), **Statistika** (real chair vs app: matches within
90 s, by type and by MP with čl. 114 fines, and a chair score 100 − 2/5/10/15 per measure the chair failed to
pronounce), **Retorika** (tone per speaker and manipulation techniques - ad hominem, etiketiranje,
relativizacija, slamnati čovek, lažna dilema, zastrašivanje… - with quotes). **Izveštaj** saves/opens an HTML
report (also written automatically on Stop, next to the session log). **Po grupama**: the same per
parliamentary group (group of each MP from the official list, `update_roster.py`), normalised per 10 minutes of
speech. **Transkript…** saves .txt / .srt / .html (final names after renames; .txt is also auto-saved on Stop).
Right-click a transcript line (or selected text): check the claim on the web, or mark it true/false yourself.

**Wider context / spin** (`profiles.py`, context mode of `factcheck.py`): every named MP who speaks gets a
public-record profile (Claude + web search, every item attributed to a source with a date, cached in
`data/profiles/`, ~$0.04 once per MP; shown in **Profili**). When the judge sees an accusation or blame
(etiketiranje, napad na ličnost, žrtveno jagnje, relativizacija, teorija zavere…), a context check gets the
speaker's profile, profiles of MPs from the speaker's own group and the session record per group, searches the
web and returns: dvostruki aršini / nedosledno ranijim izjavama / suprotno zapisniku sednice / nedostaje
kontekst / potkrepljeno / nema dovoljno podataka (Retorika tab, report). Same standard for every party.
Information only - measures come only from the Poslovnik. Settings `profiles` / `context_checks` turn them off.

Costs measured on a heated 6-minute stretch with Haiku 5.5 (effort medium): judge ≈ $0.01 (≈ 6-10 ¢/hour),
web fact-check ≈ $0.01-0.02 per claim. Haiku must use `web_search_20250305` (with `_20260209` it didn't search);
the web_search tool rejects `user_location` country RS.

Every session is logged to `sessions/*.jsonl`.

## Notes
- `app.py` imports ctranslate2 before PyQt6 on purpose (an older msvcp140.dll loaded first crashes CTranslate2).
- Voice model: `models/nemo_en_titanet_small.onnx` (k2-fsa sherpa-onnx release, downloaded on first use). Picked by test on a real
  session: same MP 0.83-0.89 vs chair 0.26-0.36 cosine; the voxceleb models could not tell them apart.
- Windows build: `python build_exe.py <work_dir>` in a clean venv with `requirements.txt` + `pyinstaller`
  (recipe `VirtuelniPredsednik.spec`; copies data/, ffmpeg, node, downloads yt-dlp.exe into bin/).
- `test_offline.py file.wav start_s` runs speech + speaker labelling on a WAV without the UI.
- Packages: `requirements.txt` (faster-whisper, nvidia-cublas/cudnn-cu12, sherpa-onnx, anthropic, PyQt6,
  sounddevice, winocr, yt-dlp); external: ffmpeg and node on PATH. `start.bat` uses `.venv` if present.
