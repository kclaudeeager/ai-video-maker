# MVP — Library + Reader Implementation Plan

> **For agentic workers:** this plan is the current task list and it is
> normative. Read `/CLAUDE.md` first. Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to work task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. One commit per task, `git commit -s`,
> tick the box in the same commit.

**Goal:** a reader opens a work in the library, picks a chapter, and takes it in
three ways — the source text, a plain-language brief, or narration they can
listen to with the current verse highlighted — in whatever language the corpus
and the configured voice provider support. Nothing is generated that cannot be
traced back to the text.

**Definition of done:** the acceptance script at the bottom of this file runs
green on a clean clone.

**Architecture, and the one decision everything follows from:**

> **The reader synthesises directly from the corpus. It does not create a
> `Project`, does not enter `STAGE_ORDER`, and does not touch the `script`,
> `voice` or `align` stages.**

That is what makes this MVP additive and safe. Narration is the source text
segmented by rule, so fidelity is true by construction rather than by
instruction. Verse timings come from the *duration of each verse's own audio
file*, so there is no forced alignment and **faster-whisper is not an MVP
dependency at all**. Video mode arrives later and is the thing that materialises
a `Project`; see `docs/superpowers/plans/2026-09-09-m7-library-and-reader.md`
for the fuller roadmap this is a subset of.

**Tech stack:** everything from M0–M6, plus `usfm-grammar` (MIT) used at import
time only. No new dependency reaches the render path.

## Global constraints

Everything in `/CLAUDE.md` applies. On top of it, for this plan:

- **Additive only.** No existing module changes behaviour. The edits to
  `providers/base.py`, `runner.py`, `config.py` and `doctor.py` are additions.
- **No scripture text in the repository.** ~5 MB per translation, replaceable by
  one command, and `.gitignore` already excludes `workspace/`.
- **A paid voice API must never be able to surprise the user with a bill, and
  must never hammer a vendor.** Task 5's budget guard and rate limiter are not
  optional and not nice-to-haves. `providers/ratelimit.py` already exists —
  `QuotaTracker`, `Budget`, `SOFT_BUDGETS` — and is the thing to extend, not to
  reimplement.
- Out of scope, deliberately: video, the verbatim script stage, the LLM wiki,
  series/episodes, consumer accounts, payments.

### Read these first

- `docs/multimodal-reader-design.md` — the design, and §9 "Decisions since the
  first draft", which records what was measured and what changed.
- `docs/ui-design.md` — the visual direction. Phase D applies it; do not invent
  a second one.
- `docs/language-support.md` — which languages were *measured* to work end to
  end, and why the binding constraint is the phonemiser rather than the font.

### What exists (verified against the tree — do not re-derive)

- `providers/base.py` — `LLMProvider`, `TTSProvider`, `STTProvider`,
  `ImageProvider`, `StockProvider`, `VisionProvider`, `LLMResult`, `TTSResult`.
  `TTSProvider.synthesize(*, text, voice, out_path, speed=1.0, language="en") -> TTSResult`
  and `TTSProvider.voices() -> list[str]`. **`CorpusProvider` goes here.**
- `config.py` — `Settings` (pydantic-settings, `.env`), `provider_chains`
  defaulting to `{"llm": ["groq","gemini"], "tts": ["kokoro"], "stt": [...],
  "stock": [...], "image": [...], "vision": [...]}`.
- `runner.py` — `PROVIDER_KINDS = ("llm","tts","stt","stock","image")`,
  `provider_override(settings, name)`, `build_deps`.
- `providers/ratelimit.py` — `QuotaTracker`, `Budget`, `SOFT_BUDGETS`. The
  quota ledger is **global, not per project**: quota is spent against the
  account, and `runner.py` keeps it at `~/.cache/ai-video-maker/quota.json` for
  that reason. Anything metered goes through it.
- `cache.py` — `hash_inputs(**parts)`, `stage_key`, `StageCache`,
  `ResponseCache`, atomic writes, `STAGE_ORDER`.
