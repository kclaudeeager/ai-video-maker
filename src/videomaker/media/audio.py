"""Mixing a music bed under the narration: the duck, the loop, the loudness.

`videomaker.audio` knows what is on disk; this module knows what to do with one
track once it has been chosen. It is pure string-and-argument construction plus a
single measuring pass, so the whole mix is inspectable without decoding audio —
`tests/unit/test_audio_mix.py` reads the graph, and `tests/integration/test_render_music.py`
renders a clip and *measures* the duck, because a graph containing
`sidechaincompress` proves nothing about what a listener hears.

Three decisions here are worth the reader's time.

**The track is an input, never a filter argument.** M0 found that FFmpeg parses a
filter graph before it looks at the filesystem, so a path containing a colon breaks
the parse. Every path in the render is therefore relative and FFmpeg runs with `cwd`
set to the project. The user's library, though, lives outside the project folder
(`assets/music/`, wherever `config.yaml` puts it), so it cannot be made relative.
It does not need to be: it is passed to `-i`, which is not parsed as a graph, and
the graph refers to it only by input index. `music_input_args` is the only place a
track's path appears.

**One code path loops and trims.** `-stream_loop -1` on the input makes any track as
long as the render needs; `atrim` then cuts it to the finished cut's length and
`afade` lands it. A 40-second loop under a two-minute video and a five-minute track
under the same video take the identical route through this module.

**The duck is a target, not a guarantee.** A sidechain compressor's gain reduction is
`(1 - 1/ratio) x (sidechain level - threshold)`: it depends on how loud the take
actually is. `duck_ratio` inverts that against `NOMINAL_SPEECH_DBFS`, so asking for
12 dB gets ~12 dB from a normally-recorded narration and somewhat less from a quiet
one. That is how every broadcast ducker behaves, and it is why the guarantee this
module is allowed to claim is "measurably quieter under speech", measured for real.

Loudness is two-pass **only when there is music** (M1 follow-up 11: a single pass
measured -14.9 LUFS against a -14 target). With an empty library the render is
byte-for-byte the arguments M1 shipped — see `pipeline/render._render_args`.
"""

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from videomaker.audio import Track
from videomaker.media.ffmpeg import FFmpegError, run_ffmpeg

#: How loud the bed sits before ducking. -18 dB under a narration that `loudnorm`
#: then lifts to -14 LUFS is the usual "present but not competing" setting.
DEFAULT_VOLUME_DB = -18.0

#: How far the bed drops while someone is talking, in dB. Positive: it is a depth.
DEFAULT_DUCK_DB = 12.0

#: The compressor's knee, well under any speech but well over a noise floor, so the
#: bed is at full level in the gaps and pulled down as soon as a word starts.
DUCK_THRESHOLD_DB = -45.0

#: The level `duck_db` is calibrated against — an ordinary narration bed measures
#: near this. See the module docstring: it is what makes the depth a target.
NOMINAL_SPEECH_DBFS = -21.0

#: `sidechaincompress` accepts 1..20; an extreme request clamps rather than failing.
MAX_RATIO = 20.0

#: Fast enough that the first syllable is not left uncovered, slow enough that the
#: bed does not pump between words.
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 300

#: How long the bed takes to arrive and to leave, and the most of a cut either may
#: eat. The cap matters for a Short: a 2 s clip must not be all fade.
FADE_IN_S = 1.0
FADE_OUT_S = 2.0
MAX_FADE_FRACTION = 0.25

#: `loudnorm`'s JSON keys, and how many stderr lines the measuring pass keeps. The
#: block is thirteen lines and FFmpeg prints it last, so this is slack, not a limit.
MEASURED_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
MEASURE_STDERR_LINES = 80


# --------------------------------------------------------------------- the bed


@dataclass(frozen=True)
class MusicBed:
    """One chosen track and the two knobs a person actually turns."""

    path: Path
    key: str = ""
    volume_db: float = DEFAULT_VOLUME_DB
    duck_db: float = DEFAULT_DUCK_DB

    def knobs(self) -> dict[str, float | str]:
        """Everything about this bed that changes the finished bytes, minus the file.

        The file itself is hashed by content by the caller, the same way every other
        render input is, so a re-downloaded but different track re-renders.
        """
        return {"key": self.key, "volume_db": self.volume_db, "duck_db": self.duck_db}


