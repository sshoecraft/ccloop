import io
import json

import pytest

from ccloop import guard


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Point the ccusage cache lookup at an empty temp dir by default."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def write_transcript(path, tokens):
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x"}],
                     "usage": {"input_tokens": tokens,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}},
    }) + "\n", encoding="utf-8")


def write_cache(session_id, used_percentage, window=1000000):
    import os
    cache = os.path.join(os.environ["TMPDIR"], f"ccusage-{os.getuid()}.json")
    with open(cache, "w") as fh:
        json.dump({"session_id": session_id,
                   "context_window": {"used_percentage": used_percentage,
                                       "context_window_size": window}}, fh)


def run_guard(monkeypatch, stdin_obj):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stdin_obj)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    rc = guard.main([])
    return rc, out.getvalue()


def test_noop_without_run_id(monkeypatch):
    monkeypatch.delenv("CCLOOP_RUN_ID", raising=False)
    rc, out = run_guard(monkeypatch, {})
    assert rc == 0 and out == ""


def test_exact_pct_from_matching_cache(monkeypatch):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "sess-A")
    monkeypatch.setenv("CCLOOP_THRESHOLD_SOFT", "70")
    write_cache("sess-A", 88)  # exact 88% from Claude Code itself
    rc, out = run_guard(monkeypatch, {})
    payload = json.loads(out)
    assert "88%" in payload["hookSpecificOutput"]["additionalContext"]


def test_ignores_cache_from_other_session(monkeypatch, tmp_path):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "sess-A")
    monkeypatch.setenv("CCLOOP_THRESHOLD_SOFT", "70")
    write_cache("sess-OTHER", 99)  # a concurrent session's cache — must be ignored
    # No transcript → no estimate either → silent.
    rc, out = run_guard(monkeypatch, {})
    assert rc == 0 and out == ""


def test_estimate_uses_cache_window_size(monkeypatch, tmp_path):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "sess-A")
    monkeypatch.setenv("CCLOOP_THRESHOLD_SOFT", "50")
    # Cache is another session's, but its window size (1M) is still used for
    # the transcript estimate. 600k / 1M = 60% → fires at threshold 50.
    write_cache("sess-OTHER", 5, window=1000000)
    t = tmp_path / "t.jsonl"
    write_transcript(t, 600000)
    rc, out = run_guard(monkeypatch, {"transcript_path": str(t)})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_silent_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_THRESHOLD_SOFT", "70")
    t = tmp_path / "t.jsonl"
    write_transcript(t, 10000)  # 5% of default 200k, no cache
    rc, out = run_guard(monkeypatch, {"transcript_path": str(t)})
    assert rc == 0 and out == ""


def test_fallback_respects_env_window(monkeypatch, tmp_path):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_THRESHOLD_SOFT", "50")
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "20000")
    t = tmp_path / "t.jsonl"
    write_transcript(t, 15000)  # 75% of 20k (no cache present)
    rc, out = run_guard(monkeypatch, {"transcript_path": str(t)})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_handles_empty_stdin(monkeypatch):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert guard.main([]) == 0
    assert out.getvalue() == ""
