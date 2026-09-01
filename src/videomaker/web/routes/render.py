"""Gate 3: watching the 480p preview, approving it, and watching the final render.

**This module's reason to exist is the progress bar.** Everything else here is
the shape gates 1 and 2 already established — a page, an approve form that stamps
and enqueues, a 303, and a live region that stops asking to be polled once there
is nothing left to watch. What is new is that the last stage of the run is also,
by a wide margin, the longest: M1 measured 70 s of encode inside a 184 s run.
`on_stage` fires once per *stage*, so a bar driven by it alone sits frozen at six
sevenths for the entire part of the run the user actually sits and watches, and
then jumps to done. That is worse than no bar, because it looks like a hang.

So the encode reports on itself. `run_ffmpeg` already emits **output seconds** to
an `on_progress` callback, and `assemble` already knows how many output seconds
the finished video has (`scene_timeline`). The ratio of the two is a real
completion fraction, and `encode_reporter` maps it onto the slice of the bar the
render stage owns — from `ENCODE_FLOOR` (everything before it) up to 1.0.

**`run_render` takes the hook as a parameter (M3 Task 18).** M2 could not change
the pipeline, so it substituted an instrumented `run_ffmpeg` into
`videomaker.pipeline.render` for the length of one call. That was honest only
because there is one worker thread running one job at a time, and its own docstring
named the trigger to replace it: a second worker. It is replaced now — before
anything adds one, rather than after — and with it went the arithmetic that banked
each aspect's output seconds out here. `run_render` does that itself, because
`run_render` is what knows there is more than one file to encode; this module is
left with the part that is genuinely its own, which is mapping a fraction onto a
bar. `build_preview` already worked this way, so both bars are now driven the same
way by the same `encode_reporter`.

**Staleness is judged by mtime here, not by re-hashing.** `build_preview` decides
for real, by content hash, and skips the encode when nothing moved — it is the
authority and it is cheap when the answer is "nothing to do". This page only has
to label a button, and it is re-rendered every 1.5 s while a job runs; digesting a
hundred megabytes of `build/video_wide.mp4` on every poll to choose between two
words would be an absurd price. mtime errs in the safe direction: an input
rewritten with identical bytes reads as stale, which costs one no-op rebuild,
while a changed input can never read as fresh.

**Gate 3 shows two cuts and a music picker (M3 Task 11).** One project yields the
long cut and the Short, and the Short is a different *edit* rather than a reframe of
the same one, so judging it means watching it — hence a proxy per aspect, side by
side, and `preview.short_problem`'s own sentence in place of the Short when its
`in_short` set is empty. The picker under them is the fourth and finest level of
control `docs/audio-design.md` describes; it writes `Project.music` and nothing else.

That adds a second freshness question the mtime rule above cannot answer. The bed
lives in the user's own `assets/music/`, outside the project folder, so choosing a
different track moves no mtime inside it at all — `preview.mix_is_current` settles
that half by fingerprinting the mix's knobs, which costs no ffprobe and no file
digest. Both answers land on the same `stale` state, because to the reviewer they
mean the same thing: what you are watching is not what would ship.

**The pages poll themselves rather than `/job`.** `_job.html` is the dashboard's
partial and reports only the job; these pages have to reveal a `<video>` element
the moment the file behind it exists, which a job fragment cannot do. So the live
region carries `hx-select` naming its own id: htmx re-fetches the page, lifts that
section out and swaps it. The rule that ends the loop is unchanged and is the
important half — `hx-trigger` is emitted **only** while a job is in flight, so a
finished render stops making requests instead of re-reading `project.json` forty
times a minute for as long as the tab is left open.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from videomaker.audio import Library, format_duration, select_track
from videomaker.cache import STAGE_ORDER
from videomaker.config import Settings
from videomaker.models import Aspect, MusicSelection, Project
from videomaker.pipeline.assemble import (
    SCENE_GAP_S,
    narration_relpath,
    scene_timeline,
    video_relpath,
)
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.captions import caption_relpath
from videomaker.pipeline.render import (
    RENDER_ASPECTS,
    music_mood_for,
    output_relpath,
    scan_library,
    sfx_wanted,
)
from videomaker.preview import (
    PREVIEW_ASPECTS,
    build_previews,
    mix_is_current,
    preview_relpath,
    short_problem,
)
from videomaker.preview import STAGE as PREVIEW_STAGE
from videomaker.project import ProjectStore
from videomaker.runner import (
    GATE_BEFORE,
    build_deps,
    derive_status,
    run_pipeline,
    stage_cache_for,
    stage_is_current,
)

# The shared 404 helper, the status palette, the job palette, the gate titles and
# the terminal-job rule all belong to the dashboard module. Importing them keeps
# one definition of each: a second 404 helper would be a second chance to return
# a 500 for a URL somebody typed by hand, and a second copy of the terminal-state
# set would be a second chance for a page to poll forever.
from videomaker.web.guidance import next_step
from videomaker.web.routes.projects import (
    _JOB_TONES,
    _STATUS_TONES,
    GATE_TITLES,
    _load,
    _terminal,
)
from videomaker.web.worker import (
    JobFn,
    JobProgress,
    JobQueue,
    JobQueueFull,
    JobState,
    stage_progress,
)

router = APIRouter()

#: The gate this page clears, and the stage it guards (`render`).
GATE = "preview"
GATED_STAGE = next(stage for stage, gate in GATE_BEFORE.items() if gate == GATE)

#: The aspect every *single-artefact* view on these pages means: the deliverable
#: the render page offers, and the wide half of gate 3's pair. The pair itself is
#: `preview.PREVIEW_ASPECTS`, which is `RENDER_ASPECTS` — one proxy per file the
#: render really writes, so a panel can never advertise a cut that does not exist.
ASPECT = Aspect.WIDE

#: What each panel is called, and which way round it stands. Both are read by the
#: template alone; keeping them here means the words appear once.
ASPECT_LABELS: dict[Aspect, str] = {
    Aspect.WIDE: "The long cut",
    Aspect.VERTICAL: "The Short",
}
ASPECT_NOTES: dict[Aspect, str] = {
    Aspect.WIDE: "16:9 — every scene, in project order.",
    Aspect.VERTICAL: "9:16 — only the scenes ticked into the Short.",
}

#: Where a fresh clone is sent to learn how to fill an empty library. A path inside
#: the repository, not a URL: the sources are listed *in* that file, and `docs/
#: audio-design.md` is explicit that the project ships no audio and links to nothing
#: it does not control.
MUSIC_README = "assets/music/README.md"

#: What the two sliders may ask for. Wide enough to be useful, bounded so a
#: hand-written POST cannot hand FFmpeg a level that makes the mix meaningless.
VOLUME_RANGE = (-40.0, 0.0)
DUCK_RANGE = (0.0, 30.0)
LEVEL_STEP = 0.5

#: How close to `config.yaml`'s value counts as *being* it. A slider dragged back to
#: the default has to be able to clear the override — otherwise the first touch pins
#: the project to a number the config can never move again, which is the opposite of
#: what "empty means no opinion" promises (`MusicSelection`).
LEVEL_TOLERANCE = 1e-6

#: The picker's two answers, in the storyboard's words, so one vocabulary covers
#: every form on the three gates.
SAVED_NOTE = "updated"
UNCHANGED_NOTE = "no change"
UNKNOWN_TRACK = "unknown-track"

#: Where the bar stands when the final encode is about to begin: the fraction
#: `on_stage` reported for the stage before the gated one. Derived from
#: `STAGE_ORDER` rather than written down, so a stage added in M3 moves it
#: without anyone having to remember that it needed moving.
ENCODE_FLOOR: float = stage_progress(STAGE_ORDER[STAGE_ORDER.index(GATED_STAGE) - 1]) or 0.0

#: How much of the whole bar has to be gained before another update is published.
#: FFmpeg is asked for progress every 10 ms of wall time, which for a long encode
#: is thousands of callbacks; each one takes the worker's lock, and no human can
#: see a tenth of a percent. At most ~140 updates across the render's seventh of
#: the bar: enough that it visibly moves, few enough that the lock is barely
#: touched.
ENCODE_STEP = 0.001

#: Prefix of the message an encode update carries. It is what distinguishes a
#: reading that came from inside FFmpeg from `on_stage`'s coarse end-of-stage
#: tick, on the page and in the tests.
ENCODE_NOTE = "encoding"

#: The job kinds that rewrite each page's artefact, and so hide it while they run.
PREVIEW_KINDS: tuple[str, ...] = ("preview",)
OUTPUT_KINDS: tuple[str, ...] = ("run",)

#: How often the live region re-fetches itself while a job is in flight. Same
#: cadence as the dashboard's job partial, for one rhythm across the UI.
POLL_MS = 1500


# --------------------------------------------------------------- progress maths


def timeline_seconds(project: Project) -> float:
    """How long the finished video is, in output seconds.

    Straight from `assemble`'s own timeline, so the denominator of the progress
    fraction is the same number the encode is working towards rather than a
    second estimate that could disagree with it.

    Summed over **every** aspect in `RENDER_ASPECTS`, because `run_render` encodes
    one file per aspect and the bar covers the whole stage. Denominating on wide
    alone made it sweep 0->1 once per aspect (M3 Task 6 finding).
    """
    return sum(
        segment.duration_s
        for aspect in RENDER_ASPECTS
        for segment in scene_timeline(project, gap_s=SCENE_GAP_S, aspect=aspect)
    )


def encode_fraction(seconds: float, total: float) -> float:
    """`seconds` of output as a fraction of `total`, clamped to 0…1.

    Clamped at the top because FFmpeg can report an `out_time` a frame or two
    past the nominal duration, and at the bottom because a project with nothing
    assemblable in it has a timeline of zero and no fraction to speak of.
    """
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, seconds / total))


def encode_reporter(
    progress: JobProgress,
    total: float,
    *,
    stage: str,
    floor: float = ENCODE_FLOOR,
    offset: float = 0.0,
) -> Callable[[float], None]:
    """A `run_ffmpeg` `on_progress` hook that drives `JobState.progress`.

    Output seconds land on the slice of the bar between `floor` and 1.0, which is
    the slice the encoding stage owns. Updates below `ENCODE_STEP` of movement are
    dropped: the point is a bar a human can see moving, not a lock contended
    thousands of times.
    """
    span = 1.0 - floor
    last = -1.0

    def report(seconds: float) -> None:
        nonlocal last
        # `seconds` restarts at zero for each aspect's file; `offset` is what the
        # earlier aspects already contributed, so the bar climbs once overall.
        fraction = encode_fraction(offset + seconds, total)
        value = floor + span * fraction
        if fraction < 1.0 and value - last < ENCODE_STEP:
            return
        last = value
        progress.update(
            stage=stage,
            progress=value,
            message=f"{ENCODE_NOTE} {offset + seconds:.1f}s of {total:.1f}s",
        )

    return report


def encode_hook(
    progress: JobProgress, total_seconds: Callable[[], float], *, stage: str
) -> Callable[[float], None]:
    """The `on_progress` `run_render` is handed: seconds in, a bar position out.

    The seconds arriving here already climb once across every aspect the stage
    encodes — `render._AspectProgress` is what makes that true, and it is true
    there because that is where the aspects are. All that is left to do out here is
    the mapping onto `ENCODE_FLOOR`…1.0.

    **`total_seconds` is called when the encode starts, not when the job does**,
    which is why the reporter is built on the first reading rather than up front.
    Everything between those two moments — a re-voiced scene, a narration the
    script stage regenerated — is precisely what decides how long the finished
    video is. Measuring the timeline up front and holding on to the number gives a
    bar that pins at 100 % a fifth of the way through a run that grew, which is the
    same uselessness as a bar that never moves, only harder to notice.
    """
    report: Callable[[float], None] | None = None

    def hook(seconds: float) -> None:
        nonlocal report
        if report is None:
            report = encode_reporter(progress, total_seconds(), stage=stage)
        report(seconds)

    return hook


# ------------------------------------------------------------------- job bodies


def preview_job(settings: Settings, project_id: str) -> JobFn:
    """Bring the project up to `assemble`, then encode **both** 480p proxies.

    Running the pipeline first is not belt and braces: the preview is a picture of
    the *assembled* timeline, so previewing a project whose narration moved since
    the last assemble would show the reviewer something that is not what would
    ship. `run_pipeline` skips everything already current, so the usual cost of
    this line is nothing, and a project that has not cleared gates 1 and 2 stops
    at the right one with the right message rather than failing here.

    Two encodes, one bar. Each file reports output seconds from zero, so what the
    previous aspect reached is banked and used as the next one's offset — the same
    arithmetic `render._AspectProgress` does inside the render stage, for the same
    reason: a bar that rewinds to the floor halfway through reads as a crash. It is
    written out here because `build_previews` hands back a hook per aspect and so
    puts the seam in the caller's hands; the render stage keeps its own.

    An aspect that *cannot* be previewed is skipped rather than raised
    (`build_previews`): an empty Short must not cost the reviewer the long cut,
    which is finished and sitting there. The page says why the panel is empty.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        run_pipeline(deps.store.load(project_id), deps, until="assemble", on_stage=progress)
        project = deps.store.load(project_id)
        total = timeline_seconds(project)
        banked = 0.0
        reached = 0.0

        def on_aspect(_aspect: Aspect) -> Callable[[float], None]:
            nonlocal banked, reached
            banked += reached
            reached = 0.0
            report = encode_reporter(progress, total, stage=PREVIEW_STAGE, offset=banked)

            def hook(seconds: float) -> None:
                nonlocal reached
                reached = max(reached, seconds)
                report(seconds)

            return hook

        build_previews(project, deps, on_aspect=on_aspect)

    return job


