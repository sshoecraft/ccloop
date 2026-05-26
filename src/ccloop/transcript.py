"""Helpers for locating and reading Claude Code session transcripts.

Claude Code writes a per-session JSONL transcript to
``~/.claude/projects/<cwd-slug>/<session-id>.jsonl``. The slug is the
working directory with every character that is not alphanumeric or a dash
replaced by a dash. These helpers reproduce that path and extract the few
facts ccloop needs (token usage, tool calls, text turns).
"""

import json
import os
import re
from pathlib import Path


def cwd_slug(cwd=None):
    """Reproduce Claude Code's per-project directory name for ``cwd``."""
    real = os.path.realpath(cwd) if cwd else os.path.realpath(os.getcwd())
    return re.sub(r"[^A-Za-z0-9-]", "-", real)


def transcript_path(session_id, cwd=None):
    """Absolute path to the transcript JSONL for ``session_id`` under ``cwd``."""
    return Path.home() / ".claude" / "projects" / cwd_slug(cwd) / f"{session_id}.jsonl"


def iter_events(path):
    """Yield parsed JSON objects from a transcript, skipping bad lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _assistant_content(event):
    if event.get("type") != "assistant":
        return []
    return event.get("message", {}).get("content") or []


def context_tokens(path):
    """Total context tokens at the last assistant turn that reported usage.

    Sum of input + cache-creation + cache-read tokens, which is how Claude
    Code accounts for the live context window. Returns ``None`` if no usage
    data is present.
    """
    total = None
    for event in iter_events(path):
        if event.get("type") != "assistant":
            continue
        usage = event.get("message", {}).get("usage")
        if not usage:
            continue
        total = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
    return total


def last_text(path, limit=4000):
    """Concatenated assistant text turns, trimmed to the last ``limit`` chars."""
    chunks = []
    for event in iter_events(path):
        for block in _assistant_content(event):
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    text = "\n".join(chunks)
    return text[-limit:] if len(text) > limit else text


def files_edited(path):
    """Distinct file paths touched by Write/Edit/MultiEdit/NotebookEdit, in order."""
    seen = []
    edit_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    for event in iter_events(path):
        for block in _assistant_content(event):
            if block.get("type") != "tool_use" or block.get("name") not in edit_tools:
                continue
            inp = block.get("input") or {}
            fp = inp.get("file_path") or inp.get("notebook_path")
            if fp and fp not in seen:
                seen.append(fp)
    return seen


def bash_commands(path, last=20, width=160):
    """Last ``last`` Bash commands, newlines flattened, each clipped to ``width``."""
    cmds = []
    for event in iter_events(path):
        for block in _assistant_content(event):
            if block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            cmd = (block.get("input") or {}).get("command")
            if cmd:
                cmds.append(" ".join(cmd.split("\n"))[:width])
    return cmds[-last:]


def tool_counts(path):
    """Mapping of tool name -> call count across all assistant turns."""
    counts = {}
    for event in iter_events(path):
        for block in _assistant_content(event):
            if block.get("type") == "tool_use":
                name = block.get("name", "?")
                counts[name] = counts.get(name, 0) + 1
    return counts


def assistant_turns(path):
    """Number of assistant events in the transcript (a rough progress signal)."""
    return sum(1 for e in iter_events(path) if e.get("type") == "assistant")
