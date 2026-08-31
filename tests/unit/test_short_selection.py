"""Making the Short actually short: `in_short` defaults from the template's beats.

The bug this file pins down was a *default value*. `Scene.in_short` defaulted to
`True`, so every scene joined the vertical cut and the first rendered Short came
out at the same 1:55 as the wide video — "the whole thing, cropped", which is
exactly the low-effort repurposing the platforms suppress. Engagement peaks at
30-45 s on Shorts, 21-34 s on TikTok and under 30 s on Reels; three minutes is
only where the *upload* stops being accepted.

So the pipeline now carries two numbers and they are not interchangeable:

* `MAX_SHORT_S` (180 s) is the **platform limit**. It refuses: `render` will not
  produce a vertical cut longer than it.
* `SHORT_TARGET_S` (45 s) is the **editorial target**. It only advises. Nothing
  anywhere blocks on it, and a test below mutation-proves that.

The selection itself is deterministic and free: templates already declare
`structure`, an editorial map, and `short_beats` names the subset of those beats
a Short is built from. `assign_beats` maps the scenes the model actually returned
onto that map positionally, which is a **guess** — so the last word belongs to
the human. `short_pinned` records that a person touched the tick, and nothing in
the pipeline may then overwrite it. That is the property most worth protecting
here: a silent re-tick would only be discovered after publishing.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from videomaker.models import Project, Scene, SceneVisual
from videomaker.pipeline.assemble import (
    MAX_SHORT_S,
    SHORT_TARGET_S,
    short_duration_s,
    short_meets_target,
)
from videomaker.pipeline.script import apply_short_defaults, assign_beats
from videomaker.templates import Template, load_template

#: The same real pre-change project used by the `preview_url` compatibility test:
#: a file written before `beat` or `short_pinned` existed.
_OLD_PROJECT = Path(__file__).parent.parent / "fixtures" / "project_pre_preview_url.json"

FIVE = ["hook", "context", "mechanism", "implication", "close"]


def _template(**overrides) -> Template:
    fields = {
        "name": "t",
        "display_name": "T",
        "system_prompt": "write well",
        "structure": list(FIVE),
        "scene_count": (1, 20),
        "visual_kind_order": ["stock_video"],
    }
    fields.update(overrides)
    return Template(**fields)


def _scene(sid: str, beat: str | None, *, duration: float = 10.0, **overrides) -> Scene:
    return Scene(
        id=sid,
        narration=f"scene {sid}",
        visual=SceneVisual(query="q"),
        duration_s=duration,
        beat=beat,
        **overrides,
    )


# ------------------------------------------------------------------ assign_beats


def test_every_scene_gets_exactly_one_beat():
    for count in range(1, 13):
        assert len(assign_beats(FIVE, count)) == count


def test_beats_land_in_the_template_order():
    beats = assign_beats(FIVE, 10)
    assert beats == sorted(beats, key=FIVE.index)


def test_the_first_scene_opens_and_the_last_scene_closes():
    """The failure mode a bare `floor(i*b/n)` has: with fewer scenes than beats it
    runs off the end of the list before reaching the closing beat, so the scene the
    model wrote as the close gets labelled `implication` and drops out of the Short."""
    for count in range(2, 13):
        beats = assign_beats(FIVE, count)
        assert beats[0] == "hook"
        assert beats[-1] == "close"


def test_a_single_scene_is_the_opening_beat():
    assert assign_beats(FIVE, 1) == ["hook"]


def test_a_one_beat_template_gives_every_scene_that_beat():
    assert assign_beats(["body"], 4) == ["body"] * 4


def test_the_middle_scenes_spread_over_the_middle_beats():
    assert assign_beats(FIVE, 10) == [
        "hook",
        "context", "context", "context",
        "mechanism", "mechanism", "mechanism",
        "implication", "implication",
        "close",
    ]


def test_fewer_scenes_than_beats_still_keeps_the_heart():
    assert assign_beats(FIVE, 4) == ["hook", "context", "mechanism", "close"]


def test_a_two_beat_template_splits_the_middle_between_them():
    assert assign_beats(["open", "close"], 5) == ["open", "open", "open", "close", "close"]


def test_no_scenes_no_beats():
    assert assign_beats(FIVE, 0) == []


# ------------------------------------------------------ short_beats on the template


def test_a_template_that_says_nothing_has_no_opinion():
    """The backward-compatibility hinge: no `short_beats` means today's behaviour."""
    assert _template().short_beats == []


