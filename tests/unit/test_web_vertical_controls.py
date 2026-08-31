"""Gate 2's vertical controls: the crop-focus slider and the `in_short` toggle.

Both controls exist because a Short is not a smaller copy of the wide video. It
is a **re-frame** (`assemble.reframe_filter` cuts a 9:16 window out of the source
asset) and a **shorter edit** (`assemble.aspect_scenes` keeps only the `in_short`
subset). Neither decision can be taken by the pipeline, so both are taken here.

The two properties this file exists to pin:

* **The overlay tells the truth.** The rectangle drawn over the shot is computed
  from the same `min(iw, ih·w/h)` window `reframe_filter` emits, positioned by
  the same `crop_focus_x`. `test_the_overlay_matches_the_filter_ffmpeg_will_run`
  re-derives it from the filter string itself rather than from a second copy of
  the arithmetic, so the two cannot drift apart in silence.
* **A no-op costs nothing.** The slider fires on every release and the toggle on
  every click, and `run_pipeline` holds the very same `flock` for the length of a
  render. Both no-op tests therefore assert `locks_taken == []`: not "wrote
  nothing", but "did not so much as reach for the lock".

The running-duration readout is asserted against `assemble.short_duration_s`,
never against a number typed into the test — and `short_fits` is a *duration*
test, so a project with nothing ticked "fits" at 0 s. That vacuous pass is the
one thing the readout must not repeat, hence `data-short-state` has three values
and not two.

Since M3 Task 22 the readout also separates the **target** from the **limit**.
`SHORT_TARGET_S` (45 s) is where engagement peaks and it only ever advises;
`MAX_SHORT_S` (180 s) is the platform limit and is the only one anything refuses
on. `test_the_target_never_blocks_anything` is the mutation guard on that: make
the target refuse and it fails.
"""

import re

import pytest
from fastapi.testclient import TestClient

from videomaker import runner as runner_module
from videomaker.config import Settings
from videomaker.models import Aspect
from videomaker.pipeline.assemble import (
    MAX_SHORT_S,
    SHORT_TARGET_S,
    VERTICAL_SPEC,
    reframe_filter,
    short_duration_s,
    timeline_scene_ids,
)
from videomaker.project import PROJECT_FILE, ProjectStore
from videomaker.runner import build_deps, run_pipeline
from videomaker.web.app import create_app
from videomaker.web.routes.storyboard import CROP_STEP

#: What htmx puts on every request it makes.
_HX = {"HX-Request": "true"}

#: One card per scene; the crop overlay and the toggle each keep their attribute
#: pair adjacent in the template so these can read them.
_CARD = re.compile(r'data-scene="(s\d+)"')
_CROP = re.compile(r'data-crop-window="([\d.]+)" data-crop-focus="([\d.]+)"')
_IN_SHORT = re.compile(r'data-in-short="(s\d+)" data-included="(true|false)"')

#: The gate card's Short readout. Four states, not a bare boolean: an empty
#: `in_short` set passes `short_fits` vacuously and must still read as a problem,
#: and `long` (past `SHORT_TARGET_S`) is advice while `over` (past `MAX_SHORT_S`)
#: is the platform limit that gate 3 will actually refuse.
_SHORT = re.compile(r'data-short-state="(ok|over|long|empty)" data-short-seconds="([\d.]+)"')

#: `min(iw,ih*9/16)':ih:'(iw-ow)*0.500` — the geometry the overlay must match.
_CROP_FILTER = re.compile(r"crop='min\(iw,ih\*(\d+)/(\d+)\)':ih:'\(iw-ow\)\*([\d.]+)'")

#: A scene the template's `short_beats` put *in* the Short by default, so the
#: toggle tests start from a ticked box. `_storyboarded` asks for 4 scenes, which
#: `assign_beats` labels hook / context / mechanism / close.
SCENE = "s03"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


@pytest.fixture
def locks_taken(monkeypatch) -> list[str]:
    """Every `ProjectStore.lock` the code under test opens.

    A control with nothing to change must not appear here at all.
    """
    taken: list[str] = []
    original = ProjectStore.lock

    def spying_lock(self: ProjectStore, project_id: str):
        taken.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectStore, "lock", spying_lock)
    return taken


def _storyboarded(app, store: ProjectStore):
    """A project run as far as `visuals`, so every scene has a real chosen shot."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(app.state.settings, project.id), until="visuals", yes=True)
    return store.load(project.id)


def _card_html(body: str, scene_id: str) -> str:
    chunk = body.split(f'data-scene="{scene_id}"', 1)[1]
    return chunk.split('data-scene="', 1)[0]


def _gate_html(body: str) -> str:
    """The gate card, which is everything before the first scene card."""
    return body.split('data-scene="', 1)[0]


def _on_disk(store: ProjectStore, project_id: str) -> tuple[bytes, int, int]:
    """`project.json`'s bytes, mtime **and inode**: `save()` is an atomic `os.replace`."""
    stat_path = store.path_for(project_id) / PROJECT_FILE
    stat = stat_path.stat()
    return (stat_path.read_bytes(), stat.st_mtime_ns, stat.st_ino)


