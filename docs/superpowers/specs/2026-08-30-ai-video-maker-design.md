# AI Video Maker — Design Spec

Date: 2026-08-30 · Status: awaiting owner review · License decision: AGPL-3.0

## 1. Vision

A local-first, open-source **AI video studio** that turns a topic into finished videos: long-form 16:9 for YouTube and 9:16 Shorts/Reels derived from the same project. First user is the owner (educational channels: tech, cooking, weight loss; narrative channels: life story). Later released publicly under AGPL-3.0 so anyone can self-host and contribute.

**Core thesis:** YouTube's 2026 "inauthentic content" policy demonetizes mass-produced templated AI videos but explicitly allows AI-assisted content with real human creative input. This app is therefore a **quality-first, human-in-the-loop studio** — the pipeline pauses at three mandatory review gates (script → storyboard → preview) so every video is individually curated. That is simultaneously the compliance strategy, the differentiation vs. existing open-source tools (MoneyPrinterTurbo, ShortGPT), and the open-source positioning.

## 2. Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime (phase 1) | Local-first on owner's Mac | True $0/month; same codebase deploys to VPS/Docker later |
| Stack | Python 3.12 (pinned via `uv`) + FastAPI + Jinja2 + vendored htmx | Python owns the ML ecosystem; no frontend build chain |
| Output | Dual-format from one project (16:9 + 9:16) | Doubles distribution per unit of effort |
| License | AGPL-3.0 | Genuinely open; forks offering it as a service must publish changes — the owner's desired "regulation" |
| Cost ceiling | $0/mo until revenue | All providers on permanent free tiers; all models run on CPU locally |

## 3. Business analysis

### 3.1 SWOT

**Strengths** — $0 marginal cost per video; full pipeline ownership (no vendor lock-in); Kokoro-82M (Apache 2.0) is near-commercial TTS on CPU; dual-format output; multi-niche template system; human-in-the-loop design aligned with 2026 platform policy where competitors are not.