def test_tech_explainer_ships_a_short_selection():
    template = load_template("tech_explainer")
    assert template.short_beats == ["hook", "mechanism", "close"]
    assert set(template.short_beats) < set(template.structure), "a strict subset, or why bother"


def test_a_short_beat_outside_the_structure_is_rejected():
    """A typo here would silently produce an empty Short, which `render` then refuses
    with a message about unticking scenes the user never ticked."""
    with pytest.raises(ValueError, match="short_beats"):
        _template(short_beats=["hook", "mechansim"])


# ------------------------------------------------------------ apply_short_defaults


def test_the_default_follows_the_beat():
    scenes = [_scene(f"s{i:02d}", beat) for i, beat in enumerate(assign_beats(FIVE, 5), start=1)]
    apply_short_defaults(scenes, _template(short_beats=["hook", "mechanism", "close"]))
    assert [s.in_short for s in scenes] == [True, False, True, False, True]


def test_a_template_with_no_short_beats_changes_nothing():
    scenes = [_scene(f"s{i:02d}", beat) for i, beat in enumerate(assign_beats(FIVE, 5), start=1)]
    apply_short_defaults(scenes, _template())
    assert all(s.in_short for s in scenes), "today's behaviour, unchanged"


def test_a_scene_with_no_recorded_beat_is_left_alone():
    """Projects written before this change carry no beat, and must not be re-selected
    behind the owner's back the first time they are opened."""
    scenes = [_scene("s01", None), _scene("s02", None, in_short=False)]
    apply_short_defaults(scenes, _template(short_beats=["hook"]))
    assert [s.in_short for s in scenes] == [True, False]


def test_a_hand_toggled_scene_is_never_overwritten():
    """THE rule. The template's guess loses to the human's choice, every time."""
    scenes = [
        _scene("s01", "hook", in_short=False, short_pinned=True),  # unticked by hand
        _scene("s02", "context", in_short=True, short_pinned=True),  # ticked by hand
        _scene("s03", "context"),  # untouched
    ]
    apply_short_defaults(scenes, _template(short_beats=["hook", "mechanism", "close"]))
    assert [s.in_short for s in scenes] == [False, True, False]


def test_carried_choices_survive_a_rewritten_script():
    """A stale re-run rebuilds every scene from scratch. The ticks a person set are
    re-applied by scene id and stay pinned, so the second re-run cannot undo them
    either."""
    scenes = [_scene(f"s{i:02d}", beat) for i, beat in enumerate(assign_beats(FIVE, 5), start=1)]
    template = _template(short_beats=["hook", "mechanism", "close"])

    apply_short_defaults(scenes, template, pinned={"s02": True, "s03": False})

    assert [s.in_short for s in scenes] == [True, True, False, False, True]
    assert [s.short_pinned for s in scenes] == [False, True, True, False, False]

    apply_short_defaults(scenes, template)
    assert [s.in_short for s in scenes] == [True, True, False, False, True]


# --------------------------------------------------------- the target and the limit


def test_the_target_advises_and_the_limit_refuses():
    assert SHORT_TARGET_S < MAX_SHORT_S
    assert SHORT_TARGET_S == 45.0
    assert MAX_SHORT_S == 180.0


