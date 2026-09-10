"""Reading audio, built straight from the text: one verse, one file, in order.

**No forced alignment, and therefore no STT.** The narration is the source text
segmented by rule — `segment_for_reading` returns the verses and nothing else — so
fidelity is true by construction rather than by instruction, and the fidelity test
in `tests/unit/test_reading_audio.py` is the most important test in the plan.
Verse timings come from the duration of each verse's own audio file: synthesising
verse by verse makes verse-level highlighting free, and verse level is the right
granularity for scripture. Word level is not needed until captions are burned into
a frame, which is the video plan's problem.

**Per-verse caching is what makes a rate limit survivable.** Each verse is written
atomically and probed as it arrives under
`library/<work>/derived/audio/<reading_key>/verses/`, so `build_reading` called
again after a `RateLimited` or a `TTSBudgetExceeded` synthesises only the verses
still missing and stitches the rest from cache. A half-written file that looks
present would be worse than an absent one, hence the `.part` rename.

The concatenated file is an mp3 because the plan says so and because `<audio>`
plays it everywhere. Measured here: LAME's encoder delay plus its end padding add
about 64 ms to the container duration at 24 kHz (1,105 samples of delay and up to
one 1,152-sample frame of padding), which browsers trim on playback via the LAME
tag. `Reading.duration_s` is the container's own number, so it is that much longer
than the last segment's `end_s`; the segments are the timeline the VTT is cut on.
"""

import os
from pathlib import Path

from pydantic import BaseModel

from videomaker.cache import _write_atomic, hash_inputs
from videomaker.corpus.importer import DERIVED_DIRNAME, work_dir
from videomaker.corpus.models import UnitRef, UnitText
from videomaker.media.ffmpeg import probe_duration, run_ffmpeg
from videomaker.pipeline.base import StageDeps
from videomaker.providers.base import CorpusProvider, TTSProvider
from videomaker.providers.tts.http_api import (
    HTTPTTSProvider,
    TTSBudget,
    check_budget,
    estimate_minutes,
)

AUDIO_DIRNAME = "audio"
VERSES_DIRNAME = "verses"
READING_FILE = "reading.json"
MP3_FILE = "reading.mp3"
VTT_FILE = "reading.vtt"
CONCAT_LIST = "concat.txt"

#: Verse files carry no format suffix because the provider decides the format —
#: Kokoro and the mock write wav, an HTTP vendor returns whatever it was asked
#: for — and ffmpeg identifies a file by its bytes, not its name.
VERSE_SUFFIX = ".audio"

#: LAME VBR quality 4 is ~165 kbps for speech: transparent for narration and a
#: fifth the size of a wav. The whole point of the mp3 is to be small enough to
#: stream from a laptop over a tunnel.
MP3_QUALITY = "4"


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
    audio_relpath: str
    vtt_relpath: str
    segments: list[ReadingSegment]


def segment_for_reading(unit: UnitText) -> list[tuple[int, str]]:
    """The verses, in order, and nothing that is not a verse. This is the fidelity
    guarantee, and it is deliberately too simple to get wrong."""
    return [(verse.number, verse.text) for verse in unit.verses]


def reading_key(unit: UnitText, *, provider: str, voice: str, speed: float) -> str:
    return hash_inputs(text=unit.plain, provider=provider, voice=voice, speed=speed)


def reader_deps(settings, *, cache_dir: Path | None = None) -> StageDeps:
    """`StageDeps` for the reader, which has no project.

    The stage cache is the library's, not a project's — the reader creates no
    `Project` and enters no stage — and the response cache and quota ledger are the
    same user-wide ones every run shares, so a brief cached by the CLI is a hit in
    the browser and a vendor's daily cap is counted once.
    """
    from videomaker import runner
    from videomaker.cache import ResponseCache, StageCache
    from videomaker.project import ProjectStore
    from videomaker.providers.ratelimit import QuotaTracker

    root = runner.USER_CACHE_DIR if cache_dir is None else Path(cache_dir)
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(work_dir(settings.workspace_dir, ".library") / "stages.json"),
        response_cache=ResponseCache(root / runner.RESPONSES_DIRNAME),
        quota=QuotaTracker(root / runner.QUOTA_FILENAME),
    )


def _work_language(deps: StageDeps, work_id: str) -> str:
    corpus = deps.provider("corpus")
    assert isinstance(corpus, CorpusProvider)
    for work in corpus.works():
        if work.id == work_id:
            return work.language
    raise KeyError(f"no work {work_id!r} in the library")


