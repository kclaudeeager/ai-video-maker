"""Fast-render mode, and the progress hook `run_render` finally takes for real.

Two carried follow-ups that land on the same two functions, so they are tested
together.

**Fast render.** `doctor` promised since M0 that hardware encoding was "detected";
nothing ever used it. `--fast` does. The tests here are almost all *argument*
assertions, for the same reason `assemble`'s graph tests are: an encoder chosen
wrongly still produces a playable mp4, so only reading the command line can say
which encoder ran. Two of them are about honesty rather than speed —
`ffmpeg -encoders` listing `h264_qsv` says only that the *build* has the wrapper,
not that this machine can open it (this one cannot: no MFX runtime), so the
capability probe opens a one-frame encode and believes the result.

**The fingerprints.** `--fast` is a *how*, but a hardware encode is not the same
picture, and mixing an x264 segment with a VAAPI one in a concat stream-copy makes
FFmpeg rewrite timestamps ("Non-monotonic DTS"). So the encoder **is** hashed —
but the key is written **only when it is not the default**, exactly as the music
key is. That is what keeps the ten real projects in the owner's workspace out of
this: their fingerprints are byte-identical, and nothing re-renders.

**The progress hook.** M2 instrumented the encode by substituting `run_ffmpeg`
into `pipeline.render`, safe only while one worker thread runs one job. It is a
real parameter now, and `run_render` — not the web layer — is what banks each
aspect's output seconds, because `run_render` is what knows there is more than one
file to encode.
"""

from datetime import UTC, datetime

import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings, load_settings
from videomaker.media.ffmpeg import (
    HW_ENCODER_PREFERENCE,
    HW_QUALITY,
    SOFTWARE_ENCODER,
    VAAPI_UPLOAD_FILTER,
    FFmpegCaps,
    choose_encoder,
    hardware_encoder,
    probe_capabilities,
    software_encoder,
)
from videomaker.models import Aspect, AssetRef, Project, Scene, SceneVisual
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import render as render_module
from videomaker.pipeline.assemble import (
    CRF,
    PIX_FMT,
    PRESET,
    TIMESCALE,
    WIDE_SPEC,
    _segment_args,
    build_scene_filter,
    narration_relpath,
    scene_hash,
    stage_encoder,
    video_relpath,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.render import _render_args, render_hash, run_render
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

VAAPI = "h264_vaapi"


def _software():
    return software_encoder(crf=CRF, preset=PRESET, pix_fmt=PIX_FMT)


def _vaapi(quality: int = HW_QUALITY):
    return hardware_encoder(VAAPI, quality=quality)


# ------------------------------------------------------- what the probe may claim


def test_a_listed_encoder_is_not_a_working_one(monkeypatch):
    """`ffmpeg -encoders` lists the wrapper, not the driver behind it.

    This machine is the case in point: `h264_qsv` is listed and cannot open a
    session at all. A `--fast` that trusted the listing would announce a hardware
    render and quietly encode on the CPU, which is worse than no feature.
    """
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "videomaker.media.ffmpeg._run",
        lambda args: {
            "-version": "ffmpeg version 6.1.1\n",
            "-filters": " subtitles ",
            "-encoders": " h264_qsv \n h264_vaapi \n",
        }[args[-1]],
    )

    caps = probe_capabilities(verify=lambda name: name == VAAPI)

    assert caps.hw_encoder == "h264_qsv"  # what the build lists, in preference order
    assert caps.fast_encoder == VAAPI  # what actually opened


def test_nothing_that_opens_means_no_fast_encoder(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "videomaker.media.ffmpeg._run",
        lambda args: {
            "-version": "ffmpeg version 6.1.1\n",
            "-filters": " subtitles ",
            "-encoders": " h264_qsv \n",
        }[args[-1]],
    )

    caps = probe_capabilities(verify=lambda name: False)

    assert caps.hw_encoder == "h264_qsv"
    assert caps.fast_encoder == ""


