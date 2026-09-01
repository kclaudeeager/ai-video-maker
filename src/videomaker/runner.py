"""Stage orchestration: derived status, the three review gates, and `run_pipeline`.

Status is **derived, never stored** (spec 4.4): walk the stages in order and return
at the first unit whose inputs moved or whose output is missing, or at the first
unapproved gate. A stored status would drift from the artefacts the moment anyone
edited `project.json` by hand — which is exactly what the human-in-the-loop design
invites them to do.

**Why this module keeps its own fingerprints.** Each stage decides what to re-run
from `cache/stages.json`, whose hashes cover things a status view cannot see: the
leading provider *name* (which needs `Settings`) and the *bytes* of files on disk
(which need the project folder). `derive_status(project, stage_cache)` is given
neither, so it compares a second, deliberately narrower hash — the project-visible
inputs of each unit — recorded under a `status:` prefix in the same cache by
`run_pipeline` after each stage succeeds. The two agree on every edit a human can
make to `project.json`; where they differ (a provider swapped in `config.yaml`, an
artefact deleted from disk) the stage cache still wins at run time, and the status
view is refreshed by the run it triggers. Units skipped because a scene is `locked`
are stamped as current: locking says "keep this take", and status should not nag
about a take the editor deliberately froze.

Gates sit exactly where the spec's state machine puts them: gate 1 (`script`)
before `voice`, gate 2 (`storyboard`) before `captions`, gate 3 (`preview`) before
`render`. `--yes` stamps the approval it passes; without it the run stops.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from videomaker.cache import STAGE_ORDER, ResponseCache, StageCache, hash_inputs, stage_key
from videomaker.config import Settings
from videomaker.models import Aspect, Project, Status
from videomaker.pipeline.align import run_align
from videomaker.pipeline.assemble import (
    ALL_ASPECTS,
    ASSEMBLE_ASPECTS,
    aspect_scenes,
    run_assemble,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps, StageResult
from videomaker.pipeline.captions import CAPTION_ASPECTS, aspect_hash, run_captions
from videomaker.pipeline.render import RENDER_ASPECTS, run_render
from videomaker.pipeline.script import run_script
from videomaker.pipeline.thumbnail import THUMBNAIL_ASPECT, headline, run_thumbnail
from videomaker.pipeline.visuals import run_visuals
from videomaker.pipeline.voice import DEFAULT_SPEED, run_voice
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.templates import load_template

#: Response cache and quota ledger are global, not per project: quota is spent
#: against the account, and an identical prompt must never be paid for twice.
USER_CACHE_DIR = Path.home() / ".cache" / "ai-video-maker"
RESPONSES_DIRNAME = "responses"
QUOTA_FILENAME = "quota.json"
STAGES_RELPATH = "cache/stages.json"

#: Every provider kind `--providers <name>` has to cover. Anything configured in
#: `config.yaml` beyond this list is overridden too — see `provider_override`.
PROVIDER_KINDS: tuple[str, ...] = ("llm", "tts", "stt", "stock", "image")

StageRunner = Callable[[Project, StageDeps], StageResult]
OnStage = Callable[[str, StageResult], None]
#: Output seconds encoded so far, climbing once across the whole render stage.
OnEncode = Callable[[float], None]

#: The one stage that can report on itself from *inside* FFmpeg, and the only one
#: `run_pipeline`'s `on_encode` reaches. Named rather than written inline so the
#: special case is a fact about the render stage rather than a string in a branch.
ENCODE_STAGE = "render"

STAGE_RUNNERS: dict[str, StageRunner] = {
    "script": run_script,
    "voice": run_voice,
    "align": run_align,
    "visuals": run_visuals,
    "captions": run_captions,
    "assemble": run_assemble,
    "render": run_render,
    "thumbnail": run_thumbnail,
}

#: The status a project has reached once that stage is current. `voice` alone does
#: not make a project "voiced": the spec pairs voice with align.
STATUS_AFTER: dict[str, Status] = {
    "script": Status.SCRIPT_READY,
    "voice": Status.SCRIPT_READY,
    "align": Status.VOICED,
    "visuals": Status.STORYBOARD_READY,
    "captions": Status.STORYBOARD_READY,
    "assemble": Status.PREVIEW_READY,
    "render": Status.RENDERED,
    # There is no status above `rendered`, and there should not be: a thumbnail is
    # the last thing a finished video needs, not a new state of the project.
    "thumbnail": Status.RENDERED,
}

#: stage -> the `Approvals` field that must be stamped before it may run.
GATE_BEFORE: dict[str, str] = {
    "voice": "script",
    "captions": "storyboard",
    "render": "preview",
}

#: What the human is being asked to look at, printed when a run stops at a gate.
GATE_REVIEW: dict[str, str] = {
    "script": "the narration of every scene in project.json",
    "storyboard": "the chosen visual of every scene in scenes/sNN/",
    "preview": "the assembled preview in build/ before the final render",
}

#: Prefix for this module's own fingerprints, so they cannot collide with the
#: stage keys the pipeline writes into the same cache file.
STATUS_PREFIX = "status"


class StageFailed(RuntimeError):
    """A stage raised. Carries the stage name so the CLI can say where it broke."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"stage {stage!r} failed: {cause}")
        self.stage = stage
        self.cause = cause


