"""Snapshot tools end to end: fake EVE-NG (REST), fake IOS console (POD 3, node 1), fake SSH."""

import asyncio
import os
import time

from mcp.server.fastmcp import FastMCP

LAB = "/CCNP - Test.unl"


def _call(tool, **args):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    from eveng_mcp_server.tools.snapshot_tools import register_snapshot_tools
    mcp = FastMCP("t")
    register_snapshot_tools(mcp, EVENGClientWrapper())
    r = asyncio.run(mcp.call_tool(tool, {"arguments": {"lab_path": LAB, "pod": 3, **args}}))
    content = r[0] if isinstance(r, tuple) else r
    return content[0].text


import pytest


def _fake_export_env(monkeypatch, tmp_path, seconds):
    monkeypatch.setenv("FAKE_WRAPPER_LOG", str(tmp_path / "wrapper.log"))
    monkeypatch.setenv("FAKE_EXPORT_SECONDS", str(seconds))
    monkeypatch.setenv("EVENG_EXPORT_LOG_DIR", str(tmp_path))
    return tmp_path / "wrapper.log"


@pytest.fixture
def quick_export(monkeypatch, tmp_path):   # request BEFORE ssh_env so the fake inherits it
    return _fake_export_env(monkeypatch, tmp_path, 1)


@pytest.fixture
def slow_export(monkeypatch, tmp_path):
    return _fake_export_env(monkeypatch, tmp_path, 60)


def test_save_exports_one_at_a_time_in_background_job(eve_env, fake_ios2, quick_export, ssh_env):
    start = time.time()
    text = _call("save_lab_configs")
    assert "✅ R1: saved and exported" in text
    assert "Skipped (not running): R2, R3" in text
    assert time.time() - start < 30
    assert quick_export.read_text().split()[:6] == ["-a", "export", "-T", "3", "-D", "1"]
    status = _call("export_status")
    assert "R1: export ok" in status and "Finished." in status


def test_slow_export_returns_in_time_and_keeps_going(eve_env, fake_ios2, slow_export, ssh_env):
    start = time.time()
    text = _call("save_lab_configs")
    assert time.time() - start < 55            # never runs into the ~60s remote limit
    assert "⏳ R1: saved; export still running in the background" in text
    assert "export_status" in text
    assert "Still running" in _call("export_status")


def test_backup_writes_files(eve_env, fake_ios2, monkeypatch, tmp_path):
    monkeypatch.setenv("EVENG_BACKUP_DIR", str(tmp_path / "bk"))
    text = _call("backup_lab_configs", label="ospf done")
    assert "✅ R1:" in text
    files = list((tmp_path / "bk").rglob("R1.cfg"))
    assert len(files) == 1 and files[0].parent.name.endswith("-ospf-done")
    assert "hostname R1" in files[0].read_text()


def test_reset_requires_confirmation(eve_env, ssh_env):
    text = _call("reset_lab", nodes=["R2"])
    assert text.startswith("NEEDS CONFIRMATION")
    assert not [l for l in ssh_env["log"].read_text().splitlines() if l.startswith("CMD")]
    text = _call("reset_lab", nodes=["R2"], confirm=True)
    cmds = [l for l in ssh_env["log"].read_text().splitlines() if l.startswith("CMD")]
    assert [c.split()[3] for c in cmds] == ["stop", "wipe", "start"]
