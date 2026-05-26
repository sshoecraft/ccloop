"""Command-line entry point and dispatch.

Kept deliberately light at import time: the ``guard`` no-op path (the
common case when the hook fires outside a ccloop run) returns before any
heavy modules are imported.
"""

import re
import sys
from pathlib import Path

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

USAGE = """ccloop — relay-loop wrapper for Claude Code

Usage:
  ccloop "<criteria>" "<task>"  start a new run with success criteria
                                (empty criteria "" = legacy DONE-marker mode)
  ccloop --resume-run <run-id>  resume an existing run
  ccloop --list                 list runs in the current project
  ccloop --prune [--force]      delete converged runs; dry-run by default
  ccloop install [--uninstall]  manually (un)register the guard hook
  ccloop guard                  PostToolUse hook (invoked by Claude Code)
  ccloop keepgoing              Stop hook — re-feed model until criteria met
                                (invoked by Claude Code; no-op outside ccloop)
  ccloop --help                 show this help

Tip: load criteria from a file with shell substitution, e.g.
  ccloop "$(cat criteria.md)" "fix the silent dirent loss bug"

Options:
  --no-hook                     skip guard-hook registration for this run
  -i, --interactive             force the interactive Claude TUI (relay on exit)
  --headless                    force headless mode (autonomous, parsed output)
                                (default: auto — interactive on a TTY, else headless)

Environment variables:
  CCLOOP_MAX_ITERATIONS    hard cap on sessions per run (default: 0 = unlimited)
  CCLOOP_SESSION_TIMEOUT   SIGTERM a session after N seconds (default: 0 = none)
  CCLOOP_THRESHOLD_SOFT    guard injection threshold % (default: 70)
  CCLOOP_THRESHOLD_HARD    interactive auto-relay threshold % (default: 85; 0 disables)
  CCLOOP_WATCH_INTERVAL    interactive context-poll seconds (default: 3)
  CCLOOP_STUCK_LIMIT       consecutive no-progress sessions before abort (default: 3)
  CCLOOP_MAX_CONTINUES     cap keepgoing re-feeds per session (default: 0 = unlimited)
  CCLOOP_STOP_HOOK_BLOCK_CAP  override Claude Code's Stop hook cap (default: -1 = unlimited)
  CCLOOP_PERMISSION_MODE   default: bypassPermissions
  CCLOOP_MODEL             override model
  CCLOOP_EFFORT            override effort level
  CCLOOP_SETTINGS          path/JSON for claude --settings
  CCLOOP_MAX_BUDGET_USD    per-session cost cap
  CCLOOP_CLAUDE_BIN        claude binary to invoke (default: claude)
  CCLOOP_CLAUDE_EXTRA_ARGS extra args appended to every claude invocation

State: .ccloop/runs/<run-id>/ in the current directory.
"""


def _run(fn, *args, **kwargs):
    from .runner import CcloopError
    try:
        return fn(*args, **kwargs)
    except CcloopError as exc:
        print(f"ccloop: {exc}", file=sys.stderr)
        return 1


def _cmd_install(args):
    from . import install
    settings_path = None
    action = "install"
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--uninstall":
            action = "uninstall"
        elif a == "--project":
            settings_path = Path(".claude/settings.json")
        elif a == "--settings":
            i += 1
            if i >= len(args):
                print("ccloop install: --settings requires a path", file=sys.stderr)
                return 2
            settings_path = Path(args[i])
        else:
            print(f"ccloop install: unknown option: {a}", file=sys.stderr)
            return 2
        i += 1

    try:
        if action == "uninstall":
            changed = install.uninstall(settings_path)
            print("removed ccloop hooks" if changed else "no ccloop hooks to remove")
        else:
            status = install.ensure_registered(settings_path=settings_path)
            target = settings_path or install.default_settings_path()
            print(f"ccloop hooks {status}: {target}")
            for event, sub in install.HOOKS.items():
                print(f"  {event}: {install.hook_command(sub)}")
    except (ValueError, OSError) as exc:
        print(f"ccloop install: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Fast no-op gate for hooks — return before importing anything heavy.
    if argv[:1] == ["guard"]:
        from . import guard
        return guard.main(argv[1:])
    if argv[:1] == ["keepgoing"]:
        from . import keepgoing
        return keepgoing.main(argv[1:])

    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        from . import __version__
        print(__version__)
        return 0

    ensure_hook = True
    if "--no-hook" in argv:
        ensure_hook = False
        argv = [a for a in argv if a != "--no-hook"]

    interactive = None  # auto-detect from TTY
    if "--interactive" in argv or "-i" in argv:
        interactive = True
        argv = [a for a in argv if a not in ("--interactive", "-i")]
    if "--headless" in argv:
        interactive = False
        argv = [a for a in argv if a != "--headless"]
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if argv and argv[0] == "install":
        return _cmd_install(argv[1:])

    from . import runner

    if argv[0] == "--list":
        return runner.cmd_list()

    if argv[0] == "--prune":
        rest = argv[1:]
        force = False
        if rest == ["--force"]:
            force = True
        elif rest:
            print(f"ccloop: unknown option after --prune: {rest[0]}", file=sys.stderr)
            return 2
        return runner.cmd_prune(force=force)

    if argv[0] == "--resume-run":
        if len(argv) < 2:
            print("ccloop: --resume-run requires a run-id", file=sys.stderr)
            return 2
        run_id = argv[1]
        if not UUID_RE.match(run_id):
            print(
                f"ccloop: invalid run-id: must be a lowercase UUID (got: {run_id})",
                file=sys.stderr,
            )
            return 2
        return _run(runner.cmd_resume, run_id, ensure_hook=ensure_hook,
                    interactive=interactive)

    if argv[0].startswith("-"):
        print(f"ccloop: unknown option: {argv[0]}", file=sys.stderr)
        return 2

    if len(argv) < 2:
        print(
            "ccloop: two arguments required — criteria and task.\n"
            "  ccloop \"<criteria>\" \"<task>\"\n"
            "  ccloop \"\" \"<task>\"           # legacy mode (no criteria gate)\n"
            "  ccloop \"$(cat crit.md)\" \"<task>\"  # criteria from file\n"
            "Or use --resume-run / --list / --prune; see --help.",
            file=sys.stderr,
        )
        return 2
    if len(argv) > 2:
        print(
            f"ccloop: too many positional arguments ({len(argv)}); expected 2 "
            "(criteria, task). Quote each argument.",
            file=sys.stderr,
        )
        return 2
    criteria, task = argv[0], argv[1]
    if not task.strip():
        print("ccloop: task argument is empty", file=sys.stderr)
        return 2
    return _run(runner.cmd_run, criteria, task, ensure_hook=ensure_hook,
                interactive=interactive)


if __name__ == "__main__":
    sys.exit(main())
