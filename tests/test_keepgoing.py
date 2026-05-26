import io
import json

import pytest

from ccloop import keepgoing


def run(monkeypatch, stdin_obj):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stdin_obj)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    rc = keepgoing.main([])
    return rc, out.getvalue()


def test_noop_outside_ccloop(monkeypatch):
    monkeypatch.delenv("CCLOOP_RUN_ID", raising=False)
    rc, out = run(monkeypatch, {})
    assert rc == 0 and out == ""


def test_allows_stop_when_done(monkeypatch, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("DONE\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""


def test_blocks_stop_and_refeeds(monkeypatch, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("task body, not converged\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "DONE" in payload["reason"]
    assert "re-fed #1" in payload["systemMessage"]


def test_counter_increments(monkeypatch, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("body\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    run(monkeypatch, {"session_id": "s1"})
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert "re-fed #2" in json.loads(out)["systemMessage"]


def test_cap_allows_stop_after_max(monkeypatch, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("body\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    monkeypatch.setenv("CCLOOP_MAX_CONTINUES", "2")
    # Two re-feeds, third call should give up and let the model stop.
    run(monkeypatch, {"session_id": "s1"})
    run(monkeypatch, {"session_id": "s1"})
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""


def test_ignores_other_session(monkeypatch, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("body\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "DIFFERENT"})
    assert rc == 0 and out == ""


def test_missing_resume_file_treated_as_done(monkeypatch, tmp_path):
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(tmp_path / "absent.md"))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""


def test_done_with_trailing_text(monkeypatch, tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("  DONE: everything verified\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""


# ── criteria-gate path ────────────────────────────────────────────────


def _setup_criteria(tmp_path, criteria_text="all tests pass with zero errors\n"):
    resume = tmp_path / "resume.md"
    resume.write_text("task body\n")
    (tmp_path / "criteria.md").write_text(criteria_text)
    return resume


def test_criteria_path_done_marker_is_ignored(monkeypatch, tmp_path):
    # With criteria configured, raw DONE in resume.md must NOT allow stop.
    resume = _setup_criteria(tmp_path)
    resume.write_text("DONE\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "HAVE YOU MET THE CRITERIA" in payload["reason"]
    assert "all tests pass with zero errors" in payload["reason"]
    assert str(tmp_path / "criteria-met") in payload["reason"]


def test_criteria_path_marker_yes_allows_stop(monkeypatch, tmp_path):
    resume = _setup_criteria(tmp_path)
    (tmp_path / "criteria-met").write_text("YES\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""


def test_criteria_path_marker_other_content_blocks(monkeypatch, tmp_path):
    # Marker file exists but doesn't say YES — still block.
    resume = _setup_criteria(tmp_path)
    (tmp_path / "criteria-met").write_text("MAYBE\n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "HAVE YOU MET THE CRITERIA" in payload["reason"]


def test_criteria_path_no_marker_blocks(monkeypatch, tmp_path):
    resume = _setup_criteria(tmp_path)
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "HAVE YOU MET THE CRITERIA" in payload["reason"]


def test_empty_criteria_falls_back_to_legacy(monkeypatch, tmp_path):
    # Empty criteria.md = explicit opt-out; raw DONE is enough.
    resume = tmp_path / "resume.md"
    resume.write_text("DONE\n")
    (tmp_path / "criteria.md").write_text("   \n")
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""


def test_criteria_path_max_continues_cap(monkeypatch, tmp_path):
    resume = _setup_criteria(tmp_path)
    monkeypatch.setenv("CCLOOP_RUN_ID", "r1")
    monkeypatch.setenv("CCLOOP_SESSION_ID", "s1")
    monkeypatch.setenv("CCLOOP_RESUME_FILE", str(resume))
    monkeypatch.setenv("CCLOOP_MAX_CONTINUES", "2")
    run(monkeypatch, {"session_id": "s1"})
    run(monkeypatch, {"session_id": "s1"})
    rc, out = run(monkeypatch, {"session_id": "s1"})
    assert rc == 0 and out == ""
