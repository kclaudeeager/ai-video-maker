"""Searching stock from the storyboard card, and the free-tier headroom indicator.

**The test this file exists for is `test_repeating_a_search_costs_no_quota`.**
Pexels' soft budget is 190 requests an hour and browsing a storyboard burns it
fast, so the one guarantee that makes a live search box safe is that *asking the
same question twice is free*. M1 already provides it — `PexelsProvider.search`
goes through the shared `ResponseCache` before it goes anywhere near the network
— and this file proves the web route did not accidentally route around it, with
the same three witnesses the earlier tasks used: zero HTTP requests, and
`quota.json` unchanged in bytes, mtime **and inode** (`QuotaTracker.save` is an
atomic `os.replace`, so a rewrite with identical contents still moves the inode).

That test is the only one here that leaves the mock chain: a cache guarantee
about HTTP cannot be demonstrated by a provider that makes no HTTP calls. It
swaps *only* the stock chain to the real `PexelsProvider` and gives it an
`httpx.MockTransport`, so the code under test is M1's real caching, quota
checking and response parsing with the socket removed.

The rest is the shape gate 2 established:

* **A search that changes nothing writes nothing** — and does not even take the
  project lock, which `run_pipeline` may be holding for the length of a render.
  Searching the query the visuals stage already ran is a genuine no-op: the
  offers it rebuilds are field-for-field the candidates already stored.
* **A spent budget is a message in the card, not a 500.** So is a search that
  found nothing.
* **An empty query is refused on both sides** — `required` in the markup, 422 in
  the handler.
* **The search never rewrites `scene.visual.query`.** That field is an input to
  M1's `visuals` fingerprint: moving it would make the scene stale, and the very
  next run would re-fetch and overwrite both the alternatives just found and the
  shot picked from them. The search would undo itself.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from videomaker import runner as runner_module
from videomaker.config import Settings
from videomaker.project import PROJECT_FILE, ProjectStore
from videomaker.providers import mock as mock_module
from videomaker.providers.errors import QuotaExceeded
from videomaker.providers.ratelimit import SOFT_BUDGETS, QuotaTracker
from videomaker.providers.stock import pexels as pexels_module
from videomaker.runner import QUOTA_FILENAME, build_deps, run_pipeline
from videomaker.web.app import create_app

#: What htmx puts on every request it makes.
_HX = {"HX-Request": "true"}

#: The two data attributes of a quota row, kept adjacent in the template.
_QUOTA_ROW = re.compile(r'data-quota="([a-z]+)" data-remaining="(\d+)"')

#: One tile of the candidate list, as the storyboard tests read it.
_CANDIDATE = re.compile(r'data-candidate="(\d+)" data-chosen="(true|false)"')

#: The search box itself, whole, so its attributes can be read.
_QUERY_INPUT = re.compile(r'<input[^>]*name="query"[^>]*>')

SCENE = "s01"


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    # The search route builds real `StageDeps`, so the response cache and the
    # quota ledger must land in the test's own tmp dir, never in ~/.cache.
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    """No lifespan, so no worker thread: nothing can run a stage behind our back."""
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


@pytest.fixture
def locks_taken(monkeypatch) -> list[str]:
    """Every `ProjectStore.lock` the code under test opens.

    A search with nothing to save must not appear here at all: a no-op guard that
    still takes the `flock` would park the request thread behind a running render
    for no reason.
    """
    taken: list[str] = []
    original = ProjectStore.lock

    def spying_lock(self: ProjectStore, project_id: str):
        taken.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectStore, "lock", spying_lock)
    return taken


def _storyboarded(app, store: ProjectStore):
    """A project run as far as `visuals`, so every scene has real candidates."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(app.state.settings, project.id), until="visuals", yes=True)
    return store.load(project.id)


def _card_html(body: str, scene_id: str) -> str:
    chunk = body.split(f'data-scene="{scene_id}"', 1)[1]
    return chunk.split('data-scene="', 1)[0]


def _witness(path: Path) -> tuple[bytes, int, int]:
    """Bytes, mtime **and inode**: both `save()` helpers are atomic `os.replace`."""
    stat = path.stat()
    return (path.read_bytes(), stat.st_mtime_ns, stat.st_ino)


def _project_witness(store: ProjectStore, project_id: str) -> tuple[bytes, int, int]:
    return _witness(store.path_for(project_id) / PROJECT_FILE)