@dataclass(frozen=True)
class MusicMix:
    """Everything sitting under one finished cut, plus pass one's measurement.

    `bed` is optional and `sfx` may be empty, but a `MusicMix` that is *both* is never
    constructed: `pipeline/render._plan_mix` returns `None` instead, which is what
    keeps a fresh clone on M1's exact single-pass argument list.
    """

    bed: "MusicBed | None"
    duration_s: float
    measured: "Loudness | None" = None
    sfx: "SfxPlan" = field(default_factory=lambda: SfxPlan())


# ---------------------------------------------------------------- the loudness


@dataclass(frozen=True)
class Loudness:
    """What `loudnorm` measured on pass one, in the form pass two wants it back."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    offset: float

    def as_args(self) -> str:
        return (
            f":measured_I={self.input_i:.2f}"
            f":measured_TP={self.input_tp:.2f}"
            f":measured_LRA={self.input_lra:.2f}"
            f":measured_thresh={self.input_thresh:.2f}"
            f":offset={self.offset:.2f}"
            ":linear=true"
        )


def parse_loudness(text: str) -> Loudness | None:
    """The measurement in `text`, or `None` if there is nothing usable in it.

    Every failure collapses to `None` on purpose: pass one is an optimisation, and a
    render that cannot be measured must still be normalised, not abandoned. The
    `-inf` case is the one that would otherwise reach FFmpeg as a filter argument it
    silently mis-parses — `loudnorm` reports it for a bed that is entirely silent,
    which is exactly what the mock TTS writes.
    """
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        values = [float(data[key]) for key in MEASURED_KEYS]
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return Loudness(*values)


def loudnorm_filter(target: str, *, measured: Loudness | None, print_json: bool) -> str:
    """The `loudnorm` filter for one pass: measuring, or applying a measurement."""
    graph = f"loudnorm={target}"
    if measured is not None:
        graph += measured.as_args()
    if print_json:
        graph += ":print_format=json"
    return graph


# -------------------------------------------------------------------- the duck


def duck_ratio(duck_db: float) -> float:
    """The compressor ratio that yields `duck_db` of reduction on nominal speech.

    Inverts `reduction = (1 - 1/ratio) x span`, where `span` is how far a normal
    narration bed sits above the threshold. A request deeper than the span saturates
    at `MAX_RATIO` rather than raising: the answer to "duck by 40 dB" is "as hard as
    the filter can", not a stack trace.
    """
    if duck_db <= 0:
        return 1.0
    span = NOMINAL_SPEECH_DBFS - DUCK_THRESHOLD_DB
    remaining = max(1.0 - duck_db / span, 1.0 / MAX_RATIO)
    return min(1.0 / remaining, MAX_RATIO)


def fade_window(duration_s: float) -> tuple[float, float]:
    """How long the bed's fade in and fade out run on a cut this long."""
    cap = max(duration_s, 0.0) * MAX_FADE_FRACTION
    return min(FADE_IN_S, cap), min(FADE_OUT_S, cap)


# --------------------------------------------------------------------- the SFX
#
# Two layers, in the order `docs/audio-design.md` ranks them.
#
# **Transitions on scene cuts** come first because they are the highest return per
# unit of work in the whole audio design: every cut timestamp is already known from
# `scene_timeline`, so the feature needs no new analysis at all, and one reused file
# is what makes a run of cuts read as deliberate rather than abrupt.
#
# **Beat-mapped accents** come second, and they are just as free. A template's
# `structure` is already an editorial map — `[hook, context, mechanism, implication,
# close]` — and `Scene.beat` already records which beat each scene was written for
# (M3 Task 22). Accenting the hook, rising into the mechanism and resolving on the
# close is therefore a lookup, not an inference. There is no LLM call here and
# nothing reads the footage.
#
# What is *not* here is literal foley, and that is a decision rather than an
# omission. Pexels clips arrive effectively mute, so there is no diegetic audio to
# sync against, and a key click landing slightly off the frame where a finger moves
# reads as worse than silence. The design doc puts it last and possibly never.
#
# Effects are **never ducked**. Only the bed is sidechained; a sub-second whoosh sits
# in the mix as it is, and `loudnorm` handles the sum.


