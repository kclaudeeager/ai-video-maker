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
from dataclasses import dataclass
from pathlib import Path

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
    """A bed placed against one finished cut, plus pass one's measurement if it ran."""

    bed: MusicBed
    duration_s: float
    measured: "Loudness | None" = None


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


# --------------------------------------------------------------- the mix graph


def _layout(channels: int) -> str:
    return "mono" if channels == 1 else "stereo"


def mix_filter_complex(
    bed: MusicBed,
    *,
    duration_s: float,
    rate: int,
    channels: int,
    target: str,
    speech: int,
    music: int,
    measured: Loudness | None = None,
    print_json: bool = False,
) -> str:
    """The audio graph: resample, place the bed, duck it, mix, normalise.

    `speech` and `music` are input indices rather than paths, which is what lets the
    measuring pass (narration and music only) and the real render (video first) share
    one graph. The narration is resampled here as well as the bed: the bed decides
    nothing about the rate, `assemble` does, and both branches of `sidechaincompress`
    must agree on rate, layout and sample format or the filter refuses to link.
    """
    fmt = f"aresample={rate},aformat=sample_fmts=fltp:channel_layouts={_layout(channels)}"
    duration = max(duration_s, 1.0 / rate)
    fade_in, fade_out = fade_window(duration)
    chains = [
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
        # `normalize=0` because amix would otherwise halve both inputs to avoid
        # clipping, quietly undoing the levels above; `loudnorm` is what handles the
        # sum. `duration=first` ends the mix with the narration, which is authored to
        # the exact timeline.
        "[voice][ducked]amix=inputs=2:normalize=0:duration=first[mixed]",
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
    bed: MusicBed,
    *,
    narration: str,
    duration_s: float,
    rate: int,
    channels: int,
    target: str,
) -> list[str]:
    """Pass one: the finished mix, measured, with no video decoded and nothing written."""
    return [
        "-i",
        narration,
        *music_input_args(bed),
        "-filter_complex",
        mix_filter_complex(
            bed,
            duration_s=duration_s,
            rate=rate,
            channels=channels,
            target=target,
            speech=0,
            music=1,
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
