"""Transition SFX on cuts, and accents mapped onto the template's beats.

Everything here is about *where* an effect lands and *what reaches FFmpeg*. None of
it proves a listener hears anything — a graph containing `adelay` proves that a
string was written. `tests/integration/test_render_sfx.py` renders real clips and
measures the energy at a known cut with and without the effect; read that one for
the guarantee.

The first section is the one that matters most in practice, and it is first on
purpose. `assets/sfx/` is empty on every fresh clone, including the owner's, so "no
effects" is not an edge case: it is the default path, and it has to be *exactly* the
render Task 9 shipped — same arguments, same graph, same hash, no re-encode.

The placement itself costs nothing to compute. Every cut timestamp is already known
from `scene_timeline`, and `Scene.beat` already records which of the template's
`structure` beats each scene was written for (M3 Task 22). So the whole feature is a
walk over two lists the pipeline already had: no LLM call, no analysis of the
footage, and — deliberately — no literal foley (`docs/audio-design.md` explains why:
Pexels clips arrive effectively mute, so a hit slightly out of sync with what is on
screen reads as worse than silence).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from videomaker.audio import Library, Track, select_sfx
from videomaker.config import Settings, load_settings
from videomaker.media import audio as mix
from videomaker.models import Aspect, Project, Scene, SceneVisual
from videomaker.pipeline import render as render_module
from videomaker.pipeline.assemble import AUDIO_RATE
from videomaker.pipeline.render import (
    AUDIO_CHANNELS,
    LOUDNORM,
    _render_args,
    render_hash,
    sfx_plan,
    sfx_profile_for,
)
from videomaker.templates import SFX_PROFILE_NAMES, Template

#: The graph Task 9 shipped for a bed with no effects under it, written out rather
#: than composed, so an accidental change to the music path fails here.
TASK9_GRAPH = (
    "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
    "asplit=2[sc][voice];"
    "[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
    "atrim=end=10.000,asetpts=N/SR/TB,afade=t=in:st=0:d=1.000,"
    "afade=t=out:st=8.000:d=2.000,volume=-18.00dB[bed];"
    "[bed][sc]sidechaincompress=threshold=0.005623:ratio=2.000:attack=20:release=300"
    ":makeup=1:level_sc=1[ducked];"
    "[voice][ducked]amix=inputs=2:normalize=0:duration=first[mixed];"
    "[mixed]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
)

#: The `render:wide` hash M1 computed for the fixture bytes in `test_audio_mix.py`.
#: Repeated here because this task is the second chance to move it by accident.
M1_WIDE_HASH = "fc340720a6e8bfed"

FIVE = ("hook", "context", "mechanism", "implication", "close")


# ------------------------------------------------------------------- fixtures


def _sfx(role: str, name: str, *, probe_error: str = "") -> Track:
    return Track(
        key=f"sfx/{role}/{name}",
        kind="sfx",
        group=role,
        path=Path("/library/sfx") / role / name,
        probe_error=probe_error,
    )


def _library(*specs: tuple[str, str]) -> Library:
    return Library(tracks=tuple(_sfx(role, name) for role, name in specs))


FULL_LIBRARY = _library(
    ("transition", "whoosh.wav"), ("accent", "ding.wav"), ("riser", "rise.wav")
)


def _pick(library: Library, seed: str = "p1"):
    return lambda role: select_sfx(library, role, seed=seed)


def _cues(
    cuts, *, beats=None, library=FULL_LIBRARY, profile="subtle", transitions=True
) -> tuple[mix.SfxCue, ...]:
    return mix.plan_sfx(
        cuts,
        beats=beats or {},
        pick=_pick(library),
        profile=mix.SFX_PROFILES[profile],
        transitions=transitions,
    ).cues


def _at(cues, role: str) -> list[float]:
    return [round(cue.at_s, 4) for cue in cues if cue.role == role]


def _scene(sid: str, beat: str | None, *, duration: float = 4.0, **overrides) -> Scene:
    return Scene(
        id=sid,
        narration=f"scene {sid}",
        visual=SceneVisual(query="q"),
        duration_s=duration,
        beat=beat,
        **overrides,
    )


def _project(*scenes: Scene) -> Project:
    return Project(
        id="p1",
        topic="t",
        template="tech_explainer",
        created_at=datetime.now(UTC),
        scenes=list(scenes),
    )


def _deps(tmp_path, library=FULL_LIBRARY, **settings_kwargs):
    """Just enough of `StageDeps` for `sfx_plan`, with the library handed in."""
    from videomaker.cache import ResponseCache, StageCache
    from videomaker.pipeline.base import StageDeps
    from videomaker.project import ProjectStore
    from videomaker.providers.ratelimit import QuotaTracker

    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        music_dir=tmp_path / "music",
        sfx_dir=tmp_path / "sfx",
        **settings_kwargs,
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _bed(tmp_path, name="calm.wav"):
    path = tmp_path / name
    if not path.exists():
        path.write_bytes(b"a track")
    return mix.MusicBed(path=path, key=f"music/calm/{name}")


# ----------------------------------------------- an empty library changes nothing


def test_an_empty_library_places_nothing(tmp_path):
    """Every fresh clone, the owner's included. This is the default path."""
    project = _project(_scene("s01", "hook"), _scene("s02", "close"))

    plan = sfx_plan(project, _deps(tmp_path, library=Library()), Aspect.WIDE, library=Library())

    assert plan.cues == ()
    assert not plan
    assert plan.input_args() == []


