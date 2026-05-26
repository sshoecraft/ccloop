# ccloop

Run Claude Code in a relay loop. When a session ends — because it
finished a chunk of work, or filled its context — ccloop summarizes its
transcript and hands the work to a fresh session automatically, until the
task converges.

Built for long-running autonomous tasks (large refactors, codebase
audits, multi-file implementation work) that would otherwise overrun a
single session's context window and stall or degrade.

## How it works

```
ccloop "<task>"
   │
   ├─► creates .ccloop/runs/<run-id>/ (task.md, resume.md, sessions.log)
   │
   └─► loop:
         ├─► claude -p --session-id <new-uuid> --output-format stream-json
         │     │     (prompt = preamble + resume.md)
         │     ├─► output streams to your terminal live, raw saved to session-N.out
         │     ├─► Claude works; a PostToolUse guard hook may nudge it to wrap
         │     │     up as context fills (best-effort)
         │     └─► claude exits (chunk done, or context wall)
         │
         ├─► did Claude write DONE to resume.md? → exit 0
         ├─► otherwise summarize the transcript JSONL → new resume.md
         └─► loop
```

The handoff is driven by the **transcript** Claude Code already writes to
disk per session. ccloop extracts what was done, builds the next
session's prompt, and hands off. Claude never has to write a handoff
document; it just works, and optionally writes `DONE` when finished.

## Requirements

- `claude` CLI on `PATH`
- Python 3.9+
- No third-party Python dependencies (pure standard library)

## Install

ccloop is a Python package with a single `ccloop` console entry point. The
recommended install is an isolated venv kept **outside** the source tree,
with the command symlinked onto your `PATH`:

```sh
git clone <repo> /src/ccloop
python3 -m venv ~/.venvs/ccloop
~/.venvs/ccloop/bin/pip install -e /src/ccloop
ln -sf ~/.venvs/ccloop/bin/ccloop ~/.local/bin/ccloop   # ~/.local/bin on PATH
```