def test_short_meets_target_is_a_pure_reading_of_the_duration():
    project = Project(
        id="p", topic="t", template="tech_explainer",
        created_at=datetime.now(UTC),
        scenes=[_scene("s01", "hook", duration=SHORT_TARGET_S - 5.0)],
    )
    assert short_meets_target(project) is True
    project.scenes[0].duration_s = SHORT_TARGET_S + 5.0
    assert short_meets_target(project) is False
    # And it is still comfortably legal: the target is not a gate.
    assert short_duration_s(project) < MAX_SHORT_S


# ------------------------------------------------------------- backward compatibility


#: Every status fingerprint the pre-change code derived for `_OLD_PROJECT`, and the
#: hash of `tech_explainer`'s own template fingerprint, **captured by running the
#: pre-change tree** (`git stash` of this task's src/ and templates/) and pasted
#: here as literals. Recomputing them with the new code would make the test agree
#: with whatever it produces, which is the failure mode it exists to prevent.
#:
#: The trap this closes is not `Scene.beat` — no fingerprint dumps a whole `Scene`,
#: they all list their fields. It is `Template`: `runner._template_fingerprint` used
#: to hash `model_dump_json()`, the *entire* model, so merely adding `short_beats`
#: moved every template's fingerprint, staled `script:all` for every project on
#: disk, and re-derived ten finished projects as `new` — after which `run_script`
#: would have replaced their scenes, and with them every voiced take, chosen shot
#: and approval. `Template.script_fingerprint` excludes the field instead.
_M2_FINGERPRINTS = {
    "template:tech_explainer": "3eadbd2b5493dd88",
    "script:all": "892d619463b359a3",
    "voice:s01": "abdcbcc313968045",
    "voice:s02": "2ee726fa1051389c",
    "align:s01": "ca8cf7284cac96d8",
    "align:s02": "934be027d469c80b",
    "visuals:s01": "d29281e805200b82",
    "visuals:s02": "5d2be641a30f0ba0",
    "captions:wide": "d092b5ac94e42942",
    "captions:vertical": "0f696f027fcb9d6d",
    "assemble:wide": "b7eb6e810b79d309",
    "assemble:vertical": "ead326a35448f567",
    "render:wide": "6a755c7737643c4a",
    "render:vertical": "ffee641ac6a2aa77",
}


def _fingerprints(project: Project) -> dict[str, str]:
    from videomaker.cache import hash_inputs
    from videomaker.runner import STAGE_UNITS, _template_fingerprint

    found = {"template:tech_explainer": hash_inputs(t=_template_fingerprint("tech_explainer"))}
    for stage, units_for in STAGE_UNITS.items():
        for unit in units_for(project):
            found[f"{stage}:{unit.unit}"] = unit.fingerprint
    return found


def test_no_unit_hash_moved_for_a_project_written_before_this_task():
    """The upgrade guarantee, in the shape M3 Task 1 established.

    A person with ten finished projects must open this version and see the same ten
    statuses. `derive_status` returns at the first unit whose fingerprint moved, so a
    single changed hash anywhere in this table reads as "re-run everything from here"
    — and for `script:all` that means re-writing the narration those projects were
    built on.

    Checked key by key rather than as one dict, because a later milestone may add a
    *new* unit — M3 Task 15's `thumbnail:all` is the first — and that is not the same
    event as an old one moving. The second assertion is what keeps the first honest:
    nothing pinned may vanish, and every addition has to be named here deliberately.
    """
    project = Project.model_validate_json(_OLD_PROJECT.read_text())

    found = _fingerprints(project)

    assert set(_M2_FINGERPRINTS) <= set(found), "a pinned unit disappeared"
    assert {key: found[key] for key in _M2_FINGERPRINTS} == _M2_FINGERPRINTS
    assert set(found) - set(_M2_FINGERPRINTS) == {"thumbnail:all"}