def test_with_no_effects_the_graph_is_the_one_task_9_shipped(tmp_path):
    """Byte-identical, not merely equivalent: a changed graph re-encodes everything."""
    graph = mix.mix_filter_complex(
        _bed(tmp_path),
        duration_s=10.0,
        rate=AUDIO_RATE,
        channels=AUDIO_CHANNELS,
        target=LOUDNORM,
        speech=1,
        music=2,
        sfx=mix.SfxPlan(),
    )

    assert graph == TASK9_GRAPH


def test_with_no_music_and_no_effects_nothing_about_a_mix_reaches_ffmpeg(tmp_path):
    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=False, mix=None)

    assert "-filter_complex" not in args
    assert "adelay" not in " ".join(args)
    assert args.count("-af") == 1


def test_with_no_effects_the_render_hash_is_the_one_m1_computed(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"video")
    (tmp_path / "n.wav").write_bytes(b"narration")
    (tmp_path / "c.ass").write_bytes(b"captions")

    current = render_hash(
        tmp_path,
        Aspect.WIDE,
        video=tmp_path / "v.mp4",
        narration=tmp_path / "n.wav",
        captions=tmp_path / "c.ass",
        music=None,
        sfx=mix.SfxPlan(),
    )

    assert current == M1_WIDE_HASH


def test_with_nothing_to_mix_the_render_plans_no_mix_at_all(tmp_path, monkeypatch):
    """No bed and no effects means no `-filter_complex` and one loudness pass.

    Pinned at `_plan_mix` because that is the only place the decision is made, and
    the cost of getting it wrong is invisible: a mix of one input renders a perfectly
    good video, two-pass, off M1's argument list, re-encoding every finished file in
    the workspace. The refused probe is part of the claim — a fresh clone must not
    even ffprobe its own narration to discover it has nothing to mix.
    """

    def refuse(path):
        raise AssertionError(f"a render with nothing to mix probed {path}")

    monkeypatch.setattr(render_module, "probe_duration", refuse)

    assert render_module._plan_mix(None, tmp_path, Aspect.WIDE, sfx=mix.SfxPlan()) is None
    assert render_module._plan_mix(None, tmp_path, Aspect.WIDE) is None


def test_effects_alone_are_enough_to_plan_a_mix(tmp_path, monkeypatch):
    """The other half: a library with whooshes and no music still mixes."""
    monkeypatch.setattr(render_module, "probe_duration", lambda path: 10.0)
    monkeypatch.setattr(render_module, "measure_loudness", lambda args, cwd: None)

    plan = render_module._plan_mix(
        None, tmp_path, Aspect.WIDE, sfx=_file_plan(tmp_path)
    )

    assert plan is not None
    assert plan.bed is None
    assert plan.duration_s == 10.0