@dataclass(frozen=True)
class SfxProfile:
    """How loud this template's effects sit, per role.

    Two profiles and an off switch, rather than a dB per role in every template: the
    person choosing is picking a house style ("there, but restrained" against "there,
    and you noticed"), and a template that had to name six numbers would be a mixing
    desk pretending to be an editorial recipe.
    """

    name: str
    levels: Mapping[str, float] = field(default_factory=dict)

    def gain_db(self, role: str) -> float:
        return self.levels.get(role, 0.0)

    def places(self, role: str) -> bool:
        return role in self.levels


SFX_TRANSITION = "transition"
SFX_ACCENT = "accent"
SFX_RISER = "riser"

#: Which role marks which beat. The `structure` beats not named here — `context` and
#: `implication` in the default template — carry the argument, and a sting over them
#: would punctuate a sentence that is still running.
BEAT_ROLES: dict[str, str] = {
    "hook": SFX_ACCENT,
    "mechanism": SFX_RISER,
    "close": SFX_ACCENT,
}

#: How far ahead of the thing it introduces each effect starts. A transition swells
#: *into* the cut, so it leads it by rather less than a beat; a riser is authored to
#: build, so it starts a full second early.
#:
#: Both are fixed rather than derived from the file's own length, deliberately. The
#: library is scanned with `probe=None` during a render, so a duration is only known
#: when `videomaker music scan` happens to have run — placing effects by length would
#: make the render fingerprint move when someone rebuilt an index, re-encoding
#: finished videos for no change anybody asked for.
TRANSITION_LEAD_S = 0.12
RISER_LEAD_S = 1.0
ROLE_LEAD_S: dict[str, float] = {
    SFX_TRANSITION: TRANSITION_LEAD_S,
    SFX_ACCENT: 0.0,
    SFX_RISER: RISER_LEAD_S,
}

DEFAULT_SFX_PROFILE = "subtle"

SFX_PROFILES: dict[str, SfxProfile] = {
    # Nothing at all, for a template whose subject would be cheapened by a whoosh.
    "none": SfxProfile(name="none"),
    # The default: present under the narration, never over it.
    "subtle": SfxProfile(
        name="subtle",
        levels={SFX_TRANSITION: -14.0, SFX_ACCENT: -11.0, SFX_RISER: -14.0},
    ),
    "punchy": SfxProfile(
        name="punchy",
        levels={SFX_TRANSITION: -7.0, SFX_ACCENT: -4.0, SFX_RISER: -7.0},
    ),
}


@dataclass(frozen=True)
class SfxCue:
    """One effect, once, at one place on the finished timeline."""

    path: Path
    key: str
    role: str
    at_s: float
    gain_db: float

    def knobs(self) -> dict[str, object]:
        """Everything about this placement that changes the finished bytes, minus the
        file — which the caller hashes by content, like every other render input."""
        return {
            "key": self.key,
            "role": self.role,
            "at_s": round(self.at_s, 3),
            "gain_db": self.gain_db,
        }


@dataclass(frozen=True)
class SfxPlan:
    """Every effect placed against one aspect's cut. Empty is the default everywhere."""

    cues: tuple[SfxCue, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.cues)

    @property
    def files(self) -> tuple[Path, ...]:
        """The distinct files, in first-use order — one `-i` each, however many cues."""
        seen: dict[Path, None] = {}
        for cue in self.cues:
            seen.setdefault(cue.path, None)
        return tuple(seen)

    def input_args(self) -> list[str]:
        """The effects as FFmpeg inputs. **The only place an effect's path is written.**

        No `-stream_loop`: an effect plays once where it was placed. A file longer than
        what is left of the video is cut by the `duration=first` on the mix, the same
        way the bed's `atrim` cuts a long track.
        """
        return [argument for path in self.files for argument in ("-i", str(path))]

    def knobs(self) -> list[dict[str, object]]:
        return [cue.knobs() for cue in self.cues]