- `project.py` — `ProjectStore`, `slugify`, per-project `fcntl` lock.
- `languages.py` — `LANGUAGES: dict[str, Language]`, `OFFERED_CODES`,
  `language_for(code)`.
- `media/audio.py`, `media/ffmpeg.py` — FFmpeg wrappers; concat and probe live
  here, reuse them rather than shelling out afresh.
- `web/app.py` — `create_app(settings=None, *, providers=None)` factory,
  `TEMPLATES_DIR`, `STATIC_DIR`, `JobQueue` on `app.state.jobs`, `PasswordGate`.
- `web/templates/base.html` — blocks `title`, `eyebrow`, `heading`, `content`,
  `scripts`. htmx is vendored at `/static/vendor/htmx.min.js`.
- `web/static/style.css` — the token block at the top: `--ground`, `--paper`,
  `--sunk`, `--rule`, `--rule-field`, `--ink`, `--ink-soft`, `--accent`,
  `--on-accent`, `--warn`, `--ok`, `--bad`, `--font-ui`, `--font-text`,
  `--t-eyebrow` … `--t-section`.
- 30 routes across `web/routes/{projects,script,storyboard,render}.py`.

---

## Phases

| phase | tasks | ends with |
|---|---|---|
| **A — Corpus** | 1–4 | WEB and BSB imported and addressable. No reader yet. |
| **B — Voice providers** | 5–6 | Any HTTP voice vendor usable by config, with a budget guard. |
| **C — Reader** | 7–10 | Text, brief and narration in the browser. **The MVP is functional here.** |
| **D — Look** | 11–12 | It reads like a reading app rather than an admin panel. |
| **E — Close** | 13 | Doctor, README, acceptance test. |

---

# Phase A — Corpus

### Task 1: Corpus models, the `CorpusProvider` contract, and the mock

- [x] **Files:** new `src/videomaker/corpus/__init__.py`, `corpus/models.py`;
  modify `providers/base.py`, `providers/mock.py`, `runner.py`, `config.py`;
  test `tests/unit/test_corpus_contract.py`

**Interfaces (normative):**

```python
# corpus/models.py
class Versification(StrEnum):
    KJV = "kjv"; VULG = "vulg"; LXX = "lxx"; ORG = "org"

class Verse(BaseModel):
    number: int = Field(ge=1)
    text: str = Field(min_length=1)

class WorkRef(BaseModel):
    id: str                      # slug: "web", "bsb"
    title: str
    language: str                # ISO 639-1; must be a key of languages.LANGUAGES
    licence: str = Field(min_length=1)     # one sentence. See below.
    licence_url: str
    source_url: str
    versification: Versification = Versification.KJV

class UnitRef(BaseModel):
    work_id: str
    book: str                    # USFM/Paratext 3-letter code, uppercase: GEN, JHN, REV
    chapter: int = Field(ge=1)
    verses: tuple[int, int] | None = None      # None = the whole chapter

    def key(self) -> str: ...                  # "web/JHN/003" | "web/JHN/003.16-18"
    @classmethod
    def parse(cls, key: str) -> "UnitRef": ...

class UnitText(BaseModel):
    ref: UnitRef
    title: str                   # "John 3"
    verses: list[Verse]
    @property
    def plain(self) -> str: ...  # verses joined by a single space, no numbers
    @property
    def word_count(self) -> int: ...

# providers/base.py
class CorpusProvider(ABC):
    @abstractmethod
    def works(self) -> list[WorkRef]: ...
    @abstractmethod
    def outline(self, work_id: str) -> list[UnitRef]: ...
    @abstractmethod
    def unit(self, ref: UnitRef) -> UnitText: ...
```

- `runner.PROVIDER_KINDS` gains `"corpus"`; `Settings.provider_chains` gains
  `"corpus": ["bible"]`. Both are additive.
- `providers/mock.py` gains `MockCorpus` serving one invented work of three
  chapters, so every later test can run with `--providers mock`.

**`WorkRef.licence` is `min_length=1` and that is load-bearing**, not defensive
typing: it is where `docs/multimodal-reader-design.md` §5's rule — ship nothing
whose licence you cannot state in one sentence — stops being prose.

