# Sound effects

**This directory is empty on purpose, and it stays that way in the repository.**

As with music, the project ships **no audio files, ever** — see
`assets/music/README.md` and `docs/audio-design.md` for why. `.gitignore` keeps
audio out of the repository, so nothing you drop here can be committed by
accident, and an empty tree is **not an error**: with no effects the pipeline
renders exactly what it renders today.

## Layout — four roles

```
assets/sfx/
├── README.md
├── transition/   # whooshes and swells on scene cuts
├── accent/       # a stat reveal, the hook landing
├── riser/        # tension into the mechanism beat
└── ambient/      # per-scene room tone, sat well under the narration
```

Unlike moods, these four role names are fixed — the pipeline places effects by
role, so `transition/` is where cut effects are looked for.

**One good file per role is enough to start.** A single reused whoosh on every cut
is what makes the cuts read as deliberate rather than abrupt; it is the highest
return per unit of work in the whole audio layer. Ambience is a distant fourth,
and literal foley (key clicks over typing footage) is deliberately last and
possibly never — stock clips arrive effectively mute, so there is nothing to sync
against and a hit slightly out of time reads as worse than silence.

Keep transition and accent effects **short** — well under a second. They sit in
the mix as-is and are never ducked; only music is sidechained under narration.

## Where to get licence-clear effects

The same five sources as music, and the same rule: save the licence and the source
URL at the moment you download.

| source | licence | notes |
|---|---|---|
| **[Freesound](https://freesound.org)** | per file — **filter to CC0** | the best SFX source by far; the CC0 filter is not optional |
| **[Pixabay SFX](https://pixabay.com/sound-effects/)** | Pixabay Content Licence, commercial use OK | good whooshes and risers, attribution not required |
| **[YouTube Audio Library](https://studio.youtube.com)** (Studio → Audio Library, Sound effects tab) | free for creators, attribution-free | small but entirely safe on YouTube |
| **[Free Music Archive](https://freemusicarchive.org)** | per file — check each one | mostly music, occasional usable beds |
| **[Incompetech](https://incompetech.com/music/royalty-free/)** (Kevin MacLeod) | CC-BY — **attribution required** | stingers and short cues; record the exact credit line |

## Never rip audio from a YouTube video

Do **not** use `yt-dlp` (or anything else) to extract audio from a YouTube watch
URL for this library. It violates YouTube's Terms of Service and the underlying
audio is virtually always copyrighted — it is the fastest route to the Content ID
claims and strikes this project's curation workflow exists to prevent. There is no
"paste a YouTube URL" field for audio in the tool, and there will not be. The
YouTube **Audio Library** in the table above is a different thing, and is fine.

## Attribution

Effects are recorded in the same file as music, `assets/music/library.yaml`, keyed
by their `sfx/<role>/<file>` path:

```yaml
tracks:
  sfx/transition/whoosh-soft.wav:
    title: Soft Whoosh
    artist: someuser
    licence: CC0-1.0
    source_url: https://freesound.org/s/000000/
```

A CC0 effect needs no credit line, but recording it anyway is what proves — to
you, in a year — that it *was* CC0. A file with no entry still works; it is only
reported as unattributed by `videomaker music list` and `videomaker doctor`.