def plan_sfx(
    cuts: Sequence[tuple[str, float]],
    *,
    beats: Mapping[str, str | None],
    pick: Callable[[str], Track | None],
    profile: SfxProfile,
    transitions: bool = True,
) -> SfxPlan:
    """Place this cut's effects. `cuts` is `(scene_id, start_s)` in timeline order.

    A pure function over two lists the pipeline already has, which is the whole claim
    of this feature: `scene_timeline` supplies the starts, `Scene.beat` supplies the
    beats, and nothing else is consulted.

    `pick` is called **at most once per role**, so every cut gets the same whoosh and
    every accent the same ding. A role `pick` has nothing for simply places nothing:
    a library with `transition/` filled and `riser/` empty is a perfectly ordinary
    library, not a half-configured one.

    The first scene's start is not a cut — there is nothing to cut *from* — and a lead
    in never goes negative, so a scene starting 50 ms in still gets its whoosh, at 0.

    **Every cue's path is resolved**, exactly as `render.music_bed` resolves the bed's,
    and for exactly the same reason: FFmpeg runs with `cwd` set to the *project*, while
    the library is configured relative to the *user's* cwd (`sfx_dir: ./assets/sfx` is
    what `config.example.yaml` ships). An unresolved path is written into `-i` and the
    render dies with "No such file or directory" the moment a user drops in their first
    whoosh. The bed was given `.resolve()` when it was written; the effects were not.
    """
    wanted: list[tuple[str, float]] = []
    for index, (scene_id, start_s) in enumerate(cuts):
        if transitions and index > 0:
            wanted.append((SFX_TRANSITION, start_s))
        role = BEAT_ROLES.get(beats.get(scene_id) or "")
        if role is not None:
            wanted.append((role, start_s))

    chosen: dict[str, Track | None] = {}
    cues: list[SfxCue] = []
    for role, start_s in wanted:
        if not profile.places(role):
            continue
        if role not in chosen:
            chosen[role] = pick(role)
        track = chosen[role]
        if track is None:
            continue
        cues.append(
            SfxCue(
                path=track.path.resolve(),
                key=track.key,
                role=role,
                at_s=max(start_s - ROLE_LEAD_S.get(role, 0.0), 0.0),
                gain_db=profile.gain_db(role),
            )
        )
    cues.sort(key=lambda cue: (cue.at_s, cue.role, cue.key))
    return SfxPlan(cues=tuple(cues))


# --------------------------------------------------------------- the mix graph


def _layout(channels: int) -> str:
    return "mono" if channels == 1 else "stereo"


def _sfx_chains(sfx: SfxPlan, *, fmt: str, first_input: int) -> tuple[list[str], list[str]]:
    """One chain per file, then one per placement. Returns `(chains, mix labels)`.

    A file used on six cuts is decoded once and `asplit`, rather than opened six
    times: the whole point of the layer is that it is one reused sound, and six
    identical `-i` arguments would be six decoders for the same bytes.
    """
    chains: list[str] = []
    labels: list[str] = []
    for index, path in enumerate(sfx.files):
        cues = [cue for cue in sfx.cues if cue.path == path]
        outs = "".join(f"[sfxsrc{index}_{n}]" for n in range(len(cues)))
        chains.append(f"[{first_input + index}:a]{fmt},asplit={len(cues)}{outs}")
        for n, cue in enumerate(cues):
            delay_ms = max(round(cue.at_s * 1000), 0)
            # `adelay` is what places it; an effect at 0 needs no filter at all, and
            # `adelay=0` would only be a no-op in the graph a reader has to check.
            place = f",adelay={delay_ms}:all=1" if delay_ms else ""
            chains.append(
                f"[sfxsrc{index}_{n}]volume={cue.gain_db:.2f}dB{place}[sfx{index}_{n}]"
            )
            labels.append(f"[sfx{index}_{n}]")
    return chains, labels