def test_a_role_with_nothing_in_it_simply_places_nothing():
    """A library of whooshes and no risers still gets its transitions."""
    library = _library(("transition", "whoosh.wav"))
    beats = {"s01": "hook", "s02": "mechanism"}

    cues = _cues([("s01", 0.0), ("s02", 5.0)], beats=beats, library=library)

    assert _at(cues, "riser") == []
    assert _at(cues, "accent") == []
    assert _at(cues, "transition") == [round(5.0 - mix.TRANSITION_LEAD_S, 4)]


def test_an_effect_ffprobe_could_not_read_is_never_placed():
    library = Library(
        tracks=(
            _sfx("transition", "broken.wav", probe_error="Invalid data found"),
            _sfx("transition", "whoosh.wav"),
        )
    )

    chosen = select_sfx(library, "transition", seed="p1")

    assert chosen is not None
    assert chosen.key == "sfx/transition/whoosh.wav"


# ------------------------------------------------------- transitions on the cuts


def test_every_cut_but_the_first_gets_a_transition():
    """The first scene's start is not a cut; there is nothing to cut *from*."""
    cuts = [("s01", 0.0), ("s02", 5.0), ("s03", 11.0), ("s04", 14.0)]

    assert _at(_cues(cuts), "transition") == [
        round(5.0 - mix.TRANSITION_LEAD_S, 4),
        round(11.0 - mix.TRANSITION_LEAD_S, 4),
        round(14.0 - mix.TRANSITION_LEAD_S, 4),
    ]


def test_a_single_scene_has_no_cuts_at_all():
    assert _at(_cues([("s01", 0.0)]), "transition") == []


def test_the_whoosh_leads_into_the_cut_rather_than_following_it():
    """A transition that starts *on* the cut arrives late; the swell precedes it."""
    cues = _cues([("s01", 0.0), ("s02", 5.0)])

    assert 0 < mix.TRANSITION_LEAD_S < 0.5
    assert _at(cues, "transition") == [round(5.0 - mix.TRANSITION_LEAD_S, 4)]


def test_a_lead_in_is_never_placed_before_the_video_starts():
    cues = _cues([("s01", 0.0), ("s02", 0.05)])

    assert _at(cues, "transition") == [0.0]


def test_one_file_is_reused_on_every_cut():
    """The point of the layer: the same whoosh, so the cuts read as deliberate."""
    library = _library(*(("transition", f"{n}.wav") for n in "abcdefgh"))
    cuts = [("s01", 0.0), ("s02", 5.0), ("s03", 11.0), ("s04", 14.0)]

    cues = _cues(cuts, library=library)

    assert len({cue.key for cue in cues}) == 1
    assert len(cues) == 3


def test_different_projects_do_not_all_get_the_same_whoosh():
    library = _library(*(("transition", f"{n}.wav") for n in "abcdefgh"))

    picks = {select_sfx(library, "transition", seed=f"p{i}").key for i in range(20)}

    assert len(picks) > 1


def test_the_same_project_always_gets_the_same_whoosh():
    library = _library(*(("transition", f"{n}.wav") for n in "abcdefgh"))

    picks = {select_sfx(library, "transition", seed="p7").key for _ in range(5)}

    assert len(picks) == 1


def test_transitions_can_be_turned_off_on_their_own():
    """`transition_sfx_enabled: false` silences the cuts and leaves the beats alone."""
    cuts = [("s01", 0.0), ("s02", 5.0)]
    beats = {"s01": "hook", "s02": "close"}

    cues = _cues(cuts, beats=beats, transitions=False)

    assert _at(cues, "transition") == []
    assert _at(cues, "accent") == [0.0, 5.0]


# ------------------------------------------------------------ beat-mapped accents


