import json

import pytest

from videomaker.project import ProjectStore, slugify


@pytest.fixture
def store(tmp_path) -> ProjectStore:
    return ProjectStore(tmp_path)


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("how ssds work", "how-ssds-work"),
        ("  Why the Ocean is BLUE!  ", "why-the-ocean-is-blue"),
        ("C++ & Rust: a comparison", "c-rust-a-comparison"),
        ("émoji 🎬 test", "emoji-test"),
        ("a" * 90, "a" * 60),
    ],
)
def test_slugify(topic, expected):
    assert slugify(topic) == expected


def test_create_makes_folder_layout(store):
    p = store.create("how ssds work", "tech_explainer")
    root = store.path_for(p.id)
    assert (root / "project.json").exists()
    for sub in ("scenes", "audio", "captions", "build", "output", "cache"):
        assert (root / sub).is_dir()


def test_create_disambiguates_collisions(store):
    a = store.create("how ssds work", "tech_explainer")
    b = store.create("how ssds work", "tech_explainer")
    assert a.id == "how-ssds-work"
    assert b.id == "how-ssds-work-2"


def test_save_then_load_round_trips(store):
    p = store.create("topic here", "tech_explainer")
    p.topic = "changed"
    store.save(p)
    assert store.load(p.id).topic == "changed"


def test_save_is_atomic_and_leaves_no_temp_file(store):
    p = store.create("topic here", "tech_explainer")
    store.save(p)
    root = store.path_for(p.id)
    assert list(root.glob("*.tmp")) == []
    json.loads((root / "project.json").read_text())  # valid JSON, not truncated


def test_load_unknown_id_raises(store):
    with pytest.raises(FileNotFoundError):
        store.load("does-not-exist")


def test_list_ids_sorted(store):
    store.create("b topic", "tech_explainer")
    store.create("a topic", "tech_explainer")
    assert store.list_ids() == ["a-topic", "b-topic"]


def test_lock_is_reentrant_safe_across_processes(store, tmp_path):
    p = store.create("topic here", "tech_explainer")
    with store.lock(p.id):
        assert (store.path_for(p.id) / ".lock").exists()
    # Lock is released, so a second acquisition succeeds immediately.
    with store.lock(p.id):
        pass


def test_scene_dir_is_created_on_demand(store):
    p = store.create("topic here", "tech_explainer")
    d = store.scene_dir(p, "s01")
    assert d.is_dir() and d.name == "s01"


# ------------------------------------------------- the voice decides the language
#
# M3 Task 21. The store is the one place both front ends build a `Project` from a
# voice id, so deriving here is what makes a voice/language mismatch impossible
# on the CLI as well as in the web form — there is nowhere else to get it wrong.


def test_create_derives_the_language_from_the_voice(store):
    assert store.create("como funcionan", "tech_explainer", voice="ef_dora").language == "es"
    assert store.create("comment ca marche", "tech_explainer", voice="ff_siwis").language == "fr"
    assert store.create("how ssds work", "tech_explainer", voice="bm_george").language == "en"


def test_create_keeps_the_default_language_for_an_unreadable_voice(store):
    """A voice id outside Kokoro's convention says nothing about the language."""
    project = store.create("how ssds work", "tech_explainer", voice="robot")

    assert project.language == "en"


def test_an_explicit_language_still_wins(store):
    """The derivation is a default, not a lock: a caller that knows, knows."""
    project = store.create("topic", "tech_explainer", voice="ef_dora", language="pt")

    assert project.language == "pt"


def test_the_derived_language_is_written_to_disk(store):
    project = store.create("como funcionan", "tech_explainer", voice="ef_dora")

    assert store.load(project.id).language == "es"
