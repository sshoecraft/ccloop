import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME so transcripts/settings land in a temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temp working directory used as the ccloop project root."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Point ccloop at the fake claude via a single-token wrapper script."""
    wrapper = tmp_path / "fake-claude"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("CCLOOP_CLAUDE_BIN", str(wrapper))
    return wrapper