# ------------------------------------------------------ a real Pexels, no socket

#: One page of the Pexels `/videos/search` shape, measured in M0. Four hits so a
#: search fills `MAX_CANDIDATES`, all far longer than any scene here so none is
#: filtered out for being too short to cover its segment.
PEXELS_PAGE = {
    "videos": [
        {
            "id": 900 + index,
            "url": f"https://pexels.invalid/video/{900 + index}/",
            "image": f"https://pexels.invalid/preview/{900 + index}.jpg",
            "duration": 600,
            "user": {"name": f"Camera Person {index}"},
            "video_files": [
                {
                    "width": 1920,
                    "height": 1080,
                    "link": f"https://pexels.invalid/download/{900 + index}.mp4",
                }
            ],
        }
        for index in range(4)
    ]
}


def _use_pexels_stock(app, monkeypatch) -> list[httpx.Request]:
    """Point *only* the stock chain at the real provider, over a mock transport.

    Everything else stays on the mock chain, so this costs no credentials and no
    network — but `PexelsProvider.search`, its `ResponseCache` lookup and its
    `QuotaTracker.check`/`record` are the genuine article.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PEXELS_PAGE)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    monkeypatch.setattr(pexels_module.PexelsProvider, "client", property(lambda self: client))

    chains = dict(app.state.settings.provider_chains)
    chains["stock"] = [pexels_module.PROVIDER_NAME]
    app.state.settings = app.state.settings.model_copy(
        update={"provider_chains": chains, "pexels_api_key": "test-key"}
    )
    return seen


# ------------------------------------------------------------- the search form


def test_every_card_offers_a_stock_search(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    for scene in project.scenes:
        assert f'action="/projects/{project.id}/scenes/{scene.id}/search"' in body
    assert body.count('name="query"') == len(project.scenes)


def test_the_query_field_is_required_client_side(app, client, store):
    """The first half of "rejected client-side and server-side"."""
    project = _storyboarded(app, store)

    card = _card_html(client.get(f"/projects/{project.id}/storyboard").text, SCENE)
    field = _QUERY_INPUT.search(card)

    assert field is not None
    assert "required" in field.group(0)


def test_the_form_says_what_a_search_costs(app, client, store):
    """The whole point of the indicator: the price is on the button, not in a log."""
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    assert "costs one" in body
    assert "free" in body


# ------------------------------------------------------------ POST /.../search


def test_searching_replaces_the_candidates(app, client, store):
    project = _storyboarded(app, store)
    before = [ref.source_id for ref in project.scene_by_id(SCENE).visual.candidates]

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert response.status_code == 200
    after = [ref.source_id for ref in store.load(project.id).scene_by_id(SCENE).visual.candidates]
    assert len(after) >= 2
    assert after != before
    # The mock provider derives its ids from the query, so the same words twice
    # give the same hits back: what was searched is what was typed.
    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )
    assert [
        ref.source_id for ref in store.load(project.id).scene_by_id(SCENE).visual.candidates
    ] == after


def test_searching_returns_the_scene_card_partial_for_htmx(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert "<!doctype" not in response.text.lower()
    assert f'data-scene="{SCENE}"' in response.text
    # The gate card rides along out of band, and the quota indicator rides inside
    # it — so spending a request is visible without a reload.
    assert 'hx-swap-oob="true"' in response.text
    assert _QUOTA_ROW.search(response.text)


def test_the_new_alternatives_are_all_offered(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    saved = store.load(project.id).scene_by_id(SCENE)
    tiles = _CANDIDATE.findall(_card_html(response.text, SCENE))
    assert [index for index, _ in tiles] == [
        str(index) for index in range(len(saved.visual.candidates))
    ]


def test_a_searched_offer_carries_the_provider_thumbnail(app, client, store):
    """The offers a search writes must be drawable, same as the visuals stage's."""
    project = _storyboarded(app, store)

    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    saved = store.load(project.id).scene_by_id(SCENE)
    offers = [ref for ref in saved.visual.candidates if not ref.local_path]
    assert offers, "the search must actually offer un-downloaded alternatives"
    assert all(ref.preview_url.startswith("https://") for ref in offers)