**Tests:** `UnitRef.parse(ref.key()) == ref` for whole-chapter and verse-range
forms; a `WorkRef` with `licence=""` raises `ValidationError` (mutation-test by
removing `min_length`); `provider_override(settings, "mock")` overrides
`"corpus"` too; `UnitText.plain` contains no digits from verse numbers.

---

### Task 2: Reference parsing

- [x] **Files:** new `corpus/refs.py`; test `tests/unit/test_refs.py`

**Interfaces (normative):**

```python
BOOK_NAMES: dict[str, str]      # normalised name/abbreviation -> USFM code
BOOK_ORDER: tuple[str, ...]     # the 66 USFM codes in canonical order

def parse_reference(text: str, *, work_id: str) -> UnitRef: ...
def format_reference(ref: UnitRef) -> str: ...        # "John 3:16-18"
def book_name(code: str) -> str: ...                  # "JHN" -> "John"
```

Accepts `"John 3"`, `"Jn 3:16"`, `"John 3:16-18"`, `"JHN 3"`, `"1 Cor 13"`,
`"I Corinthians 13"`, `"1co13"`. Normalisation is case-folding, whitespace
stripping, and mapping `I`/`II`/`III` to `1`/`2`/`3`.

**Exact table lookup only. No fuzzy matching, ever.** `"Jn"` is John and `"Jon"`
is Jonah; an edit-distance match will confuse them silently, which is worse than
an error. An unknown name raises `ValueError` naming the input and listing the
three alphabetically nearest known names.

**Tests:** every entry in `BOOK_NAMES` round-trips through
`parse_reference → format_reference → parse_reference`; `"Jn 1"` and `"Jon 1"`
resolve to `JHN` and `JON` respectively; `"Jhn 99"` parses (range checking is
the corpus's job, not the parser's); `"Gospel of Fred"` raises `ValueError`
whose message contains the input.

---

### Task 3: The importer, and the licence gate

- [x] **Files:** new `corpus/usfm.py`, `corpus/importer.py`; modify `cli.py`,
  `pyproject.toml`; test `tests/unit/test_importer.py`,
  `tests/integration/test_import_web.py` (marked `slow` — it downloads)

**Interfaces (normative):**

```python
class ImportSpec(BaseModel):
    work_id: str
    title: str
    language: str
    licence: str = Field(min_length=1)
    licence_url: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    versification: Versification = Versification.KJV
    archive: str | None = None      # URL or local path to a USFM zip

def parse_usfm(text: str) -> UnitText | None: ...   # one book file -> one chapter at a time
def import_work(spec: ImportSpec, root: Path) -> WorkRef: ...
```

On-disk layout, written atomically:

```
<workspace>/library/<work_id>/
├── work.yaml            # the WorkRef, verbatim
├── source/              # the USFM exactly as downloaded. Never edited.
├── units/<BOOK>/<NNN>.json     # UnitText, derived from source/
└── derived/             # briefs and audio, cache-keyed and disposable
```

CLI:
- `videomaker library import <work-id> [--from <url|path>]`
- `videomaker library list` — id, title, language, licence, chapter count.

**Footnotes, cross-references and section headings are dropped from `verses[]`
and preserved in `source/`.** A narrator must not read a footnote marker aloud.

**The licence gate is the point of this task.** ebible.org has no site-wide
licence; each translation's own page is the authority. So an `ImportSpec`
missing any of `licence`, `licence_url`, `source_url` fails validation, and
`import_work` **writes nothing** — not a directory, not a partial file — when
validation fails. **There is no `--force` flag and none may be added.**

`import_work` also appends the work to a "Bundled texts" section in `NOTICE.md`,
creating the section if absent.

**Tests:** importing the two-book USFM fixture produces the expected `units/`
tree and a `work.yaml` that round-trips to `WorkRef`; an import with
`licence=""` raises before any path is created (assert the work directory does
not exist afterwards — mutation-test by moving the check after `mkdir`);
footnote markers present in the fixture appear in `source/` and in no
`verses[].text`; re-importing the same work replaces `source/` and `units/` and
leaves `derived/` untouched.