def test_the_hook_is_accented():
    cues = _cues([("s01", 0.0), ("s02", 5.0)], beats={"s01": "hook", "s02": "context"})

    assert _at(cues, "accent") == [0.0]


def test_the_close_resolves_on_an_accent():
    cues = _cues([("s01", 0.0), ("s02", 5.0)], beats={"s01": "hook", "s02": "close"})

    assert _at(cues, "accent") == [0.0, 5.0]


def test_the_mechanism_is_risen_into():
    """The riser leads the beat it introduces; it does not start on top of it."""
    cues = _cues([("s01", 0.0), ("s02", 5.0)], beats={"s01": "hook", "s02": "mechanism"})

    assert mix.RISER_LEAD_S > mix.TRANSITION_LEAD_S
    assert _at(cues, "riser") == [round(5.0 - mix.RISER_LEAD_S, 4)]


def test_the_beats_between_are_left_alone():
    """`context` and `implication` carry the argument; nothing is stung over them."""
    cuts = [("s01", 0.0), ("s02", 5.0), ("s03", 9.0)]
    beats = {"s01": "context", "s02": "implication", "s03": "context"}

    cues = _cues(cuts, beats=beats)

    assert _at(cues, "accent") == []
    assert _at(cues, "riser") == []
    assert len(_at(cues, "transition")) == 2


def test_a_project_written_before_beats_existed_gets_no_accents():
    """`Scene.beat` is `None` on every project written before M3 Task 22."""
    cuts = [("s01", 0.0), ("s02", 5.0)]

    cues = _cues(cuts, beats={"s01": None, "s02": None})

    assert _at(cues, "accent") == []
    assert _at(cues, "riser") == []
    assert len(cues) == 1  # the cut still gets its whoosh


def test_the_beat_map_is_the_templates_editorial_map_and_nothing_else():
    """No LLM call and no inference from footage: three named beats, by name."""
    assert set(mix.BEAT_ROLES) <= set(FIVE)
    assert mix.BEAT_ROLES == {"hook": "accent", "mechanism": "riser", "close": "accent"}


# ------------------------------------------------------------------- the profiles


def test_the_default_profile_is_subtle():
    assert Template(
        name="t",
        display_name="T",
        system_prompt="write well",
        structure=list(FIVE),
        scene_count=(1, 20),
        visual_kind_order=["stock_video"],
    ).sfx_profile == "subtle"


def test_a_template_may_not_invent_a_profile():
    with pytest.raises(ValueError, match="sfx_profile"):
        Template(
            name="t",
            display_name="T",
            system_prompt="write well",
            structure=list(FIVE),
            scene_count=(1, 20),
            visual_kind_order=["stock_video"],
            sfx_profile="deafening",
        )


def test_every_named_profile_exists_in_the_mixer():
    assert set(SFX_PROFILE_NAMES) == set(mix.SFX_PROFILES)
    assert set(SFX_PROFILE_NAMES) == {"subtle", "punchy", "none"}


def test_the_none_profile_places_nothing():
    cuts = [("s01", 0.0), ("s02", 5.0)]

    cues = _cues(cuts, beats={"s01": "hook", "s02": "close"}, profile="none")

    assert cues == ()


def test_punchy_is_louder_than_subtle_everywhere():
    subtle, punchy = mix.SFX_PROFILES["subtle"], mix.SFX_PROFILES["punchy"]

    for role in ("transition", "accent", "riser"):
        assert punchy.gain_db(role) > subtle.gain_db(role)
        assert subtle.gain_db(role) < 0  # an effect never sits over the narration


def test_the_profiles_level_reaches_the_cue():
    cues = _cues([("s01", 0.0), ("s02", 5.0)], profile="punchy")

    assert cues[0].gain_db == mix.SFX_PROFILES["punchy"].gain_db("transition")