class GateBlocked(RuntimeError):
    """A run reached an unapproved gate. Not an error: work stopped, as designed."""

    def __init__(self, gate: str, status: Status) -> None:
        review = GATE_REVIEW[gate]
        super().__init__(f"blocked at the {gate} gate: review {review}")
        self.gate = gate
        self.status = status
        self.review = review


# ------------------------------------------------------------------ dependencies


def provider_override(settings: Settings, name: str) -> Settings:
    """A copy of `settings` whose every provider kind resolves to `name`.

    `--providers mock` has to be total: one kind left pointing at a real provider
    would reach for credentials, the network, or the `ml` extra.
    """
    kinds = dict.fromkeys((*PROVIDER_KINDS, *settings.provider_chains))
    return settings.model_copy(update={"provider_chains": {kind: [name] for kind in kinds}})


def stage_cache_for(store: ProjectStore, project_id: str) -> StageCache:
    """The project's own stage cache — all a read-only view (`status`, `list`) needs."""
    return StageCache(store.path_for(project_id) / STAGES_RELPATH)


def build_deps(settings: Settings, project_id: str, *, cache_dir: Path | None = None) -> StageDeps:
    """One `StageDeps` for the whole run: one quota budget, one response cache."""
    store = ProjectStore(settings.workspace_dir)
    root = USER_CACHE_DIR if cache_dir is None else Path(cache_dir)
    return StageDeps(
        settings=settings,
        store=store,
        stage_cache=stage_cache_for(store, project_id),
        response_cache=ResponseCache(root / RESPONSES_DIRNAME),
        quota=QuotaTracker(root / QUOTA_FILENAME),
    )


# --------------------------------------------------------------- status fingerprints


@dataclass(frozen=True)
class Unit:
    """One cache unit as the status view sees it: its inputs, and whether it ran.

    `required` is what keeps a *listed* unit from being a *blocking* one. While the
    vertical aspect had a spec, a timeline and a fingerprint but no stage that could
    build it, its units were listed — a caller could see the Short was not made yet —
    without holding `derive_status` back.

    That derivation has now paid out in the other direction: with vertical in all
    three aspect tuples, the vertical units are required, and a project that has only
    ever rendered wide **stops** deriving as `rendered` until it builds its Short.
    That is the honest answer — the project genuinely has no `final_vertical.mp4` —
    and it is deliberately not switchable: a second flag saying "required after all"
    is the thing that would drift from what the pipeline actually produces.
    """

    unit: str
    fingerprint: str
    produced: bool
    required: bool = True


def status_key(stage: str, unit: str = "all") -> str:
    return f"{STATUS_PREFIX}:{stage_key(stage, unit)}"


def _template_fingerprint(name: str) -> str:
    """The template's *script-shaping* content, so editing its prompt shows up as a
    stale script — and adding a field that shapes nothing does not.

    `Template.script_fingerprint` owns that distinction; this must never go back to
    a bare `model_dump_json()`, or the next field added to `Template` restages every
    project on disk.
    """
    try:
        return load_template(name).script_fingerprint()
    except ValueError:
        # A missing or broken template is the run's problem to report, not the
        # status view's: fall back to the name so `status` and `list` still work.
        return name


def _script_units(project: Project) -> list[Unit]:
    fingerprint = hash_inputs(
        topic=project.topic,
        template=_template_fingerprint(project.template),
        target_minutes=project.target_minutes,
        language=project.language,
    )
    return [Unit("all", fingerprint, bool(project.scenes))]


def _voice_units(project: Project) -> list[Unit]:
    return [
        Unit(
            scene.id,
            hash_inputs(narration=scene.narration, voice=project.voice, speed=DEFAULT_SPEED),
            scene.audio_path is not None and scene.duration_s is not None,
        )
        for scene in project.scenes
    ]