Add `tests/fixtures/usfm/` with two small hand-written USFM book files, one of
which carries a footnote and a section heading.

---

### Task 4: `BibleCorpus` and the catalogue

- [x] **Files:** new `corpus/bible.py`, `corpus/catalogue.py`; test
  `tests/unit/test_bible_corpus.py`

`BibleCorpus(CorpusProvider)` reads `<workspace>/library/`. It holds no
scripture in Python source. `outline(work_id)` returns every chapter in
`BOOK_ORDER` order. `unit(ref)` raises `KeyError` naming the reference when the
chapter is absent.

`catalogue.py` carries only *pointers* — one row per blessed text:

| id | title | lang | licence | source |
|---|---|---|---|---|
| `web` | World English Bible | en | Public domain | ebible.org |
| `bsb` | Berean Standard Bible | en | CC0 (public-domain dedication, 2023-04-30) | berean.bible |

Commented out with the reason rather than deleted: `rv1909` (es, public domain),
`lsg` (fr, public domain), `kjv` (PD in the US and internationally; UK printing
sits under perpetual Crown letters patent — carry that note if it is ever
enabled).

**Tests:** `works()` on an empty library returns `[]`, not an error; `outline`
is in canonical book order; `unit` for a missing chapter raises `KeyError` whose
message contains the formatted reference.

---

# Phase B — Voice providers

### Task 5: A vendor-neutral HTTP voice provider, with a budget guard and a rate limiter

- [x] **Files:** new `providers/tts/http_api.py`; modify `config.py`,
  `config.example.yaml`, `.env.example`; test
  `tests/unit/test_http_tts.py`, `tests/unit/test_tts_budget.py`

**Why this exists.** Kokoro covers the five measured languages of
`docs/language-support.md` and nothing else. espeak-ng — which Kokoro
phonemises through — ships 131 voices including Swahili and **not**
Kinyarwanda, so no amount of local work reaches Kinyarwanda through that path.
A vendor's HTTP API does. The abstraction is what keeps that a configuration
choice rather than a dependency: **no vendor name appears anywhere except a YAML
file.**

**Interfaces (normative):**

```python
class HTTPTTSConfig(BaseModel):
    name: str                      # provider key used in provider_chains
    endpoint: str                  # POST target
    api_key_env: str = ""          # env var holding the key; "" = no auth
    auth_header: str = "Authorization"
    auth_format: str = "Bearer {key}"
    text_field: str = "text"
    voice_field: str = "voice"
    language_field: str = "language"
    speed_field: str = ""          # "" = vendor does not accept speed
    extra_body: dict[str, str] = Field(default_factory=dict)
    audio_response: Literal["raw", "base64_json"] = "raw"
    audio_json_path: str = ""      # dotted path when audio_response is base64_json
    voices: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)   # ISO 639-1
    cost_per_minute_usd: float = 0.0
    timeout_s: float = 60.0
    # Rate limiting. Zero means "no stated limit" and applies no throttle.
    requests_per_minute: int = 0
    requests_per_day: int = 0
    max_concurrency: int = 1
    retry_attempts: int = 4
    retry_base_delay_s: float = 1.0

class HTTPTTSProvider(TTSProvider):
    def __init__(self, cfg: HTTPTTSConfig, *, budget: "TTSBudget | None" = None) -> None: ...
```

Configured under a new `voice_providers:` key in `config.yaml`, a list of
`HTTPTTSConfig`. Anything listed there becomes selectable in
`provider_chains["tts"]`.

**The budget guard:**

```python
class TTSBudget(BaseModel):
    max_minutes_per_run: float = 30.0
    max_usd_per_run: float = 1.00
    confirmed: bool = False        # set by --yes / an explicit UI confirmation

class TTSBudgetExceeded(RuntimeError): ...

def estimate_minutes(text: str, *, wpm: int = 150) -> float: ...
```

