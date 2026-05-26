import json

from ccloop import stream


def test_tool_use_and_result_formatting():
    fmt = stream.StreamFormatter()
    out = fmt.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}, "id": "x"}]}}))
    assert out == ["→ Bash     ls -la"]
    out = fmt.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "a\nb\nc", "is_error": False}]}}))
    assert out == ["  ok 3 lines"]


def test_text_block_surfaced():
    fmt = stream.StreamFormatter()
    out = fmt.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "  hello there  "}]}}))
    assert out == ["hello there"]


def test_error_result_summary():
    fmt = stream.StreamFormatter()
    fmt.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "boom"}, "id": "y"}]}}))
    out = fmt.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "y", "content": "kaboom", "is_error": True}]}}))
    assert out == ["  ERROR: kaboom"]


def test_result_line_and_capture():
    fmt = stream.StreamFormatter()
    out = fmt.feed(json.dumps({"type": "result", "total_cost_usd": 0.1234,
                               "num_turns": 7, "duration_ms": 4500, "subtype": "success"}))
    assert out == ["Done 4.5s · 7 turns · $0.1234"]
    assert fmt.result == {"cost": 0.1234, "turns": 7, "duration": 4.5}


def test_prompt_too_long_flag_from_plain_line():
    fmt = stream.StreamFormatter()
    out = fmt.feed("Prompt is too long")
    assert out == ["Prompt is too long"]
    assert fmt.saw_prompt_too_long is True


def test_blank_lines_ignored():
    fmt = stream.StreamFormatter()
    assert fmt.feed("\n") == []
    assert fmt.feed("   ") == []
