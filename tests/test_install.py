import json

import pytest

from ccloop import install


CMD = "/usr/local/bin/ccloop guard"


def read(p):
    return json.loads(p.read_text())


def test_fresh_install_adds_entry(tmp_path):
    s = tmp_path / "settings.json"
    assert install.ensure_registered(CMD, s) == "added"
    data = read(s)
    cmds = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert CMD in cmds


def test_idempotent_reinstall(tmp_path):
    s = tmp_path / "settings.json"
    install.ensure_registered(CMD, s)
    assert install.ensure_registered(CMD, s) == "present"
    data = read(s)
    assert len(data["hooks"]["PostToolUse"]) == 1


def test_self_heal_replaces_stale_path(tmp_path):
    s = tmp_path / "settings.json"
    install.ensure_registered("/old/place/ccloop guard", s)
    status = install.ensure_registered(CMD, s)
    assert status == "updated"
    data = read(s)
    cmds = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert cmds == [CMD]  # exactly one, the new path


def test_migrates_legacy_bash_hook(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"hooks": [{"type": "command", "command": "/src/ccloop/hooks/context-guard.sh"}]}]}}))
    status = install.ensure_registered(CMD, s)
    assert status == "updated"
    data = read(s)
    cmds = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert cmds == [CMD]  # legacy bash entry removed, single new entry


def test_preserves_foreign_hooks(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"hooks": [{"type": "command", "command": "/other/hook"}]}]}}))
    install.ensure_registered(CMD, s)
    data = read(s)
    cmds = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert "/other/hook" in cmds and CMD in cmds


def test_is_registered(tmp_path):
    s = tmp_path / "settings.json"
    assert not install.is_registered(CMD, [s])
    install.ensure_registered(CMD, s)
    assert install.is_registered(CMD, [s])


def test_uninstall(tmp_path):
    s = tmp_path / "settings.json"
    install.ensure_registered(CMD, s)
    assert install.uninstall(s) is True
    data = read(s)
    assert "PostToolUse" not in data.get("hooks", {})
    assert install.uninstall(s) is False  # nothing left to remove


def test_uninstall_keeps_foreign_hooks(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"hooks": [{"type": "command", "command": "/other/hook"}]}]}}))
    install.ensure_registered(CMD, s)
    install.uninstall(s)
    data = read(s)
    cmds = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert cmds == ["/other/hook"]


def test_malformed_json_rejected(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text("{not valid")
    with pytest.raises(ValueError):
        install.ensure_registered(CMD, s)


def test_backup_created_on_change(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"PostToolUse": []}}))
    install.ensure_registered(CMD, s)
    backups = list(tmp_path.glob("settings.json.bak.*"))
    assert backups


# ── multi-hook (guard + keepgoing) ──────────────────────────────────────


def test_ensure_registers_both_hooks(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    monkeypatch.setattr(install.sys, "argv", ["/abs/path/to/ccloop"])
    status = install.ensure_registered(settings_path=s)
    assert status in ("added", "updated")
    data = read(s)
    post = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    stop = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "/abs/path/to/ccloop guard" in post
    assert "/abs/path/to/ccloop keepgoing" in stop


def test_ensure_idempotent_multi_hook(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    monkeypatch.setattr(install.sys, "argv", ["/abs/path/to/ccloop"])
    install.ensure_registered(settings_path=s)
    assert install.ensure_registered(settings_path=s) == "present"
    data = read(s)
    assert len(data["hooks"]["PostToolUse"]) == 1
    assert len(data["hooks"]["Stop"]) == 1


def test_relocate_self_heals_both(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    monkeypatch.setattr(install.sys, "argv", ["/old/place/ccloop"])
    install.ensure_registered(settings_path=s)
    monkeypatch.setattr(install.sys, "argv", ["/new/place/ccloop"])
    status = install.ensure_registered(settings_path=s)
    assert status == "updated"
    data = read(s)
    post = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    stop = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert post == ["/new/place/ccloop guard"]
    assert stop == ["/new/place/ccloop keepgoing"]


def test_uninstall_removes_both(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    monkeypatch.setattr(install.sys, "argv", ["/abs/ccloop"])
    install.ensure_registered(settings_path=s)
    assert install.uninstall(s) is True
    data = read(s)
    assert "hooks" not in data or not data["hooks"]
    assert install.uninstall(s) is False  # nothing to remove now


def test_legacy_bash_recognized_as_ours():
    # The legacy bash hook lived under PostToolUse.
    assert install._is_ours("/src/ccloop/hooks/context-guard.sh")
    # The new entries.
    assert install._is_ours("/abs/ccloop guard")
    assert install._is_ours("/abs/ccloop keepgoing")
    # Not ours.
    assert not install._is_ours("/other/tool --help")