Before any paid synthesis run, the caller estimates minutes and cost and raises
`TTSBudgetExceeded` — naming the estimate, the limit and how to raise it — unless
`confirmed`. A local provider whose `cost_per_minute_usd` is `0.0` is never
blocked by the USD limit.

**This is not optional.** A whole Bible is ~5,270 narration minutes; at
$0.10/minute that is ~$527 in one command. A tool that can do that silently is
a tool that will, once.

**The rate limiter, and why it is a separate concern from the budget.** The
budget stops you spending too much. The rate limiter stops you being cut off
mid-chapter — a different failure with a different fix. A chapter is dozens of
short requests fired back to back, which is exactly the shape a per-minute cap
rejects, and a bulk run is thousands.

```python
class RateLimited(RuntimeError):
    # The vendor said slow down, and the retries were exhausted.
    retry_after_s: float | None

def throttle(cfg: HTTPTTSConfig, tracker: QuotaTracker) -> None: ...
```

Rules, all normative:

- **Pace before sending.** `requests_per_minute` is enforced client-side by
  sleeping, not by firing and catching the rejection. Being rate-limited is a
  bug in the caller, not an event to handle.
- **`requests_per_day` is tracked in the existing `QuotaTracker` ledger**, so it
  survives process restarts and is shared across the CLI and the web worker. A
  run that would cross the daily cap stops with the count and the reset time
  rather than starting and failing at verse 300.
- **Retry `429` and `5xx` with exponential backoff and full jitter**, honouring
  `Retry-After` when the vendor sends it, up to `retry_attempts`; then raise
  `RateLimited`. Never retry a `4xx` that is not `429` — a malformed request
  retried four times is four times wrong.
- **`max_concurrency` defaults to 1.** Verse-by-verse synthesis is naturally
  serial and a reader is waiting; parallelism here buys little and is the
  fastest way to trip a cap.
- **Resume, do not restart.** Task 7 caches per verse, so a run interrupted by
  `RateLimited` keeps every verse already synthesised and continues from the
  first missing one. That property is what makes a daily cap survivable rather
  than fatal.
- The same applies to slowness: a slow vendor is handled by `timeout_s` plus the
  same backoff, not by a longer timeout.

**Rate-limit tests:** a config with `requests_per_minute=60` sleeps between
calls rather than firing immediately (inject a fake clock and assert the sleep,
never wall time); a `429` carrying `Retry-After: 2` waits ~2 s and retries; four
consecutive `429`s raise `RateLimited`; a `400` is **not** retried
(mutation-test by widening the retry predicate to all `4xx` and watching this
fail); a run that would cross `requests_per_day` raises before the first call;
an interrupted run resumes and re-synthesises only the missing verses.

**Tests:** the provider builds a correct request from a config (assert headers,
body keys, and that the API key comes from the environment and never appears in
a log line or an exception message — mutation-test by interpolating the key into
the error and watching the test fail); `base64_json` decoding via
`audio_json_path`; a run over the minute limit raises `TTSBudgetExceeded` and
**makes no HTTP call**; the same run with `confirmed=True` proceeds; a
zero-cost provider ignores `max_usd_per_run`. Use `httpx.MockTransport` — no
network in the suite.

---

### Task 6: A configured example, and the documentation

- [x] **Files:** modify `config.example.yaml`, `.env.example`, `README.md`,
  new `docs/voice-providers.md`; test `tests/unit/test_voice_provider_config.py`

Ship one worked example in `config.example.yaml`, **commented out**, showing a
generic pay-as-you-go vendor: endpoint, key env var, the request field mapping,
`cost_per_minute_usd`, and the language codes it covers. `docs/voice-providers.md`
explains how to add a vendor in YAML without writing Python, and states plainly
that a metered provider spends real money and that the budget guard is what
stands between a chapter and a bill.

`.env.example` gains a commented `VOICE_API_KEY=` with a line saying which
config field points at it.

**Tests:** the shipped `config.example.yaml` parses into `Settings` with every
voice provider commented out, and the example, when uncommented in a fixture,
validates as an `HTTPTTSConfig`.