def _vtt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def write_vtt(segments: list[ReadingSegment]) -> str:
    """One metadata cue per verse whose payload is the verse number — what
    `reader.js` reads on `cuechange` to light the verse."""
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines += [
            str(segment.verse),
            f"{_vtt_time(segment.start_s)} --> {_vtt_time(segment.end_s)}",
            str(segment.verse),
            "",
        ]
    return "\n".join(lines)


def _synthesise_missing(
    tts: TTSProvider,
    pieces: list[tuple[int, str]],
    verses_dir: Path,
    *,
    voice: str,
    speed: float,
    language: str,
) -> list[float]:
    """Every verse's duration, synthesising only the ones not already on disk."""
    durations: list[float] = []
    for number, text in pieces:
        path = verses_dir / f"{number:03d}{VERSE_SUFFIX}"
        if path.is_file():
            durations.append(probe_duration(path))
            continue
        part = path.with_name(f"{path.name}.part")
        result = tts.synthesize(text=text, voice=voice, out_path=part, speed=speed, language=language)
        os.replace(part, path)
        durations.append(result.duration_s)
    return durations


def _concat_to_mp3(root: Path, segments: list[ReadingSegment]) -> None:
    """Stitch the verse files with the concat demuxer, re-encoding once to mp3.

    Run with `cwd=root` and relative paths, like every FFmpeg call in the project:
    an absolute path with a colon in it breaks the filter parser (M0 finding).
    """
    listing = "\n".join(
        f"file '{VERSES_DIRNAME}/{seg.verse:03d}{VERSE_SUFFIX}'" for seg in segments
    )
    (root / CONCAT_LIST).write_text(listing + "\n")
    run_ffmpeg(
        [
            "-f", "concat", "-safe", "0", "-i", CONCAT_LIST,
            "-c:a", "libmp3lame", "-q:a", MP3_QUALITY,
            MP3_FILE,
        ],
        cwd=root,
    )


def root_relpath(root: Path) -> str:
    """`derived/audio/<key>` — the reading directory relative to its work."""
    return Path(*root.parts[-3:]).as_posix()


def build_reading(
    unit: UnitText,
    deps: StageDeps,
    *,
    voice: str,
    speed: float = 1.0,
    confirmed: bool = False,
) -> Reading:
    """The reading for `unit` in `voice`, from cache when it exists.

    A second call with identical inputs makes zero provider calls: the finished
    `reading.json` is the answer. A call after an interrupted run finds the verse
    files already made and synthesises only the rest.
    """
    provider_name = deps.leading_name("tts")
    key = reading_key(unit, provider=provider_name, voice=voice, speed=speed)
    work_root = work_dir(deps.settings.workspace_dir, unit.ref.work_id)
    root = work_root / DERIVED_DIRNAME / AUDIO_DIRNAME / key
    finished = root / READING_FILE
    if finished.is_file():
        return Reading.model_validate_json(finished.read_text())

    language = _work_language(deps, unit.ref.work_id)
    pieces = segment_for_reading(unit)
    verses_dir = root / VERSES_DIRNAME
    verses_dir.mkdir(parents=True, exist_ok=True)
    tts = deps.provider("tts")
    assert isinstance(tts, TTSProvider)
    if isinstance(tts, HTTPTTSProvider):
        # The guards run over the *missing* verses, up front, so the refusal names
        # the whole remaining bill and not the first verse's share of it.
        missing = [
            (n, t) for n, t in pieces if not (verses_dir / f"{n:03d}{VERSE_SUFFIX}").is_file()
        ]
        tts.budget = TTSBudget(confirmed=confirmed)
        minutes = sum(estimate_minutes(t) for _n, t in missing) / max(speed, 0.01)
        check_budget(minutes, cfg=tts.cfg, budget=tts.budget)
        tts.reserve(len(missing))

    durations = _synthesise_missing(
        tts, pieces, verses_dir, voice=voice, speed=speed, language=language
    )
    segments: list[ReadingSegment] = []
    at = 0.0
    for (number, text), duration in zip(pieces, durations, strict=True):
        segments.append(
            ReadingSegment(
                verse=number,
                text=text,
                start_s=at,
                end_s=at + duration,
                audio_relpath=f"{root_relpath(root)}/{VERSES_DIRNAME}/{number:03d}{VERSE_SUFFIX}",
            )
        )
        at += duration
    _concat_to_mp3(root, segments)
    _write_atomic(root / VTT_FILE, write_vtt(segments))
    reading = Reading(
        ref=unit.ref,
        voice=voice,
        language=language,
        provider=provider_name,
        duration_s=probe_duration(root / MP3_FILE),
        audio_relpath=f"{root_relpath(root)}/{MP3_FILE}",
        vtt_relpath=f"{root_relpath(root)}/{VTT_FILE}",
        segments=segments,
    )
    _write_atomic(finished, reading.model_dump_json(indent=1))
    return reading
