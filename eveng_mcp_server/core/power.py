"""Start/stop EVE-NG nodes for a specific POD over SSH.

Why: the REST API starts nodes for the *logged-in* user only. When this server
logs in as its own user (so it doesn't kick you out of the web UI), nodes it
starts through the API are separate copies you can't see. EVE-NG's own
wrapper on the server can start/stop nodes for any POD directly:

    /opt/unetlab/wrappers/unl_wrapper -a start|stop|wipe -T <pod> -D <node_id> -F <lab file>

Only that wrapper is ever run, with validated arguments.

Running state is checked by trying the node's console port
(32768 + 128 * pod + node_id), which works for any POD.
"""

import asyncio
import logging
import os
import posixpath
import shlex
from typing import Optional, Tuple

from .console import console_port

logging.getLogger("asyncssh").setLevel(logging.WARNING)

WRAPPER = "/opt/unetlab/wrappers/unl_wrapper"
LABS_ROOT = "/opt/unetlab/labs"
ACTIONS = ("start", "stop", "wipe")


class PowerError(Exception):
    pass


def ssh_settings() -> dict:
    return {
        "user": os.environ.get("EVENG_SSH_USER", "root"),
        "password": os.environ.get("EVENG_SSH_PASSWORD") or None,
        "port": int(os.environ.get("EVENG_SSH_PORT", "22")),
        "key": os.environ.get("EVENG_SSH_KEY") or None,
    }


def lab_file(lab_path: str) -> str:
    p = "/" + lab_path.strip().lstrip("/")
    if not p.endswith(".unl"):
        p += ".unl"
    norm = posixpath.normpath(p)
    if ".." in norm.split("/") or not norm.endswith(".unl"):
        raise PowerError(f"Invalid lab path: {lab_path}")
    return LABS_ROOT + norm


def build_command(action: str, pod: int, node_id: int, lab_path: str, user: str) -> str:
    if action not in ACTIONS:
        raise PowerError(f"action must be one of {ACTIONS}")
    cmd = (f"{WRAPPER} -a {action} -T {int(pod)} -D {int(node_id)} "
           f"-F {shlex.quote(lab_file(lab_path))}")
    return cmd if user == "root" else "sudo -n " + cmd


async def ssh_run(host: str, command: str, timeout: float = 40) -> Tuple[int, str]:
    try:
        import asyncssh
    except ImportError:
        raise PowerError("asyncssh is not installed. Run 'uv sync' in the server folder.")
    s = ssh_settings()
    if not s["password"] and not s["key"]:
        raise PowerError("SSH is not configured. Set EVENG_SSH_PASSWORD (or EVENG_SSH_KEY) "
                         "in the MCP server's env in the Claude Desktop config.")
    kwargs = dict(port=s["port"], username=s["user"], known_hosts=None)
    if s["key"]:
        kwargs["client_keys"] = [s["key"]]
    if s["password"]:
        kwargs["password"] = s["password"]
    try:
        async with asyncssh.connect(host, **kwargs) as conn:
            r = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.exit_status or 0, out
    except asyncssh.PermissionDenied:
        raise PowerError(f"SSH login to {host} as '{s['user']}' was refused (wrong password or key).")
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
        raise PowerError(f"SSH to {host}:{s['port']} failed: {e.__class__.__name__}: {e}")


async def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        w.close()
        try:
            await asyncio.wait_for(w.wait_closed(), 1)
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def is_running(host: str, node_id: int, pod: int) -> bool:
    return await port_open(host, console_port(node_id, pod))


async def wait_state(host: str, node_id: int, pod: int, want_running: bool, seconds: float = 12) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        if await is_running(host, node_id, pod) == want_running:
            return True
        if loop.time() >= end:
            return False
        await asyncio.sleep(1)