def test_searching_leaves_the_shot_in_use_alone(app, client, store):
    """A search offers; it does not choose. The rendered video must not move."""
    project = _storyboarded(app, store)
    chosen = project.scene_by_id(SCENE).visual.chosen

    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    saved = store.load(project.id).scene_by_id(SCENE).visual.chosen
    assert saved == chosen
    assert (store.path_for(project.id) / saved.local_path).is_file()


def test_searching_does_not_rewrite_the_scenes_own_query(app, client, store):
    """Deliberate: `visual.query` feeds M1's `visuals` fingerprint.

    Moving it would make the scene stale, and the next run would re-fetch and
    overwrite both these alternatives and whatever was picked from them.
    """
    project = _storyboarded(app, store)
    original = project.scene_by_id(SCENE).visual.query

    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert store.load(project.id).scene_by_id(SCENE).visual.query == original


def test_searching_touches_only_that_scene(app, client, store):
    project = _storyboarded(app, store)
    neighbour = project.scene_by_id("s02").visual.model_dump()

    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert store.load(project.id).scene_by_id("s02").visual.model_dump() == neighbour


def test_a_search_that_finds_a_downloaded_hit_again_keeps_its_file(app, client, store):
    """Swapping back has to stay free, so a known `local_path` is carried over."""
    project = _storyboarded(app, store)
    chosen = project.scene_by_id(SCENE).visual.chosen
    original_query = project.scene_by_id(SCENE).visual.query

    # Away from the stage's own query, then back to it.
    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )
    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": original_query},
        headers=_HX,
    )

    saved = store.load(project.id).scene_by_id(SCENE).visual.candidates
    match = next(ref for ref in saved if ref.source_id == chosen.source_id)
    assert match.local_path == chosen.local_path


def test_searching_the_query_already_searched_writes_nothing(app, client, store, locks_taken):
    """The no-op guard, and it must sit *before* the lock, not inside it."""
    project = _storyboarded(app, store)
    query = project.scene_by_id(SCENE).visual.query
    before = _project_witness(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": query},
        headers=_HX,
    )

    assert response.status_code == 200
    assert _project_witness(store, project.id) == before
    assert locks_taken == []


def test_a_plain_form_post_redirects_back_to_the_card(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/storyboard#scene-{SCENE}"


# --------------------------------------------------------------- refusing a query


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"query": ""}, id="empty"),
        pytest.param({"query": "   "}, id="whitespace"),
        # FastAPI substitutes a `Form` default for any empty value, so an absent
        # field and an empty one arrive identically. Both are refused the same way.
        pytest.param({}, id="absent"),
    ],
)
def test_an_empty_query_is_refused(app, client, store, locks_taken, payload):
    project = _storyboarded(app, store)
    before = _project_witness(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search", data=payload, headers=_HX
    )

    assert response.status_code == 422
    assert _project_witness(store, project.id) == before
    assert locks_taken == []


def test_searching_an_unknown_scene_is_a_404(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s99/search", data={"query": "ice"}, headers=_HX
    )

    assert response.status_code == 404


def test_searching_an_unknown_project_is_a_404(client):
    response = client.post(
        "/projects/nope/scenes/s01/search", data={"query": "ice"}, headers=_HX
    )

    assert response.status_code == 404


# ---------------------------------------------------------- a spent free tier


def test_a_spent_quota_is_a_message_in_the_card_not_a_500(
    app, client, store, monkeypatch, locks_taken
):
    project = _storyboarded(app, store)
    before = _project_witness(store, project.id)

    def broke(self, **kwargs):
        raise QuotaExceeded(
            "pexels soft budget spent: 190/190 units in the last 3600s (per_hour)"
        )

    monkeypatch.setattr(mock_module.MockStock, "search", broke)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert response.status_code == 200
    card = _card_html(response.text, SCENE)
    assert 'data-scene-problem="true"' in card
    assert "soft budget spent" in card
    # Nothing was written, and nothing was locked to write it.
    assert _project_witness(store, project.id) == before
    assert locks_taken == []


def test_a_search_that_finds_nothing_is_a_message_too(app, client, store, monkeypatch):
    project = _storyboarded(app, store)
    monkeypatch.setattr(mock_module.MockStock, "search", lambda self, **kwargs: [])

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert response.status_code == 200
    assert 'data-scene-problem="true"' in _card_html(response.text, SCENE)
    assert store.load(project.id).scene_by_id(SCENE).visual.candidates == (
        project.scene_by_id(SCENE).visual.candidates
    )