**Weaknesses** — Solo dev + solo creator (owner's time is the scarce resource); Intel Mac limits (renders take minutes, no local image gen/LLM); free-tier rate limits cap throughput (fine for quality cadence, blocks bulk use — also a feature); cold-start audience of zero; smaller voice variety than paid TTS.

**Opportunities** — No well-maintained *quality-first* open-source AI video studio exists (MoneyPrinterTurbo is templated-output-focused, ShortGPT stalled); Kokoro's ~8 languages enable multilingual re-versions; open-core/hosted/premium-template revenue later; GitHub Sponsors after release; meta-content ("building this tool") feeds the owner's tech channel; niche templates are pure data files → low contribution barrier.

**Threats** — Further platform tightening on AI content (channel-level detection is live); free tiers shrinking (Oracle cut its free ARM tier in 2026; Groq/Gemini could change) → mitigated by provider abstraction; Shorts monetization bar is brutal (10M views/90 days) → long-form is the revenue engine, Shorts are discovery; funded SaaS competitors and frontier video models raising expectations → different market (free, self-hosted); weight-loss niche is YMYL → common-knowledge advice only + baked-in disclaimers; music copyright → only YouTube Audio Library/Pixabay-licensed local library, never MusicGen (non-commercial weights).

### 3.2 Income model (honest)

Income is lottery-shaped; the tool's edge is reducing cost-per-attempt to ~$0 + time, enabling many quality attempts.

**Stream A — owner's channels (primary).** YPP thresholds: 1,000 subs + 4,000 watch-hours (long-form) or 10M Shorts views/90 days; fan funding at 500 subs. Long-form RPM: tech $4–15, cooking $2–6, weight loss $3–10, story $1–4; Shorts ~$0.05–0.30. Affiliates (Amazon Associates etc.) work **before** YPP. Month-12 scenarios at 2–3 long-form + 3–5 Shorts/week on ≤2 channels: conservative $0–50/mo (affiliates only); moderate (YPP by month 6–9, 50–150k views/mo) $200–1,500/mo; strong (niche hit, 300k+ views/mo) $1,000–4,000/mo + sponsorships. Strategy: max 2 channels, quality cadence over volume.

**Stream B — the open-source project.** GitHub Sponsors/Open Collective after public release (young-project realistic: $0–200/mo); open-core hosted version + premium template packs later, only when Stream A or sponsors cover the VPS. AGPL prevents others from beating the owner to a closed-source hosted version.

**Cost timeline.** Phase 0 (now → first income): **$0/mo** (local Mac; Groq/Gemini/Pexels/Cloudflare/GitHub free tiers; domain deferrable). Phase 1: ~€4–15/mo (Hetzner VPS if a hosted instance is wanted — or Cloudflare Tunnel, free, exposing the local Mac; domain ~$10/yr; optional paid TTS $5–22/mo). Phase 2 (SaaS, only if justified): storage/workers scaling with revenue.

### 3.3 Platform compliance (design inputs, non-negotiable)

- Human-edited original scripts at every gate; never publish a raw LLM draft.
- YouTube synthetic-content disclosure metadata generated for AI-voiced videos.
- Templates define *style*, not fixed structure clones; vary visuals/structure per video.
- YouTube API uploads from unaudited API projects are **locked private** → v1 default is metadata generation + manual upload (~2 min/video); API upload is an optional, documented module (10k units/day, 1600/upload ≈ 6/day).
- Instagram Reels API needs Business account + Meta app review (2–4 weeks) → v1 produces the correctly-specced file (9:16, 5–90s, H.264) for manual upload; API automation deferred.

### 3.4 Open-source governance ("regulations")

AGPL-3.0 license; project name stays owner's trademark (forks must rename — code is free, the name is protected); DCO sign-off instead of CLA; Contributor Covenant CoC; responsible-use policy in README (intended for original creator content, not spam farms/misinformation — a normative signal plus ToS basis for any hosted instance). Repo is open-source-ready from day 1; community launch is a post-v1 milestone.

## 4. Technical design

Working name `ai-video-maker`, Python package `videomaker`.

### 4.1 Machine-verified constraints (checked on this Mac, 2026-08-30)

- Intel i5-8259U (8 threads), 16GB RAM, ~120GB free; `uv` 0.9.28 with Python 3.12.12 already available; git/gh/brew present.
- **FFmpeg 8.1.1 installed but it is a lean build WITHOUT libass/freetype** — no `subtitles`/`ass`/`drawtext` filters. Caption burning is impossible until `brew reinstall ffmpeg` (standard bottle includes libass). `videomaker doctor` probes for the `subtitles` filter and prints this remediation. Bonus: `h264_videotoolbox` HW encoder is present → optional fast-render mode (2–4×; libx264 veryfast remains default quality path).
- **PyTorch has no Intel-macOS wheels after 2.2.2** → the `kokoro` pip package is not viable here. Default TTS is **kokoro-onnx** with onnxruntime pinned to the last macOS x86_64 cp312 release (verify exact pin in M0; ~1.18–1.19). Fallback ladder: sherpa-onnx (also runs Kokoro) → macOS `say` (dev-only). Same wheel risk for faster-whisper/CTranslate2 → pin (faster-whisper 0.10.x + ctranslate2 3.24.x) or fall back to a `whisper-cli` (brew) subprocess provider. The provider abstraction confines each swap to one file; M0 runs a hard install spike before any feature work.
- espeak-ng not installed — only needed for some non-English Kokoro voices; `doctor` checks conditionally.
- No MoviePy anywhere; `ffprobe -print_format json` covers probing.

### 4.2 Repo structure

```
AI-video-maker/
├── LICENSE  README.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  NOTICE.md
├── pyproject.toml  uv.lock  .python-version(3.12)  .env.example  .gitignore
├── config.example.yaml            # provider chains, paths, render prefs → copied to config.yaml
├── Dockerfile  docker-compose.yml # M6
├── .github/workflows/ci.yml       # ruff + pytest + golden-path render (ubuntu)
├── .github/ISSUE_TEMPLATE/{bug,feature,new_niche}.yml
├── docs/{quickstart,providers,templates,architecture,self-hosting,youtube-policy}.md
├── docs/superpowers/specs/        # this spec
├── templates/                     # niche templates = DATA (YAML), schema-validated
│   ├── _schema.md  tech_explainer.yaml  cooking_recipe.yaml  weight_loss_tips.yaml  life_story.yaml
├── assets/fonts/                  # 2 bundled OFL fonts + OFL.txt
├── assets/music/README.md         # user drops tracks into mood folders (calm/upbeat/dramatic)
├── src/videomaker/
│   ├── cli.py                     # Typer: new run stage approve serve list status upload doctor setup clean
│   ├── config.py  models.py  project.py  templates.py  cache.py
│   ├── pipeline/{base,script,voice,align,visuals,captions,assemble,thumbnail,metadata}.py
│   ├── providers/{__init__,base,ratelimit,mock}.py
│   ├── providers/llm/{groq,gemini,ollama,openai_compat}.py
│   ├── providers/tts/{kokoro_onnx,say,piper,elevenlabs}.py
│   ├── providers/stt/{fasterwhisper,whispercpp}.py
│   ├── providers/image/cloudflare.py   providers/stock/{pexels,pixabay}.py
│   ├── providers/upload/{manual,youtube}.py
│   ├── media/{ffmpeg,ass,audio}.py     # runner+filter builders / ASS writer / music-duck graphs
│   └── web/{app,worker}.py  web/templates/*.html  web/static/{style.css,app.js,vendor/htmx.min.js}
├── tests/{conftest.py,fixtures/,unit/,integration/test_golden_path.py,contract/}
└── workspace/projects/<slug>/     # gitignored project folders
```

Deliberately absent in v1: databases, job queues, React builds, entry-point plugin discovery, auth (binds 127.0.0.1; `--host 0.0.0.0` prints a warning).

### 4.3 Data model (pydantic, `models.py`)

`Project` (id/topic/template/language/voice/target_minutes, `Approvals` timestamps, `MusicSelection`, `outputs: dict[Aspect, OutputSpec]`, `VideoMetadata`, `scenes[]`). `Scene` (stable id `s01…`, narration, `SceneVisual`, `audio_path`+duration, `WordTiming[]`, `in_short: bool`, `locked`, per-scene `error`). `SceneVisual` (query, kind auto/stock_video/stock_photo/ai_image, `chosen: AssetRef`, `candidates[]`, motion pan/zoom/none, `crop_focus_x` 0–1 for the vertical crop, `trim_start_s`). `AssetRef` carries provider/source ids/urls, local relative path, dimensions, attribution, license. `OutputSpec` per aspect (`wide` 1920×1080 / `vertical` 1080×1920) with its own `scene_ids` timeline — vertical uses the `in_short` subset. `VideoMetadata` includes YouTube-rule-conformant chapters (≥3, first at 0:00, ≥10s each).

**Project folder**: `project.json` (source of truth, atomic tmp+replace writes, per-project lock) · `cache/stages.json` (per-unit input hashes) · `scenes/sNN/{narration.wav,words.json,asset.*,candidates/}` · `audio/{voice_wide.wav,voice_vertical.wav,music.mp3}` · `captions/{wide,vertical}.ass` · `build/` (disposable intermediates, concat lists, timelines, 480p previews) · `output/{final_wide.mp4,final_vertical.mp4,thumbnail.jpg,metadata.json,metadata.md}`.

### 4.4 State machine and caching

```
new →script→ script_ready →[GATE 1 approve script]→ voiced(voice+align) → storyboard_ready
→[GATE 2 approve storyboard]→ preview_ready →[GATE 3 approve preview]→ rendered → published
```

Status is **derived**: walk stages in order; first stale (input-hash mismatch) or missing artifact, or first unapproved gate, determines status. Every stage unit — mostly per-scene — hashes exactly its inputs (e.g. `voice:s01` = narration+voice+speed+provider). Editing scene 7's narration invalidates only `voice:s07`, `align:s07`, captions, and assembly; other scenes' artifacts and quota spends are untouched. Two global caches in `~/.cache/ai-video-maker/`: LLM responses (key = provider+model+prompts+temperature+schema) and stock searches (7-day TTL) — retries and re-runs never re-burn free-tier quota for identical inputs.

### 4.5 Pipeline stages

| Stage | Unit | Essence |
|---|---|---|
| script | all | LLM → strict-JSON `{scenes:[{narration, visual_query, visual_kind}]}`; pydantic-validate; one repair retry with the validation error; then provider fallback. Template controls structure/word budget. |
| voice | scene | kokoro-onnx CPU → `narration.wav`; duration via ffprobe; sequential (low RAM). |
| align | scene | faster-whisper base int8, word timestamps, `initial_prompt=narration`; then **snap-to-script** (difflib) — script spelling with whisper timings. |
| visuals | scene | Stock search (4 candidates; videos ≥ scene duration + gap, ≥1080p; photos min-side ≥1600 so both crops work) with kind=auto resolution order from template; Cloudflare flux-schnell 1024² for AI images (2× Lanczos upscale at use). |
| captions | aspect | Pure-function ASS writer. Karaoke word-pop = per-word Dialogue events with active word recolored (ASS colors are BGR — unit-tested). Per-aspect layout: wide ~64px, 4–6-word chunks, lower-third; vertical ~96px, 2–3-word chunks, centered ~62% height. Never scaled from the other aspect. |
| assemble | aspect | Per-scene uniform silent intermediates (libx264 crf18 veryfast, 30fps, exact duration = audio + gap) → concat demuxer (stream copy) + `timeline.json` → single final pass. |
| render | aspect | Final pass burns `subtitles=….ass:fontsdir=assets/fonts`, mixes music with `sidechaincompress` ducking + `amix normalize=0` + `loudnorm I=-14:TP=-1.5`, `+faststart`. Run with `cwd=project` and relative paths (kills macOS subtitle-path escaping bugs). Preview = same graph at 480p ultrafast. |
| thumbnail | all | Pillow: frame extract or AI image → 1280×720, gradient scrim, auto-fit OFL display font. |
| metadata | all | LLM titles(3)/description/tags; chapters computed locally from timeline; stock attribution block; `metadata.md` copy-paste-ready. |

**Vertical is not a crop of the finished wide render.** Each scene re-crops from source assets: `crop='min(iw,ih*9/16)':…:'(iw-ow)*crop_focus_x':…` → 1080×1920 (or template `blur_pad` style: blurred-fill background + fitted foreground). Stills default to `pan` (animated crop over a 120% pre-scale — smoother and cheaper than zoompan; zoompan Ken Burns is opt-in with 2× pre-upscale to kill jitter). The vertical voice track is a separate concatenation of the same per-scene wavs over the `in_short` subset.

### 4.6 Provider layer

Six sync ABCs in `providers/base.py` (pipeline runs in a worker thread; httpx.Client):

- `LLMProvider.generate(*, system, user, json_schema=None, temperature, max_tokens) -> LLMResult`
- `TTSProvider.voices() / synthesize(*, text, voice, out_path, speed, language) -> TTSResult`
- `STTProvider.transcribe_words(*, audio_path, language, hint_text) -> list[WordTiming]`
- `ImageProvider.generate_image(*, prompt, out_path, aspect, negative_prompt, seed) -> AssetRef`
- `StockProvider.search(*, query, kind, min_duration_s, orientation, per_page) -> list[StockResult]` / `download(result, out_path, *, max_height) -> AssetRef`
- `Uploader.upload(*, video_path, metadata, privacy="private", thumbnail_path, progress) -> UploadResult`

Decorator registry `@register(kind, name)`; config selects ordered fallback chains (`llm: [groq, gemini]`, `stock: [pexels, pixabay]`) driven by an error taxonomy (`QuotaExceeded` → next provider; `TransientError` → backoff honoring Retry-After; `ProviderConfigError` → fail fast in `doctor`). Token-bucket RPM + daily counters persisted in `~/.cache/ai-video-maker/quota.json` with soft budgets under the hard limits (groq 28 rpm, gemini-flash 240/day, pexels 190/h). `mock.py` implements every ABC (canned JSON, silent wav, fixture jpg) for tests and `--providers mock`.

### 4.7 Web UI

FastAPI + Jinja2 + vendored htmx (~14KB, no npm) + ~200 lines vanilla JS (autosave debounce, crop slider, clipboard). Progress via **polling** partial (1.5s) reading a `JobState` dict fed by ffmpeg `-progress pipe:1` parsing. One daemon worker thread + `queue.Queue`, one job at a time. Routes: `/` list+create · `/projects/{id}` stepper dashboard · `/…/script` Gate 1 (per-scene textareas, split/merge/reorder, visual query) · `/…/storyboard` Gate 2 (candidate swap, live search with remaining-quota indicator, motion select, crop-focus slider with 9:16 overlay, `in_short` toggle, per-scene re-voice) · `/…/preview` Gate 3 (side-by-side previews, music picker) · `/…/render` (progress, metadata copy, Reveal in Finder, optional upload) · `/media/{id}/{path}` (path-traversal-guarded FileResponse) · `/healthz`.

### 4.8 Testing

Unit (models, hashing, caption chunker, ASS snapshot, filter-graph builders, template validation, rate limiter) · integration golden path (mock providers + **real ffmpeg** → both MP4s, ffprobe assertions; runs in CI on ubuntu) · contract tests per provider auto-skipped without keys. GitHub Actions free tier.

## 5. Milestones (each ends with a runnable definition-of-done)

- **M0 — Scaffold + install spike.** Repo skeleton, pyproject/uv.lock, community files, CI, `videomaker doctor` (ffmpeg `subtitles`-filter probe with remediation, keys, disk) + `setup` (downloads Kokoro ONNX + whisper model, 5-word TTS+STT smoke test). Proves the Intel-Mac wheel stacks before feature work. *DoD: `uv run videomaker doctor` all green; pytest green in CI.*
- **M1 — CLI happy path (wide).** ProjectStore + hash engine, providers + mocks, stages through render, golden-path CI test. *DoD: `videomaker new "how ssds work" -t tech_explainer && videomaker run <id> --yes` → playable `final_wide.mp4`; immediate re-run <10s.*
- **M2 — Review web UI.** All three gates in browser. *DoD: full project via browser; editing one scene at storyboard re-generates only that scene.*
- **M3 — Dual format + polish.** Vertical pipeline, karaoke captions, music ducking, thumbnails, `clean`, videotoolbox fast mode. *DoD: one project → final_wide + ≤3-min final_vertical + thumbnail; captions styled per aspect; music ducks audibly.*
- **M4 — Four niche templates** as validated YAML + `templates lint` + authoring docs. *DoD: 4 real projects, structurally correct and stylistically distinct; lint in CI.*
- **M5 — Metadata + optional YouTube uploader** (OAuth installed-app, resumable, private default; audit + quota caveats documented; manual stays default). *DoD: metadata.md paste-ready; configured upload puts a private video on the owner's channel.*
- **M6 — Docker + open-source readiness.** Compose (apt ffmpeg has libass), self-hosting + quickstart docs verified on a clean machine, issue templates, NOTICE.md, v0.1.0. *DoD: `docker compose up` on a clean VM → rendered MP4 via browser.*

Post-v1 (deferred): Instagram API publishing, plugin entry-points, SQLite index, multi-job worker, auth, community launch.

## 6. Risk register (top items)

1. FFmpeg without libass on this Mac — certain/blocker → `brew reinstall ffmpeg`; doctor probe. 
2. Intel-Mac ML wheels (onnxruntime, ctranslate2) — high/blocker if unpinned → pins + fallback ladder, proven in M0. 
3. Render time on 4-core i5 (10-min video ≈ 10–20 min/aspect libx264) — certain/medium → per-scene caching, 480p previews, videotoolbox 2–4×. 
4. Pexels 200 req/h during storyboard browsing — medium → batched search, 7-day cache, budget UI, pixabay fallback. 
5. Free-LLM JSON flakiness — medium → schema mode + repair retry + fallback + response cache. 
6. Disk pressure (1–3GB intermediates/project) — low/medium → `clean --keep-outputs`, doctor warning <20GB. 
7. YouTube API private-lock for unaudited apps — certain/low → manual upload is the default and recommendation. 
8. Platform AI-content policy — the three human gates and original-script workflow exist precisely for this; documented in docs/youtube-policy.md.
