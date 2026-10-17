"""Power tools: start/stop/wipe nodes for a chosen EVE-NG POD (default 0 = admin) over SSH."""

import asyncio
import os
from typing import List, Optional, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.power import PowerError, build_command, ssh_run, ssh_settings, is_running, wait_state

logger = get_logger("PowerTools")


def _pod(p: Optional[int]) -> int:
    return p if p is not None else int(os.environ.get("EVENG_CONSOLE_POD", "0"))


class PowerNodesArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    action: str = Field(description="'start', 'stop', or 'wipe' (wipe resets a stopped node to factory config)")
    nodes: Optional[List[str]] = Field(default=None, description="Node names or IDs. Leave empty for every node in the lab.")
    pod: Optional[int] = Field(default=None, description="EVE-NG POD to act on. Default: EVENG_CONSOLE_POD env, else 0 (admin)")


class NodeStatusArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    pod: Optional[int] = Field(default=None, description="EVE-NG POD to check. Default: EVENG_CONSOLE_POD env, else 0 (admin)")


def register_power_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:

    async def _resolve(lab_path: str, refs: Optional[List[str]]):
        await eveng_client.ensure_connected()
        if refs:
            return [await asyncio.to_thread(eveng_client._node_name, lab_path, r) for r in refs]
        nodes = (await eveng_client.list_nodes(lab_path)).get("data") or {}
        items = [(str(n.get("id", k)), n.get("name", str(k))) for k, n in nodes.items()]
        return sorted(items, key=lambda x: int(x[0]))

    @mcp.tool()
    async def power_nodes(arguments: PowerNodesArgs) -> list[TextContent]:
        """
        Start, stop, or wipe nodes as the EVE-NG web UI user (POD 0 = admin by default).

        Unlike start_node/stop_node (which act for this server's own login and create
        copies the web UI user can't see), this runs EVE-NG's own unl_wrapper over
        SSH, so the nodes power on/off in the same lab view the user has open, and
        run_node_commands can reach them. Add all links BEFORE starting nodes.
        After 'start', devices still need 1-3 minutes to boot before CLI works.
        """
        try:
            action = arguments.action.lower().strip()
            pod = _pod(arguments.pod)
            host = eveng_client.config.eveng.host
            user = ssh_settings()["user"]
            targets = await _resolve(arguments.lab_path, arguments.nodes)
            if not targets:
                return [TextContent(type="text", text="No nodes found in that lab.")]
            lines = []
            for nid, name in targets:
                cmd = build_command(action, pod, int(nid), arguments.lab_path, user)
                rc, out = await ssh_run(host, cmd)
                if action in ("start", "stop"):
                    ok = await wait_state(host, int(nid), pod, want_running=(action == "start"))
                    state = ("running" if action == "start" else "stopped") if ok else "NOT confirmed"
                else:
                    ok, state = rc == 0, ("wiped" if rc == 0 else "failed")
                mark = "✅" if ok else "⚠️"
                line = f"{mark} {name} (ID {nid}): {action} -> {state}"
                if not ok or rc != 0:
                    line += f"\n     exit {rc}: {out[-400:] or '(no output)'}"
                lines.append(line)
                if action == "start" and len(targets) > 1:
                    await asyncio.sleep(1)  # small stagger, like the web UI
            tail = "\n\nDevices take 1-3 minutes to boot. run_node_commands will say if one is still booting." if action == "start" else ""
            return [TextContent(type="text", text=f"POD {pod} - {arguments.lab_path}\n" + "\n".join(lines) + tail)]
        except PowerError as e:
            return [TextContent(type="text", text=f"Power error: {e}")]
        except Exception as e:
            logger.error(f"power_nodes failed: {e}")
            return [TextContent(type="text", text=f"Failed to {arguments.action} nodes: {e}")]

    @mcp.tool()
    async def node_power_status(arguments: NodeStatusArgs) -> list[TextContent]:
        """
        Show which nodes are running for a POD (default 0 = admin), by checking each
        node's console port. Use this instead of list_nodes status, which only shows
        nodes started by this server's own login.
        """
        try:
            pod = _pod(arguments.pod)
            host = eveng_client.config.eveng.host
            targets = await _resolve(arguments.lab_path, None)
            states = await asyncio.gather(*(is_running(host, int(n), pod) for n, _ in targets))
            lines = [f"{'🟢 running' if up else '⚪ stopped'}  {name} (ID {nid})" for (nid, name), up in zip(targets, states)]
            return [TextContent(type="text", text=f"POD {pod} - {arguments.lab_path}\n" + "\n".join(lines))]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to check node status: {e}")]