If you have [pipx](https://pipx.pypa.io), that's even simpler — it manages
the venv for you and puts `ccloop` on your PATH directly:

```sh
pipx install -e /src/ccloop
```

There is **no separate install step for the hooks**. On the first run of
a task, ccloop registers two entries in `~/.claude/settings.json`
automatically (absolute paths to this `ccloop` executable, so they resolve
regardless of Claude Code's `PATH`):

- **PostToolUse → `ccloop guard`** — friendly wrap-up nudge as context fills.
- **Stop → `ccloop keepgoing`** — blocks the model from stopping mid-task
  and re-feeds "continue until DONE." This is what prevents the common
  failure mode where a model emits a final text turn and just sits idle.

Both hooks self-gate on `CCLOOP_RUN_ID` and are no-ops in every session
that isn't a ccloop run. Pass `--no-hook` to skip registration.

`ccloop install` / `ccloop install --uninstall` are available if you want
to (un)register both hooks manually.

## Usage

```sh
cd /path/to/your/project
ccloop "refactor all auth code to use the new session API"
```

### Interactive vs. headless

ccloop picks a mode from whether it's attached to a terminal:

- **Interactive** (a TTY — you ran it in your terminal): launches the real
  Claude Code TUI. You see it, type in it, hit Escape, interrupt — exactly
  like running `claude` yourself. When you exit the session, ccloop asks
  whether to relaunch a fresh one carrying the summary forward. A
  background watcher reads your **exact** context % from the ccusage
  statusline cache and, if it crosses the hard threshold, auto-relays to a
  fresh session *before* you hit the context wall.
- **Headless** (no TTY — piped, redirected, cron, `nohup`, backgrounded):
  runs `claude -p` and streams parsed output (`→ Read PLAN.md / ok 553
  lines`). Fully autonomous; sessions hand off automatically. This is what
  you want for overnight / unattended runs.

```sh
ccloop "task"                  # in a terminal → interactive
ccloop "task" >run.log 2>&1 &  # backgrounded → headless autonomous
ccloop -i "task"               # force interactive
ccloop --headless "task"       # force headless even in a terminal
```

In **headless** mode, each session's output streams live and Ctrl-C
terminates the whole session process group and stops the loop. In
**interactive** mode the TUI owns the keyboard (Ctrl-C/Escape go to
Claude); to stop the loop, answer `n` at the relaunch prompt or write
`DONE`. Either way `resume.md` is preserved so you can `--resume-run <id>`.

### Signaling completion (this matters more than it sounds)

When the task is done, Claude (or you) writes `DONE` to the resume file:

```sh
echo DONE > "$CCLOOP_RESUME_FILE"   # from inside a session
```

`DONE` is the only way the model can legitimately exit. The Stop hook
treats any stop without `DONE` as "model got lazy" and re-feeds "keep
going." If the model writes `DONE` falsely, it will exit early — so the
preamble is explicit about only writing it when the task is verifiably
complete.

If you need to bound this (e.g. the model genuinely can't make progress
and you don't want infinite re-feeds), set `CCLOOP_MAX_CONTINUES=N`. The
keepgoing hook gives up after N re-feeds in the same session and lets the
model exit, after which ccloop's normal relay takes over.

### Inspecting, resuming, cleaning up

```sh
ccloop --list                       # list runs in current project
ccloop --resume-run <run-id>        # continue an aborted run
ccloop --prune                      # dry-run: show converged runs to delete
ccloop --prune --force              # actually delete converged runs
```

`--list` shows each run's ID, session count, status (`done` / `active` /
`empty` / `missing`), and the start of the original task. `--prune`
removes only runs with `DONE` (or empty) resume files. State lives under
`.ccloop/runs/<run-id>/`.

### Safety guards

ccloop will abort with a clear message rather than spin forever if:

- a session fails with **`Prompt is too long`** — the resume prompt has
  outgrown the model's context window; trim it or narrow the task, then
  `--resume-run`.
- **`CCLOOP_STUCK_LIMIT`** consecutive sessions make no progress (no
  transcript / no assistant turns).

By default there is **no** iteration cap and **no** session timeout — a
run continues until it converges, you interrupt it, or a guard trips. Set
`CCLOOP_MAX_ITERATIONS` / `CCLOOP_SESSION_TIMEOUT` if you want them.

### Configuration

| Env var | Default | What it does |
|---|---|---|
| `CCLOOP_MAX_ITERATIONS` | 0 (unlimited) | Hard cap on sessions per run; 0 disables |
| `CCLOOP_SESSION_TIMEOUT` | 0 (none) | SIGTERM a session after N seconds; 0 disables |
| `CCLOOP_STUCK_LIMIT` | 3 | Consecutive no-progress sessions before abort |
| `CCLOOP_THRESHOLD_SOFT` | 70 | Guard hook injection threshold % |
| `CCLOOP_THRESHOLD_HARD` | 85 | Interactive auto-relay threshold %; 0 disables the watcher |
| `CCLOOP_WATCH_INTERVAL` | 3 | Interactive context-poll interval (sec) |
| `CCLOOP_MAX_CONTINUES` | 0 (unlimited) | Cap on `keepgoing` re-feeds per session; 0 disables the cap |
| `CCLOOP_STOP_HOOK_BLOCK_CAP` | -1 (unlimited) | Overrides Claude Code's `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` (default 9) so the keepgoing hook can re-feed indefinitely; -1 means never cap |
| `CCLOOP_PERMISSION_MODE` | `bypassPermissions` | Passed to `claude --permission-mode` |
| `CCLOOP_MODEL` | (unset) | Passed to `claude --model` |
| `CCLOOP_EFFORT` | (unset) | Passed to `claude --effort` |
| `CCLOOP_SETTINGS` | (unset) | Passed to `claude --settings` |
| `CCLOOP_MAX_BUDGET_USD` | (unset) | Passed to `claude --max-budget-usd` |
| `CCLOOP_CLAUDE_BIN` | `claude` | Path or name of the claude binary to invoke |
| `CCLOOP_CLAUDE_EXTRA_ARGS` | (unset) | Extra args appended to every claude invocation (whitespace-split) |

ccloop always sets `DISABLE_AUTO_COMPACT=1` on the spawned session —
compaction mid-loop scrambles state in ways that defeat the resume
mechanism. Don't override this.

### Writing effective tasks

- **Be specific about completion criteria.** "Refactor the auth code" is
  ambiguous; "Change every call to `OldAuth.login` to use
  `NewAuth.session()`, then ensure tests pass" gives Claude a clear DONE
  condition it can verify and signal.
- **Decompose serial work into checkpoints.** "Process 15 files, one per
  turn" lets the handoff happen cleanly between files.
- **Trust the transcript.** You don't need to tell Claude to "leave a note
  for the next session" — ccloop extracts that from the transcript
  automatically.

### Multiple concurrent runs in one project

Run IDs and session IDs are independent UUIDs, so multiple `ccloop`
instances in the same project don't collide. State stays under each run's
own `.ccloop/runs/<run-id>/` directory.

## When NOT to use ccloop

- Tasks with no convergence criterion (use a ralph-style loop instead)
- Work requiring human-in-the-loop decisions mid-task

## Testing

```sh
~/.venvs/ccloop/bin/python -m pytest    # full suite (no real claude needed)
```

The suite drives the full relay loop against a fake stream-json `claude`
binary, so it runs offline and deterministically.

## See also

- `DESIGN.md` — architecture, hook contract, empirical findings
- `src/ccloop/runner.py` — the relay loop
- `src/ccloop/summarize.py` — transcript → resume.md transform
- `src/ccloop/guard.py` — the PostToolUse guard hook
- `src/ccloop/install.py` — settings.json hook registration