def test_the_probe_prefers_the_first_encoder_that_opens(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "videomaker.media.ffmpeg._run",
        lambda args: {
            "-version": "ffmpeg version 6.1.1\n",
            "-filters": " subtitles ",
            "-encoders": " ".join(HW_ENCODER_PREFERENCE),
        }[args[-1]],
    )

    caps = probe_capabilities(verify=lambda name: True)

    assert caps.fast_encoder == HW_ENCODER_PREFERENCE[0]


# ------------------------------------------------------------- choosing an encoder


def test_without_fast_mode_the_encoder_is_libx264():
    chosen = choose_encoder(fast=False, crf=CRF, preset=PRESET, pix_fmt=PIX_FMT,
                            detect=lambda: VAAPI)

    assert chosen.name == SOFTWARE_ENCODER
    assert chosen.is_hardware is False


def test_fast_mode_takes_the_encoder_the_probe_found():
    chosen = choose_encoder(fast=True, crf=CRF, preset=PRESET, pix_fmt=PIX_FMT,
                            detect=lambda: VAAPI)

    assert chosen.name == VAAPI
    assert chosen.is_hardware is True


def test_fast_mode_with_nothing_usable_falls_back_to_libx264():
    chosen = choose_encoder(fast=True, crf=CRF, preset=PRESET, pix_fmt=PIX_FMT,
                            detect=lambda: "")

    assert chosen.name == SOFTWARE_ENCODER


def test_the_fallback_is_a_warning_the_run_can_see(monkeypatch):
    """A silent fall back to the CPU is the failure mode the whole feature risks."""
    monkeypatch.setattr(assemble_module, "detect_fast_encoder", lambda: "")

    encoder, warnings = stage_encoder(Settings(render_fast_mode=True))

    assert encoder.name == SOFTWARE_ENCODER
    assert len(warnings) == 1
    assert "libx264" in warnings[0]


def test_the_default_path_warns_about_nothing(monkeypatch):
    monkeypatch.setattr(assemble_module, "detect_fast_encoder", lambda: "")

    encoder, warnings = stage_encoder(Settings())

    assert encoder.name == SOFTWARE_ENCODER
    assert warnings == ()


# --------------------------------------------------------------- the argument list


def test_the_software_encoder_writes_exactly_the_arguments_that_shipped(tmp_path):
    """The default path is M1's, argument for argument. Nothing here may move it."""
    default = _render_args(tmp_path, Aspect.WIDE, burn_captions=True)

    assert default == _render_args(
        tmp_path, Aspect.WIDE, burn_captions=True, encoder=_software()
    )
    assert default[default.index("-c:v") + 1] == "libx264"
    assert "-crf" in default
    assert "-preset" in default
    assert "-pix_fmt" in default


def test_a_hardware_render_names_the_device_before_the_first_input(tmp_path):
    """VA-API needs its device opened before ffmpeg reads a frame."""
    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=True, encoder=_vaapi())

    assert args[0] == "-vaapi_device"
    assert args[1].startswith("/dev/dri/")
    assert args.index("-vaapi_device") < args.index("-i")


def test_a_hardware_render_uploads_the_frames_after_burning_the_captions(tmp_path):
    """The upload has to be *last*: libass draws on CPU frames, not GPU surfaces."""
    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=True, encoder=_vaapi())

    graph = args[args.index("-vf") + 1]
    assert graph.startswith("subtitles=")
    assert graph.endswith(VAAPI_UPLOAD_FILTER)


def test_a_hardware_render_with_no_captions_still_uploads(tmp_path):
    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=False, encoder=_vaapi())

    assert args[args.index("-vf") + 1] == VAAPI_UPLOAD_FILTER


