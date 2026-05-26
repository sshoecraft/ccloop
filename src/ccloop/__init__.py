"""ccloop — relay-loop wrapper for Claude Code.

Runs `claude -p` repeatedly. Between sessions, the prior session's
transcript is summarized into a resume file that is fed to the next
session as its prompt. The loop ends when the resume file is empty,
missing, or starts with DONE.
"""

__version__ = "0.2.2"
