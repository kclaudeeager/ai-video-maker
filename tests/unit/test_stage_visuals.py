"""The visuals stage: one asset per scene, stock first, AI image only as a last resort.

Two promises are load-bearing here. `test_editing_one_scene_query_refetches_only_that_scene`
is the per-scene cache scoping the "<10 s re-run" DoD rests on, and
`test_stock_hits_never_reach_the_image_provider` is M0 finding 6 made into a test:
Workers AI auto-bills past the free cap, so a scene stock can serve must never
reach Flux.
"""

import pytest

from videomaker.cache import ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.models import Scene, SceneVisual, VisualKind
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.visuals import MAX_CANDIDATES, run_visuals
from videomaker.project import ProjectStore
from videomaker.providers.errors import ProviderError
from videomaker.providers.mock import MockImage, MockStock
from videomaker.providers.ratelimit import QuotaTracker

QUERIES = {
    "s01": "close up circuit board solder",
    "s02": "flash memory chip macro",
    "s03": "data centre server rack lights",
}
DURATIONS = {"s01": 4.0, "s02": 5.5, "s03": 3.25}


def _deps(tmp_path, monkeypatch, *, chains=None):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains=chains if chains is not None else {"stock": ["mock"], "image": ["mock"]},
    )
    deps = StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )
    deps.searches = []
    deps.downloads = 0
    deps.images = 0

    search = MockStock.search
    download = MockStock.download
    generate = MockImage.generate_image

    def counted_search(self, **kwargs):
        deps.searches.append(kwargs)
        return search(self, **kwargs)

    def counted_download(self, result, out_path, **kwargs):
        deps.downloads += 1
        return download(self, result, out_path, **kwargs)

    def counted_generate(self, **kwargs):
        deps.images += 1
        return generate(self, **kwargs)

    monkeypatch.setattr(MockStock, "search", counted_search)
    monkeypatch.setattr(MockStock, "download", counted_download)
    monkeypatch.setattr(MockImage, "generate_image", counted_generate)
    return deps


@pytest.fixture
def mock_deps(tmp_path, monkeypatch):
    return _deps(tmp_path, monkeypatch)


def _project(mock_deps):
    """A voiced project: visuals runs after voice, so every scene has a duration."""
    project = mock_deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(
            id=sid,
            narration=f"Narration for {sid}.",
            visual=SceneVisual(query=query),
            audio_path=f"scenes/{sid}/narration.wav",
            duration_s=DURATIONS[sid],
        )
        for sid, query in QUERIES.items()
    ]
    mock_deps.store.save(project)
    return project


def _asset(mock_deps, project, scene_id):
    scene = project.scene_by_id(scene_id)
    return mock_deps.store.path_for(project.id) / scene.visual.chosen.local_path


def test_visuals_downloads_one_asset_per_scene(mock_deps):
    project = _project(mock_deps)

    result = run_visuals(project, mock_deps)

    assert result.changed is True
    assert mock_deps.downloads == 3
    for scene_id in QUERIES:
        chosen = project.scene_by_id(scene_id).visual.chosen
        assert chosen is not None
        # Relative to the project folder, so project folders stay movable.
        assert chosen.local_path == f"scenes/{scene_id}/asset.mp4"
        assert _asset(mock_deps, project, scene_id).is_file()


def test_candidates_are_capped_and_chosen_is_the_first(mock_deps):
    project = _project(mock_deps)

    run_visuals(project, mock_deps)

    for scene_id in QUERIES:
        visual = project.scene_by_id(scene_id).visual
        assert 1 <= len(visual.candidates) <= MAX_CANDIDATES
        assert visual.chosen == visual.candidates[0]
        # Only the chosen candidate is downloaded: the rest are offers, not files.
        assert all(ref.local_path == "" for ref in visual.candidates[1:])


def test_offered_candidates_keep_the_provider_thumbnail(mock_deps):
    """An offer has no file, so its remote preview is the only picture of it."""
    project = _project(mock_deps)

    run_visuals(project, mock_deps)

    for scene_id in QUERIES:
        visual = project.scene_by_id(scene_id).visual
        offers = [ref for ref in visual.candidates if not ref.local_path]
        assert offers, "the fixture must actually offer un-downloaded alternatives"
        assert all(ref.preview_url.startswith("https://") for ref in offers)
        # Distinct hits get distinct thumbnails: one shared URL would draw the
        # same picture on every tile.
        assert len({ref.preview_url for ref in offers}) == len(offers)


def test_clean_rerun_of_visuals_makes_zero_provider_calls(mock_deps):
    project = _project(mock_deps)
    run_visuals(project, mock_deps)

    result = run_visuals(project, mock_deps)

    assert mock_deps.searches and len(mock_deps.searches) == 3  # unchanged
    assert mock_deps.downloads == 3
    assert result.changed is False
    assert result.skipped_units == 3


