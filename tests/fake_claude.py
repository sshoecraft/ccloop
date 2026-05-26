#!/usr/bin/env python3
"""A stand-in for the ``claude`` binary used by the runner integration tests.

It honors the env vars ccloop sets (CCLOOP_TRANSCRIPT_PATH,
CCLOOP_RESUME_FILE) and is steered by extra test-only env vars:

  FAKE_MODE        work (default) | toolong | noprogress
  FAKE_COUNTER     path to an invocation-counter file
  FAKE_DONE_AFTER  in 'work' mode, write DONE to the resume file once the
                   invocation count reaches this value (converges the loop)
"""

import json
import os
import sys
from pathlib import Path


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def write_transcript(path, assistant_turns=1):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for _ in range(assistant_turns):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "did some work"},
                    {"type": "tool_use", "name": "Write",
                     "input": {"file_path": "/tmp/example.py"}, "id": "t1"},
                ],
                "usage": {
                    "input_tokens": 1200,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 15,
                },
            },
        }))
        lines.append(json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "wrote file", "is_error": False},
            ]},
        }))
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main():
    mode = os.environ.get("FAKE_MODE", "work")
    transcript = os.environ.get("CCLOOP_TRANSCRIPT_PATH")
    resume = os.environ.get("CCLOOP_RESUME_FILE")

    if mode == "toolong":
        sys.stdout.write("Prompt is too long\n")
        sys.stdout.flush()
        return 1

    if mode == "sleep":
        # Simulate an interactive session that writes a transcript then waits
        # (until the watcher terminates it). Honors SIGTERM by default.
        if transcript:
            write_transcript(transcript, assistant_turns=1)
        import time as _t
        _t.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
        return 0

    if mode == "noprogress":
        emit({"type": "system", "subtype": "init"})
        if transcript:
            write_transcript(transcript, assistant_turns=0)
        emit({"type": "result", "total_cost_usd": 0.0,
              "num_turns": 0, "duration_ms": 5, "subtype": "success"})
        return 0

    # mode == "work"
    count = 1
    counter = os.environ.get("FAKE_COUNTER")
    if counter:
        try:
            count = int(Path(counter).read_text()) + 1
        except (OSError, ValueError):
            count = 1
        Path(counter).write_text(str(count))

    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"working (invocation {count})"},
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": "/tmp/example.py"}, "id": "t1"},
    ]}})
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "wrote file", "is_error": False},
    ]}})

    if transcript:
        write_transcript(transcript, assistant_turns=1)

    done_after = os.environ.get("FAKE_DONE_AFTER")
    if done_after and resume and count >= int(done_after):
        Path(resume).write_text("DONE\n", encoding="utf-8")

    emit({"type": "result", "total_cost_usd": 0.01,
          "num_turns": 1, "duration_ms": 1234, "subtype": "success"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