def test_a_hardware_render_drops_the_x264_only_knobs(tmp_path):
    """`-crf`, `-preset` and `-pix_fmt` mean nothing to VA-API and break the open."""
    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=True, encoder=_vaapi())

    assert args[args.index("-c:v") + 1] == VAAPI
    assert args[args.index("-qp") + 1] == str(HW_QUALITY)
    assert "-crf" not in args
    assert "-preset" not in args
    assert "-pix_fmt" not in args


def test_a_hardware_render_keeps_the_audio_chain_untouched(tmp_path):
    """Fast mode is a picture decision. The mux, the loudness and the faststart stay."""
    software = _render_args(tmp_path, Aspect.WIDE, burn_captions=True)
    fast = _render_args(tmp_path, Aspect.WIDE, burn_captions=True, encoder=_vaapi())

    for flag in ("-af", "-c:a", "-b:a", "-ar", "-ac", "-movflags"):
        assert fast[fast.index(flag) + 1] == software[software.index(flag) + 1]
    assert "-shortest" in fast
    assert fast[-1] == software[-1]


# -------------------------------------------------------------- the segment encode


def _scene(sid: str = "s01") -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"{sid} b-roll",
            chosen=AssetRef(
                provider="mock",
                source_id=f"mock-{sid}",
                source_url=f"https://mock.invalid/{sid}",
                local_path=f"scenes/{sid}/asset.jpg",
                width=1920,
                height=1080,
            ),
        ),
        audio_path=f"scenes/{sid}/narration.wav",
        duration_s=4.0,
    )


def _segment(encoder=None):
    scene = _scene()
    graph = build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)
    return _segment_args(scene, WIDE_SPEC, graph, "build/s01_wide.mp4", encoder=encoder)


def test_segments_are_where_most_of_the_encode_time_is_so_they_take_the_choice_too():
    """Measured on this machine: 37 s of segment encode against 32 s of final pass."""
    fast = _segment(_vaapi())

    assert fast[fast.index("-c:v") + 1] == VAAPI
    assert fast[fast.index("-qp") + 1] == str(HW_QUALITY)
    assert fast[0] == "-vaapi_device"
    assert fast[fast.index("-vf") + 1].endswith(VAAPI_UPLOAD_FILTER)
    # Uniform intermediates are what let the concat demuxer stream-copy.
    assert fast[fast.index("-video_track_timescale") + 1] == str(TIMESCALE)


def test_the_default_segment_arguments_are_the_ones_that_shipped():
    assert _segment() == _segment(_software())
    default = _segment()
    assert default[default.index("-c:v") + 1] == "libx264"
    assert default[default.index("-crf") + 1] == str(CRF)
    assert default[default.index("-preset") + 1] == PRESET
    assert default[default.index("-pix_fmt") + 1] == PIX_FMT


# ------------------------------------------------------------------ fingerprints


def test_the_default_render_fingerprint_is_untouched(tmp_path):
    """The ten projects in the owner's workspace must not restage. Pinned, not derived."""
    (tmp_path / "v.mp4").write_bytes(b"video")
    (tmp_path / "n.wav").write_bytes(b"narration")
    (tmp_path / "c.ass").write_bytes(b"captions")

    def digest(**kwargs):
        return render_hash(
            tmp_path,
            Aspect.WIDE,
            video=tmp_path / "v.mp4",
            narration=tmp_path / "n.wav",
            captions=tmp_path / "c.ass",
            **kwargs,
        )

    # The literal M1/M3 digest, asserted in tests/unit/test_audio_mix.py too.
    assert digest() == "fc340720a6e8bfed"
    assert digest(encoder=_software()) == "fc340720a6e8bfed"


def test_a_hardware_render_is_a_different_fingerprint(tmp_path):
    """It is a different picture, so it is a different fingerprint — but only then."""
    (tmp_path / "v.mp4").write_bytes(b"video")
    (tmp_path / "n.wav").write_bytes(b"narration")

    def digest(**kwargs):
        return render_hash(
            tmp_path,
            Aspect.WIDE,
            video=tmp_path / "v.mp4",
            narration=tmp_path / "n.wav",
            captions=None,
            **kwargs,
        )

    assert digest(encoder=_vaapi()) != digest()
    assert digest(encoder=_vaapi(quality=24)) != digest(encoder=_vaapi())


