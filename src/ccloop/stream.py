"""Turn Claude Code ``--output-format stream-json`` events into readable
live output lines.

The formatter is fed raw stdout lines one at a time. It pairs tool calls
with their results, surfaces assistant text, prints a final cost/turns
line, and flags the ``Prompt is too long`` condition that signals the
context wall.
"""

import json

PROMPT_TOO_LONG = "Prompt is too long"


def _tool_desc(inp):
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "query", "command", "file_path", "pattern"):
        val = inp.get(key)
        if val:
            return " ".join(str(val).split("\n"))[:160]
    for key in ("sql", "prompt"):
        val = inp.get(key)
        if val:
            return " ".join(str(val).split("\n"))[:100]
    return ""


def _result_summary(block):
    content = block.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    content = (content or "").strip()
    if block.get("is_error"):
        return "ERROR: " + content[:200]
    n = len(content.split("\n")) if content else 0
    return f"ok {n} lines"


class StreamFormatter:
    """Stateful translator from stream-json lines to display strings."""

    def __init__(self):
        self.pending = {}
        self.saw_prompt_too_long = False
        self.result = None  # dict with cost/turns/duration when seen

    def feed(self, raw_line):
        """Return a list of display lines for one raw stdout line."""
        line = raw_line.rstrip("\n")
        if not line.strip():
            return []

        if PROMPT_TOO_LONG in line:
            self.saw_prompt_too_long = True

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON line (e.g. a plain error like "Prompt is too long").
            return [line]

        etype = event.get("type")
        out = []

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                kind = block.get("type")
                if kind == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        out.append(text)
                elif kind == "tool_use":
                    name = block.get("name", "")
                    desc = _tool_desc(block.get("input"))
                    tid = block.get("id")
                    if tid:
                        self.pending[tid] = name
                    out.append(f"→ {name:<8} {desc}".rstrip())

        elif etype == "user":
            for block in event.get("message", {}).get("content", []) or []:
                if block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                self.pending.pop(tid, None)
                out.append("  " + _result_summary(block))

        elif etype == "result":
            cost = event.get("total_cost_usd", 0) or 0
            turns = event.get("num_turns", 0) or 0
            dur = (event.get("duration_ms", 0) or 0) / 1000.0
            self.result = {"cost": cost, "turns": turns, "duration": dur}
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                if PROMPT_TOO_LONG in json.dumps(event):
                    self.saw_prompt_too_long = True
            out.append(f"Done {dur:.1f}s · {turns} turns · ${cost:.4f}")

        return out