# ------------------------------------------------------- the 9:16 overlay


def test_every_card_draws_the_crop_overlay(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    assert _CARD.findall(body) == [scene.id for scene in project.scenes]
    assert len(_CROP.findall(body)) == len(project.scenes)


def test_the_overlay_matches_the_filter_ffmpeg_will_run(app, client, store):
    """Derived from `reframe_filter`'s own expression, not from a second copy of it."""
    project = _storyboarded(app, store)
    scene = project.scene_by_id(SCENE)
    ref = scene.visual.chosen
    assert ref is not None and ref.width > 0 and ref.height > 0

    card = _card_html(client.get(f"/projects/{project.id}/storyboard").text, SCENE)
    window_pct, focus = (float(value) for value in _CROP.findall(card)[0])

    graph = reframe_filter(VERTICAL_SPEC, scene.visual.crop_focus_x)
    numerator, denominator, filter_focus = _CROP_FILTER.search(graph[0]).groups()
    expected = min(ref.width, ref.height * int(numerator) / int(denominator)) / ref.width

    assert window_pct == pytest.approx(expected * 100, abs=0.01)
    assert focus == pytest.approx(float(filter_focus), abs=0.005)
    # A 16:9 source really is mostly thrown away, so the overlay is worth drawing.
    assert window_pct < 50


def test_the_overlay_slides_with_the_stored_focus(app, client, store):
    project = _storyboarded(app, store)
    saved = store.load(project.id)
    saved.scene_by_id(SCENE).visual.crop_focus_x = 0.2
    store.save(saved)

    card = _card_html(client.get(f"/projects/{project.id}/storyboard").text, SCENE)
    _, focus = _CROP.findall(card)[0]

    assert float(focus) == pytest.approx(0.2)


def test_a_source_already_narrower_than_nine_by_sixteen_keeps_all_of_it(app, client, store):
    """`min(iw, ih·9/16)` is what keeps the window inside the source.

    720×1600 is narrower than 9:16, so `ih·9/16` is 900 — wider than the source
    itself. Every mock asset is 16:9, so without such a shot on the page the `min`
    could be deleted and nothing would notice until a real vertical clip drew an
    overlay wider than the picture it sits on.
    """
    project = _storyboarded(app, store)
    saved = store.load(project.id)
    scene = saved.scene_by_id(SCENE)
    scene.visual.chosen = scene.visual.chosen.model_copy(update={"width": 720, "height": 1600})
    store.save(saved)

    card = _card_html(client.get(f"/projects/{project.id}/storyboard").text, SCENE)
    window_pct, _ = _CROP.findall(card)[0]

    assert float(window_pct) == pytest.approx(100.0)
    # Nothing is being thrown away, so there is no rectangle to draw over it.
    assert 'class="crop-window"' not in card
    assert "already at 9:16 or narrower" in card


def test_a_shot_with_no_recorded_size_gets_no_overlay_at_all(app, client, store):
    """A dishonest overlay is worse than none: an unsized ref gets no geometry."""
    project = _storyboarded(app, store)
    saved = store.load(project.id)
    scene = saved.scene_by_id(SCENE)
    scene.visual.chosen = scene.visual.chosen.model_copy(update={"width": 0, "height": 0})
    store.save(saved)

    response = client.get(f"/projects/{project.id}/storyboard")

    assert response.status_code == 200
    card = _card_html(response.text, SCENE)
    assert "data-crop-window" not in card
    assert 'class="crop-window"' not in card
    # The other scenes still have theirs.
    assert len(_CROP.findall(response.text)) == len(project.scenes) - 1


def test_each_card_offers_the_crop_slider(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    for scene in project.scenes:
        assert f'action="/projects/{project.id}/scenes/{scene.id}/crop"' in body
    assert body.count('type="range"') == len(project.scenes)
    assert body.count('name="crop_focus_x"') == len(project.scenes)
    # The slider's grid is the one the handler quantises to, not a second number.
    assert body.count(f'step="{CROP_STEP}"') == len(project.scenes)


# ------------------------------------------------------- POST the crop focus


def test_moving_the_slider_persists_the_focus(app, client, store):
    project = _storyboarded(app, store)
    assert project.scene_by_id(SCENE).visual.crop_focus_x == 0.5

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/crop",
        data={"crop_focus_x": "0.25"},
        headers=_HX,
    )

    assert response.status_code == 200
    saved = store.load(project.id)
    assert saved.scene_by_id(SCENE).visual.crop_focus_x == pytest.approx(0.25)
    # The neighbour keeps whatever it had.
    assert saved.scene_by_id("s01").visual.crop_focus_x == 0.5


def test_the_slider_reply_is_the_card_partial_with_the_gate_out_of_band(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/crop",
        data={"crop_focus_x": "0.75"},
        headers=_HX,
    )

    assert response.status_code == 200
    assert "<!doctype" not in response.text.lower()
    assert f'data-scene="{SCENE}"' in response.text
    assert 'hx-swap-oob="true"' in response.text
    # And the overlay in the reply has already moved.
    _, focus = _CROP.findall(_card_html(response.text, SCENE))[0]
    assert float(focus) == pytest.approx(0.75)


def test_a_plain_slider_post_redirects_back_to_the_page(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/crop",
        data={"crop_focus_x": "0.75"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/storyboard#scene-{SCENE}"


def test_setting_the_focus_it_already_has_writes_nothing(app, client, store, locks_taken):
    project = _storyboarded(app, store)
    current = project.scene_by_id(SCENE).visual.crop_focus_x
    before = _on_disk(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/crop",
        data={"crop_focus_x": str(current)},
        headers=_HX,
    )

    assert response.status_code == 200
    assert _on_disk(store, project.id) == before
    # Not even reached for: the compare happens before the lock, on purpose.
    assert locks_taken == []


def test_the_stored_focus_is_quantised_to_the_sliders_own_grid(app, client, store, locks_taken):
    """The float-equality no-op guard is only safe because everything is on the grid."""
    project = _storyboarded(app, store)

    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/crop",
        data={"crop_focus_x": "0.333333"},
        headers=_HX,
    )
    assert store.load(project.id).scene_by_id(SCENE).visual.crop_focus_x == 0.33

    # And what the slider now sends back for that position is a no-op, lock included.
    locks_taken.clear()
    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/crop", data={"crop_focus_x": "0.33"}, headers=_HX
    )
    assert locks_taken == []


def test_a_focus_outside_zero_to_one_is_rejected(app, client, store):
    project = _storyboarded(app, store)

    for value in ("1.5", "-0.1"):
        response = client.post(
            f"/projects/{project.id}/scenes/{SCENE}/crop",
            data={"crop_focus_x": value},
            headers=_HX,
        )
        assert response.status_code == 422, value
    assert store.load(project.id).scene_by_id(SCENE).visual.crop_focus_x == 0.5


def test_setting_the_focus_of_an_unknown_scene_is_a_404(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s99/crop", data={"crop_focus_x": "0.4"}, headers=_HX
    )

    assert response.status_code == 404


# ------------------------------------------------------- the in_short toggle


def test_each_card_offers_the_in_short_toggle(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    assert _IN_SHORT.findall(body) == [
        (scene.id, "true" if scene.in_short else "false") for scene in project.scenes
    ]
    # And the default really is a selection now, not "every scene, cropped".
    assert not all(scene.in_short for scene in project.scenes)
    for scene in project.scenes:
        assert f'action="/projects/{project.id}/scenes/{scene.id}/in_short"' in body


def test_unticking_the_toggle_takes_the_scene_out_of_the_short(app, client, store):
    """An unchecked checkbox sends no field at all — that absence *is* the "off"."""
    project = _storyboarded(app, store)

    response = client.post(f"/projects/{project.id}/scenes/{SCENE}/in_short", data={}, headers=_HX)

    assert response.status_code == 200
    saved = store.load(project.id)
    assert saved.scene_by_id(SCENE).in_short is False
    assert saved.scene_by_id("s01").in_short is True
    assert SCENE not in timeline_scene_ids(saved, Aspect.VERTICAL)


def test_re_ticking_the_toggle_puts_the_scene_back(app, client, store):
    project = _storyboarded(app, store)
    client.post(f"/projects/{project.id}/scenes/{SCENE}/in_short", data={}, headers=_HX)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/in_short", data={"in_short": "on"}, headers=_HX
    )

    assert response.status_code == 200
    assert store.load(project.id).scene_by_id(SCENE).in_short is True


def test_setting_in_short_to_what_it_already_is_writes_nothing(app, client, store, locks_taken):
    project = _storyboarded(app, store)
    assert project.scene_by_id(SCENE).in_short is True
    before = _on_disk(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/in_short", data={"in_short": "on"}, headers=_HX
    )

    assert response.status_code == 200
    assert _on_disk(store, project.id) == before
    assert locks_taken == []


def test_a_plain_toggle_post_redirects_back_to_the_page(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/in_short", data={}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/storyboard#scene-{SCENE}"


def test_toggling_an_unknown_scene_is_a_404(app, client, store):
    project = _storyboarded(app, store)

    assert client.post(f"/projects/{project.id}/scenes/s99/in_short", data={}).status_code == 404


# --------------------------------------------- the running vertical duration


def test_the_gate_card_shows_the_running_short_duration(app, client, store):
    # The mock narrator speaks the same 43 words in every scene, so even the
    # beat-selected Short lands past the 45 s target. Trimmed here on purpose:
    # this test is about the number in the readout, not about which state it is.
    project = _storyboarded(app, store)
    for scene in project.scenes:
        scene.duration_s = 8.0
    store.save(project)
    project = store.load(project.id)

    gate = _gate_html(client.get(f"/projects/{project.id}/storyboard").text)
    state, seconds = _SHORT.findall(gate)[0]

    assert float(seconds) == pytest.approx(short_duration_s(project), abs=0.05)
    assert state == "ok"
    assert seconds != "0.0", "the fixture must have a Short with something in it"


def test_the_running_duration_drops_when_a_scene_leaves_the_short(app, client, store):
    project = _storyboarded(app, store)
    before = short_duration_s(project)

    response = client.post(f"/projects/{project.id}/scenes/{SCENE}/in_short", data={}, headers=_HX)

    # The gate card rides back out of band, so the total updates without a reload.
    _, seconds = _SHORT.findall(response.text)[0]
    after = short_duration_s(store.load(project.id))
    assert after < before
    assert float(seconds) == pytest.approx(after, abs=0.05)


def test_a_short_over_the_three_minute_limit_is_flagged_here_not_at_gate_three(
    app, client, store
):
    project = _storyboarded(app, store)
    saved = store.load(project.id)
    saved.scene_by_id(SCENE).duration_s = MAX_SHORT_S + 30.0
    assert saved.scene_by_id(SCENE).in_short is True
    store.save(saved)

    gate = _gate_html(client.get(f"/projects/{project.id}/storyboard").text)
    state, seconds = _SHORT.findall(gate)[0]

    assert state == "over"
    assert float(seconds) > MAX_SHORT_S
    # And it names the scene to untick, rather than only saying "too long".
    assert f"<code>{SCENE}</code>" in gate


def test_an_empty_short_reads_as_a_problem_not_as_ok(app, client, store):
    """`short_fits` is a duration test: nothing ticked runs 0 s and "fits" vacuously."""
    from videomaker.pipeline.assemble import short_fits

    project = _storyboarded(app, store)
    saved = store.load(project.id)
    for scene in saved.scenes:
        scene.in_short = False
    store.save(saved)
    assert short_fits(store.load(project.id)) is True

    gate = _gate_html(client.get(f"/projects/{project.id}/storyboard").text)
    state, seconds = _SHORT.findall(gate)[0]

    assert state == "empty"
    assert float(seconds) == 0.0


# ------------------------------------------------- the target, which only advises


def test_a_short_past_the_target_but_inside_the_limit_reads_as_a_nudge(app, client, store):
    """`long`, not `over`: nothing is broken, the cut is simply longer than the
    30-45 s where Shorts engagement actually peaks."""
    project = _storyboarded(app, store)
    saved = store.load(project.id)
    ticked = [scene for scene in saved.scenes if scene.in_short]
    ticked[0].duration_s = SHORT_TARGET_S + 20.0
    store.save(saved)
    duration = short_duration_s(store.load(project.id))
    assert SHORT_TARGET_S < duration < MAX_SHORT_S, "the fixture must sit between the two"

    gate = _gate_html(client.get(f"/projects/{project.id}/storyboard").text)
    state, seconds = _SHORT.findall(gate)[0]

    assert state == "long"
    assert float(seconds) == pytest.approx(duration, abs=0.05)
    # It names what to drop, exactly as the over-limit state does. Matched on the
    # readout's own `<code>` markup: a bare `"s01" in gate` also matches the anchor
    # the next-action link carries, and would pass with the nudge deleted.
    assert f"<code>{ticked[0].id}</code>" in gate


def test_the_target_never_blocks_anything(app, client, store):
    """The mutation guard. Make `SHORT_TARGET_S` refuse instead of advise and this
    fails: gate 2 is still approvable and the render gate still has no complaint."""
    from videomaker.pipeline.render import check_short_limit

    project = _storyboarded(app, store)
    saved = store.load(project.id)
    next(scene for scene in saved.scenes if scene.in_short).duration_s = SHORT_TARGET_S + 20.0
    store.save(saved)

    body = client.get(f"/projects/{project.id}/storyboard").text
    gate = _gate_html(body)
    assert 'data-short-state="long"' in gate
    assert "<button type=\"submit\" class=\"primary\" disabled" not in gate

    check_short_limit(store.load(project.id))  # raises only on the *limit*


def test_the_readout_carries_the_target_alongside_the_limit(app, client, store):
    project = _storyboarded(app, store)

    gate = _gate_html(client.get(f"/projects/{project.id}/storyboard").text)

    assert f'data-short-target="{SHORT_TARGET_S:.1f}"' in gate