def _align_units(project: Project) -> list[Unit]:
    return [
        Unit(
            scene.id,
            hash_inputs(
                narration=scene.narration,
                audio=scene.audio_path,
                duration_s=scene.duration_s,
            ),
            bool(scene.words),
        )
        for scene in project.scenes
    ]


def _visuals_units(project: Project) -> list[Unit]:
    units = []
    for scene in project.scenes:
        chosen = scene.visual.chosen
        units.append(
            Unit(
                scene.id,
                hash_inputs(
                    query=scene.visual.query,
                    # Written only when it holds something — the same compatibility
                    # guarantee, for the same reason, as `visuals.scene_hash`.
                    **(
                        {"alt_queries": scene.visual.alt_queries}
                        if scene.visual.alt_queries
                        else {}
                    ),
                    kind=scene.visual.kind.value,
                    duration_s=scene.duration_s,
                ),
                chosen is not None and bool(chosen.local_path),
            )
        )
    return units


def _captions_units(project: Project) -> list[Unit]:
    # The caption files are written from the words, so the stage's own aspect hash
    # is already the project-visible fingerprint — no second definition to drift.
    units = []
    for aspect in ALL_ASPECTS:
        scenes = aspect_scenes(project, aspect)
        units.append(
            Unit(
                aspect.value,
                aspect_hash(project, aspect),
                aspect in CAPTION_ASPECTS and bool(scenes) and all(s.words for s in scenes),
                required=aspect in CAPTION_ASPECTS,
            )
        )
    return units


def _assemble_fingerprint(project: Project, aspect: Aspect) -> str:
    return hash_inputs(
        aspect=aspect.value,
        gap_s=SCENE_GAP_S,
        scenes=[
            {
                "id": scene.id,
                "duration_s": scene.duration_s,
                "asset": scene.visual.chosen.local_path if scene.visual.chosen else None,
                "motion": scene.visual.motion.value,
                "crop_focus_x": scene.visual.crop_focus_x,
                "trim_start_s": scene.visual.trim_start_s,
            }
            for scene in aspect_scenes(project, aspect)
        ],
    )


def _assemble_units(project: Project) -> list[Unit]:
    units = []
    for aspect in ALL_ASPECTS:
        spec = project.outputs.get(aspect)
        units.append(
            Unit(
                aspect.value,
                _assemble_fingerprint(project, aspect),
                spec is not None and bool(spec.scene_ids),
                required=aspect in ASSEMBLE_ASPECTS,
            )
        )
    return units


def _render_units(project: Project) -> list[Unit]:
    units = []
    for aspect in ALL_ASPECTS:
        spec = project.outputs.get(aspect)
        units.append(
            Unit(
                aspect.value,
                hash_inputs(
                    assembled=_assemble_fingerprint(project, aspect),
                    captions=aspect_hash(project, aspect),
                ),
                spec is not None and bool(spec.video_path),
                required=aspect in RENDER_ASPECTS,
            )
        )
    return units


def _thumbnail_units(project: Project) -> list[Unit]:
    """One unit, and it is **required**.

    Required because a thumbnail is a deliverable, and because it is free to say so:
    `thumbnail` is last in `STAGE_ORDER`, so a rendered project with no thumbnail
    still derives as `rendered` (`derive_status` returns the status of the last
    *current* stage) and merely reports one stage pending. `required=False` was the
    alternative and is strictly worse — `stage_is_current` returns `False` for a
    stage with no required units, so the stage would be permanently stale and the
    guidance panel could never say "finished" again for any project.

    The fingerprint is the wide **assembly** plus the headline, because that is what
    the picture is: a frame of that assembly with those words on it. Captions are
    deliberately absent — the frame is cut from `build/video_wide.mp4`, which has
    none burned in — and so is everything vertical, so re-cutting the Short does not
    redraw the thumbnail.
    """
    return [
        Unit(
            "all",
            hash_inputs(
                assembled=_assemble_fingerprint(project, THUMBNAIL_ASPECT),
                headline=headline(project),
            ),
            project.thumbnail_path is not None,
        )
    ]


STAGE_UNITS: dict[str, Callable[[Project], list[Unit]]] = {
    "script": _script_units,
    "voice": _voice_units,
    "align": _align_units,
    "visuals": _visuals_units,
    "captions": _captions_units,
    "assemble": _assemble_units,
    "render": _render_units,
    "thumbnail": _thumbnail_units,
}


def stage_is_current(project: Project, stage_cache: StageCache, stage: str) -> bool:
    """True when every *required* unit of `stage` has run and no input has moved.

    Units the stage cannot produce yet (see `Unit.required`) are listed but ignored
    here: a status view that waited on them would call every finished project stale.
    """
    units = [unit for unit in STAGE_UNITS[stage](project) if unit.required]
    if not units:
        return False
    return all(
        unit.produced and not stage_cache.is_stale(status_key(stage, unit.unit), unit.fingerprint)
        for unit in units
    )


