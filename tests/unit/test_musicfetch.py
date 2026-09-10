"""Fetching music, and the licence gate that decides what may land.

The gate is stricter than "free", and that is the whole design:
**NonCommercial** forbids the monetised use this tool exists for, and
**NoDerivatives** does not cover a bed mixed under narration and ducked against
it. `docs/audio-design.md` argues the project should never be the cause of a
Content ID claim; refusing what cannot be credited is the cheap half of that.

Every request goes through `httpx.MockTransport`. Nothing here reaches the
network, and nothing here writes outside `music_dir`.
"""

import json

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from videomaker.audio import LIBRARY_FILENAME
from videomaker.cli import app
from videomaker.config import Settings, load_settings
from videomaker.musicfetch import (
    ALLOWED_LICENCES,
    OPENVERSE,
    Candidate,
    MusicSource,
    Refused,
    candidate_from,
    check_licence,
    configured_sources,
    fetch,
    record,
    relpath_for,
    search,
)

HEADER = "# The licence record.\n#\n# Every comment here has to survive.\n"


def result(**overrides) -> dict:
    return {
        "title": "Calm Piano",
        "creator": "Someone",
        "license": "by",
        "license_version": "4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": '"Calm Piano" by Someone is licensed under CC BY 4.0.',
        "foreign_landing_url": "https://freesound.org/people/Someone/sounds/1/",
        "url": "https://cdn.invalid/calm-piano.mp3",
        "filetype": "mp3",
        "duration": 60_000,
        **overrides,
    }


@pytest.fixture
def settings(tmp_path) -> Settings:
    library = tmp_path / "assets" / "music"
    library.mkdir(parents=True)
    (library / LIBRARY_FILENAME).write_text(HEADER)
    return Settings(workspace_dir=tmp_path / "workspace", music_dir=library, sfx_dir=tmp_path / "sfx")


@pytest.fixture
def catalogue():
    """A client answering with whatever results the test hands it."""

    def build(results, *, audio: bytes = b"ID3fake-audio-bytes"):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "cdn.invalid":
                return httpx.Response(200, content=audio)
            return httpx.Response(200, content=json.dumps({"results": results}).encode())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        client.seen = seen
        return client

    return build


# ------------------------------------------------------------------ the gate


@pytest.mark.parametrize("licence", sorted(ALLOWED_LICENCES))
def test_the_licences_a_monetised_video_can_carry_are_accepted(licence):
    check_licence(licence)


@pytest.mark.parametrize(
    ("licence", "because"),
    [
        ("by-nc", "NonCommercial"),
        ("by-nc-sa", "NonCommercial"),
        ("nc-sampling+", "NonCommercial"),
        ("by-nd", "NoDerivatives"),
        ("by-nc-nd", "NonCommercial"),
        ("sampling+", "sampling"),
    ],
)
def test_a_licence_that_forbids_the_use_is_refused_with_the_reason(licence, because):
    with pytest.raises(Refused, match=because):
        check_licence(licence)


def test_an_unknown_or_absent_licence_is_refused(): 
    with pytest.raises(Refused, match="nothing to stand behind"):
        check_licence("")
    with pytest.raises(Refused, match="not one this library accepts"):
        check_licence("all-rights-reserved")


def test_there_is_no_way_to_force_one_in():
    """The text importer has no `--force` and neither does this."""
    runner = CliRunner()
    assert "--force" not in runner.invoke(app, ["music", "fetch", "--help"]).output


# --------------------------------------------------------------- what qualifies


def test_a_well_formed_result_becomes_a_candidate():
    candidate = candidate_from(result(), OPENVERSE)

    assert candidate is not None
    assert candidate.title == "Calm Piano"
    assert candidate.licence == "by"
    assert candidate.attribution.startswith('"Calm Piano"')
    assert candidate.filename() == "calm-piano.mp3"
    assert candidate.duration_s == 60


@pytest.mark.parametrize(
    "broken",
    [
        {"attribution": ""},
        {"license": ""},
        {"foreign_landing_url": ""},
        {"url": ""},
        {"license": "by-nc"},
        {"filetype": "mp4"},
        {"duration": 900},
        {"duration": 3_600_000},
    ],
)
def test_a_result_that_cannot_be_credited_or_used_is_not_a_candidate(broken):
    assert candidate_from(result(**broken), OPENVERSE) is None


def test_a_title_that_is_not_a_filename_still_becomes_one():
    candidate = candidate_from(result(title="Curious Ambience.wav / take 2!"), OPENVERSE)

    assert candidate.filename() == "curious-ambience-wav-take-2.mp3"


# ------------------------------------------------------------------- searching


def test_the_search_asks_for_licences_it_would_accept(settings, catalogue):
    client = catalogue([result()])

    found = search("calm piano", settings=settings, client=client)

    (request,) = client.seen
    assert request.url.params["q"] == "calm piano"
    assert set(request.url.params["license"].split(",")) <= ALLOWED_LICENCES
    assert [c.title for c in found] == ["Calm Piano"]


