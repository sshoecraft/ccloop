import json
import os

import pytest

from ccloop import runner


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *a, **k: None)


def test_build_command_interactive_omits_print_and_streamjson():
    cfg = runner._config()
    cmd = runner._build_command(cfg, "sid", "the prompt", interactive=True)
    assert "-p" not in cmd
    assert "--output-format" not in cmd
    assert "--session-id" in cmd and "sid" in cmd
    assert cmd[-1] == "the prompt"


def test_build_command_headless_has_streamjson():
    cfg = runner._config()
    cmd = runner._build_command(cfg, "sid", "the prompt", interactive=False)
    assert "-p" in cmd
    assert "stream-json" in cmd
    # Prompt must not appear on argv in headless mode — it is piped via stdin
    # so it stays out of /proc/<pid>/cmdline and out of `pgrep -f` matches.
    assert "the prompt" not in cmd


def _write_cache(tmpdir, session_id, pct):
    cache = os.path.join(tmpdir, f"ccusage-{os.getuid()}.json")
    with open(cache, "w") as fh:
        json.dump({"session_id": session_id,
                   "context_window": {"used_percentage": pct,
                                       "context_window_size": 1000000}}, fh)


def test_interactive_watcher_relays_on_hard_threshold(fake_claude, tmp_path, monkeypatch):
    tmpd = tmp_path / "tmp"
    tmpd.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpd))
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "30")
    _write_cache(str(tmpd), "watch-sess", 95)  # over the hard threshold

    exit_code, relayed = runner.run_session_interactive(
        [str(fake_claude)], dict(os.environ), "watch-sess",
        hard_threshold=90, poll=0.2,
    )
    assert relayed is True


def test_interactive_no_relay_when_below_threshold(fake_claude, tmp_path, monkeypatch):
    tmpd = tmp_path / "tmp"
    tmpd.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpd))
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "0.5")  # exits on its own quickly
    _write_cache(str(tmpd), "watch-sess", 10)  # well under threshold

    exit_code, relayed = runner.run_session_interactive(
        [str(fake_claude)], dict(os.environ), "watch-sess",
        hard_threshold=90, poll=0.2,
    )
    assert relayed is False


def test_loop_converges_on_done(project, isolated_home, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_COUNTER", str(project / "counter"))
    monkeypatch.setenv("FAKE_DONE_AFTER", "2")
    rc = runner.cmd_run("", "do the thing", ensure_hook=False)
    assert rc == 0
    runs = list((project / ".ccloop" / "runs").iterdir())
    assert len(runs) == 1
    run = runs[0]
    assert run.joinpath("sessions.log").read_text().count("\n") == 2
    assert run.joinpath("resume.md").read_text().strip() == "DONE"


def test_prompt_too_long_aborts(project, isolated_home, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "toolong")
    with pytest.raises(runner.CcloopError) as exc:
        runner.cmd_run("", "do the thing", ensure_hook=False)
    assert "Prompt is too long" in str(exc.value)


def test_stuck_aborts(project, isolated_home, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "noprogress")
    monkeypatch.setenv("CCLOOP_STUCK_LIMIT", "2")
    with pytest.raises(runner.CcloopError) as exc:
        runner.cmd_run("", "do the thing", ensure_hook=False)
    assert "no progress" in str(exc.value)


def test_max_iterations_cap(project, isolated_home, fake_claude, monkeypatch):
    # work mode that never converges; cap stops it.
    monkeypatch.setenv("FAKE_COUNTER", str(project / "counter"))
    monkeypatch.setenv("CCLOOP_MAX_ITERATIONS", "3")
    rc = runner.cmd_run("", "never done", ensure_hook=False)
    assert rc == 1
    run = next((project / ".ccloop" / "runs").iterdir())
    assert run.joinpath("sessions.log").read_text().count("\n") == 3


def test_missing_claude_bin_aborts(project, isolated_home, monkeypatch):
    monkeypatch.setenv("CCLOOP_CLAUDE_BIN", "/nonexistent/claude-xyz")
    with pytest.raises(runner.CcloopError) as exc:
        runner.cmd_run("", "do the thing", ensure_hook=False)
    assert "claude binary not found" in str(exc.value)


def test_list_and_prune_after_run(project, isolated_home, fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_COUNTER", str(project / "counter"))
    monkeypatch.setenv("FAKE_DONE_AFTER", "1")
    runner.cmd_run("", "do the thing", ensure_hook=False)
    capsys.readouterr()

    assert runner.cmd_list() == 0
    out = capsys.readouterr().out
    assert "done" in out

    assert runner.cmd_prune(force=False) == 0
    assert "would delete" in capsys.readouterr().out

    assert runner.cmd_prune(force=True) == 0
    assert "pruned" in capsys.readouterr().out
    assert not list((project / ".ccloop" / "runs").iterdir())


def test_resume_continues_numbering(project, isolated_home, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_COUNTER", str(project / "counter"))
    monkeypatch.setenv("CCLOOP_MAX_ITERATIONS", "1")
    runner.cmd_run("", "keep going", ensure_hook=False)
    run = next((project / ".ccloop" / "runs").iterdir())
    assert run.joinpath("sessions.log").read_text().count("\n") == 1

    # Resume the same run; it should add a 2nd session (numbering continues).
    runner.cmd_resume(run.name, ensure_hook=False)
    assert run.joinpath("sessions.log").read_text().count("\n") == 2