def test_short_beats_is_not_a_script_input():
    """Retuning which beats make the Short must not rewrite the narration.

    Two claims in one: `short_beats` is excluded from the fingerprint, and the rest
    of the template is not — a `script_fingerprint` that ignored everything would
    satisfy the first half and break the second.
    """
    base = _template(short_beats=["hook"])

    assert base.script_fingerprint() == _template().script_fingerprint()
    assert base.script_fingerprint() != _template(system_prompt="write badly").script_fingerprint()
    assert base.script_fingerprint() != _template(words_per_minute=200).script_fingerprint()


def test_a_project_json_written_before_beats_still_loads_unchanged():
    blob = _OLD_PROJECT.read_text()
    assert '"beat"' not in blob and "short_pinned" not in blob, "fixture must be pre-change"

    project = Project.model_validate_json(blob)

    assert project.scenes, "the fixture must actually carry scenes"
    assert all(scene.beat is None for scene in project.scenes)
    assert all(scene.short_pinned is False for scene in project.scenes)
    assert all(scene.in_short is True for scene in project.scenes), "no silent re-selection"


# ------------------------------------------------ the whole point, measured end to end


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Mock providers only: these tests must never reach a network."""
    from videomaker import runner as runner_module
    from videomaker.config import Settings
    from videomaker.runner import PROVIDER_KINDS

    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
    )


def _voiced(settings, *, minutes: float = 2.0):
    """A real project, mock providers, run far enough for every scene to be timed."""
    from videomaker.project import ProjectStore
    from videomaker.runner import build_deps, run_pipeline

    store = ProjectStore(settings.workspace_dir)
    project = store.create("how ssds work", "tech_explainer", target_minutes=minutes)
    run_pipeline(project, build_deps(settings, project.id), until="visuals", yes=True)
    return store.load(project.id)


def test_a_fresh_project_makes_the_short_a_selection_not_a_copy(settings):
    """The owner's bug, in one assertion: the Short used to equal the wide cut."""
    from videomaker.models import Aspect
    from videomaker.pipeline.assemble import timeline_duration_s

    project = _voiced(settings)
    wide = timeline_duration_s(project, Aspect.WIDE)
    short = short_duration_s(project)

    assert 0.0 < short < wide, "the Short must be an edit, not the wide cut re-cropped"
    assert short < wide / 1.5, f"barely a selection: {short:.1f}s of {wide:.1f}s"
    assert short <= MAX_SHORT_S


def test_the_script_stage_records_the_beat_each_scene_was_written_for(settings):
    project = _voiced(settings)
    template = load_template("tech_explainer")

    beats = [scene.beat for scene in project.scenes]
    assert beats == assign_beats(template.structure, len(project.scenes))
    assert [scene.in_short for scene in project.scenes] == [
        beat in template.short_beats for beat in beats
    ]
    assert not any(scene.short_pinned for scene in project.scenes), "nothing pinned by the machine"


def test_a_re_run_never_re_ticks_a_scene_a_person_unticked(settings):
    """Mutation-proof for the rule that matters: a silent re-tick would only be
    discovered after publishing, so it is asserted across a *forced* script rewrite."""
    from videomaker.project import ProjectStore
    from videomaker.runner import build_deps, run_pipeline

    store = ProjectStore(settings.workspace_dir)
    project = _voiced(settings)
    ticked = [scene.id for scene in project.scenes if scene.in_short]
    assert len(ticked) >= 2, "the fixture needs something to untick"

    # What the storyboard toggle does: set the flag and record that a human did it.
    victim = ticked[0]
    scene = project.scene_by_id(victim)
    scene.in_short = False
    scene.short_pinned = True
    store.save(project)

    # Force the script stage stale the only honest way: change one of its inputs.
    project = store.load(project.id)
    project.topic = "how ssds actually work"
    store.save(project)
    run_pipeline(store.load(project.id), build_deps(settings, project.id), until="script", yes=True)

    after = store.load(project.id)
    assert after.scene_by_id(victim).in_short is False, "the human's choice was overwritten"
    assert after.scene_by_id(victim).short_pinned is True
