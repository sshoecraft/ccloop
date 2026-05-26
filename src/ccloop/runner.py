"""The relay loop.

Spawns ``claude -p`` repeatedly, streams each session's output live,
summarizes the transcript into the next session's prompt, and stops when
the resume file converges (missing / empty / DONE), the user interrupts,
or a death-loop guard trips.
"""

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import install, stream, summarize, usage
from . import transcript as tx


def log(msg):
    sys.stderr.write(f"[ccloop] {msg}\n")
    sys.stderr.flush()


class CcloopError(Exception):
    """Fatal error that should abort the run with a message."""


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _config():
    return {
        "max_iterations": _env_int("CCLOOP_MAX_ITERATIONS", 0),
        "session_timeout": _env_int("CCLOOP_SESSION_TIMEOUT", 0),
        "threshold_soft": str(_env_int("CCLOOP_THRESHOLD_SOFT", 70)),
        "threshold_hard": str(_env_int("CCLOOP_THRESHOLD_HARD", 85)),
        "permission_mode": os.environ.get("CCLOOP_PERMISSION_MODE", "bypassPermissions"),
        "model": os.environ.get("CCLOOP_MODEL", ""),
        "effort": os.environ.get("CCLOOP_EFFORT", ""),
        "settings": os.environ.get("CCLOOP_SETTINGS", ""),
        "max_budget": os.environ.get("CCLOOP_MAX_BUDGET_USD", ""),
        "claude_bin": os.environ.get("CCLOOP_CLAUDE_BIN", "claude") or "claude",
        "extra_args": os.environ.get("CCLOOP_CLAUDE_EXTRA_ARGS", ""),
        "stuck_limit": _env_int("CCLOOP_STUCK_LIMIT", 3),
        "threshold_hard_int": _env_int("CCLOOP_THRESHOLD_HARD", 85),
        "watch_interval": _env_int("CCLOOP_WATCH_INTERVAL", 3),
    }


def _gen_uuid():
    return str(uuid.uuid4())


def runs_dir(project_root=None):
    root = Path(project_root) if project_root else Path(os.getcwd()).resolve()
    return root / ".ccloop" / "runs"


def _first_token(text):
    if not text or not text.strip():
        return ""
    return text.lstrip().split()[0]


def _criteria_path(run_dir):
    return Path(run_dir) / "criteria.md"


def _criteria_met_path(run_dir):
    return Path(run_dir) / "criteria-met"


