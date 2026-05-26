"""Transcript JSONL -> resume.md transform.

Pure data transform, no LLM calls. ccloop runs this between sessions to
produce the next session's prompt from the prior session's transcript.
"""

import os

from . import transcript as tx


def summarize(transcript_file, task, run_id="unknown", session_num="?"):
    """Return a markdown resume document built from a session transcript.

    ``task`` is the original task text. The output mirrors the original
    bash summarizer: original task, previous-session metadata, files
    edited, recent bash commands, last text turn, and a continue note.
    """
    session_id = os.path.basename(str(transcript_file))
    if session_id.endswith(".jsonl"):
        session_id = session_id[: -len(".jsonl")]

    ctx = tx.context_tokens(transcript_file)
    ctx_str = str(ctx) if ctx is not None else "unknown"

    counts = tx.tool_counts(transcript_file)
    if counts:
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        tools_str = " ".join(f"{name}×{n}" for name, n in ordered)
    else:
        tools_str = "none"

    files = tx.files_edited(transcript_file)
    if files:
        files_block = "\n".join(f"- {f}" for f in files)
    else:
        files_block = "_(none)_"

    cmds = tx.bash_commands(transcript_file)
    if cmds:
        cmds_block = "\n".join(f"    {c}" for c in cmds)
    else:
        cmds_block = "_(none)_"

    text = tx.last_text(transcript_file)
    if text.strip():
        text_block = text
    else:
        text_block = "_(no text turn — session may have crashed mid-tool)_"

    return f"""# Resume — run {run_id}, after session {session_num}

## Original task

{task}

## Previous session

- session-id: `{session_id}`
- transcript: `{transcript_file}`
- approx context at last assistant turn: {ctx_str} tokens
- tools used: {tools_str}

## Files written or edited in the previous session

{files_block}

## Last 20 bash commands (truncated to 160 chars each)

{cmds_block}

## Last text from previous session

{text_block}

## Continue

Continue the original task from where the previous session stopped. The
previous session's transcript is at the path noted above — you may Read
it if you need full detail on what was done. (Loop mechanics and how to
signal DONE are in the wrapper preamble above this summary.)
"""
