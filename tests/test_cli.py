import pytest

from ccloop import cli


def test_guard_fast_path_noop(monkeypatch, capsys):
    monkeypatch.delenv("CCLOOP_RUN_ID", raising=False)
    assert cli.main(["guard"]) == 0
    assert capsys.readouterr().out == ""


def test_keepgoing_fast_path_noop(monkeypatch, capsys):
    monkeypatch.delenv("CCLOOP_RUN_ID", raising=False)
    assert cli.main(["keepgoing"]) == 0
    assert capsys.readouterr().out == ""


def test_help(capsys):
    assert cli.main(["--help"]) == 0
    assert "relay-loop wrapper" in capsys.readouterr().out


def test_no_args_shows_usage(capsys):
    assert cli.main([]) == 0
    assert "Usage:" in capsys.readouterr().out


def test_version(capsys):
    from ccloop import __version__
    assert cli.main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_option(capsys):
    assert cli.main(["--bogus"]) == 2
    assert "unknown option" in capsys.readouterr().err


def test_one_arg_rejected(capsys):
    # Single positional is no longer valid — must be (criteria, task).
    assert cli.main(["just the task"]) == 2
    assert "two arguments required" in capsys.readouterr().err


def test_empty_task_rejected(capsys):
    assert cli.main(["", "   "]) == 2
    assert "task argument is empty" in capsys.readouterr().err


def test_too_many_args_rejected(capsys):
    assert cli.main(["criteria", "task", "extra"]) == 2
    assert "too many positional" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../etc", "not-a-uuid", "FOO-BAR", "; rm -rf /"])
def test_resume_rejects_non_uuid(bad, capsys):
    assert cli.main(["--resume-run", bad]) == 2
    assert "invalid run-id" in capsys.readouterr().err


def test_resume_requires_arg(capsys):
    assert cli.main(["--resume-run"]) == 2


def test_list_empty(project, capsys):
    assert cli.main(["--list"]) == 0
    assert "no runs" in capsys.readouterr().out


def test_prune_empty(project, capsys):
    assert cli.main(["--prune"]) == 0
    assert "no runs" in capsys.readouterr().out


def test_prune_rejects_extra_option(project, capsys):
    assert cli.main(["--prune", "--bogus"]) == 2


class _FakeStream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _stub_run(monkeypatch):
    from ccloop import runner
    captured = {}
    def fake(criteria, task, ensure_hook=True, interactive=False):
        captured["criteria"] = criteria
        captured["task"] = task
        captured["interactive"] = interactive
        return 0
    monkeypatch.setattr(runner, "cmd_run", fake)
    return captured


def test_interactive_flag_forces_true(monkeypatch):
    captured = _stub_run(monkeypatch)
    cli.main(["-i", "", "do a thing"])
    assert captured["interactive"] is True
    assert captured["task"] == "do a thing"


def test_headless_flag_forces_false(monkeypatch):
    captured = _stub_run(monkeypatch)
    monkeypatch.setattr("sys.stdin", _FakeStream(True))
    monkeypatch.setattr("sys.stdout", _FakeStream(True))
    cli.main(["--headless", "", "do a thing"])
    assert captured["interactive"] is False


def test_autodetect_interactive_on_tty(monkeypatch):
    captured = _stub_run(monkeypatch)
    monkeypatch.setattr("sys.stdin", _FakeStream(True))
    monkeypatch.setattr("sys.stdout", _FakeStream(True))
    cli.main(["", "do a thing"])
    assert captured["interactive"] is True


def test_autodetect_headless_when_not_tty(monkeypatch):
    captured = _stub_run(monkeypatch)
    monkeypatch.setattr("sys.stdin", _FakeStream(False))
    monkeypatch.setattr("sys.stdout", _FakeStream(False))
    cli.main(["", "do a thing"])
    assert captured["interactive"] is False


def test_criteria_passed_through(monkeypatch):
    captured = _stub_run(monkeypatch)
    cli.main(["all tests pass with zero errors", "fix the bug"])
    assert captured["criteria"] == "all tests pass with zero errors"
    assert captured["task"] == "fix the bug"