def render_job(settings: Settings, project_id: str) -> JobFn:
    """Run to the end, with the final encode reporting its own progress.

    No `until`: `run_pipeline` stops at the first unapproved gate on its own, so
    "run to the end" needs no second copy of where the gates are.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        project = deps.store.load(project_id)
        # `project` is the object `run_pipeline` mutates, so reading the timeline
        # off it *when the encode starts* is reading it after every stage that
        # could have changed its length has already run.
        run_pipeline(
            project,
            deps,
            on_stage=progress,
            on_encode=encode_hook(progress, lambda: timeline_seconds(project), stage=GATED_STAGE),
        )

    return job


# ------------------------------------------------------------------ view models


@dataclass(frozen=True)
class GateView:
    """Gate 3 as this page shows it. Derived from `project.approvals`, never stored."""

    name: str
    title: str
    approved_at: datetime | None

    @property
    def approved(self) -> bool:
        return self.approved_at is not None

    @property
    def marker(self) -> str:
        return "true" if self.approved else "false"

    @property
    def state_label(self) -> str:
        return "approved" if self.approved else "not approved"

    @property
    def tone(self) -> str:
        return "status-ok" if self.approved else "status-waiting"


@dataclass(frozen=True)
class ArtefactView:
    """One playable file: whether it is there, whether it is current, and its URLs.

    `state` is what the templates and the tests both read — `ready`, `stale` or
    `missing` — so the three-way answer is decided once, here, rather than by a
    chain of `{% if %}` in two templates that could drift apart.
    """

    project_id: str
    relpath: str
    path: Path
    exists: bool
    fresh: bool
    aspect: Aspect = Aspect.WIDE
    #: Why this artefact cannot exist at all, in the words the pipeline refuses in
    #: (`preview.short_problem`). Empty for every artefact that is merely unbuilt —
    #: "you have not made this yet" and "this cut cannot be made" are different
    #: sentences, and only the second one is worth a paragraph on the page.
    problem: str = ""

    @property
    def state(self) -> str:
        if self.problem:
            return "blocked"
        if not self.exists:
            return "missing"
        return "ready" if self.fresh else "stale"

    @property
    def label(self) -> str:
        return ASPECT_LABELS.get(self.aspect, self.aspect.value)

    @property
    def note(self) -> str:
        return ASPECT_NOTES.get(self.aspect, "")

    @property
    def orientation(self) -> str:
        """`landscape` or `portrait` — the frame the `<video>` box is drawn at."""
        return "portrait" if self.aspect is Aspect.VERTICAL else "landscape"

    @property
    def url(self) -> str:
        return f"/media/{self.project_id}/{self.relpath}"

    @property
    def location(self) -> str:
        """The absolute path, for copying into a shell or a file manager."""
        return str(self.path)

    @property
    def download_name(self) -> str:
        """The filename a download should land under.

        Not the artefact's own name: every project renders to `final_wide.mp4`, so
        downloading three projects would give `final_wide.mp4`,
        `final_wide(1).mp4`, `final_wide(2).mp4` and no way to tell them apart.
        Prefixing the project id keeps them identifiable in a Downloads folder.
        """
        return f"{self.project_id}-{Path(self.relpath).name}"


def _newest_input(root: Path, aspect: Aspect = ASPECT) -> float:
    """The mtime of the most recently written thing this proxy is made from."""
    inputs = (
        root / video_relpath(aspect),
        root / narration_relpath(aspect),
        root / caption_relpath(aspect),
    )
    stamps = [path.stat().st_mtime_ns for path in inputs if path.is_file()]
    return max(stamps) if stamps else 0.0


def assembled(root: Path, aspect: Aspect = ASPECT) -> bool:
    """True when `assemble` has left the two files a preview needs."""
    return (root / video_relpath(aspect)).is_file() and (
        root / narration_relpath(aspect)
    ).is_file()


def preview_view(
    store: ProjectStore,
    project: Project,
    aspect: Aspect = ASPECT,
    *,
    problem: str = "",
    mixed_as_asked: bool = True,
) -> ArtefactView:
    """One 480p proxy: present, newer than its inputs, and mixed with today's bed?

    Two freshness questions, and they are answered by different means for a reason.
    The *inputs* are files inside the project, so mtime settles them cheaply — see
    the module docstring for why this page must not re-hash a hundred megabytes on
    every 1.5 s poll. The *bed* is not: it lives in the user's own `assets/music/`,
    so nothing inside the project moves when the picker changes the choice, and
    `preview.mix_is_current` answers that half by fingerprinting knobs instead.
    Either one going stale means the same thing to the reviewer — what you are
    watching is not what would ship — so they land on the same `stale` state.
    """
    root = store.path_for(project.id)
    path = root / preview_relpath(aspect)
    exists = path.is_file()
    fresh = exists and mixed_as_asked and path.stat().st_mtime_ns >= _newest_input(root, aspect)
    return ArtefactView(
        project_id=project.id,
        relpath=preview_relpath(aspect),
        path=path,
        exists=exists,
        fresh=fresh,
        aspect=aspect,
        problem=problem,
    )


def preview_views(
    store: ProjectStore, project: Project, deps: StageDeps, *, library: Library
) -> list[ArtefactView]:
    """Both cuts, in the order gate 3 shows them: the long one, then the Short.

    A Short that could not ship gets `short_problem`'s sentence rather than a panel
    that says "not built yet" about a file nothing would ever build — that is Task
    6's `ShortNotRenderable`, surfaced instead of raised.
    """
    return [
        preview_view(
            store,
            project,
            aspect,
            problem=short_problem(project, aspect=aspect),
            mixed_as_asked=mix_is_current(project, deps, aspect, library=library),
        )
        for aspect in PREVIEW_ASPECTS
    ]


def output_view(store: ProjectStore, project: Project) -> ArtefactView:
    """The deliverable. Freshness is `stage_is_current`, the same answer the CLI gives."""
    root = store.path_for(project.id)
    path = root / output_relpath(ASPECT)
    exists = path.is_file()
    stage_cache = stage_cache_for(store, project.id)
    return ArtefactView(
        project_id=project.id,
        relpath=output_relpath(ASPECT),
        path=path,
        exists=exists,
        fresh=exists and stage_is_current(project, stage_cache, GATED_STAGE),
    )


# ------------------------------------------------------------ the music picker
#
# The fourth and finest of the four levels of control `docs/audio-design.md`
# describes — template, then `config.yaml`, then the project, then this — and the
# one the design doc says matters day to day. It writes `Project.music` and
# nothing else.
#
# **An empty library is the default path, not an edge case.** The project ships no
# audio files and never will (a licensing decision, stated as binding), so
# `assets/music/` is empty on every fresh clone including the owner's. The empty
# state therefore has to be a plain sentence pointing at the README that explains
# how to fill it, not an apology and not a disabled form pretending there is
# something to choose.
#
# **Every field is an override, and the empty value means "no opinion".** That is
# `MusicSelection`'s own contract, and it is what keeps the four levels in order.
# It has one consequence worth stating: a slider dragged back onto the configured
# default must *clear* the override rather than freeze the project at a number
# `config.yaml` can no longer move. `_level` is where that happens, and it is also
# why re-submitting an untouched form is a no-op rather than a write.


@dataclass(frozen=True)
class TrackOption:
    """One track as the picker offers it.

    The key carries its own mood — `music/calm/rain.wav` — so there is no second
    field for it, and no second place for the two to disagree.
    """

    key: str
    #: `""` when nothing has measured this file yet. `scan_library` deliberately
    #: runs with `probe=None` — the whole audio layer costs no ffprobe — so a length
    #: is known only when `videomaker music scan` has filled the index. Showing
    #: `?` for every track would read as "unreadable", which is a different fact.
    duration: str
    unreadable: bool
    chosen: bool


@dataclass(frozen=True)
class MusicView:
    """Gate 3's picker: what is in the library, and what this project asks for."""

    options: tuple[TrackOption, ...]
    selection: MusicSelection
    volume_db: float
    duck_db: float
    sfx_enabled: bool
    default_volume_db: float
    default_duck_db: float
    mood: str
    #: The track that would really play if the render started now — the answer to
    #: "Automatic, but automatically *what*?". Resolved through `audio.select_track`,
    #: the same function `render.music_bed` calls, so the page cannot promise a bed
    #: the render would not choose.
    playing: str = ""
    playing_credit: str = ""
    warnings: tuple[str, ...] = ()
    note: str = ""
    problem: str = ""
    readme: str = MUSIC_README
    volume_range: tuple[float, float] = VOLUME_RANGE
    duck_range: tuple[float, float] = DUCK_RANGE
    step: float = LEVEL_STEP

    @property
    def state(self) -> str:
        return "ready" if self.options else "empty"

    @property
    def count(self) -> int:
        return len(self.options)

    @property
    def tone(self) -> str:
        """The picker is chrome, never a summons. See §3 of `docs/ui-design.md`:
        amber is reserved for the one thing waiting on a human, and choosing a
        backing track is not it."""
        return "status-ok" if self.note == SAVED_NOTE else ""


