"""Register/unregister ccloop hooks in Claude Code's settings.json.

ccloop owns two hooks:

- ``PostToolUse`` → ``ccloop guard``  : context-fill nudge
- ``Stop``        → ``ccloop keepgoing``: prevents the model from sitting
  idle waiting for input — re-feeds "continue" on every stop unless the
  task is converged.

Each command is the absolute path to this very ``ccloop`` executable plus
the subcommand. Absolute paths mean the hooks resolve regardless of
whether ccloop is on Claude Code's PATH. Both hooks self-gate on
``CCLOOP_RUN_ID`` (and ``keepgoing`` also gates on session id), so they
are no-ops in every session that isn't a ccloop run.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

# event name → ccloop subcommand
HOOKS = {
    "PostToolUse": "guard",
    "Stop": "keepgoing",
}


def default_settings_path():
    return Path.home() / ".claude" / "settings.json"


def _exe(executable=None):
    return executable or os.path.realpath(sys.argv[0])


def hook_command(subcommand, executable=None):
    """Command string to register: ``<abs-path-to-ccloop> <subcommand>``."""
    return f"{_exe(executable)} {subcommand}"


def guard_command(executable=None):
    """Back-compat: the PostToolUse guard command string."""
    return hook_command("guard", executable)


def keepgoing_command(executable=None):
    """The Stop keepgoing command string."""
    return hook_command("keepgoing", executable)


def _entry_commands(entry):
    return [h.get("command") for h in (entry.get("hooks") or []) if isinstance(h, dict)]


# Known ccloop subcommands that own a hook slot. Used to decide whether an
# existing entry is "ours" (so we can self-heal a relocated executable)
# without clobbering foreign hooks.
_OUR_SUBCOMMANDS = set(HOOKS.values())


def _is_ours(command):
    """True if ``command`` looks like any ccloop hook registration.

    Matches loosely so a relocated executable is self-healed rather than
    duplicated. Also recognizes the legacy bash hook
    (``hooks/context-guard.sh``) so an upgrade from the shell version
    cleans the old registration on first run.
    """
    if not command:
        return False
    parts = command.split()
    if not parts:
        return False
    if parts[-1] in _OUR_SUBCOMMANDS and (
        os.path.basename(parts[0]) == "ccloop" or "ccloop" in parts[0]
    ):
        return True
    if os.path.basename(parts[0]) == "context-guard.sh":
        return True
    return False


def _load(settings_path):
    p = Path(settings_path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{settings_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path} top level is not a JSON object")
    return data


def _atomic_write(settings_path, data):
    p = Path(settings_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        backup = f"{p}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(p, backup)
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)


def _ensure_event(data, event, command):
    """Place ``command`` under ``data.hooks[event]``, self-healing stale ours.

    Returns ``"present"`` / ``"added"`` / ``"updated"`` for this single
    event slot. Does NOT write — caller atomically writes the whole file.
    """
    hooks = data.setdefault("hooks", {})
    entries = hooks.get(event) or []

    had_exact = False
    had_stale = False
    rebuilt = []
    for entry in entries:
        cmds = _entry_commands(entry)
        if command in cmds:
            had_exact = True
        ours = [c for c in cmds if _is_ours(c)]
        if not ours:
            rebuilt.append(entry)
            continue
        kept_hooks = [
            h for h in (entry.get("hooks") or [])
            if not (isinstance(h, dict) and _is_ours(h.get("command")))
        ]
        if any(c != command for c in ours):
            had_stale = True
        if kept_hooks:
            entry = dict(entry)
            entry["hooks"] = kept_hooks
            rebuilt.append(entry)

    if had_exact and not had_stale:
        hooks[event] = entries
        return "present"

    rebuilt.append({"hooks": [{"type": "command", "command": command}]})
    hooks[event] = rebuilt
    return "updated" if had_stale else "added"


def is_registered(command, settings_paths=None):
    """True if ``command`` is registered under any ccloop-owned hook slot."""
    if settings_paths is None:
        settings_paths = [default_settings_path()]
    for sp in settings_paths:
        try:
            data = _load(sp)
        except ValueError:
            continue
        for event in HOOKS:
            for entry in (data.get("hooks") or {}).get(event) or []:
                if command in _entry_commands(entry):
                    return True
    return False


def ensure_registered(command=None, settings_path=None, executable=None):
    """Make sure every ccloop hook is registered for ``executable``.

    The ``command`` argument is accepted for back-compat (older callers pass
    just the guard command). New callers should leave it ``None`` and rely
    on the ``HOOKS`` table.

    Returns the *worst-case* status across hook slots:
    ``"present"`` < ``"added"`` < ``"updated"`` (later == more change).
    Raises ``ValueError``/``OSError`` if the settings file can't be used.
    """
    settings_path = settings_path or default_settings_path()
    data = _load(settings_path)

    if command is not None:
        # Legacy single-command path: still ensure the rest are present too.
        # If caller passed a non-default command, register it as a guard.
        events = [("PostToolUse", command)]
        for event, sub in HOOKS.items():
            target = hook_command(sub, executable)
            if event == "PostToolUse" and command == target:
                continue
            if event != "PostToolUse":
                events.append((event, target))
    else:
        events = [(e, hook_command(s, executable)) for e, s in HOOKS.items()]

    rank = {"present": 0, "added": 1, "updated": 2}
    worst = "present"
    for event, cmd in events:
        status = _ensure_event(data, event, cmd)
        if rank[status] > rank[worst]:
            worst = status

    if worst != "present":
        _atomic_write(settings_path, data)
    return worst


def uninstall(settings_path=None):
    """Remove all ccloop hook entries (every event). True if anything changed."""
    settings_path = settings_path or default_settings_path()
    try:
        data = _load(settings_path)
    except ValueError:
        return False
    hooks = data.get("hooks") or {}
    changed = False
    for event in list(hooks):
        entries = hooks.get(event) or []
        rebuilt = []
        ev_changed = False
        for entry in entries:
            kept = [
                h for h in (entry.get("hooks") or [])
                if not (isinstance(h, dict) and _is_ours(h.get("command")))
            ]
            if len(kept) != len(entry.get("hooks") or []):
                ev_changed = True
                changed = True
            if kept:
                entry = dict(entry)
                entry["hooks"] = kept
                rebuilt.append(entry)
        if ev_changed:
            if rebuilt:
                hooks[event] = rebuilt
            else:
                hooks.pop(event, None)
    if not changed:
        return False
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    _atomic_write(settings_path, data)
    return True