def _has_criteria(run_dir):
    p = _criteria_path(run_dir)
    if not p.is_file():
        return False
    try:
        return bool(p.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def converged_reason(resume_file):
    """Reason string if the run signals convergence, else None.

    Two convergence modes, picked by whether ``<run-dir>/criteria.md``
    exists and is non-empty:

    - Criteria mode: ``<run-dir>/criteria-met`` first token == YES.
    - Legacy mode: DONE in the resume file (missing / empty also count).
    """
    p = Path(resume_file)
    run_dir = p.parent

    if _has_criteria(run_dir):
        marker = _criteria_met_path(run_dir)
        if marker.is_file():
            try:
                tok = _first_token(marker.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                tok = ""
            if tok.upper().rstrip(":") == "YES":
                return "criteria-met=YES"
        return None

    if not p.exists():
        return "missing resume file"
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "missing resume file"
    if not txt.strip():
        return "empty resume file"
    if _first_token(txt).upper().rstrip(":")[:4] == "DONE":
        return "DONE marker"
    return None


PREAMBLE_LEGACY = """You are running inside ccloop, a relay-loop wrapper that hands work between
fresh Claude Code sessions as context fills. This is session {iter} of the
current run.

IMPORTANT — how to stop:

The only legitimate way to end the task is to run this Bash command once
the task is verifiably complete:

    echo DONE > "$CCLOOP_RESUME_FILE"

A Stop hook is active: if you try to end a turn without having written
DONE first, it will block the stop and re-feed "keep going" so you
continue working. This is intentional — it prevents the common failure
mode where a session stops mid-task and sits idle.

Therefore:

- Do NOT write DONE unless the task is actually finished and verified.
  Lying to escape the loop just wastes work; the wrapper trusts you.
- Do NOT pause to ask clarifying questions. Make a reasonable choice
  and proceed; the wrapper has no human to answer them.
- If you are genuinely blocked on something you cannot resolve, document
  the blocker in a brief text turn, write DONE, and stop — the next
  iteration or the user will pick it up.

If context starts filling before the task is done, just stop normally —
the wrapper will summarize your transcript and hand off to a fresh
session automatically.

---

"""


PREAMBLE_CRITERIA = """You are running inside ccloop, a relay-loop wrapper that hands work between
fresh Claude Code sessions as context fills. This is session {iter} of the
current run.

IMPORTANT — this run has explicit success criteria:

<BEGIN CRITERIA>
{criteria}
<END CRITERIA>

These criteria are your ONLY exit condition.

YOU HAVE UNLIMITED CONTEXT AND UNLIMITED SESSIONS.

ccloop is wrapping your session. When your context fills, ccloop will:
  1. summarize this session's transcript into a resume file
  2. spawn a fresh Claude Code session with that summary as its prompt
  3. pass through the same criteria so the new session continues the work
The new session inherits the project state, the criteria, and a digest
of what you've already learned and tried. You can take as many sessions
as the problem needs. There is no session cap, no turn cap, no time cap.

Because of this, NEVER:

- propose to "kill the wrapper", "end the session", or "exit so a fresh
  session can take over". The wrapper handles relay automatically.
- ask the user "what should I do?" or offer them options. This is
  autonomous — there is no human in the loop.
- claim the work "requires a fresh session", "needs multi-day effort
  outside this session's scope", or "is architectural and out of scope".
  Session scope is irrelevant; the work is in scope by definition.
- write YES dishonestly to escape the loop. The criteria are checked;
  lying wastes downstream work.

The Stop hook is active. Every time you try to end a turn it asks:
HAVE YOU MET THE CRITERIA? If YES, write YES to the marker:

    echo YES > "{marker}"

Only on cited, third-party-verifiable evidence that EVERY criterion is
met.

If NO, keep working. Pick a new angle: read more of the code, write a
targeted diagnostic, generate a minimal reproducer, consult any external
analysis tools available to you, form a new hypothesis and test it.
Then return to the criteria.

---

"""


def _build_prompt(resume_file, iteration):
    body = Path(resume_file).read_text(encoding="utf-8", errors="replace")
    run_dir = Path(resume_file).parent
    if _has_criteria(run_dir):
        criteria = _criteria_path(run_dir).read_text(encoding="utf-8", errors="replace").strip()
        marker = str(_criteria_met_path(run_dir))
        return PREAMBLE_CRITERIA.format(iter=iteration, criteria=criteria, marker=marker) + body
    return PREAMBLE_LEGACY.format(iter=iteration) + body


def _build_command(cfg, session_id, prompt_text, interactive=False):
    # Non-interactive: the prompt is fed via stdin (see run_session), keeping
    # it out of /proc/<pid>/cmdline so `pgrep -f <task-keyword>` from inside
    # the session can't match its own parent. Interactive mode inherits the
    # TTY and has no stdin path for the seed prompt, so we still pass it on
    # argv there — a known exposure, accepted because a human is driving.
    cmd = [cfg["claude_bin"]]
    if not interactive:
        cmd.append("-p")
    cmd += ["--session-id", session_id, "--permission-mode", cfg["permission_mode"]]
    if not interactive:
        # stream-json is parsed for live output; the interactive TUI renders
        # itself, so we leave its output untouched.
        cmd += ["--verbose", "--output-format", "stream-json"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    if cfg["effort"]:
        cmd += ["--effort", cfg["effort"]]
    if cfg["settings"]:
        cmd += ["--settings", cfg["settings"]]
    if cfg["max_budget"]:
        cmd += ["--max-budget-usd", cfg["max_budget"]]
    if cfg["extra_args"]:
        cmd += cfg["extra_args"].split()
    if interactive:
        cmd.append(prompt_text)
    return cmd


def _session_env(cfg, run_id, session_id, resume_file, transcript_file):
    env = dict(os.environ)
    env["CCLOOP_RUN_ID"] = run_id
    env["CCLOOP_SESSION_ID"] = session_id
    env["CCLOOP_RESUME_FILE"] = str(resume_file)
    env["CCLOOP_TRANSCRIPT_PATH"] = str(transcript_file)
    env["CCLOOP_THRESHOLD_SOFT"] = cfg["threshold_soft"]
    env["CCLOOP_THRESHOLD_HARD"] = cfg["threshold_hard"]
    env["DISABLE_AUTO_COMPACT"] = "1"

    # The whole point of ccloop is that the Stop hook keeps blocking until the
    # task is actually done. Claude Code's harness has a separate safety cap
    # (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, default 9) that overrides the hook
    # after N consecutive blocks — directly hostile to ccloop's purpose.
    # Default to unlimited; CCLOOP_STOP_HOOK_BLOCK_CAP=-1 means never cap.
    # A user who explicitly sets CLAUDE_CODE_STOP_HOOK_BLOCK_CAP in their
    # own env wins (we don't overwrite).
    if "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP" not in os.environ:
        try:
            cap = int(os.environ.get("CCLOOP_STOP_HOOK_BLOCK_CAP", "-1"))
        except ValueError:
            cap = -1
        env["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = str(2**31 - 1) if cap < 0 else str(cap)
    return env


def run_session(cmd, env, out_path, timeout, prompt_text):
    """Spawn a session, stream output live, return (exit_code, formatter).

    The child runs in its own process group; SIGINT kills the whole group
    (escalating to SIGKILL on a second Ctrl-C) and re-raises
    KeyboardInterrupt so the loop can stop and preserve state.

    ``prompt_text`` is written to the child's stdin instead of appearing on
    argv, so the task description stays out of /proc/<pid>/cmdline.
    """
    fmt = stream.StreamFormatter()
    interrupted = {"count": 0}

    with open(out_path, "w", encoding="utf-8") as raw_log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        try:
            proc.stdin.write(prompt_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None

        def handle_sigint(signum, frame):
            interrupted["count"] += 1
            sig = signal.SIGKILL if interrupted["count"] > 1 else signal.SIGTERM
            if pgid is not None:
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    pass

        old_handler = signal.signal(signal.SIGINT, handle_sigint)

        timer = None
        if timeout and timeout > 0 and pgid is not None:
            def on_timeout():
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    time.sleep(5)
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            timer = threading.Timer(timeout, on_timeout)
            timer.daemon = True
            timer.start()

        try:
            for line in proc.stdout:
                raw_log.write(line)
                raw_log.flush()
                for disp in fmt.feed(line):
                    print(disp, flush=True)
            proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            signal.signal(signal.SIGINT, old_handler)

        if interrupted["count"] > 0:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise KeyboardInterrupt

    return proc.returncode, fmt


def run_session_interactive(cmd, env, session_id, hard_threshold, poll=3.0):
    """Run the real Claude TUI with inherited terminal; return (exit, relayed).

    A background thread polls the ccusage cache for THIS session's exact
    context % and terminates the session once it crosses ``hard_threshold``
    so the loop can relay to a fresh session before the context wall. The
    soft guard hook has already nudged a graceful wrap-up earlier.
    """
    import termios

    relayed = {"flag": False}
    stop = threading.Event()

    proc = subprocess.Popen(cmd, env=env)  # inherits this process's std fds
    pid = proc.pid

    def watcher():
        while not stop.wait(poll):
            pct = usage.exact_pct(session_id)
            if pct is not None and hard_threshold > 0 and pct >= hard_threshold:
                relayed["flag"] = True
                log(f"context {round(float(pct))}% ≥ hard {hard_threshold}% "
                    "— relaying to a fresh session")
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                return

    wt = None
    if hard_threshold > 0:
        wt = threading.Thread(target=watcher, daemon=True)
        wt.start()

    # The TUI owns the terminal (raw mode handles Ctrl-C/Escape itself);
    # ignore SIGINT in the wrapper so a stray ^C can't kill the loop here.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        saved_term = termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, ValueError, OSError):
        saved_term = None

    try:
        proc.wait()
    finally:
        stop.set()
        if wt is not None:
            wt.join(timeout=1)
        signal.signal(signal.SIGINT, old_sigint)
        if saved_term is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_term)
            except (termios.error, ValueError, OSError):
                pass

    if relayed["flag"] and proc.poll() is None:
        try:
            time.sleep(1)
            proc.kill()
        except ProcessLookupError:
            pass

    return proc.returncode, relayed["flag"]


def _confirm_relaunch():
    """Ask whether to relaunch, with the terminal guaranteed to be in cooked
    mode (the TUI may have left it raw on exit, which would swallow input)."""
    # Best-effort: force a sane terminal state before reading a line. Without
    # this the TUI's raw-mode leftovers can eat keystrokes including Enter.
    try:
        subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
    except (OSError, ValueError):
        pass
    # Make sure Ctrl-C is escapable here too, in case the interactive runner
    # left SIGINT ignored.
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass
    try:
        ans = input("[ccloop] Relaunch a fresh session? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("", "y", "yes")


def _link_transcript(transcript_file, transcripts_dir, iteration):
    dest = Path(transcripts_dir) / f"session-{iteration}.jsonl"
    try:
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        dest.symlink_to(transcript_file)
    except OSError:
        pass


def _setup_new_run(task, criteria=""):
    run_id = _gen_uuid()
    run_dir = runs_dir() / run_id
    (run_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (run_dir / "task.md").write_text(task + "\n", encoding="utf-8")
    (run_dir / "resume.md").write_text(task + "\n", encoding="utf-8")
    (run_dir / "sessions.log").write_text("", encoding="utf-8")
    # criteria.md is always written (empty if no criteria) so resumes can
    # see "criteria intentionally empty" vs "old run from before the flag".
    (run_dir / "criteria.md").write_text((criteria or "").strip() + "\n", encoding="utf-8")
    log(f"starting run {run_id}")
    log(f"state at {run_dir}")
    if (criteria or "").strip():
        log("criteria gate active — stop allowed only on criteria-met=YES")
    return run_id, run_dir


def _setup_resume(run_id):
    run_dir = runs_dir() / run_id
    if not run_dir.is_dir():
        raise CcloopError(f"run not found: {run_dir}")
    if not (run_dir / "task.md").is_file():
        raise CcloopError(f"missing task.md in {run_dir}")
    if not (run_dir / "resume.md").is_file():
        raise CcloopError(f"missing resume.md in {run_dir}")
    log(f"resuming run {run_id}")
    return run_id, run_dir


def loop(run_id, run_dir, ensure_hook=True, interactive=False):
    cfg = _config()
    run_dir = Path(run_dir)
    resume_file = run_dir / "resume.md"
    task_file = run_dir / "task.md"
    sessions_log = run_dir / "sessions.log"
    transcripts_dir = run_dir / "transcripts"
    task = task_file.read_text(encoding="utf-8")

    import shutil
    if shutil.which(cfg["claude_bin"]) is None and not os.path.isfile(cfg["claude_bin"]):
        raise CcloopError(f"claude binary not found: {cfg['claude_bin']}")

    if ensure_hook:
        _ensure_hook()

    if interactive:
        log("interactive mode — you drive the Claude TUI; ccloop relays on "
            "exit or when context fills")

    existing = sessions_log.read_text(encoding="utf-8").count("\n") if sessions_log.exists() else 0
    start_iter = existing
    iteration = existing
    stuck = 0

    try:
        while True:
            iteration += 1

            if cfg["max_iterations"] > 0 and iteration > start_iter + cfg["max_iterations"]:
                log(f"max iterations ({cfg['max_iterations']}) reached without convergence")
                return 1

            reason = converged_reason(resume_file)
            if reason:
                log(f"converged: {reason} (after {iteration - 1} sessions)")
                return 0

            session_id = _gen_uuid()
            transcript_file = tx.transcript_path(session_id)

            log(f"── session {iteration} ── id={session_id}")
            with open(sessions_log, "a", encoding="utf-8") as fh:
                fh.write(session_id + "\n")

            prompt_text = _build_prompt(resume_file, iteration)
            (run_dir / f"session-{iteration}.prompt").write_text(prompt_text, encoding="utf-8")
            cmd = _build_command(cfg, session_id, prompt_text, interactive=interactive)
            env = _session_env(cfg, run_id, session_id, resume_file, transcript_file)

            start = time.time()
            relayed = False
            if interactive:
                exit_code, relayed = run_session_interactive(
                    cmd, env, session_id, cfg["threshold_hard_int"],
                    poll=cfg["watch_interval"],
                )
            else:
                exit_code, fmt = run_session(
                    cmd, env, run_dir / f"session-{iteration}.out",
                    cfg["session_timeout"], prompt_text,
                )
                # Death-loop guard 1: the fed prompt exceeded the model window.
                if fmt.saw_prompt_too_long:
                    raise CcloopError(
                        "session prompt exceeds the model context window "
                        "('Prompt is too long'). The resume file is too large to "
                        f"hand off. Inspect/trim {resume_file} or narrow the task, "
                        "then resume with: ccloop --resume-run " + run_id
                    )
            duration = time.time() - start
            log(f"session {iteration} ended exit={exit_code} duration={duration:.0f}s")

            have_transcript = transcript_file.is_file()
            if have_transcript:
                _link_transcript(transcript_file, transcripts_dir, iteration)
            else:
                log(f"WARNING: no transcript at {transcript_file}")
                if iteration == 1 and duration < 10 and exit_code != 0:
                    raise CcloopError(
                        "session 1 failed before producing a transcript — "
                        f"aborting. Check {run_dir}/session-1.out"
                    )

            # Did Claude write a convergence signal during the session?
            reason = converged_reason(resume_file)
            if reason:
                log(f"converged: {reason} (signalled during session {iteration})")
                return 0

            # Death-loop guard 2: consecutive sessions with no real work.
            productive = have_transcript and tx.assistant_turns(transcript_file) >= 1
            if productive:
                stuck = 0
            else:
                stuck += 1
                log(f"no-progress session ({stuck}/{cfg['stuck_limit']})")
                if stuck >= cfg["stuck_limit"]:
                    raise CcloopError(
                        f"{stuck} consecutive sessions made no progress — "
                        "aborting to avoid an infinite loop. Check the "
                        f"session-N.out logs in {run_dir}"
                    )

            # Summarize transcript → resume.md (atomic).
            if have_transcript:
                try:
                    new_resume = summarize.summarize(
                        transcript_file, task, run_id, iteration
                    )
                    tmp = resume_file.with_suffix(".md.tmp")
                    tmp.write_text(new_resume, encoding="utf-8")
                    os.replace(tmp, resume_file)
                    log("resume.md updated from transcript")
                except OSError as exc:
                    log(f"WARNING: summarize failed ({exc}); keeping prior resume.md")
            else:
                log("WARNING: no transcript; keeping prior resume.md")

            # Interactive: a watcher relay (context hit the hard threshold)
            # continues automatically; a plain user exit asks first, so
            # quitting the TUI doesn't trap you in an endless relaunch.
            if interactive and not relayed and not _confirm_relaunch():
                log(f"stopping at your request — resume preserved at {resume_file}")
                return 0

            time.sleep(1)
    except KeyboardInterrupt:
        log("interrupt received — terminating session")
        log(f"resume file preserved at: {resume_file}")
        return 130


def _ensure_hook():
    """Self-register all ccloop hooks (guard + keepgoing) in user settings."""
    try:
        status = install.ensure_registered()
        if status in ("added", "updated"):
            log(f"ccloop hooks {status} in {install.default_settings_path()}")
    except (ValueError, OSError) as exc:
        raise CcloopError(
            f"unable to register ccloop hooks in {install.default_settings_path()}: "
            f"{exc}. Re-run with --no-hook to proceed without them."
        )


# ── run / resume / list / prune entry points ─────────────────────────────


def cmd_run(criteria, task, ensure_hook=True, interactive=False):
    run_id, run_dir = _setup_new_run(task, criteria=criteria)
    return loop(run_id, run_dir, ensure_hook=ensure_hook, interactive=interactive)


def cmd_resume(run_id, ensure_hook=True, interactive=False):
    run_id, run_dir = _setup_resume(run_id)
    return loop(run_id, run_dir, ensure_hook=ensure_hook, interactive=interactive)


def _status_of(run_dir):
    run_dir = Path(run_dir)
    if _has_criteria(run_dir):
        marker = _criteria_met_path(run_dir)
        if not marker.is_file():
            return "active"
        try:
            tok = _first_token(marker.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return "active"
        return "done" if tok.upper().rstrip(":") == "YES" else "active"
    resume = run_dir / "resume.md"
    if not resume.exists():
        return "missing"
    txt = resume.read_text(encoding="utf-8", errors="replace")
    if not txt.strip():
        return "empty"
    if _first_token(txt).upper().rstrip(":")[:4] == "DONE":
        return "done"
    return "active"


def cmd_list():
    rd = runs_dir()
    if not rd.is_dir():
        print(f"no runs in {rd}")
        return 0
    print(f"{'RUN-ID':<36}  {'SESSIONS':<8}  {'STATUS':<9}  TASK")
    for d in sorted(rd.iterdir()):
        if not d.is_dir():
            continue
        slog = d / "sessions.log"
        sessions = slog.read_text(encoding="utf-8").count("\n") if slog.exists() else 0
        status = _status_of(d)
        task = "(no task.md)"
        tf = d / "task.md"
        if tf.is_file():
            for line in tf.read_text(encoding="utf-8", errors="replace").split("\n"):
                if line.strip():
                    task = line[:80]
                    break
        print(f"{d.name:<36}  {sessions:<8}  {status:<9}  {task}")
    return 0


def cmd_prune(force=False):
    rd = runs_dir()
    if not rd.is_dir():
        print(f"no runs in {rd}")
        return 0
    converged = [
        d for d in sorted(rd.iterdir())
        if d.is_dir() and _status_of(d) in ("done", "empty")
    ]
    if not converged:
        print("no converged runs to prune")
        return 0
    if not force:
        print("would delete (use --force to actually delete):")
        for d in converged:
            print(f"  {d.name}")
        print(f"{len(converged)} run(s) match")
        return 0
    import shutil
    for d in converged:
        shutil.rmtree(d)
        print(f"deleted: {d.name}")
    print(f"{len(converged)} run(s) pruned")
    return 0