def test_choosing_the_profile_does_not_rewrite_the_narration():
    """`sfx_profile` changes nothing the model writes, so it is not a script input."""

    def template(**overrides):
        return Template(
            name="t",
            display_name="T",
            system_prompt="write well",
            structure=list(FIVE),
            scene_count=(1, 20),
            visual_kind_order=["stock_video"],
            **overrides,
        )

    assert template(sfx_profile="punchy").script_fingerprint() == template().script_fingerprint()
    assert template(sfx_profile="none").script_fingerprint() == template().script_fingerprint()
    # ...and the exclusion is narrow: the rest of the template still counts.
    assert template(words_per_minute=200).script_fingerprint() != template().script_fingerprint()


# ------------------------------------------------------------------ the mix graph


def _graph(tmp_path, plan, *, bed=True, sfx_input=3):
    return mix.mix_filter_complex(
        _bed(tmp_path) if bed else None,
        duration_s=10.0,
        rate=AUDIO_RATE,
        channels=AUDIO_CHANNELS,
        target=LOUDNORM,
        speech=1,
        music=2,
        sfx=plan,
        sfx_input=sfx_input,
    )


def _plan(*cues: tuple[str, str, float, float]) -> mix.SfxPlan:
    return mix.SfxPlan(
        cues=tuple(
            mix.SfxCue(
                path=Path("/library/sfx") / role / name,
                key=f"sfx/{role}/{name}",
                role=role,
                at_s=at_s,
                gain_db=gain_db,
            )
            for role, name, at_s, gain_db in cues
        )
    )


def test_an_effects_path_never_reaches_the_filter_graph(tmp_path):
    """M0's rule again: a path in a filter argument breaks the parser on its colon."""
    plan = mix.SfxPlan(
        cues=(
            mix.SfxCue(
                path=Path("/lib/a: whoosh.wav"),
                key="sfx/transition/a: whoosh.wav",
                role="transition",
                at_s=5.0,
                gain_db=-12.0,
            ),
        )
    )

    graph = _graph(tmp_path, plan)

    assert "a: whoosh.wav" not in graph
    assert plan.input_args() == ["-i", "/lib/a: whoosh.wav"]


def test_one_input_per_file_however_many_times_it_is_used(tmp_path):
    plan = _plan(
        ("transition", "whoosh.wav", 5.0, -12.0),
        ("transition", "whoosh.wav", 11.0, -12.0),
        ("accent", "ding.wav", 0.0, -9.0),
    )

    assert plan.input_args() == [
        "-i", str(Path("/library/sfx/transition/whoosh.wav")),
        "-i", str(Path("/library/sfx/accent/ding.wav")),
    ]
    graph = _graph(tmp_path, plan)
    assert "asplit=2" in graph  # the whoosh, split for its two placements
    assert graph.count("[3:a]") == 1
    assert graph.count("[4:a]") == 1


def test_every_effect_lands_where_the_plan_put_it(tmp_path):
    graph = _graph(tmp_path, _plan(("transition", "whoosh.wav", 5.25, -12.0)))

    assert "adelay=5250:all=1" in graph


def test_an_effect_at_the_very_start_is_not_delayed_at_all(tmp_path):
    graph = _graph(tmp_path, _plan(("accent", "ding.wav", 0.0, -9.0)))

    assert "adelay" not in graph
    assert "volume=-9.00dB" in graph


def test_the_effects_are_resampled_like_everything_else(tmp_path):
    """A 44.1 kHz whoosh against a 48 kHz bed: resample at mix time (M1 follow-up 5)."""
    graph = _graph(tmp_path, _plan(("transition", "whoosh.wav", 5.0, -12.0)))

    assert graph.count(f"aresample={AUDIO_RATE}") == 3  # narration, bed, whoosh


def test_the_effects_join_the_mix_rather_than_replacing_it(tmp_path):
    graph = _graph(
        tmp_path,
        _plan(("transition", "whoosh.wav", 5.0, -12.0), ("accent", "ding.wav", 0.0, -9.0)),
    )

    assert "amix=inputs=4:normalize=0:duration=first[mixed]" in graph
    assert "[bed][sc]sidechaincompress=" in graph


