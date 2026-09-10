# Audio design — music and sound effects

Design notes agreed before M3 is planned. M3 implements this; M2 is unaffected.

## Principle: the library is the user's, always

The tool ships **no audio files**. Every track and effect is dropped in by the
owner of the repo. This is a licensing decision before it is an architectural
one: Content ID issues false claims against Creative Commons music routinely, and
a claim on a monetised video is exactly the outcome this project exists to avoid.
Shipping nothing means the project can never be the source of a claim.

It is also the better architecture — the library suits the niche the owner is
actually making videos for.

### Where audio comes from (all free, all licence-clear)

| source | licence | how |
|---|---|---|
| **YouTube Audio Library** (studio.youtube.com → Audio Library) | free for creators; some CC-BY, most attribution-free | manual download — **no public API** |
| **Pixabay Music / SFX** | Pixabay licence, commercial use OK | manual download |
| **Freesound** | per-file; filter to **CC0** | manual download |
| **Free Music Archive** | per-file; check each | manual download |
| **Incompetech** (Kevin MacLeod) | CC-BY, attribution required | manual download |

### Fetching, which does not change the principle

`videomaker music fetch <query> --mood calm --take 2` searches a licence-clear
catalogue and downloads into `assets/music/<mood>/`. **The project still ships no
audio.** `.gitignore` excludes `assets/music/**`, so a fetched track cannot be
committed even by accident — the rule holds by construction rather than by care,
which is the only way a rule like this survives contact with a hurry.

What the command adds is the tedious half: writing the credit line down *at
download time*, into `library.yaml`, which is the one moment anybody actually
knows it.

**The gate is stricter than "free", and deliberately so.** Only CC0, Public
Domain Mark, CC BY and CC BY-SA are accepted:

* **NonCommercial is refused.** The videos this tool makes are meant to be
  monetised, and NC is precisely the term that forbids that.
* **NoDerivatives is refused.** A bed mixed under narration and ducked against it
  is an adaptation, whatever it is called in conversation.

There is no `--force`, for the same reason the text importer has none. A track
whose licence cannot be stated is a Content ID claim waiting to happen, and that
claim is the outcome this whole section exists to avoid.

Sources live in `config.yaml` under `music_sources:`, the same shape as
`voice_providers:` and for the same reason — a second catalogue should be a YAML
entry, not a patch. The default is **Openverse**, which aggregates Freesound,
Jamendo, ccMixter and Wikimedia, states a licence per item, needs no API key, and
returns a ready-made attribution string.

**Explicitly rejected: extracting audio from arbitrary YouTube videos**
(`yt-dlp` against a watch URL). It violates YouTube's Terms of Service and the
underlying music is virtually always copyrighted. It would generate exactly the
Content ID claims and strikes the three-gate curation workflow exists to prevent.
Do not add it, and do not add a "paste a YouTube URL" field for audio.

## Layout

Extends the `assets/music/<mood>/` pattern the spec already describes:

```
assets/
├── music/
│   ├── README.md              # how to add tracks, and the source table above
│   ├── calm/  upbeat/  dramatic/
│   └── library.yaml           # per-file: title, artist, licence, attribution, source_url
└── sfx/
    ├── README.md
    ├── transition/            # whooshes on scene cuts
    ├── accent/                # stat reveals, the hook
    ├── riser/                 # into the mechanism beat
    └── ambient/               # per-scene room tone
```

`library.yaml` is the licensing record. It is what lets M5 generate a correct
attribution block automatically, the same way Pexels attribution already flows
from `AssetRef.attribution`. A track with no entry is usable but is reported by
`videomaker doctor` as unattributed — a warning, never a hard failure.

## Audio layers, ranked by value per unit of work

1. **Music bed, ducked under narration** — the single biggest lift. Already in
   M3 via `sidechaincompress` + `amix normalize=0` + `loudnorm I=-14:TP=-1.5`.
2. **Transition SFX on scene cuts** — highest ROI of the new work. One reused
   file makes cuts read as deliberate. Every cut timestamp is already known from
   `scene_timeline`, so this needs no new analysis.
3. **Beat-mapped accents** — templates already declare
   `structure: [hook, context, mechanism, implication, close]`. That is an
   editorial map: accent the `hook`, rise into `mechanism`, resolve on `close`.
   No LLM call and no inference from footage — the structure already says where
   the emphasis belongs.
4. **Ambient bed per scene** — moderate effort, moderate payoff.
5. **Literal foley** (key clicks over typing footage, fan hum over a server rack)
   — **deliberately last, and possibly never.** Pexels clips arrive effectively
   mute, so there is no diegetic audio to match against; a foley hit slightly out
   of sync with on-screen action reads as worse than silence. Revisit only after
   watching finished videos and finding it is genuinely missed.

## User control

Control lives at four levels, coarsest first:

- **Template** — `music_mood: calm` already exists per template; add
  `sfx_profile: subtle | punchy | none`.
- **`config.yaml`** — global defaults: `music_volume_db`, `duck_amount_db`,
  `sfx_enabled`, `transition_sfx_enabled`.
- **Per project** — `MusicSelection` already exists on the `Project` model.
- **Gate 3 (preview), in the browser** — the music picker the spec already lists
  on the `/preview` route: choose a track, preview the mix, toggle SFX, adjust
  ducking. This is the one that matters day to day.

Nothing is mandatory: with an empty `assets/music/` the pipeline renders exactly
what it renders today, narration only. Absence of a library is not an error.

## Implementation notes for M3

- `videomaker music scan` indexes `assets/` (ffprobe for duration, read
  `library.yaml`) into `~/.cache/ai-video-maker/music_index.json`. `music list`
  prints it. The index is a cache, never the source of truth.
- Mixing happens in the **existing** render pass — one more `amix` input, no new
  pipeline stage, no per-video cost.
- A track shorter than the video loops; a longer one is trimmed with a fade.
- M1 measured the audio chain as 24 kHz mono end to end (Kokoro's native rate).
  Music will be 44.1/48 kHz stereo, so **resample at mix time, not at TTS time** —
  this is M1 spike follow-up 5, and this is the feature that makes it bite.
- Loudness is measured once at −14.9 LUFS against the −14 target with a
  single-pass `loudnorm`; adding music makes the ~1 LU miss more visible
  (M1 follow-up 11). Two-pass `loudnorm` becomes worth it here.

## Non-goals

- **Trending audio** (TikTok/Reels sounds). It genuinely boosts distribution and
  is genuinely unavailable through any legal API — and leaning on it is the most
  reliable way to look like the templated AI content that platforms are
  demonetising. The project's thesis is original curated content; this is the
  wrong lever.
- **AI music or AI SFX generation.** No free tier that holds up, and the $0
  promise is load-bearing.
- Ducking anything other than music under narration. SFX are short enough that
  they sit in the mix without sidechaining.
