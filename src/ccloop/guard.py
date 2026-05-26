"""PostToolUse hook — context guard.

Fires after every tool call inside a ccloop run. If context usage is at or
above ``CCLOOP_THRESHOLD_SOFT`` (default 70%), it injects a friendly
wrap-up suggestion via ``additionalContext`` so the next assistant turn
sees it. It is a no-op outside a ccloop run (``CCLOOP_RUN_ID`` unset).

Context-usage source priority:
  1. The ccusage statusline cache (``$TMPDIR/ccusage-<uid>.json``), which
     Claude Code's statusline writes every turn. It carries the EXACT
     ``context_window.used_percentage`` Claude Code itself computes, plus
     the real ``context_window_size``. Used only when its ``session_id``
     matches this session, so a concurrent session's cache is never
     trusted.
  2. Fallback estimate from the per-session transcript JSONL: summed
     usage tokens over the window size (taken from the cache if present,
     else ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``, else 200000). Used when the
     statusline hasn't run for this session (e.g. headless -p).
"""

import json
import os
import sys
import time
from pathlib import Path

from . import transcript as tx
from . import usage


def _read_stdin_json():
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _pct_estimate(transcript_path, cache):
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    tokens = tx.context_tokens(transcript_path)
    if not tokens or tokens <= 0:
        return None
    window = usage.window_size(cache)
    if not window:
        env = os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
        window = int(env) if env else 200000
    return (tokens * 100.0) / window


WRAP_UP = (
    "Heads up — context window is now at {pct}% (threshold {thr}%). "
    "Good stopping point: please wrap up the current sub-step, then end "
    "with a brief text summary. Remaining work will continue in a fresh "
    "session with full transcript access — no need to write any handoff "
    "document, the loop wrapper produces it from your transcript "
    "automatically."
)


def main(argv=None):
    if not os.environ.get("CCLOOP_RUN_ID"):
        return 0

    threshold = int(os.environ.get("CCLOOP_THRESHOLD_SOFT") or 70)

    hook_input = _read_stdin_json()
    transcript_path = hook_input.get("transcript_path") or os.environ.get(
        "CCLOOP_TRANSCRIPT_PATH"
    )

    cache = usage.read_cache()
    pct = usage.exact_pct(os.environ.get("CCLOOP_SESSION_ID"), cache)
    if pct is None:
        pct = _pct_estimate(transcript_path, cache)
    if pct is None:
        return 0

    pct_int = round(float(pct))
    if pct_int < threshold:
        return 0

    resume_file = os.environ.get("CCLOOP_RESUME_FILE")
    if resume_file:
        run_dir = Path(resume_file).parent
        if run_dir.is_dir():
            try:
                with open(run_dir / "hook-events.log", "a", encoding="utf-8") as fh:
                    fh.write(
                        "%s\tfired\t%s%%\t%s\n"
                        % (
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            pct_int,
                            os.environ.get("CCLOOP_SESSION_ID", "unknown"),
                        )
                    )
            except OSError:
                pass

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": WRAP_UP.format(pct=pct_int, thr=threshold),
        }
    }
    sys.stdout.write(json.dumps(out) + "\n")
    return 0
