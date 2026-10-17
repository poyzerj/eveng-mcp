"""Fixtures that run fake EVE-NG, IOS console and SSH servers as subprocesses.

Each fake gets its own POD so their console ports never collide:
  fake_ios  (boot dialog, config modes)   POD 2  -> ports 33025..33028
  fake_ios2 (questions, debug, ping, logs) POD 3 -> port 33153 (node 1)
  fake_ssh  (unl_wrapper over SSH)        POD 4 -> opens/closes 33281.. on start/stop
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

FAKES = Path(__file__).parent / "fakes"
EVE_PORT = 18099
SSH_PORT = 12222


def _wait_port(port: int, timeout: float = 10):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"fake server on port {port} did not start")


def _port_free(port: int, timeout: float = 10):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)


def _spawn(script: str, *args, env=None, port=None, tmp_path=None):
    if port:
        _port_free(port)   # don't mistake a previous test's dying server for this one
    log = open((tmp_path or Path("/tmp")) / f"{script}.log", "w")
    p = subprocess.Popen([sys.executable, str(FAKES / script), *map(str, args)],
                         env={**os.environ, **(env or {})}, stdout=log, stderr=subprocess.STDOUT)
    if port:
        _wait_port(port)
    return p


@pytest.fixture
def fake_eve(tmp_path):
    p = _spawn("fake_eve.py", env={"FAKE_EVE_PORT": str(EVE_PORT)}, port=EVE_PORT, tmp_path=tmp_path)
    yield p
    p.kill(); p.wait()


@pytest.fixture
def eve_env(monkeypatch, fake_eve):
    for k, v in {"EVENG_HOST": "127.0.0.1", "EVENG_PORT": str(EVE_PORT), "EVENG_PROTOCOL": "http",
                 "EVENG_USERNAME": "admin", "EVENG_PASSWORD": "eve"}.items():
        monkeypatch.setenv(k, v)
    # settings are loaded once at import; reload so the env above is used
    from eveng_mcp_server.config.settings import reload_config
    reload_config()
    yield
    reload_config()


@pytest.fixture
def fake_ios(tmp_path):
    """Routers 1-4 on POD 2 that 'boot' for 2s, show the setup dialog, then a CLI."""
    p = _spawn("fake_ios.py", 2, env={"FAKE_POD": "2"}, port=32768 + 128 * 2 + 1, tmp_path=tmp_path)
    yield {"host": "127.0.0.1", "pod": 2}
    p.kill(); p.wait()


@pytest.fixture
def slow_ios(tmp_path):
    """Same, but boot takes 60s (to test the 'still booting' reply)."""
    p = _spawn("fake_ios.py", 60, env={"FAKE_POD": "2"}, port=32768 + 128 * 2 + 1, tmp_path=tmp_path)
    yield {"host": "127.0.0.1", "pod": 2}
    p.kill(); p.wait()


@pytest.fixture
def fake_ios2(tmp_path):
    """Router on POD 3 with [confirm]/[yes/no] questions, debug, endless ping, shutdown logs."""
    p = _spawn("fake_ios2.py", env={"FAKE_POD": "3"}, port=32768 + 128 * 3 + 1, tmp_path=tmp_path)
    yield {"host": "127.0.0.1", "port": 32768 + 128 * 3 + 1, "log": tmp_path / "fake_ios2.py.log"}
    p.kill(); p.wait()


@pytest.fixture
def ssh_keys(tmp_path):
    import asyncssh
    k = asyncssh.generate_private_key("ssh-ed25519")
    k.write_private_key(str(tmp_path / "id_ed25519"))
    k.write_public_key(str(tmp_path / "authorized_keys"))
    return tmp_path


@pytest.fixture
def fake_ssh(tmp_path, ssh_keys):
    def start():
        return _spawn("fake_ssh.py", env={"FAKE_SSH_PORT": str(SSH_PORT),
                      "FAKE_AUTHORIZED_KEYS": str(ssh_keys / "authorized_keys")},
                      port=SSH_PORT, tmp_path=tmp_path)
    procs = [start()]

    def restart():  # new host key, like a rebuilt (or impersonated) server
        procs[-1].kill(); procs[-1].wait()
        procs.append(start())
    yield {"restart": restart, "log": tmp_path / "fake_ssh.py.log", "keys": ssh_keys}
    for p in procs:
        p.kill(); p.wait()


@pytest.fixture
def ssh_env(monkeypatch, fake_ssh, tmp_path):
    monkeypatch.setenv("EVENG_SSH_USER", "root")
    monkeypatch.setenv("EVENG_SSH_PORT", str(SSH_PORT))
    monkeypatch.setenv("EVENG_SSH_KEY", str(fake_ssh["keys"] / "id_ed25519"))
    monkeypatch.setenv("EVENG_SSH_KNOWN_HOSTS", str(tmp_path / "known_hosts"))
    monkeypatch.delenv("EVENG_SSH_PASSWORD", raising=False)
    return fake_ssh