def test_the_effects_are_never_ducked(tmp_path):
    """Only the bed is sidechained; effects are short enough to sit in the mix."""
    graph = _graph(tmp_path, _plan(("transition", "whoosh.wav", 5.0, -12.0)))

    assert graph.count("sidechaincompress") == 1
    sidechained = graph.split("sidechaincompress", 1)[0].rsplit(";", 1)[-1]
    assert sidechained.startswith("[bed][sc]")


def test_effects_play_with_no_music_at_all(tmp_path):
    """An `assets/sfx/` with files and an `assets/music/` without is a real library."""
    graph = _graph(tmp_path, _plan(("transition", "whoosh.wav", 5.0, -12.0)), bed=False, sfx_input=2)

    assert "sidechaincompress" not in graph
    assert "amix=inputs=2:normalize=0:duration=first[mixed]" in graph
    assert "[2:a]" in graph
    assert f"loudnorm={LOUDNORM}" in graph


# ------------------------------------------------------------ the render arguments


def test_the_effects_become_inputs_after_the_music(tmp_path):
    bed = _bed(tmp_path)
    plan = _plan(("transition", "whoosh.wav", 5.0, -12.0))

    args = _render_args(
        tmp_path,
        Aspect.WIDE,
        burn_captions=False,
        mix=mix.MusicMix(bed=bed, duration_s=12.0, sfx=plan),
    )

    assert args[:10] == [
        "-i", "build/video_wide.mp4",
        "-i", "build/narration_wide.wav",
        "-stream_loop", "-1",
        "-i", str(bed.path),
        "-i", str(Path("/library/sfx/transition/whoosh.wav")),
    ]
    graph = args[args.index("-filter_complex") + 1]
    assert "[3:a]" in graph


def test_effects_without_music_take_the_slot_the_music_would_have(tmp_path):
    plan = _plan(("transition", "whoosh.wav", 5.0, -12.0))

    args = _render_args(
        tmp_path,
        Aspect.WIDE,
        burn_captions=False,
        mix=mix.MusicMix(bed=None, duration_s=12.0, sfx=plan),
    )

    assert "-stream_loop" not in args
    assert args[4:6] == ["-i", str(Path("/library/sfx/transition/whoosh.wav"))]
    graph = args[args.index("-filter_complex") + 1]
    assert "[2:a]" in graph


def test_the_measuring_pass_hears_the_effects_too(tmp_path):
    """Pass one measures the finished mix; a mix missing its effects is not it."""
    args = mix.measure_args(
        _bed(tmp_path),
        narration="build/narration_wide.wav",
        duration_s=10.0,
        rate=AUDIO_RATE,
        channels=AUDIO_CHANNELS,
        target=LOUDNORM,
        sfx=_plan(("transition", "whoosh.wav", 5.0, -12.0)),
    )

    assert args[-5:] == ["-map", "[aout]", "-f", "null", "-"]
    assert "-i" in args and str(Path("/library/sfx/transition/whoosh.wav")) in args
    graph = args[args.index("-filter_complex") + 1]
    assert "amix=inputs=3" in graph
    assert "[2:a]" in graph  # narration 0, music 1, whoosh 2


# ------------------------------------------------------------------- the fingerprint


def _hash(tmp_path, plan, *, music=None):
    for name, blob in (("v.mp4", b"video"), ("n.wav", b"narration"), ("c.ass", b"captions")):
        (tmp_path / name).write_bytes(blob)
    return render_hash(
        tmp_path,
        Aspect.WIDE,
        video=tmp_path / "v.mp4",
        narration=tmp_path / "n.wav",
        captions=tmp_path / "c.ass",
        music=music,
        sfx=plan,
    )


def _file_plan(tmp_path, at_s=5.0, gain_db=-12.0, blob=b"a whoosh") -> mix.SfxPlan:
    path = tmp_path / "whoosh.wav"
    path.write_bytes(blob)
    return mix.SfxPlan(
        cues=(
            mix.SfxCue(
                path=path, key="sfx/transition/whoosh.wav", role="transition",
                at_s=at_s, gain_db=gain_db,
            ),
        )
    )


