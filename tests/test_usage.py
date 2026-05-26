import json
import os

import pytest

from ccloop import usage


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))


def write_cache(session_id, pct, window=1000000):
    cache = os.path.join(os.environ["TMPDIR"], f"ccusage-{os.getuid()}.json")
    with open(cache, "w") as fh:
        json.dump({"session_id": session_id,
                   "context_window": {"used_percentage": pct,
                                       "context_window_size": window}}, fh)


def test_no_cache_returns_none():
    assert usage.read_cache() is None
    assert usage.exact_pct("anything") is None
    assert usage.window_size() is None


def test_exact_pct_matches_session():
    write_cache("S1", 42)
    assert usage.exact_pct("S1") == 42


def test_exact_pct_rejects_other_session():
    write_cache("S1", 42)
    assert usage.exact_pct("S2") is None


def test_window_size():
    write_cache("S1", 42, window=200000)
    assert usage.window_size() == 200000


def test_corrupt_cache_safe():
    cache = os.path.join(os.environ["TMPDIR"], f"ccusage-{os.getuid()}.json")
    with open(cache, "w") as fh:
        fh.write("{not json")
    assert usage.read_cache() is None
    assert usage.exact_pct("S1") is None
