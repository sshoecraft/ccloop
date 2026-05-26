import json

from ccloop import transcript as tx


def write_transcript(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def sample_events():
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "first turn"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la\npwd"}, "id": "a"},
        ], "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50,
                      "cache_read_input_tokens": 25, "output_tokens": 5}}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "second turn"},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/x/y.py"}, "id": "b"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y.py"}, "id": "c"},
        ], "usage": {"input_tokens": 200, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 100, "output_tokens": 9}}},
    ]


def test_context_tokens_uses_last_usage(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, sample_events())
    assert tx.context_tokens(t) == 300  # 200 + 0 + 100 from last assistant turn


def test_context_tokens_none_when_no_usage(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, [{"type": "user", "message": {"content": []}}])
    assert tx.context_tokens(t) is None


def test_files_edited_dedups_in_order(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, sample_events())
    assert tx.files_edited(t) == ["/x/y.py"]


def test_bash_commands_flatten_newlines(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, sample_events())
    assert tx.bash_commands(t) == ["ls -la pwd"]


def test_tool_counts(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, sample_events())
    assert tx.tool_counts(t) == {"Bash": 1, "Write": 1, "Edit": 1}


def test_assistant_turns(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, sample_events())
    assert tx.assistant_turns(t) == 2


def test_last_text(tmp_path):
    t = tmp_path / "t.jsonl"
    write_transcript(t, sample_events())
    assert "second turn" in tx.last_text(t)


def test_malformed_lines_tolerated(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("not json\n" + json.dumps(sample_events()[0]) + "\n{bad\n", encoding="utf-8")
    assert tx.context_tokens(t) == 175  # 100 + 50 + 25


def test_missing_file_returns_safely(tmp_path):
    assert tx.context_tokens(tmp_path / "nope.jsonl") is None
    assert tx.files_edited(tmp_path / "nope.jsonl") == []


def test_cwd_slug_replaces_specials():
    assert tx.cwd_slug("/a/b_c.d") == tx.cwd_slug("/a/b_c.d")
    slug = tx.cwd_slug("/Users/x/some_dir")
    assert "/" not in slug and "_" not in slug