def test_editing_one_scene_query_refetches_only_that_scene(mock_deps):
    project = _project(mock_deps)
    run_visuals(project, mock_deps)
    searches_before = len(mock_deps.searches)
    untouched = _asset(mock_deps, project, "s01")
    untouched_mtime = untouched.stat().st_mtime_ns

    project.scene_by_id("s02").visual.query = "electron trapped in an insulator"
    run_visuals(project, mock_deps)

    assert len(mock_deps.searches) == searches_before + 1
    assert mock_deps.searches[-1]["query"] == "electron trapped in an insulator"
    assert mock_deps.downloads == 4
    assert untouched.stat().st_mtime_ns == untouched_mtime  # s01's asset was not rewritten


def test_changing_a_scene_duration_refetches_that_scene(mock_deps):
    """A longer scene needs a longer clip, so duration is part of the hash."""
    project = _project(mock_deps)
    run_visuals(project, mock_deps)

    project.scene_by_id("s03").duration_s = 12.0
    run_visuals(project, mock_deps)

    assert len(mock_deps.searches) == 4
    assert mock_deps.searches[-1]["min_duration_s"] == 12.0


def test_locked_scene_is_never_refetched_even_when_stale(mock_deps):
    project = _project(mock_deps)
    run_visuals(project, mock_deps)
    scene = project.scene_by_id("s02")
    original = scene.visual.chosen
    scene.locked = True
    scene.visual.query = "a completely different picture"

    result = run_visuals(project, mock_deps)

    assert len(mock_deps.searches) == 3  # no fourth search
    assert result.changed is False
    assert scene.visual.chosen == original


def test_deleted_asset_is_refetched_even_though_the_hash_is_fresh(mock_deps):
    project = _project(mock_deps)
    run_visuals(project, mock_deps)
    _asset(mock_deps, project, "s03").unlink()

    result = run_visuals(project, mock_deps)

    assert result.changed is True
    assert mock_deps.downloads == 4
    assert _asset(mock_deps, project, "s03").is_file()


def test_stock_hits_never_reach_the_image_provider(mock_deps):
    """M0 finding 6: Workers AI auto-bills past the free cap. Stock comes first."""
    project = _project(mock_deps)

    run_visuals(project, mock_deps)

    assert mock_deps.images == 0
    assert [call["kind"] for call in mock_deps.searches] == [VisualKind.STOCK_VIDEO] * 3


def test_falls_back_to_ai_image_only_when_stock_has_nothing(mock_deps, monkeypatch):
    monkeypatch.setattr(MockStock, "search", lambda self, **kwargs: [])
    project = _project(mock_deps)

    result = run_visuals(project, mock_deps)

    assert result.changed is True
    assert mock_deps.images == 3
    assert mock_deps.downloads == 0
    for scene_id in QUERIES:
        chosen = project.scene_by_id(scene_id).visual.chosen
        assert chosen.local_path == f"scenes/{scene_id}/asset.jpg"
        assert _asset(mock_deps, project, scene_id).is_file()


def test_an_explicit_kind_overrides_the_template_order(mock_deps):
    project = _project(mock_deps)
    project.scene_by_id("s02").visual.kind = VisualKind.STOCK_PHOTO

    run_visuals(project, mock_deps)

    kinds = {call["query"]: call["kind"] for call in mock_deps.searches}
    assert kinds[QUERIES["s02"]] is VisualKind.STOCK_PHOTO
    assert project.scene_by_id("s02").visual.chosen.local_path == "scenes/s02/asset.jpg"


def test_auto_is_left_on_the_scene_so_a_rerun_stays_cheap(mock_deps):
    """Resolving AUTO must not be written back: that would change the hash next run."""
    project = _project(mock_deps)

    run_visuals(project, mock_deps)

    assert all(scene.visual.kind is VisualKind.AUTO for scene in project.scenes)


def test_visuals_persists_the_project_and_its_per_scene_cache_units(mock_deps):
    project = _project(mock_deps)
    run_visuals(project, mock_deps)

    reloaded = mock_deps.store.load(project.id)
    assert reloaded.scene_by_id("s01").visual.chosen is not None
    persisted = StageCache(mock_deps.stage_cache.path)
    for scene_id in QUERIES:
        assert persisted.is_stale(stage_key("visuals", scene_id), "not-the-real-hash") is True
    assert run_visuals(project, mock_deps).skipped_units == 3


def test_a_scene_no_source_can_serve_fails_loudly(tmp_path, monkeypatch):
    deps = _deps(tmp_path, monkeypatch, chains={"stock": [], "image": []})
    project = _project(deps)

    with pytest.raises(ProviderError, match="s01"):
        run_visuals(project, deps)