def test_the_default_segment_fingerprint_is_untouched():
    scene = _scene()
    graph = build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)

    plain = scene_hash(scene, WIDE_SPEC, graph, "assetdigest")

    assert plain == scene_hash(scene, WIDE_SPEC, graph, "assetdigest", encoder=_software())
    # Pinned: a segment fingerprint that moved would re-encode every project's build/.
    assert plain == "86e2a8f2f5f98a41"


def test_a_hardware_segment_is_a_different_fingerprint():
    """Otherwise a partial re-run would concat x264 and VA-API segments together.

    Measured: FFmpeg accepts that concat and rewrites the timestamps as it goes
    ("Non-monotonic DTS in output stream"), and the scenes visibly disagree about
    how much grain they kept. Re-encoding the whole build on the first `--fast` run
    is the cost of never shipping that.
    """
    scene = _scene()
    graph = build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert scene_hash(scene, WIDE_SPEC, graph, "d", encoder=_vaapi()) != scene_hash(
        scene, WIDE_SPEC, graph, "d"
    )


# ------------------------------------------------------------------ configuration


def test_config_yaml_can_turn_fast_mode_on(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("render:\n  fast_mode: true\n")

    assert load_settings(path).render_fast_mode is True


def test_fast_mode_is_off_unless_asked_for(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("paths:\n  workspace_dir: elsewhere\n")

    assert load_settings(path).render_fast_mode is False
    assert Settings().render_fast_mode is False


# -------------------------------------------------- run_render's progress hook


def _deps(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        music_dir=tmp_path / "music",
        sfx_dir=tmp_path / "sfx",
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _assembled(deps):
    """A project with both aspects' assemble artefacts already on disk."""
    project = Project(
        id="fast-render",
        topic="how ssds work",
        template="tech_explainer",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        scenes=[_scene("s01"), _scene("s02")],
    )
    root = deps.store.path_for(project.id)
    for aspect in render_module.RENDER_ASPECTS:
        for relpath in (video_relpath(aspect), narration_relpath(aspect)):
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"pretend " + relpath.encode())
    deps.store.save(project)
    return project


def test_run_render_takes_a_progress_hook_and_hands_it_to_ffmpeg(tmp_path, monkeypatch):
    deps = _deps(tmp_path)
    project = _assembled(deps)
    hooks: list[object] = []

    def stub(args, *, cwd=None, on_progress=None):
        hooks.append(on_progress)
        (cwd / args[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub)

    run_render(project, deps, on_progress=lambda seconds: None)

    assert len(hooks) == len(render_module.RENDER_ASPECTS)
    assert all(hook is not None for hook in hooks)


def test_without_a_hook_ffmpeg_is_asked_for_no_progress(tmp_path, monkeypatch):
    """`-progress pipe:1` is not free, and every CLI run goes down this path."""
    deps = _deps(tmp_path)
    project = _assembled(deps)
    hooks: list[object] = []

    def stub(args, *, cwd=None, on_progress=None):
        hooks.append(on_progress)
        (cwd / args[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub)

    run_render(project, deps)

    assert hooks == [None] * len(render_module.RENDER_ASPECTS)


def test_the_seconds_climb_once_across_every_aspect(tmp_path, monkeypatch):
    """The single-climb contract, moved down into the stage that owns it.

    FFmpeg reports *this file's* output seconds, from zero, once per aspect. A hook
    handed those raw would see the count rewind when the Short starts, which on the
    bar reads as a crash. `run_render` banks what each aspect reached.
    """
    deps = _deps(tmp_path)
    project = _assembled(deps)
    seen: list[float] = []

    def stub(args, *, cwd=None, on_progress=None):
        for seconds in (0.0, 5.0, 10.0):
            on_progress(seconds)
        (cwd / args[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub)

    run_render(project, deps, on_progress=seen.append)

    aspects = len(render_module.RENDER_ASPECTS)
    assert seen == sorted(seen)
    assert seen == [0.0, 5.0, 10.0] + [10.0, 15.0, 20.0] * (aspects - 1)


def test_a_skipped_aspect_banks_nothing(tmp_path, monkeypatch):
    """A cached aspect encodes nothing, so it must not advance the count either."""
    deps = _deps(tmp_path)
    project = _assembled(deps)
    seen: list[float] = []
    encoded: list[str] = []

    def stub(args, *, cwd=None, on_progress=None):
        encoded.append(args[-1])
        for seconds in (0.0, 7.0):
            on_progress(seconds)
        (cwd / args[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub)
    first = render_module.RENDER_ASPECTS[0]
    root = deps.store.path_for(project.id)
    out = root / render_module.output_relpath(first)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"already rendered")
    deps.stage_cache.mark(
        f"render:{first.value}",
        render_module.render_hash(
            root,
            first,
            video=root / video_relpath(first),
            narration=root / narration_relpath(first),
            captions=None,
        ),
    )

    run_render(project, deps, on_progress=seen.append)

    assert render_module.output_relpath(first) not in encoded, "the cache was not honoured"
    assert encoded, "nothing was encoded, so the test proves nothing"
    # Not offset by the cached aspect's timeline: no seconds of it were encoded.
    assert seen == [0.0, 7.0]


def test_fast_mode_reaches_the_real_encode(tmp_path, monkeypatch):
    deps = _deps(tmp_path)
    deps.settings = deps.settings.model_copy(update={"render_fast_mode": True})
    project = _assembled(deps)
    commands: list[list[str]] = []

    def stub(args, *, cwd=None, on_progress=None):
        commands.append(args)
        (cwd / args[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub)
    monkeypatch.setattr(assemble_module, "detect_fast_encoder", lambda: VAAPI)

    result = run_render(project, deps)

    assert commands
    for args in commands:
        assert args[args.index("-c:v") + 1] == VAAPI
    assert result.warnings == ()


def test_fast_mode_with_no_hardware_says_so_rather_than_pretending(tmp_path, monkeypatch):
    deps = _deps(tmp_path)
    deps.settings = deps.settings.model_copy(update={"render_fast_mode": True})
    project = _assembled(deps)

    def stub(args, *, cwd=None, on_progress=None):
        assert args[args.index("-c:v") + 1] == SOFTWARE_ENCODER
        (cwd / args[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub)
    monkeypatch.setattr(assemble_module, "detect_fast_encoder", lambda: "")

    result = run_render(project, deps)

    assert len(result.warnings) == 1
    assert "libx264" in result.warnings[0]


@pytest.mark.parametrize("caps_kwargs, level, wanted", [
    ({"hw_encoder": "h264_vaapi", "fast_encoder": "h264_vaapi"}, "ok", "--fast"),
    ({"hw_encoder": "h264_qsv", "fast_encoder": ""}, "warn", "libx264"),
    ({"hw_encoder": "", "fast_encoder": ""}, "warn", "none detected"),
])
def test_doctor_reports_what_is_true_of_this_machine(tmp_path, caps_kwargs, level, wanted):
    from videomaker.doctor import run_checks

    caps = FFmpegCaps(
        installed=True,
        version="ffmpeg version 6.1.1",
        has_subtitles_filter=True,
        has_ffprobe=True,
        **caps_kwargs,
    )
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")

    check = next(r for r in run_checks(settings, caps) if r.name == "hardware encoder")

    assert check.level == level
    assert wanted in check.detail
    # The M3 promissory note has been paid; nothing may still be promising it.
    assert "lands in M3" not in check.detail
