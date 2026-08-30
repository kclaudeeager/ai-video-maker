import time

from videomaker.cache import ResponseCache, StageCache, hash_inputs, stage_key


def test_hash_is_order_independent():
    assert hash_inputs(a=1, b="x") == hash_inputs(b="x", a=1)


def test_hash_changes_with_any_input():
    base = hash_inputs(text="hello", voice="af_heart", speed=1.0)
    assert base != hash_inputs(text="hello!", voice="af_heart", speed=1.0)
    assert base != hash_inputs(text="hello", voice="af_bella", speed=1.0)
    assert base != hash_inputs(text="hello", voice="af_heart", speed=1.1)


def test_hash_tolerates_float_noise():
    assert hash_inputs(d=1.0000000001) == hash_inputs(d=1.0)


def test_hash_is_stable_across_runs():
    # Guards against dict/set iteration order or repr() leaking in.
    assert hash_inputs(text="hello", n=3) == hash_inputs(text="hello", n=3)


def test_stage_cache_reports_missing_as_stale(tmp_path):
    cache = StageCache(tmp_path / "stages.json")
    assert cache.is_stale(stage_key("voice", "s01"), "abc") is True


def test_stage_cache_marks_and_persists(tmp_path):
    path = tmp_path / "stages.json"
    cache = StageCache(path)
    cache.mark(stage_key("voice", "s01"), "abc")
    cache.save()

    reloaded = StageCache(path)
    assert reloaded.is_stale(stage_key("voice", "s01"), "abc") is False
    assert reloaded.is_stale(stage_key("voice", "s01"), "different") is True


def test_invalidate_by_prefix_is_scoped(tmp_path):
    cache = StageCache(tmp_path / "stages.json")
    cache.mark(stage_key("voice", "s01"), "a")
    cache.mark(stage_key("voice", "s02"), "b")
    cache.mark(stage_key("align", "s01"), "c")
    cache.invalidate("voice:")
    assert cache.is_stale(stage_key("voice", "s01"), "a") is True
    assert cache.is_stale(stage_key("voice", "s02"), "b") is True
    assert cache.is_stale(stage_key("align", "s01"), "c") is False


def test_response_cache_round_trips(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.get("k1") is None
    cache.put("k1", {"scenes": [1, 2]})
    assert cache.get("k1") == {"scenes": [1, 2]}


def test_response_cache_expires_with_ttl(tmp_path, monkeypatch):
    cache = ResponseCache(tmp_path, ttl_days=7)
    cache.put("k1", {"v": 1})
    assert cache.get("k1") == {"v": 1}
    # Read the real clock before patching: afterwards a bare `time.time()` inside
    # the lambda would resolve to the lambda itself and recurse forever.
    later = time.time() + 8 * 86400
    monkeypatch.setattr(time, "time", lambda: later)
    assert cache.get("k1") is None


def test_response_cache_survives_corrupt_entry(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("k1", {"v": 1})
    # A truncated write must degrade to a miss, never crash the pipeline.
    for blob in tmp_path.rglob("*.json"):
        blob.write_text("{not json")
    assert cache.get("k1") is None
