# ccloop design

## Problem

Claude Code sessions have a finite context window — 200k tokens for
standard models, 1M for `claude-opus-4-7[1m]`. Long autonomous tasks
routinely exceed this. When they do, auto-compaction degrades the working
state or the session stalls outright.

The `/context-handoff` skill addresses this manually: a human notices
context is low, runs the skill, copies the handoff prompt into a new
session. That is incompatible with overnight or unattended runs.

ccloop automates the entire end-of-session → handoff → resume cycle.

## Approach

A Python CLI runs `claude -p` in a loop. Between sessions, work state is
reconstructed from the **per-session transcript JSONL** that Claude Code
already writes automatically. The wrapper produces the next session's
prompt by summarizing the just-ended session's transcript.

The wrapper handles handoff intelligence. Claude focuses purely on work —
it does not write its own resume file, does not maintain a ledger, does
not need to know it is being relayed. It optionally writes `DONE` when the
task is complete.

Auto-compaction is disabled (`DISABLE_AUTO_COMPACT=1`) so context
exhaustion produces a clean failure instead of silently scrambling session
state mid-loop. A failed session is recoverable from its transcript; a
compacted session is not.

### Why Python (0.2 rewrite)

The original implementation was bash + jq. It was rewritten as a
pip-installable Python package to get:

- **Native stream-json parsing** for live output (the bash `claude -p`
  buffered everything and showed nothing until the session ended).
- **Reliable Ctrl-C** via process groups (`start_new_session=True` +
  `os.killpg`) instead of fragile bash signal forwarding that left
  orphaned `claude` processes.
- **A single entry point** (`ccloop`) with the hook as a subcommand
  (`ccloop guard`), so settings.json registers an absolute path to the
  console script — no dangling source-relative paths, no symlink
  resolution problems.
- **No external dependencies** (jq gone; pure standard library).

The pre-0.2 shell version is archived at `legacy-bash.tar.gz`.

## Identifiers and paths

Each `ccloop` invocation generates a **run-id** (UUID). Each `claude`
session within that run gets a **session-id** (UUID, passed via
`--session-id`). This is what makes multiple concurrent ccloops in the
same project safe.

```
.ccloop/runs/<run-id>/
  ├─ task.md            ← original task (written once, never modified)
  ├─ resume.md          ← current handoff (rewritten between sessions)
  ├─ sessions.log       ← append-only list of session-ids for this run
  ├─ session-N.prompt   ← exact prompt fed to session N (debugging)
  ├─ session-N.out      ← raw stream-json output of session N
  ├─ hook-events.log    ← guard-hook firings (if any)
  └─ transcripts/       ← symlinks to each session's transcript JSONL
```

Transcript path is deterministic:
```
~/.claude/projects/<cwd with non-alphanumerics → '-'>/<session-id>.jsonl
```

The guard hook reads everything it needs from environment variables
exported by the wrapper.

| Env var | Purpose |
|---|---|
| `CCLOOP_RUN_ID` | Sentinel — hook no-ops if unset. Identifies the run. |
| `CCLOOP_SESSION_ID` | UUID passed to `--session-id` for this session |
| `CCLOOP_RESUME_FILE` | Absolute path to this run's `resume.md` |
| `CCLOOP_TRANSCRIPT_PATH` | Absolute path to this session's transcript |
| `CCLOOP_THRESHOLD_SOFT` | PostToolUse guard trigger % (default 70) |
| `CCLOOP_THRESHOLD_HARD` | Reserved safety-net % (default 85) |
| `DISABLE_AUTO_COMPACT` | Always `1` for spawned sessions |

## Components

All modules live under `src/ccloop/`.

### `cli.py` — entry point and dispatch

Parses args and dispatches. The `guard` no-op path (the common case when
the hook fires outside a ccloop run) returns **before importing any heavy
modules**, keeping per-tool-call overhead to bare interpreter startup.

Surface: bare task (run), `--resume-run <id>`, `--list`, `--prune
[--force]`, `install [--uninstall]`, `guard`, `--no-hook`, `--help`,
`--version`.

### `runner.py` — the relay loop

