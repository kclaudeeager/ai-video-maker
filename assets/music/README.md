# Music

**This directory is empty on purpose, and it stays that way in the repository.**

The project ships **no audio files, ever**. Every track under `assets/music/` is
one *you* downloaded and are entitled to use. `.gitignore` keeps audio out of the
repository, so your library is yours and nothing you drop here can be committed by
accident.

This is a licensing decision before it is an architectural one. Content ID issues
false claims against Creative Commons music routinely, and a claim on a monetised
video is precisely the outcome this project exists to avoid. If the project
shipped tracks, it could become the source of one; shipping nothing means it never
can. See `docs/audio-design.md` for the full reasoning.

An empty library is **not an error**. With nothing here the pipeline renders
narration only, exactly as it did before music existed.

## Layout

```
assets/music/
├── README.md        # this file
├── library.yaml     # the licence record — one entry per file
├── calm/            # one directory per mood
├── upbeat/
└── dramatic/
```

A mood is just a directory name. `calm`, `upbeat` and `dramatic` are created as a
starting point because templates declare `music_mood: calm`; invent
`assets/music/tense/` and a template can say `music_mood: tense` with no code
change.

Any container FFmpeg decodes works — `.mp3`, `.wav`, `.flac`, `.m4a`, `.ogg`,
`.opus`. Everything else in the tree (this file, `library.yaml`, `.gitkeep`) is
ignored by the scanner.

## Where to get licence-clear audio

All free, all with a licence you can point at. Every one of these is a **manual
download**: none has a public API worth automating, and that is fine — you pick
tracks once and reuse them for months.

| source | licence | notes |
|---|---|---|
| **[YouTube Audio Library](https://studio.youtube.com)** (Studio → Audio Library) | free for creators; some CC-BY, most attribution-free | the safest option for YouTube specifically — no public API, download by hand |
| **[Pixabay Music](https://pixabay.com/music/)** | Pixabay Content Licence, commercial use OK | large, mixed quality; attribution not required |
| **[Freesound](https://freesound.org)** | per file — **filter to CC0** | filter first, always; some files are CC-BY-NC and unusable |
| **[Free Music Archive](https://freemusicarchive.org)** | per file — check each one | licences vary track by track |
| **[Incompetech](https://incompetech.com/music/royalty-free/)** (Kevin MacLeod) | CC-BY — **attribution required** | record the exact credit line in `library.yaml` |

Whichever you use: save the licence and the source URL **at the moment you
download**, and put them in `library.yaml`. Reconstructing where a file came from
six months later is miserable, and an unattributed CC-BY track is a licence
breach even though it plays perfectly.

## Never rip audio from a YouTube video

Do **not** point `yt-dlp` (or anything else) at a watch URL to extract audio for
this library. It violates YouTube's Terms of Service, and the underlying music is
virtually always copyrighted — it would generate exactly the Content ID claims and
strikes that this project's three-gate curation workflow exists to prevent.

There is deliberately no "paste a YouTube URL" field for audio anywhere in the
tool, and there never will be. The YouTube **Audio Library** above is a different
thing entirely, and is fine.

Trending TikTok/Reels audio is out for a related reason: it is unavailable through
any legal API, and leaning on it is the most reliable way to look like the
templated AI content platforms are demonetising.

## `library.yaml` — the licence record

One entry per file, keyed by path. This is what lets the tool generate a correct
attribution block for the video description automatically, the same way Pexels
attribution already flows through from the stock provider.

```yaml
tracks:
  music/calm/rain-on-glass.mp3:
    title: Rain On Glass
    artist: Kevin MacLeod
    licence: CC-BY-4.0
    attribution: '"Rain On Glass" by Kevin MacLeod (incompetech.com), licensed under CC BY 4.0'
    source_url: https://incompetech.com/music/royalty-free/
```

Keys are `music/<mood>/<file>` or `sfx/<role>/<file>` — one record covers both
trees. Inside this file, `calm/rain-on-glass.mp3` is accepted as shorthand for
`music/calm/rain-on-glass.mp3`.

`attribution` is the exact line to print. Set it when a licence dictates wording;
otherwise leave it out and one is composed from `title`, `artist` and `licence`.

A file with **no** entry is still perfectly usable — it plays, it mixes, nothing
fails. It is only *reported* as unattributed, by `videomaker music list` and
`videomaker doctor`, because nothing can credit it.

## Commands

```bash
uv run videomaker music scan   # ffprobe every file, cache the durations
uv run videomaker music list   # print the library, flag anything uncredited
uv run videomaker doctor       # includes an "audio library" check
```

The index lives at `~/.cache/ai-video-maker/music_index.json`. It is a **cache and
nothing more**: delete it whenever you like, and the filesystem is always believed
over it. `scan` only saves you the ffprobe calls.