# ------------------------------------------------------- the quota indicator


def test_the_storyboard_page_shows_remaining_headroom_per_provider(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    rows = dict(_QUOTA_ROW.findall(body))
    assert set(rows) == set(SOFT_BUDGETS)
    assert rows["pexels"] == str(SOFT_BUDGETS["pexels"].per_hour)


def test_the_quota_partial_reflects_what_has_been_spent(app, client, store, cache_dir):
    project = _storyboarded(app, store)
    tracker = QuotaTracker(cache_dir / QUOTA_FILENAME)
    tracker.record("pexels", 30)
    tracker.save()

    response = client.get(f"/projects/{project.id}/quota")

    assert response.status_code == 200
    assert "<!doctype" not in response.text.lower()
    rows = dict(_QUOTA_ROW.findall(response.text))
    assert rows["pexels"] == str(SOFT_BUDGETS["pexels"].per_hour - 30)


def test_rendering_the_indicator_never_writes_the_ledger(app, client, store, cache_dir):
    """`remaining` is a read. An indicator that booked a unit to draw itself would
    be the exact bug it exists to prevent."""
    project = _storyboarded(app, store)
    QuotaTracker(cache_dir / QUOTA_FILENAME).save()
    before = _witness(cache_dir / QUOTA_FILENAME)

    client.get(f"/projects/{project.id}/quota")
    client.get(f"/projects/{project.id}/storyboard")

    assert _witness(cache_dir / QUOTA_FILENAME) == before


def test_the_quota_partial_of_an_unknown_project_is_a_404(client):
    assert client.get("/projects/nope/quota").status_code == 404


def test_a_missing_ledger_shows_a_full_budget(app, client, store, cache_dir):
    project = _storyboarded(app, store)
    assert not (cache_dir / QUOTA_FILENAME).exists()

    rows = dict(_QUOTA_ROW.findall(client.get(f"/projects/{project.id}/quota").text))

    assert rows["gemini"] == str(SOFT_BUDGETS["gemini"].per_day)


# ------------------------------------------- the guarantee that makes it safe


def test_the_first_search_spends_exactly_one_request(app, client, store, monkeypatch, cache_dir):
    project = _storyboarded(app, store)
    seen = _use_pexels_stock(app, monkeypatch)

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )

    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].url.params["query"] == "polar bear on sea ice"
    ledger = json.loads((cache_dir / QUOTA_FILENAME).read_text())
    assert sum(units for _at, units in ledger["pexels"]) == 1
    saved = store.load(project.id).scene_by_id(SCENE).visual.candidates
    assert [ref.source_id for ref in saved] == ["900", "901", "902", "903"]


def test_repeating_a_search_costs_no_quota(
    app, client, store, monkeypatch, cache_dir, locks_taken
):
    """**The test this file exists for.**

    An identical search is served out of M1's `ResponseCache`, so it must issue
    no HTTP request at all — and therefore never reach `QuotaTracker.record`,
    leaving `quota.json` untouched down to its inode. The offers rebuilt from the
    cached page are field-for-field what is already stored, so the no-op guard
    also keeps the project file and the project lock out of it.
    """
    project = _storyboarded(app, store)
    seen = _use_pexels_stock(app, monkeypatch)
    query = {"query": "polar bear on sea ice"}

    client.post(f"/projects/{project.id}/scenes/{SCENE}/search", data=query, headers=_HX)
    after_first = len(seen)
    ledger_before = _witness(cache_dir / QUOTA_FILENAME)
    project_before = _project_witness(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search", data=query, headers=_HX
    )

    assert response.status_code == 200
    assert after_first == 1
    assert len(seen) == 1, "the repeat search went to the network"
    assert _witness(cache_dir / QUOTA_FILENAME) == ledger_before
    assert _project_witness(store, project.id) == project_before
    assert locks_taken == []


def test_a_different_query_does_spend_a_second_request(app, client, store, monkeypatch):
    """The counterweight: the cache is keyed on the request, not switched off."""
    project = _storyboarded(app, store)
    seen = _use_pexels_stock(app, monkeypatch)

    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "polar bear on sea ice"},
        headers=_HX,
    )
    client.post(
        f"/projects/{project.id}/scenes/{SCENE}/search",
        data={"query": "meltwater running off a glacier"},
        headers=_HX,
    )

    assert len(seen) == 2
