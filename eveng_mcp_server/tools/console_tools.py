"""Console tools: run show/config commands on running nodes over their telnet console."""

import asyncio
import os
from typing import List, Optional, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.console import run_commands, console_port, ConsoleError

logger = get_logger("ConsoleTools")


class RunCommandsArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    node: str = Field(description="Node name (e.g. 'R1') or node ID")
    commands: List[str] = Field(description="Commands to run, one per item. In config mode, send config lines only (no 'configure terminal'/'end').")
    mode: str = Field(default="exec", description="'exec' for show/ping/etc., 'config' to apply configuration")
    save: bool = Field(default=False, description="In config mode, run 'write memory' afterwards")
    timeout: int = Field(default=30, description="Seconds to wait for each command's output (raise for long pings/traceroutes)")
    boot_wait: int = Field(default=40, description="Seconds to wait for a CLI prompt. If the device is still booting the tool returns quickly so you can retry; keep under ~45s.")
    pod: Optional[int] = Field(default=None, description="EVE-NG POD whose running copy to use. Default: EVENG_CONSOLE_POD env, else 0 (admin)")


def register_console_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:

    @mcp.tool()
    async def run_node_commands(arguments: RunCommandsArgs) -> list[TextContent]:
        """
        Run CLI commands on a running node through its console (Cisco IOS / IOSv / IOL style).

        Use mode='exec' for show commands, ping, traceroute.
        Use mode='config' to push configuration lines (the tool enters and exits
        config mode itself). Boot prompts like the initial configuration dialog are
        answered automatically. If a device is still booting the tool says so without
        sending anything - wait a minute and retry. Only one job runs per device at a
        time. In config mode the tool stops at the first rejected line and does not save.
        Works on nodes started from the EVE-NG web UI by the
        POD in EVENG_CONSOLE_POD (default 0 = admin).
        Run several nodes in parallel by calling this tool once per node.
        """
        try:
            await eveng_client.ensure_connected()
            node_id, node_name = await asyncio.to_thread(eveng_client._node_name, arguments.lab_path, arguments.node)
            pod = arguments.pod if arguments.pod is not None else int(os.environ.get("EVENG_CONSOLE_POD", "0"))
            port = console_port(int(node_id), pod)
            host = eveng_client.config.eveng.host
            mode = "config" if arguments.mode.lower().startswith("conf") else "exec"
            transcript, errors = await run_commands(
                host, port, arguments.commands, mode=mode, save=arguments.save,
                timeout=arguments.timeout, boot_wait=arguments.boot_wait)
            head = f"{node_name} (console {host}:{port}, {mode} mode)\n"
            if errors:
                head += "⚠️ Errors:\n" + "\n".join(f"   {e}" for e in errors) + "\n"
            return [TextContent(type="text", text=head + "\n" + transcript)]
        except ConsoleError as e:
            return [TextContent(type="text", text=f"Console error on {arguments.node}: {e}")]
        except Exception as e:
            logger.error(f"run_node_commands failed: {e}")
            return [TextContent(type="text", text=f"Failed to run commands on {arguments.node}: {e}")]