---

# Phase C — Reader

### Task 7: Reading audio, built straight from the text

- [x] **Files:** new `corpus/audio.py`; test `tests/unit/test_reading_audio.py`,
  `tests/integration/test_reading_audio_mock.py`

**Interfaces (normative):**

```python
class ReadingSegment(BaseModel):
    verse: int
    text: str
    start_s: float
    end_s: float
    audio_relpath: str

class Reading(BaseModel):
    ref: UnitRef
    voice: str
    language: str
    provider: str
    duration_s: float
    audio_relpath: str          # the concatenated mp3
    vtt_relpath: str
    segments: list[ReadingSegment]

def segment_for_reading(unit: UnitText) -> list[tuple[int, str]]: ...
def build_reading(unit: UnitText, deps, *, voice: str, speed: float = 1.0,
                  confirmed: bool = False) -> Reading: ...
def reading_key(unit: UnitText, *, provider: str, voice: str, speed: float) -> str: ...
```

One verse, one audio file, synthesised in order. Durations come from
`media/audio.py`'s probe; `start_s`/`end_s` accumulate. Concatenation uses the
existing FFmpeg wrapper. The `.vtt` is written from the segment boundaries.

**No forced alignment, and therefore no STT in this MVP.** Verse-level
highlighting is the right granularity for scripture, and it falls out of
synthesising verse by verse for free.

Cached under `library/<work>/derived/audio/<reading_key>/`, where the key is
`hash_inputs(text=..., provider=..., voice=..., speed=...)`. Re-listening costs
nothing; changing the voice builds a new one and leaves the old.

**Per-verse caching is what makes a rate limit survivable.** Each verse's audio
is written and probed as it arrives, so `build_reading` called again after a
`RateLimited` or a `TTSBudgetExceeded` synthesises only the verses still missing
and stitches the rest from cache. Write each verse atomically — a half-written
mp3 that looks present is worse than an absent one.

**The fidelity test, and it is the most important test in this plan:**

```python
def test_narration_is_the_source_text(mock_deps, sample_unit):
    reading = build_reading(sample_unit, mock_deps, voice="mock", confirmed=True)
    spoken = " ".join(seg.text for seg in reading.segments)
    assert normalise_ws(spoken) == normalise_ws(sample_unit.plain)
```

Mutation-test it: make `segment_for_reading` drop a word and watch it fail.

**Tests:** segments are contiguous and non-overlapping and the last `end_s`
equals `duration_s` to within 50 ms; the VTT cue count equals the verse count;
a second `build_reading` with identical inputs makes **zero** provider calls;
changing `voice` produces a different `reading_key`.

---

### Task 8: The brief

- [x] **Files:** new `corpus/digest.py`; modify `cli.py`; test
  `tests/unit/test_digest.py`

**Interfaces (normative):**

```python
class Brief(BaseModel):
    ref_key: str
    summary: str                 # <= 120 words, plain language
    people: list[str]
    places: list[str]
    turn: str                    # what changes in this passage
    model: str

def build_brief(unit: UnitText, deps) -> Brief: ...
def brief_key(unit: UnitText, *, model: str, prompt_version: int) -> str: ...
```

One LLM call, the inlined-JSON-schema pattern of `pipeline/script.py` — no
`$ref`/`$defs`, pydantic validation, exactly one repair retry carrying the
validation error back, then advance the chain. Cached at
`library/<work>/derived/brief/<brief_key>.json`, where the key covers the unit
text, the model name and a `PROMPT_VERSION` integer in the module.

**The brief is a retelling and every surface that shows it must say so**, with
the source text one click away. That label is the feature, not a disclaimer.

Bulk precompute is a CLI command — `videomaker library brief <work-id>
[--book JHN]` — not a web route: 1,189 chapters do not fit a request cycle.