def test_the_search_drops_what_the_gate_would_refuse(settings, catalogue):
    client = catalogue([result(license="by-nc"), result(title="Fine"), result(attribution="")])

    found = search("calm", settings=settings, client=client)

    assert [c.title for c in found] == ["Fine"]


def test_the_search_stops_at_the_limit(settings, catalogue):
    client = catalogue([result(title=f"Track {n}") for n in range(20)])

    assert len(search("calm", settings=settings, limit=3, client=client)) == 3


def test_a_source_can_be_configured_in_yaml(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "music_sources:\n"
        "  - name: elsewhere\n"
        "    endpoint: https://elsewhere.invalid/search\n"
        "    query_param: term\n"
    )
    configured = configured_sources(load_settings(tmp_path / "config.yaml"))

    assert [source.name for source in configured] == ["elsewhere"]
    assert configured[0].query_param == "term"


def test_with_nothing_configured_the_blessed_default_is_used(settings):
    (source,) = configured_sources(settings)

    assert source.name == "openverse"
    assert source is OPENVERSE or source == OPENVERSE


# ------------------------------------------------------------------- fetching


def test_fetching_writes_the_track_and_its_credit_line(settings, catalogue):
    client = catalogue([result()])
    (candidate,) = search("calm", settings=settings, client=client)

    path = fetch(candidate, mood="calm", settings=settings, client=client)

    assert path == settings.music_dir / "calm" / "calm-piano.mp3"
    assert path.read_bytes() == b"ID3fake-audio-bytes"
    library = yaml.safe_load((settings.music_dir / LIBRARY_FILENAME).read_text())
    entry = library["tracks"][relpath_for("calm", candidate)]
    assert entry["attribution"] == candidate.attribution
    assert entry["licence"] == "by"
    assert entry["source_url"].startswith("https://freesound.org/")


def test_fetching_checks_the_licence_again_rather_than_trusting_the_search(settings, catalogue):
    """`fetch` is callable from anywhere, and a gate that guards one path is not one."""
    client = catalogue([])
    forbidden = Candidate(
        title="Nope",
        licence="by-nc",
        attribution="x",
        source_url="https://example.invalid/1",
        download_url="https://cdn.invalid/nope.mp3",
    )

    with pytest.raises(Refused, match="NonCommercial"):
        fetch(forbidden, mood="calm", settings=settings, client=client)

    assert not (settings.music_dir / "calm").exists(), "nothing was written"
    assert client.seen == [], "and nothing was even requested"


def test_the_library_header_survives_being_written_to(settings):
    candidate = candidate_from(result(), OPENVERSE)

    record(candidate, relpath_for("calm", candidate), settings)

    text = (settings.music_dir / LIBRARY_FILENAME).read_text()
    assert "# Every comment here has to survive." in text
    assert yaml.safe_load(text)["tracks"]


def test_a_second_track_joins_the_first_rather_than_replacing_it(settings):
    one = candidate_from(result(title="One"), OPENVERSE)
    two = candidate_from(result(title="Two"), OPENVERSE)

    record(one, relpath_for("calm", one), settings)
    record(two, relpath_for("upbeat", two), settings)

    tracks = yaml.safe_load((settings.music_dir / LIBRARY_FILENAME).read_text())["tracks"]
    assert set(tracks) == {"music/calm/one.mp3", "music/upbeat/two.mp3"}


def test_a_hand_written_entry_is_kept(settings):
    (settings.music_dir / LIBRARY_FILENAME).write_text(
        HEADER + "\ntracks:\n  music/calm/mine.mp3:\n    title: Mine\n    licence: CC0-1.0\n"
    )
    candidate = candidate_from(result(), OPENVERSE)

    record(candidate, relpath_for("calm", candidate), settings)

    tracks = yaml.safe_load((settings.music_dir / LIBRARY_FILENAME).read_text())["tracks"]
    assert tracks["music/calm/mine.mp3"]["title"] == "Mine"
    assert "music/calm/calm-piano.mp3" in tracks


def test_a_source_that_is_not_a_music_source_is_refused():
    with pytest.raises(ValueError):
        MusicSource.model_validate({"endpoint": "https://x.invalid"})


# ------------------------------------------------ the example ships fetch-ready


def test_the_shipped_example_configures_no_source_and_documents_the_gate():
    """A fresh clone uses the built-in default; the example is there to be read."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    settings = load_settings(repo / "config.example.yaml")
    assert settings.music_sources == []

    text = (repo / "config.example.yaml").read_text()
    assert "# music_sources:" in text, "commented out, like voice_providers"
    assert "NonCommercial" in text and "NoDerivatives" in text


def test_the_commented_example_validates_as_a_source(commented_example):
    """A worked example that does not validate is worse than none: somebody copies it."""
    (entry,) = commented_example("music_sources")["music_sources"]

    source = MusicSource.model_validate(entry)
    assert source.name == "openverse"
    assert set(source.params["license"].split(",")) <= ALLOWED_LICENCES


def test_the_example_names_every_field_of_the_model(commented_example):
    (entry,) = commented_example("music_sources")["music_sources"]

    assert set(entry) == set(MusicSource.model_fields)