def test_adding_effects_changes_the_render_fingerprint(tmp_path):
    assert _hash(tmp_path, _file_plan(tmp_path)) != M1_WIDE_HASH


def test_moving_an_effect_changes_the_render_fingerprint(tmp_path):
    early = _hash(tmp_path, _file_plan(tmp_path, at_s=5.0))
    late = _hash(tmp_path, _file_plan(tmp_path, at_s=6.0))

    assert early != late


def test_turning_the_effects_up_changes_the_render_fingerprint(tmp_path):
    quiet = _hash(tmp_path, _file_plan(tmp_path, gain_db=-12.0))
    loud = _hash(tmp_path, _file_plan(tmp_path, gain_db=-6.0))

    assert quiet != loud


def test_replacing_an_effects_bytes_changes_the_render_fingerprint(tmp_path):
    """Same name, a different whoosh: hashed by content, like every other input."""
    before = _hash(tmp_path, _file_plan(tmp_path, blob=b"a whoosh"))
    after = _hash(tmp_path, _file_plan(tmp_path, blob=b"a completely different whoosh"))

    assert before != after


# --------------------------------------------------------------- through the stage


def test_the_stage_walks_the_cuts_this_aspect_actually_has(tmp_path):
    """Vertical is the `in_short` subset, so its cuts are its own, not the wide ones."""
    project = _project(
        _scene("s01", "hook"),
        _scene("s02", "context", in_short=False),
        _scene("s03", "close"),
    )
    deps = _deps(tmp_path)

    wide = sfx_plan(project, deps, Aspect.WIDE, library=FULL_LIBRARY)
    vertical = sfx_plan(project, deps, Aspect.VERTICAL, library=FULL_LIBRARY)

    gap = 0.5
    assert _at(wide.cues, "transition") == [
        round(4.5 - mix.TRANSITION_LEAD_S, 4),
        round(9.0 - mix.TRANSITION_LEAD_S, 4),
    ]
    # s02 is out of the Short, so its cut is not in the Short's timeline either.
    assert _at(vertical.cues, "transition") == [round(4.0 + gap - mix.TRANSITION_LEAD_S, 4)]
    assert _at(vertical.cues, "accent") == [0.0, round(4.0 + gap, 4)]


def test_the_stage_reads_the_profile_off_the_template(tmp_path):
    project = _project(_scene("s01", "hook"))

    assert sfx_profile_for(project).name == "subtle"


def test_an_unreadable_template_falls_back_rather_than_stopping_the_render(tmp_path):
    project = _project(_scene("s01", "hook"))
    project.template = "no_such_template"

    assert sfx_profile_for(project).name == mix.DEFAULT_SFX_PROFILE


def test_the_whole_layer_can_be_switched_off_globally(tmp_path):
    project = _project(_scene("s01", "hook"), _scene("s02", "close"))
    deps = _deps(tmp_path, sfx_enabled=False)

    assert sfx_plan(project, deps, Aspect.WIDE, library=FULL_LIBRARY).cues == ()


def test_the_cuts_can_be_switched_off_without_losing_the_beats(tmp_path):
    project = _project(_scene("s01", "hook"), _scene("s02", "close"))
    deps = _deps(tmp_path, transition_sfx_enabled=False)

    plan = sfx_plan(project, deps, Aspect.WIDE, library=FULL_LIBRARY)

    assert _at(plan.cues, "transition") == []
    assert _at(plan.cues, "accent") == [0.0, 4.5]


# ------------------------------------------------------------------ the settings


def test_the_effects_are_on_by_default():
    settings = Settings()

    assert settings.sfx_enabled is True
    assert settings.transition_sfx_enabled is True


def test_config_yaml_can_switch_the_effects_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("audio:\n  sfx_enabled: false\n  transition_sfx_enabled: false\n")

    settings = load_settings(path)

    assert settings.sfx_enabled is False
    assert settings.transition_sfx_enabled is False