**The bulk command is rate-limited and resumable, for the same reasons as Task
5.** 1,189 chapters is 1,189 calls; Gemini Flash's free tier is 1,500 requests a
day and Groq's per-minute token cap makes bulk work painful, which is why
`docs/source-driven-video-design.md` puts bulk work on Flash and per-passage work
on Groq. So: pace against `providers/ratelimit.py`, skip any chapter already
cached, print progress as `n/total`, and on a daily cap stop cleanly with the
count done and the reset time rather than erroring out. Re-running the command
the next day continues where it stopped.

**Tests:** a reply failing validation earns exactly one retry then advances the
chain; the cache is not re-read across a `PROMPT_VERSION` bump; a summary over
120 words fails validation.

---

### Task 9: The reader in the browser

- [x] **Files:** new `web/routes/library.py`,
  `web/templates/{library,work,read}.html`,
  `web/templates/_reader_mode.html`, `web/templates/_verse_list.html`;
  modify `web/app.py`, `web/templates/_nav.html`; test
  `tests/unit/test_web_library.py`, `tests/integration/test_reader_golden_path.py`

**Routes (normative):**

| method | path | returns |
|---|---|---|
| GET | `/library` | works, each with its licence shown |
| GET | `/library/{work_id}` | the outline: books, then chapters |
| GET | `/read/{work_id}/{book}/{chapter}` | the reading page |
| GET | `/read/{work_id}/{book}/{chapter}/mode/{mode}` | htmx fragment for one mode |
| POST | `/read/{work_id}/{book}/{chapter}/audio` | build the reading; returns the player fragment |
| GET | `/media/reading/{work_id}/{reading_key}/{name}` | the mp3 and the vtt |

```python
class ReadMode(StrEnum):
    SOURCE = "source"; BRIEF = "brief"; LISTEN = "listen"
```

- `book` is validated against `BOOK_ORDER` and `chapter` is an `int` — reject
  anything else with a 404 before touching the filesystem. **The media route is
  the security-critical one**: `reading_key` is attacker-controlled, so resolve
  the path and assert it is inside the work's `derived/audio/` directory, the
  way `web/media.py` already does for project media. Reuse that helper rather
  than writing a second one.
- The mode switcher renders **only the modes this work's language supports** —
  `LISTEN` appears when a configured `tts` provider lists the language, and is
  absent otherwise rather than present and broken.
- Building audio goes through the existing `JobQueue`, so a long synthesis does
  not block the request. The POST returns immediately with a polling fragment,
  matching how `render.py` already handles long work.
- When a paid provider's estimate exceeds the budget, the page shows the
  estimate and a confirm button rather than an error.

**Tests:** every route with `--providers mock`; a traversal attempt in
`reading_key` (`../../`, absolute path, URL-encoded) 404s and reads nothing —
mutation-test by removing the containment assertion; a work whose language has
no voice provider renders without a Listen control; the golden path builds a
reading end to end against `MockTTS` and gets an mp3 back.

---

### Task 10: Reader preference

- [x] **Files:** modify `web/routes/library.py`, `web/templates/base.html`;
  test `tests/unit/test_reader_prefs.py`

Preferred mode and preferred language per reader, in a signed cookie —
**not in `Settings`**, which is machine-wide, while this is per person.
Default mode is `BRIEF`. `Settings` gains only `reader_cookie_secret: str = ""`,
with an empty value meaning a per-process random secret.

**Tests:** the cookie round-trips; a tampered cookie falls back to the default
rather than raising; an unknown mode in the cookie falls back.

---

# Phase D — Look

### Task 11: Make the reading page a reading page

- [x] **Files:** modify `web/static/style.css`, `web/templates/read.html`;
  test `tests/unit/test_palette_contrast.py` (extend)

Apply `docs/ui-design.md`. Do not invent a second direction: the chrome is
achromatic, the only hue on a page is the one asking for a human, links are
underlined rather than coloured, and every value is a token at the top of the
file.

New tokens, in the existing block:

```
--measure: 34rem;        /* reading column; ~66 characters at --t-read */
--t-read: 1.1875rem;     /* 19px body for sustained reading            */
--lh-read: 1.7;
--verse-num: var(--ink-soft);
```

Concretely, and these numbers are the task:

- The reading column is `--measure` wide, centred, set in `--font-text` at
  `--t-read`/`--lh-read`. Prose is what this page is for; everything else is
  chrome and stays in `--font-ui`.
- Verse numbers are superscript, `--t-eyebrow`, `--verse-num`, non-selectable
  (`user-select: none`) so copying a passage copies the words alone.
- The mode switcher is three quiet segmented controls, not tabs and not buttons
  with a hue. The active one is marked by weight and a 2px `--accent` underline.
- The brief renders in a `--sunk` card with a `--warn`-coloured eyebrow reading
  **A retelling — tap a verse for the text**. That is the one warm thing on the
  page, which is exactly what the palette rule reserves warmth for.
- A chapter's previous/next controls sit at the foot of the column, not in a
  sidebar.
- Nothing on this page is a full-width bar or an edge stripe.

Extend the contrast test to cover the new tokens against `--paper` and `--sunk`,
asserting ≥4.5:1 for text and ≥3:1 for the verse numbers.

---

### Task 12: The player, with the current verse lit

- [ ] **Files:** new `web/static/reader.js`; modify `web/templates/read.html`;
  test `tests/unit/test_web_templates.py` (extend)

A native `<audio controls>` plus the generated `.vtt` as a `<track kind="metadata">`.
On `cuechange`, add `.verse-current` to the matching verse and scroll it into
view only when it is out of the viewport. Clicking a verse seeks to its start.

**Plain vanilla JS in one small file. No new dependency, no CDN, no build step**
— the same reasoning that keeps htmx vendored. Under 60 lines; if it is growing
past that, the design is wrong.

`.verse-current` is a `--sunk` background and a slightly heavier weight. No hue:
this is the machine reporting position, not a request for a human.

**Tests:** extend the template test to assert `reader.js` is referenced from
`/static/` and that no absolute URL appears anywhere in the new templates.

---

# Phase E — Close

### Task 13: Doctor, README, and the acceptance test

- [ ] **Files:** modify `doctor.py`, `README.md`; test
  `tests/unit/test_doctor.py`, `tests/integration/test_mvp_acceptance.py`

`videomaker doctor` gains one check: which works are imported, whether each has
a stateable licence, and which reader modes each work's language supports. A
work whose language has no voice is a **WARN naming the modes it still
supports** — not a FAIL. §6 of the design doc: a language with a text but no
voice can be read, just not heard.

README gains a "Reading a book" section: import, browse, the three modes, and a
sentence saying the narration is the source text and that a test asserts it.

**The acceptance test is the definition of done for this plan.** With
`--providers mock`, against the two-book USFM fixture:

```
import → list → outline → read source → build brief → build reading → fetch the mp3
```

and assert, at the end, that the reassembled narration equals the fixture text.

---

## Acceptance script

Run on a clean clone. Every line must succeed.

```bash
uv sync
uv run ruff check .
uv run pytest -q
uv run videomaker doctor

# Offline, against the bundled fixture:
uv run videomaker library import fixture --from tests/fixtures/usfm/
uv run videomaker library list                     # shows the work and its licence
uv run videomaker library brief fixture --book JHN --providers mock

# The real thing, if the network is available:
uv run videomaker library import web
uv run videomaker web                              # then open /library
```

In the browser: open `/library`, pick a work, pick a chapter, switch between
**Read**, **Brief** and **Listen**, press play, and watch the verse highlight
track the audio.

---

## What this MVP deliberately does not do

- **Video.** The whole illustrated path is out. It is the expensive mode and the
  one that needs the verbatim script stage, the visual policy and a measured
  cost per chapter. It is the next plan, not this one.
- **A `Project` per passage.** The reader synthesises directly. See the
  architecture note at the top; this is what keeps the MVP unable to regress the
  video pipeline.
- **Forced alignment.** Verse-level timing is free here and word-level is not
  needed until captions are burned into a frame.
- **Accounts, payments, sharing.** A reader preference cookie is the whole of
  the personalisation.
- **Any language whose voice has not been configured.** The mode table degrades
  per language rather than blocking, and says which modes a language supports.