def mix_filter_complex(
    bed: MusicBed | None,
    *,
    duration_s: float,
    rate: int,
    channels: int,
    target: str,
    speech: int,
    music: int,
    sfx: SfxPlan | None = None,
    sfx_input: int = 0,
    measured: Loudness | None = None,
    print_json: bool = False,
) -> str:
    """The audio graph: resample, place the bed, duck it, add the effects, normalise.

    `speech`, `music` and `sfx_input` are input indices rather than paths, which is
    what lets the measuring pass (audio only) and the real render (video first) share
    one graph. The narration is resampled here as well as the bed: the bed decides
    nothing about the rate, `assemble` does, and both branches of `sidechaincompress`
    must agree on rate, layout and sample format or the filter refuses to link. The
    effects are resampled for the same reason — a 44.1 kHz whoosh cannot join a 48 kHz
    `amix` unconverted (M1 follow-up 5, once more).

    With no effects this returns exactly the string Task 9 shipped, byte for byte, and
    `bed=None` is the library that has whooshes but no music — a real library, and the
    one shape where there is nothing to sidechain.
    """
    fmt = f"aresample={rate},aformat=sample_fmts=fltp:channel_layouts={_layout(channels)}"
    duration = max(duration_s, 1.0 / rate)
    chains: list[str] = []
    sources: list[str] = []

    if bed is None:
        chains.append(f"[{speech}:a]{fmt}[voice]")
        sources.append("[voice]")
    else:
        fade_in, fade_out = fade_window(duration)
        chains += [
            f"[{speech}:a]{fmt},asplit=2[sc][voice]",
            (
                f"[{music}:a]{fmt},"
                f"atrim=end={duration:.3f},asetpts=N/SR/TB,"
                f"afade=t=in:st=0:d={fade_in:.3f},"
                f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f},"
                f"volume={bed.volume_db:.2f}dB[bed]"
            ),
            (
                f"[bed][sc]sidechaincompress="
                f"threshold={10 ** (DUCK_THRESHOLD_DB / 20):.6f}"
                f":ratio={duck_ratio(bed.duck_db):.3f}"
                f":attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}"
                ":makeup=1:level_sc=1[ducked]"
            ),
        ]
        sources += ["[voice]", "[ducked]"]

    if sfx:
        effect_chains, effect_labels = _sfx_chains(sfx, fmt=fmt, first_input=sfx_input)
        chains += effect_chains
        sources += effect_labels

    chains += [
        # `normalize=0` because amix would otherwise divide every input to avoid
        # clipping, quietly undoing the levels above; `loudnorm` is what handles the
        # sum. `duration=first` ends the mix with the narration, which is authored to
        # the exact timeline — and it is also what trims an effect placed near the end.
        f"{''.join(sources)}amix=inputs={len(sources)}:normalize=0:duration=first[mixed]",
        f"[mixed]{loudnorm_filter(target, measured=measured, print_json=print_json)}[aout]",
    ]
    return ";".join(chains)


def music_input_args(bed: MusicBed) -> list[str]:
    """The bed as an FFmpeg input. **The only place a track's path is written.**

    `-stream_loop -1` is what makes a short track fill a long video; the `atrim` in
    the graph is what stops a long one from running past the end. Both cases take
    this one path, so neither needs the track's duration to be known here.
    """
    return ["-stream_loop", "-1", "-i", str(bed.path)]


def measure_args(
    bed: MusicBed | None,
    *,
    narration: str,
    duration_s: float,
    rate: int,
    channels: int,
    target: str,
    sfx: SfxPlan | None = None,
) -> list[str]:
    """Pass one: the finished mix, measured, with no video decoded and nothing written.

    **The effects are in it.** Pass one exists to tell pass two what the finished mix
    measures, and a mix missing a layer is not that mix — leaving the whooshes out
    would hand `loudnorm` a measurement of something nobody will ever hear.
    """
    sfx = sfx or SfxPlan()
    bed_args = music_input_args(bed) if bed is not None else []
    return [
        "-i",
        narration,
        *bed_args,
        *sfx.input_args(),
        "-filter_complex",
        mix_filter_complex(
            bed,
            duration_s=duration_s,
            rate=rate,
            channels=channels,
            target=target,
            speech=0,
            music=1,
            sfx=sfx,
            sfx_input=1 + (1 if bed is not None else 0),
            print_json=True,
        ),
        "-map",
        "[aout]",
        "-f",
        "null",
        "-",
    ]


def measure_loudness(args: list[str], *, cwd: Path) -> Loudness | None:
    """Run the measuring pass, or return `None` if it could not be measured.

    A failure here is swallowed deliberately. If the mix is genuinely broken the
    render pass fails on the same graph a moment later, with the same stderr; losing
    the *measurement* only costs the second pass's accuracy, and a render that
    refused to start because a throwaway analysis exited non-zero would be worse.
    """
    try:
        stderr = run_ffmpeg(args, cwd=cwd, keep_stderr_lines=MEASURE_STDERR_LINES)
    except FFmpegError:
        return None
    return parse_loudness(stderr)
