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


def test_save_stores_config_in_lab_and_enables_it(eve_env, fake_ios2):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    start = time.time()
    text = _call("save_lab_configs")
    assert "✅ R1: saved and stored in the lab" in text
    assert "Skipped (not running): R2, R3" in text
    assert time.time() - start < 20
    c = EVENGClientWrapper()
    asyncio.run(c.connect())
    stored = c.api.get_node_config_by_id(LAB, 1)["data"]["data"]
    assert stored.startswith("!\nhostname R1") and stored.rstrip().endswith("end")
    assert "GRUB" not in stored                  # interleaved log line filtered out
    assert c.api.list_nodes(LAB)["data"]["1"]["config"] == 1   # boot from config enabled


def test_running_config_parser():
    from eveng_mcp_server.tools.snapshot_tools import running_config_from_transcript
    t = ("R1# write memory\nBuilding configuration...\n[OK]\nR1# show running-config\n"
         "Building configuration...\n\nCurrent configuration : 99 bytes\n!\nversion 15.9\n"
         "*Oct 17 21:00:52.834: %GRUB-5-CONFIG_WRITTEN: GRUB configuration was written\nhostname R1\n!\nend\n")
    assert running_config_from_transcript(t) == "!\nversion 15.9\nhostname R1\n!\nend\n"


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