def stamp_stage(project: Project, stage_cache: StageCache, stage: str) -> None:
    """Record the fingerprints of everything `stage` just produced."""
    for unit in STAGE_UNITS[stage](project):
        if unit.produced:
            stage_cache.mark(status_key(stage, unit.unit), unit.fingerprint)


# ---------------------------------------------------------------- derived status


def derive_status(project: Project, stage_cache: StageCache) -> Status:
    """Walk the stages; stop at the first stale unit or unapproved gate."""
    status = Status.NEW
    for stage in STAGE_ORDER:
        gate = GATE_BEFORE.get(stage)
        if gate is not None and getattr(project.approvals, gate) is None:
            return status
        if not stage_is_current(project, stage_cache, stage):
            return status
        status = STATUS_AFTER[stage]
    return status


def clear_stale_approvals(project: Project, stage_cache: StageCache) -> bool:
    """Drop any approval whose upstream stages are no longer current.

    Without this, approving the storyboard and then rewriting a narration would
    let the rewritten content sail through gate 2 unreviewed — the one thing the
    gate exists to prevent. Returns whether anything was cleared.
    """
    cleared = False
    for stage, gate in GATE_BEFORE.items():
        if getattr(project.approvals, gate) is None:
            continue
        upstream = STAGE_ORDER[: STAGE_ORDER.index(stage)]
        if not all(stage_is_current(project, stage_cache, earlier) for earlier in upstream):
            setattr(project.approvals, gate, None)
            cleared = True
    return cleared


# -------------------------------------------------------------------- the runner


def stages_through(until: str | None) -> tuple[str, ...]:
    """`STAGE_ORDER` up to and including `until` (all of it when `until` is None)."""
    if until is None:
        return STAGE_ORDER
    if until not in STAGE_ORDER:
        known = ", ".join(STAGE_ORDER)
        raise ValueError(f"unknown stage {until!r}; known stages: {known}")
    return STAGE_ORDER[: STAGE_ORDER.index(until) + 1]


def _pass_gate(project: Project, deps: StageDeps, gate: str, *, yes: bool) -> bool:
    if getattr(project.approvals, gate) is not None:
        return True
    if not yes:
        return False
    setattr(project.approvals, gate, datetime.now(UTC))
    deps.store.save(project)
    return True


def _run_stage(
    stage: str, project: Project, deps: StageDeps, on_encode: OnEncode | None
) -> StageResult:
    """Run one stage, giving the encoding one its progress hook if there is one.

    A branch rather than a uniform signature on every runner: seven of the eight
    stages have nothing to report from inside themselves, and widening all of them
    to carry a parameter only `render` can honour would make the odd one out harder
    to find, not easier.
    """
    if stage == ENCODE_STAGE and on_encode is not None:
        return run_render(project, deps, on_progress=on_encode)
    return STAGE_RUNNERS[stage](project, deps)


def run_pipeline(
    project: Project,
    deps: StageDeps,
    *,
    until: str | None = None,
    yes: bool = False,
    on_stage: OnStage | None = None,
    on_encode: OnEncode | None = None,
) -> Project:
    """Run the stages in order, stopping at `until`, at a gate, or at a failure.

    Raises `GateBlocked` when an unapproved gate is reached without `yes`, and
    `StageFailed` when a stage raises. Held under the project lock, so two runs of
    the same project cannot interleave their writes.

    `on_stage` ticks once per stage; `on_encode` is the finer-grained one, reaching
    only `ENCODE_STAGE`, and it exists because that stage is most of the wall clock
    (M1: 70 s of a 184 s run) so a bar driven by `on_stage` alone sits still through
    exactly the part a human watches.
    """
    stages = stages_through(until)
    with deps.store.lock(project.id):
        if clear_stale_approvals(project, deps.stage_cache):
            deps.store.save(project)
        for stage in stages:
            gate = GATE_BEFORE.get(stage)
            if gate is not None and not _pass_gate(project, deps, gate, yes=yes):
                raise GateBlocked(gate, derive_status(project, deps.stage_cache))
            try:
                result = _run_stage(stage, project, deps, on_encode)
            except Exception as exc:
                raise StageFailed(stage, exc) from exc
            stamp_stage(project, deps.stage_cache, stage)
            deps.stage_cache.save()
            if on_stage is not None:
                on_stage(stage, result)
    return project
