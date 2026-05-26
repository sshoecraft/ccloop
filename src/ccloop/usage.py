"""Read the exact Claude Code context usage from the ccusage statusline cache.

Claude Code's statusline writes the raw status JSON it receives to a
per-UID cache (`$TMPDIR/ccusage-<uid>.json`) every turn. That JSON carries
the exact ``context_window.used_percentage`` Claude Code itself computes,
the real ``context_window_size``, and the ``session_id`` it belongs to.

Both the guard hook and the interactive watcher read context usage from
here so we never re-estimate a percentage that already exists exactly. The
cache is shared per-UID across concurrent sessions, so callers that need a
guarantee it is *this* session use ``exact_pct(session_id)``.
"""

import json
import os
from pathlib import Path


def cache_path():
    return Path(os.environ.get("TMPDIR", "/tmp")) / f"ccusage-{os.getuid()}.json"


def read_cache():
    p = cache_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def exact_pct(session_id, cache=None):
    """Exact used_percentage, but only when the cache is for ``session_id``."""
    cache = read_cache() if cache is None else cache
    if not cache or cache.get("session_id") != session_id:
        return None
    return (cache.get("context_window") or {}).get("used_percentage")


def window_size(cache=None):
    """Real context window size from the cache, or None."""
    cache = read_cache() if cache is None else cache
    if not cache:
        return None
    return (cache.get("context_window") or {}).get("context_window_size")