1. Create or resume `.ccloop/runs/<run-id>/`.
2. Self-register the guard hook (unless `--no-hook`).
3. Loop:
   - Converged? (`resume.md` missing / empty / starts with `DONE`) → exit.
   - Generate session-id; build prompt = preamble + `resume.md`.
   - Spawn `claude -p --session-id ... --output-format stream-json
     --verbose` in its own process group; stream output live; tee raw to
     `session-N.out`.
   - Symlink the transcript into `transcripts/`.
   - Did Claude write `DONE`? → exit.
   - Run death-loop guards (below).
   - Summarize transcript → `resume.md` (atomic).
4. Ctrl-C kills the whole session process group and preserves `resume.md`.

The prompt is passed as the final argv element. `resume.md` is bounded by
summarization, so ARG_MAX is not a concern; and an oversized resume
surfaces as the `Prompt is too long` guard rather than an argv error.

#### Modes: headless vs interactive

The mode is chosen from `sys.stdout.isatty()`/`sys.stdin.isatty()`
(overridable with `--headless` / `--interactive` / `-i`):

- **Headless** (no TTY): `claude -p --output-format stream-json --verbose`,
  stdout piped and parsed for live output, session in its own process
  group, SIGINT → killpg. Fully autonomous; the loop relays on each exit.
- **Interactive** (TTY): the real `claude` TUI with **inherited** std fds
  (no `-p`, no piping — piping would break the TUI). ccloop waits on the
  child and:
  - runs a **background watcher thread** that polls the ccusage cache for
    *this session's* exact `used_percentage` (via `usage.exact_pct`,
    session-gated) every `CCLOOP_WATCH_INTERVAL` seconds; at
    `CCLOOP_THRESHOLD_HARD` it terminates the session so the loop relays a
    fresh one *before* the context wall (`0` disables the watcher);
  - ignores SIGINT in the wrapper (the TUI owns Ctrl-C/Escape in raw
    mode) and restores terminal `termios` settings after the child exits,
    in case it was killed mid-raw-mode;
  - on a plain user exit (not a watcher relay), asks before relaunching so
    quitting the TUI doesn't trap you in an endless loop. To stop: answer
    `n`, or have Claude write `DONE`.

Detection works in *both* modes (the guard hook and, interactively, the
watcher both read the exact % from the ccusage cache). The real
difference is the exit trigger: headless `-p` exits on a tool-less turn
(auto-relay); the interactive TUI waits for you (you exit, or the watcher
relays at the hard threshold).

### `summarize.py` — transcript → resume.md

Pure data transform, no LLM call. Extracts from the transcript JSONL:
original task (carried verbatim), approximate context tokens at the last
turn, tool-use counts, files written/edited, the last 20 bash commands,
and the last assistant text turn. Emits markdown used as the next
session's prompt body.

### `transcript.py` — transcript helpers

Reproduces Claude Code's project-slug path, and parses the JSONL for
token usage, edited files, bash commands, tool counts, assistant text,
and assistant-turn count. Tolerant of malformed/truncated lines.

### `stream.py` — stream-json → readable output

Stateful translator fed one stdout line at a time. Pairs tool calls with
results, surfaces assistant text, prints a final cost/turns/duration
line, and sets `saw_prompt_too_long` when the context wall is hit.

### `usage.py` — ccusage cache reader

Shared by the guard hook and the interactive watcher. Reads
`$TMPDIR/ccusage-<uid>.json` (written by Claude Code's statusline every
turn) and exposes `exact_pct(session_id)` (the exact
`context_window.used_percentage`, returned only when the cache's
`session_id` matches) and `window_size()`. Single source of truth so the
exact percentage is never re-estimated.

### `keepgoing.py` — Stop hook (`ccloop keepgoing`)

Fires the instant the model ends a turn. Treats `$CCLOOP_RESUME_FILE`
as the source of truth: if it starts with `DONE` (or is missing/empty)
the stop is allowed; otherwise returns
``{"decision": "block", "reason": "...continue..."}`` which Claude Code
interprets as **block the stop and re-feed the reason** to the model.

This addresses the most common failure mode in long autonomous runs:
the model emits a final text turn and sits idle waiting for input. The
Stop hook intercepts that exact moment and forces the model to keep
working, regardless of what reason it stopped for.

Self-gates twice:

- ``CCLOOP_RUN_ID`` unset → no-op (registered globally; must be a no-op
  outside a ccloop run).
- Session id on stdin must match ``CCLOOP_SESSION_ID`` if both present →
  never blocks a foreign session's stop in a concurrent ccloop scenario.

