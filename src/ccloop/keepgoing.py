"""Stop hook — keep the session going.

Modern Claude models often "stop" mid-task with no real reason — they
emit a final text turn and sit idle waiting for input. The Stop hook
fires the instant a turn ends, so it is the precise point at which we can
intervene: if the task isn't actually complete, return a JSON object that
**blocks the stop and re-feeds a continue message**, and the model keeps
working.

Convergence has two modes:

1. **No criteria** (legacy path). The model signals "actually done" by
   writing ``DONE`` to ``$CCLOOP_RESUME_FILE``. Hook trusts that.

2. **Criteria configured** (``<run-dir>/criteria.md`` exists and is
   non-empty). The DONE marker is ignored entirely. The hook re-feeds
   the criteria verbatim and asks the model the direct yes/no question:
   have you met them? If yes, write YES to ``<run-dir>/criteria-met``.
   The hook accepts the stop only when that marker file exists with YES
   as its first token. The criteria text being in the model's face at
   the moment of decision is the whole point — the model can no longer
   stop by writing DONE as a reflex; it has to confront the bar.

Self-gates:
- ``CCLOOP_RUN_ID`` unset → no-op (the hook is registered globally; it
  must do nothing in non-ccloop sessions).
- Session id from stdin must match ``CCLOOP_SESSION_ID`` if both present
  → never blocks a foreign session's stop in a concurrent ccloop scenario.

Safety cap: ``CCLOOP_MAX_CONTINUES`` (default 0 = unlimited) bounds the
number of times this hook will re-feed within a single session, so a
model that genuinely cannot make progress eventually gets to exit. The
counter is kept under the run dir (``<run-dir>/keepgoing-<sess>.count``).
"""

import json
import os
import sys
from pathlib import Path


CONTINUE_MSG = (
    "Continue the task. You stopped without signaling completion. The "
    "task is only complete when you have verifiably finished what was "
    "asked AND you have run:\n\n"
    "    echo DONE > \"$CCLOOP_RESUME_FILE\"\n\n"
    "Until then, keep working. Do not ask clarifying questions or wait "
    "for input — make a reasonable choice and proceed. If you are truly "
    "blocked on something you cannot resolve yourself, document the "
    "blocker in a brief text turn, write DONE to the resume file, and "
    "stop; the next loop iteration (or the user) will pick it up."
)


CRITERIA_MSG_TEMPLATE = (
    "HAVE YOU MET THE CRITERIA?\n\n"
    "<BEGIN CRITERIA>\n"
    "{criteria}\n"
    "<END CRITERIA>\n\n"
    "IF YES: write YES to the marker file and stop:\n\n"
    "    echo YES > \"{marker}\"\n\n"
    "IF NO: continue working. *** DO NOT CONCERN YOURSELF WITH "
    "CONTEXT OR SESSION LIMITS *** Do not stop. Do not ask clarifying "
    "questions. Make reasonable choices and keep going until the "
    "criteria above are actually met."
)


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


def _first_token(text):
    if not text or not text.strip():
        return ""
    return text.lstrip().split()[0]


def _is_done_legacy(resume_file):
    """Legacy DONE-in-resume-file check (used when no criteria configured)."""
    if not resume_file:
        return False
    p = Path(resume_file)
    if not p.exists():
        return True
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.strip():
        return True
    return _first_token(text).upper().rstrip(":")[:4] == "DONE"


def _run_dir(resume_file):
    if not resume_file:
        return None
    d = Path(resume_file).parent
    return d if d.is_dir() else None


def _criteria_text(run_dir):
    """Non-empty contents of <run-dir>/criteria.md, or None."""
    if run_dir is None:
        return None
    p = run_dir / "criteria.md"
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None  # empty criteria.md = explicit opt-out
    return text.strip()


def _criteria_met(run_dir):
    """True if <run-dir>/criteria-met exists with YES as its first token."""
    if run_dir is None:
        return False
    p = run_dir / "criteria-met"
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _first_token(text).upper().rstrip(":") == "YES"


def _bump_counter(run_dir, session_id):
    if run_dir is None or not session_id:
        return 0
    counter = run_dir / f"keepgoing-{session_id}.count"
    try:
        n = int(counter.read_text().strip()) if counter.exists() else 0
    except (OSError, ValueError):
        n = 0
    n += 1
    try:
        counter.write_text(str(n))
    except OSError:
        pass
    return n


def _emit_block(reason, n):
    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": reason,
        "systemMessage": f"ccloop keepgoing — continue until done (re-fed #{n})",
    }) + "\n")


def main(argv=None):
    if not os.environ.get("CCLOOP_RUN_ID"):
        return 0

    hook_input = _read_stdin_json()
    own_sid = os.environ.get("CCLOOP_SESSION_ID")
    hook_sid = hook_input.get("session_id")
    if own_sid and hook_sid and own_sid != hook_sid:
        return 0

    resume_file = os.environ.get("CCLOOP_RESUME_FILE")
    run_dir = _run_dir(resume_file)
    criteria = _criteria_text(run_dir)

    try:
        cap = int(os.environ.get("CCLOOP_MAX_CONTINUES") or 0)
    except ValueError:
        cap = 0

    if criteria is None:
        # Legacy path: trust the DONE marker in resume.md.
        if _is_done_legacy(resume_file):
            return 0
        n = _bump_counter(run_dir, own_sid)
        if cap > 0 and n > cap:
            return 0
        _emit_block(CONTINUE_MSG, n)
        return 0

    # Criteria path: stop only when criteria-met marker says YES.
    if _criteria_met(run_dir):
        return 0

    n = _bump_counter(run_dir, own_sid)
    if cap > 0 and n > cap:
        # Safety net so a stuck run can eventually escape.
        return 0

    marker = str(run_dir / "criteria-met")
    _emit_block(CRITERIA_MSG_TEMPLATE.format(criteria=criteria, marker=marker), n)
    return 0
