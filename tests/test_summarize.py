import json

from ccloop import summarize


def write_transcript(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_summary_has_expected_sections(tmp_path):
    t = tmp_path / "sid.jsonl"
    write_transcript(t, [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I refactored the auth module"},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/p/auth.py"}, "id": "1"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}, "id": "2"},
        ], "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "output_tokens": 20}}},
    ])
    out = summarize.summarize(t, "Refactor auth", run_id="RID", session_num=3)
    assert out.startswith("# Resume — run RID, after session 3")
    assert "## Original task" in out
    assert "Refactor auth" in out
    assert "Files written or edited" in out
    assert "/p/auth.py" in out
    assert "pytest -q" in out
    assert "I refactored the auth module" in out
    assert "sid" in out  # session id derived from filename


def test_summary_handles_empty_transcript(tmp_path):
    t = tmp_path / "sid.jsonl"
    t.write_text("", encoding="utf-8")
    out = summarize.summarize(t, "task", run_id="R", session_num=1)
    assert "_(none)_" in out
    assert "crashed mid-tool" in out