Safety cap: ``CCLOOP_MAX_CONTINUES`` (default 0 = unlimited) bounds the
re-feed count per session via a counter file
(``<run-dir>/keepgoing-<sess>.count``). When exceeded the hook lets the
model exit and ccloop's normal relay takes over.

Honest caveat: a model can lie ("DONE without finishing") to escape the
hook. The preamble explicitly tells the model only to signal DONE when
the task is verifiably complete, but this is a behavioral guarantee, not
a mechanical one.

### `guard.py` — PostToolUse hook (`ccloop guard`)

Fires after every tool call. Steps:

1. No-op immediately if `CCLOOP_RUN_ID` is unset.
2. Read the hook JSON from stdin for `transcript_path` (falls back to
   `CCLOOP_TRANSCRIPT_PATH`).
3. Get context %:
   - **Exact**: the ccusage statusline cache
     (`$TMPDIR/ccusage-<uid>.json`) carries
     `context_window.used_percentage` — the number Claude Code itself
     computes — plus the real `context_window_size`. Used only when its
     `session_id` matches this session, so a concurrent session's cache is
     never trusted.
   - **Fallback** (statusline hasn't run for this session, e.g. headless
     `-p`): estimate from the transcript's summed usage tokens over the
     window size (from the cache if present, else
     `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, else 200000).
4. If % ≥ `CCLOOP_THRESHOLD_SOFT`, emit a collaborative wrap-up via
   `hookSpecificOutput.additionalContext`; else exit silently.

Using the cache's exact value matters: on a 1M-token model, estimating
against a hard-coded 200k window would report ~5× the true percentage and
fire the guard constantly. The `session_id` field is what makes the
per-UID cache safe to prefer.

The injection is **best-effort, not load-bearing** (see Empirical
findings). The transcript is recoverable whether or not it is honored.

### `install.py` — settings.json hook registration

Reads `~/.claude/settings.json` and merges **two** ccloop entries — one
per row in the ``HOOKS`` table at the top of the module:

| event         | subcommand   |
|---------------|--------------|
| PostToolUse   | `guard`      |
| Stop          | `keepgoing`  |

The command for each is the absolute path to this very `ccloop`
executable plus the subcommand. The file is backed up before writing and
written atomically. Idempotent and **self-healing**: a stale ccloop entry
(relocated executable, or the legacy `hooks/context-guard.sh` bash hook)
is removed and replaced with the current one across all events. Foreign
hooks are preserved.

`runner.py` calls this on the first run of a task; `ccloop install`
exposes it manually. There is no longer a separate installer binary.

## Convergence

ccloop exits 0 when, after a session ends:

- `resume.md` is missing, or
- `resume.md` is empty, or
- `resume.md` starts with `DONE` (case-insensitive, leading whitespace
  ignored).

Claude signals completion by writing `DONE` to `$CCLOOP_RESUME_FILE`; the
preamble in every session prompt tells it how.

## Death-loop guards

Because there is no iteration cap by default, the loop must detect
non-progress and abort rather than spin:

- **Prompt too long.** If a session's output contains `Prompt is too
  long`, the resume prompt has exceeded the model window. ccloop aborts
  with guidance to trim `resume.md` or narrow the task, then
  `--resume-run`. This cannot be auto-fixed safely (the partial transcript
  contains no new work).
- **Stuck.** A session is "no-progress" if it produced no transcript or
  zero assistant turns. After `CCLOOP_STUCK_LIMIT` (default 3) consecutive
  no-progress sessions, ccloop aborts.

Optional caps remain available: `CCLOOP_MAX_ITERATIONS` and
`CCLOOP_SESSION_TIMEOUT` (both default 0 = off). User Ctrl-C always works.

## Empirical findings

Established by direct testing (the local-model context-wall experiment and
earlier design probes):

1. **`claude -p` normal exit**: exits 0 when Claude produces an assistant
   turn with no `tool_use` blocks. There is no `--max-turns`;
   `--max-budget-usd` is an emergency cost cap. For a well-checkpointed
   task, sessions end this way at natural stopping points.

2. **The context wall is an error exit.** With `DISABLE_AUTO_COMPACT=1`,
   a prompt/context that exceeds the model window makes `claude -p` print
   `Prompt is too long` to stdout and exit **non-zero** — clean and
   detectable, not a hang. (Normally, auto-compact would instead compact
   and continue; ccloop disables that on purpose.)

3. **Large tool outputs are truncated.** Cat-ing four ~89k-token files
   grew context only to ~33k — Claude Code clips big tool results. So
   context grows slowly, mainly from accumulated turns, not from large
   reads. The wall is reached rarely in practice; sessions usually end by
   natural completion first.

4. **`--output-format stream-json --verbose`** emits events incrementally
   (assistant text, tool_use, tool_result, result) — the basis for live
   output. Plain `-p` text output buffers until the session ends.

5. **PostToolUse `additionalContext` is soft.** Claude sees it but
   finishes its current chunk first; commanding/threatening wording is
   rejected as a prompt-injection attempt. Wording is collaborative.

6. **`--session-id <uuid>`** yields a deterministic transcript path,
   enabling concurrent ccloops without collision.

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Hook injection ignored mid-loop | Transcript is recoverable regardless; wrapper synthesizes resume even from a failed session |
| Resume prompt exceeds model window | Detected (`Prompt is too long`) and aborted with guidance, not looped |
| Infinite no-progress loop | `CCLOOP_STUCK_LIMIT` aborts after N empty sessions; Ctrl-C always works |
| Claude keeps inventing work | Optional `CCLOOP_MAX_ITERATIONS` cap (off by default) |
| Single session hangs | Optional `CCLOOP_SESSION_TIMEOUT` watchdog (off by default) |
| Orphaned child processes on interrupt | Session runs in its own process group; SIGINT kills the group (SIGKILL on second Ctrl-C) |
| Permission prompts block headless mode | `--permission-mode bypassPermissions` (overridable) |
| Concurrent ccloops collide | Run-id-scoped state; session-id-scoped transcripts; hook gates on env vars |
| ccusage cache unavailable / wrong session | Hook falls back to a transcript-based estimate (per-session accurate) |
| Wrong context % on large-window models | Exact `used_percentage` + real `context_window_size` read from the ccusage cache instead of assuming 200k |
| Summarizer produces garbage | All transcripts preserved under `transcripts/`; manual recovery possible |
| Stale hook path after relocation | `install.py` self-heals on next run/install |

## Resolved decisions

- **Mode**: `-p` (headless print). Interactive mode can't be terminated by
  the loop; `-p` exits on a tool-less assistant turn or the context wall.
- **State channel**: per-session transcript JSONL (automatic, complete)
  rather than a Claude-maintained ledger.
- **Resume file location**: `.ccloop/runs/<run-id>/resume.md` (run-scoped,
  concurrent-safe).
- **Auto-compact**: disabled (`DISABLE_AUTO_COMPACT=1`).
- **Output**: stream-json parsed to live readable lines; raw saved per
  session.
- **Hook delivery**: two subcommands (`ccloop guard` for PostToolUse,
  `ccloop keepgoing` for Stop); registered as absolute paths;
  self-registered on first run; `--no-hook` to skip.
- **Idle-stop defense**: a Stop hook re-feeds the model until `DONE`,
  because models commonly stop mid-task and sit idle. The trade is that
  `DONE` becomes the only legitimate exit, so the wrapper's preamble must
  set the expectation explicitly.
- **Context % source**: exact `used_percentage` from the ccusage
  statusline cache, gated on `session_id`; transcript estimate only as a
  fallback. Do not assume a 200k window.
- **Injection wording**: collaborative, non-threatening; best-effort, not
  load-bearing.
- **Caps**: no iteration cap or session timeout by default; death-loop
  guards (`Prompt is too long`, stuck detection) provide the safety net.
- **Prompt delivery**: final argv element (resume is bounded; oversize
  surfaces as the context-wall guard).

## Testing

`tests/` is a pytest suite covering transcript parsing, the summarizer,
install/self-heal/uninstall (including legacy-bash migration), the guard
hook (gating, thresholds, custom window), the stream formatter, CLI
dispatch, and the full relay loop driven against a fake stream-json
`claude` binary (DONE convergence, `Prompt is too long` abort, stuck
abort, iteration cap, resume numbering). No real `claude` is required;
the suite runs offline and deterministically.

## Non-goals

- Replacing `/loop`, ralph-loop, or `/context-handoff` for their intended
  use cases. ccloop is for unattended long-running tasks where context
  exhaustion is the bottleneck.
- Multi-machine coordination. Single host only.
- Resuming arbitrary historical sessions outside a ccloop run.
