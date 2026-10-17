"""REST-side fixes (links, interfaces, session re-login, layout) and SSH power tools."""

import asyncio
import os
import urllib.request

import pytest

from eveng_mcp_server.core import power
from eveng_mcp_server.core.power import PowerError, build_command, lab_file

LAB = "/CCNP - Test.unl"


def run(coro):
    return asyncio.run(coro)


# ---- REST: links, interface names, session expiry -------------------------

def _client():
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    return EVENGClientWrapper()


def _ifaces(c, node):
    data = c.api.get_node_interfaces(LAB, node)["data"]["ethernet"]
    return {i["name"]: i["network_id"] for i in data}


def test_link_by_node_id_and_short_interface_names(eve_env):
    c = _client()
    assert run(c.connect_node_to_node(LAB, "1", "Gi0/0", "2", "Gi0/0")) is True
    assert _ifaces(c, "1")["Gi0/0"] == _ifaces(c, "2")["Gi0/0"] != 0


def test_long_and_lowercase_interface_names_and_session_expiry(eve_env):
    c = _client()
    run(c.connect())
    # kill every session, like EVE-NG's idle timeout or a web UI login as the same user
    urllib.request.urlopen(f"http://127.0.0.1:{os.environ['EVENG_PORT']}/test/expire")
    assert run(c.connect_node_to_node(LAB, "R2", "GigabitEthernet0/1", "3", "gi0/0")) is True


def test_bad_interface_lists_real_ones(eve_env):
    c = _client()
    with pytest.raises(Exception) as e:
        run(c.connect_node_to_node(LAB, "1", "Gi0/9", "2", "Gi0/3"))
    assert "Available ethernet interfaces: Gi0/0, Gi0/1, Gi0/2, Gi0/3" in str(e.value)


def test_move_node_uses_pixels(eve_env):
    c = _client()
    run(c.move_node(LAB, "2", 500, 200))
    nodes = c.api.list_nodes(LAB)["data"]
    assert nodes["2"]["left"] == 500 and nodes["2"]["top"] == 200


# ---- power: command building is locked down --------------------------------

def test_wrapper_command_is_validated():
    cmd = build_command("start", 0, 4, "/OSPF - Square.unl", "root")
    assert cmd == "/opt/unetlab/wrappers/unl_wrapper -a start -T 0 -D 4 -F '/opt/unetlab/labs/OSPF - Square.unl'"
    assert build_command("stop", 0, 1, "lab", "joe").startswith("sudo -n ")
    with pytest.raises(PowerError):
        build_command("reboot", 0, 1, "/x.unl", "root")
    with pytest.raises(PowerError):
        lab_file("/../../etc/passwd")
    with pytest.raises(ValueError):
        build_command("start", 0, "1; rm -rf /", "/x.unl", "root")


# ---- power over SSH: auth, host key pinning, start/stop --------------------

def test_start_stop_with_key_auth(ssh_env):
    host = "127.0.0.1"
    rc, _ = run(power.ssh_run(host, build_command("start", 4, 1, LAB, "root")))
    assert rc == 0 and run(power.wait_state(host, 1, 4, True, 5))
    rc, _ = run(power.ssh_run(host, build_command("stop", 4, 1, LAB, "root")))
    assert rc == 0 and run(power.wait_state(host, 1, 4, False, 5))


def test_host_key_pinned_and_change_refused(ssh_env, tmp_path):
    cmd = build_command("start", 4, 2, LAB, "root")
    run(power.ssh_run("127.0.0.1", cmd))
    assert "ssh-ed25519" in (tmp_path / "known_hosts").read_text()
    ssh_env["restart"]()                      # server comes back with a different host key
    with pytest.raises(PowerError, match="does NOT match"):
        run(power.ssh_run("127.0.0.1", cmd))


def test_wrong_password_and_missing_key(ssh_env, monkeypatch, tmp_path):
    monkeypatch.delenv("EVENG_SSH_KEY")
    monkeypatch.setenv("EVENG_SSH_PASSWORD", "wrong")
    with pytest.raises(PowerError, match="refused"):
        run(power.ssh_run("127.0.0.1", build_command("start", 4, 3, LAB, "root")))
    monkeypatch.delenv("EVENG_SSH_PASSWORD")
    monkeypatch.setenv("EVENG_SSH_KEY", str(tmp_path / "nope"))
    with pytest.raises(PowerError, match="doesn't exist"):
        run(power.ssh_run("127.0.0.1", build_command("start", 4, 3, LAB, "root")))


def test_no_ssh_configured(monkeypatch):
    monkeypatch.delenv("EVENG_SSH_KEY", raising=False)
    monkeypatch.delenv("EVENG_SSH_PASSWORD", raising=False)
    with pytest.raises(PowerError, match="not configured"):
        run(power.ssh_run("127.0.0.1", "true"))