def _level(raw: str, default: float, bounds: tuple[float, float]) -> float | None:
    """One slider reading as an override, or `None` for "no opinion".

    Unparseable is `None` rather than a 400: the field is a `range` input, so the
    only way to get junk here is a hand-written POST, and the honest reading of a
    level nobody can interpret is that nobody expressed one. Equalling the default
    is `None` too — that is how the override is cleared again.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    low, high = bounds
    value = min(max(value, low), high)
    return None if abs(value - default) <= LEVEL_TOLERANCE else value


def _checked(value: str) -> bool:
    """Whether a checkbox arrived ticked. An unticked box sends no field at all."""
    return value.strip().lower() in {"on", "true", "1", "yes"}


def music_selection(
    current: MusicSelection,
    settings: Settings,
    *,
    enabled: str,
    track_key: str,
    volume_db: str,
    duck_db: str,
    sfx: str,
) -> MusicSelection:
    """What the posted form means, as a `MusicSelection`. Pure — no I/O, no writes.

    Pure so the route has no second opinion in it: the comparison that decides
    whether to take the `flock` reads exactly what the write would have stored.

    `mood` is carried through untouched. The picker chooses a *track*; the mood is
    the template's job, and offering both would be two controls for one decision.
    """
    return MusicSelection(
        enabled=_checked(enabled),
        track_key=track_key.strip(),
        mood=current.mood,
        volume_db=_level(volume_db, settings.music_volume_db, VOLUME_RANGE),
        duck_db=_level(duck_db, settings.duck_amount_db, DUCK_RANGE),
        sfx_enabled=None if _checked(sfx) == settings.sfx_enabled else _checked(sfx),
    )


def track_options(library: Library, selection: MusicSelection) -> tuple[TrackOption, ...]:
    """The library as a list of choices, moods first, then names."""
    return tuple(
        TrackOption(
            key=track.key,
            duration=format_duration(track.duration_s) if track.duration_s > 0 else "",
            unreadable=bool(track.probe_error),
            chosen=track.key == selection.track_key,
        )
        for track in sorted(library.music(), key=lambda t: (t.group, t.key))
    )


def music_view(
    project: Project,
    settings: Settings,
    library: Library,
    *,
    note: str = "",
    problem: str = "",
) -> MusicView:
    """Everything the picker renders, derived — nothing here is stored."""
    selection = project.music
    mood = music_mood_for(project)
    playing = (
        select_track(library, mood=mood, track_key=selection.track_key, seed=project.id)
        if selection.enabled
        else None
    )
    return MusicView(
        options=track_options(library, selection),
        selection=selection,
        volume_db=(
            settings.music_volume_db if selection.volume_db is None else selection.volume_db
        ),
        duck_db=settings.duck_amount_db if selection.duck_db is None else selection.duck_db,
        sfx_enabled=sfx_wanted(project, settings),
        default_volume_db=settings.music_volume_db,
        default_duck_db=settings.duck_amount_db,
        mood=mood,
        playing=playing.key if playing is not None else "",
        playing_credit=(
            playing.attribution.credit_line()
            if playing is not None and playing.attribution is not None
            else ""
        ),
        warnings=library.warnings(),
        note=note,
        problem=problem,
    )


def writing(job: JobState | None, kinds: tuple[str, ...]) -> bool:
    """True while a job that rewrites this page's artefact is still in flight.

    FFmpeg writes its output in place, so from the instant the encode starts
    there is a file at `output/final_wide.mp4` that is not a video yet. Offering
    it — as a `<video>` that spins forever, and then as a "this file is out of
    date" warning about the very render that is producing it — is worse than
    offering nothing. `kinds` is what makes this specific rather than "any job":
    a render running does not make the 480p preview unplayable, and vice versa.
    """
    return not _terminal(job) and job is not None and job.kind in kinds


def _job_view(request: Request, project_id: str) -> dict[str, object]:
    """The job, its pill colour, and the one decision that ends the polling loop."""
    jobs: JobQueue = request.app.state.jobs
    job = jobs.state_for(project_id)
    return {
        "job": job,
        "poll": not _terminal(job),
        "job_tone": _JOB_TONES.get(job.state, "") if job is not None else "",
        "gate_titles": GATE_TITLES,
        "poll_ms": POLL_MS,
    }


def _page_context(request: Request, project: Project) -> dict[str, object]:
    """What both pages share: the project, its derived status, and the job."""
    store: ProjectStore = request.app.state.store
    stage_cache = stage_cache_for(store, project.id)
    status = derive_status(project, stage_cache)
    job_view = _job_view(request, project.id)
    job: JobState | None = job_view["job"]  # type: ignore[assignment]
    return {
        **job_view,
        # Derived from the same walk `derive_status` just performed, so the panel
        # and the pipeline cannot describe the project differently.
        "next": next_step(
            project,
            stage_cache,
            busy=bool(job_view["poll"]),
            running_stage=job.stage if job is not None else None,
        ),
        "project": project,
        "status": status,
        "status_label": status.value.replace("_", " "),
        "status_tone": _STATUS_TONES.get(status, ""),
        "gate": GateView(
            name=GATE,
            title=GATE_TITLES.get(GATE, GATE.title()),
            approved_at=getattr(project.approvals, GATE),
        ),
    }


# -------------------------------------------------------------- the two pages


@router.get("/projects/{project_id}/preview")
def preview_page(request: Request, project_id: str):
    """Gate 3: play the 480p proxy, rebuild it, or approve and render.

    A project that has not been assembled is an **empty state, not a 404**: this
    is where the button that fixes it lives, and a page that refuses to load
    would leave the reviewer with nowhere to press it. The artefact URL itself
    still 404s, cleanly, through the usual media guard.
    """
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store
    settings: Settings = request.app.state.settings
    deps = build_deps(settings, project_id)
    # One walk of `assets/` for the whole page: the picker lists it and each panel
    # asks whether its proxy was mixed from it, and this page polls itself every
    # 1.5 s while a job runs.
    library = scan_library(deps)
    previews = preview_views(store, project, deps, library=library)

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            **_page_context(request, project),
            "previews": previews,
            # The wide cut, by name, for the render page and for every `data-preview`
            # contract written before there were two of them.
            "preview": previews[0],
            "assembled": assembled(store.path_for(project_id)),
            "seconds": timeline_seconds(project),
            "music": music_view(project, settings, library),
            "writing": writing(
                request.app.state.jobs.state_for(project_id), PREVIEW_KINDS
            ),
        },
    )


@router.post("/projects/{project_id}/music")
def set_music(
    request: Request,
    project_id: str,
    enabled: str = Form(""),
    track_key: str = Form(""),
    volume_db: str = Form(""),
    duck_db: str = Form(""),
    sfx: str = Form(""),
):
    """Write gate 3's music choice onto the project. Nothing is encoded here.

    **Compare first, lock second.** `run_pipeline` holds the project `flock` for the
    length of a whole render, so a picker that locked before comparing would park a
    request thread behind a twenty-minute encode every time somebody re-submitted the
    settings already on the project — which a form with two sliders in it does
    constantly. The comparison is against the exact object the write would store, so
    it cannot disagree with what is saved (M2 Task 9's mutant 5).

    **Nothing upstream is invalidated, and no approval is cleared.** The audio layer's
    entire reach into the cache engine is `render_hash`: a new bed re-encodes
    `output/final_*.mp4` and re-assembles, re-captions and re-voices nothing. Gate 3's
    stamp stays where it is for the same reason — `clear_stale_approvals` walks the
    stages *upstream* of a gate, and music is downstream of every one of them. What
    tells the reviewer to listen again is the proxy going `stale`, which it does
    (`preview.mix_is_current`), with the notice that panel already carries.

    A `track_key` that is not in the library is **refused** rather than stored: a
    renamed or deleted file would otherwise be written onto the project and then
    silently fall back to another track at render time, which is how a bed disappears
    without anybody being told.
    """
    store: ProjectStore = request.app.state.store
    settings: Settings = request.app.state.settings
    project = _load(request, project_id)
    library = scan_library(build_deps(settings, project_id))

    posted = {
        "enabled": enabled,
        "track_key": track_key,
        "volume_db": volume_db,
        "duck_db": duck_db,
        "sfx": sfx,
    }
    wanted = music_selection(project.music, settings, **posted)
    if wanted.track_key and wanted.track_key not in {t.key for t in library.music()}:
        return _music_reply(request, project_id, library, problem=UNKNOWN_TRACK)

    note = UNCHANGED_NOTE
    if wanted != project.music:
        with store.lock(project_id):
            project = _load(request, project_id)
            fresh = music_selection(project.music, settings, **posted)
            if fresh != project.music:
                project.music = fresh
                store.save(project)
                note = SAVED_NOTE

    return _music_reply(request, project_id, library, note=note)


def _music_reply(
    request: Request,
    project_id: str,
    library: Library,
    *,
    note: str = "",
    problem: str = "",
):
    """The picker plus an out-of-band pair of panels, or a 303 for a plain browser.

    Same dual response gate 2 uses, for the same reason: every control on these
    pages is a real form first, so the whole product still works with JavaScript
    switched off (`docs/ui-design.md` §10).

    **Both fragments, not just the picker.** A new bed moves no byte inside the
    project folder, so nothing on the page would notice on its own — yet the two
    proxies stop being what would ship the instant Apply is pressed. Swapping the
    picker alone would leave the page contradicting itself, with a new track named
    above two players still stamped `ready`. The out-of-band swap is the storyboard's
    `#gate-state` trick, pointed at `#preview-cuts`.
    """
    project = _load(request, project_id)
    settings: Settings = request.app.state.settings
    if request.headers.get("HX-Request", "").lower() != "true":
        return RedirectResponse(url=f"/projects/{project_id}/preview#music", status_code=303)

    store: ProjectStore = request.app.state.store
    deps = build_deps(settings, project_id)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_music_reply.html",
        {
            "project": project,
            "music": music_view(project, settings, library, note=note, problem=problem),
            "previews": preview_views(store, project, deps, library=library),
            "writing": writing(
                request.app.state.jobs.state_for(project_id), PREVIEW_KINDS
            ),
        },
    )


@router.post("/projects/{project_id}/preview/build")
def build_preview_action(request: Request, project_id: str):
    """Enqueue the 480p encode and redirect back. Decision 2: nothing runs here.

    A second click while a job is in flight is harmless — `JobQueue.submit` is a
    no-op for a project that already has one — so the redirect is unconditional
    and the page reports whatever state it finds.
    """
    _load(request, project_id)
    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(project_id, "preview", preview_job(request.app.state.settings, project_id))
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(url=f"/projects/{project_id}/preview", status_code=303)


@router.post("/projects/{project_id}/approve/preview")
def approve_preview(request: Request, project_id: str):
    """Stamp gate 3, enqueue the final render, and redirect to the render page.

    Idempotent by construction: an existing stamp is left exactly as it is, so a
    double-clicked button cannot rewrite the moment the human actually approved.
    """
    _load(request, project_id)
    store: ProjectStore = request.app.state.store
    with store.lock(project_id):
        project = _load(request, project_id)
        if getattr(project.approvals, GATE) is None:
            setattr(project.approvals, GATE, datetime.now(UTC))
            store.save(project)

    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(project_id, "run", render_job(request.app.state.settings, project_id))
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(url=f"/projects/{project_id}/render", status_code=303)


@router.get("/projects/{project_id}/render")
def render_page(request: Request, project_id: str):
    """The render's progress, and — once there is one — the finished file.

    Also the page a failed render lands on, which is why FFmpeg's stderr tail is
    rendered here in full rather than collapsed into "the render failed": that
    tail is usually the only line that says what to change.
    """
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "render.html",
        {
            **_page_context(request, project),
            "output": output_view(store, project),
            "preview": preview_view(store, project),
            "seconds": timeline_seconds(project),
            "writing": writing(
                request.app.state.jobs.state_for(project_id), OUTPUT_KINDS
            ),
        },
    )
