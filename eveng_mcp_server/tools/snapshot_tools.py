"""Lab snapshot tools: save, back up, and reset whole-lab device configs.

- save_lab_configs:   `write memory` + `show running-config` on every running
                      node, then store each config in the lab file through the
                      EVE-NG API (startup-config + "boot from config"), so the
                      lab boots preconfigured. Avoids EVE-NG's console-scraping
                      export script, whose 15s timeout fails on longer IOSv configs.
- backup_lab_configs: `show running-config` from every running node into dated
                      files on this PC (versioned history you can diff).
- reset_lab:          stop -> wipe -> start, so nodes boot from the lab's
                      exported configs. Destructive: requires confirm=true,
                      which must only be set after the user approves.
"""

import asyncio
import datetime as dt
import os
import re
from typing import List, Optional, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.console import ConsoleError, console_port, run_commands
from ..core.power import PowerError, is_running
from .power_tools import _pod, _resolve_nodes, do_power

logger = get_logger("SnapshotTools")


def backup_root() -> str:
    return os.path.expanduser(os.environ.get("EVENG_BACKUP_DIR", "~/eveng-mcp-backups"))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "lab"


def running_config_from_transcript(t: str) -> str:
    """Pull the config body out of a `show running-config` transcript:
    from the first line after 'Current configuration' through the final 'end'."""
    m = re.search(r"show running-config\n(.*)", t, re.S)
    body = m.group(1) if m else t
    lines = body.split("\n")
    for i, l in enumerate(lines):
        if l.startswith("Current configuration"):
            lines = lines[i + 1:]
            break
    # drop syslog lines that can interleave (e.g. %GRUB right after write memory)
    lines = [l for l in lines if not re.match(r"^\*?[A-Z][a-z]{2} +\d+ \d\d:\d\d:\d\d", l)]
    while lines and lines[-1].strip() != "end":
        lines.pop()
    return "\n".join(lines).strip("\n") + "\n" if lines else ""


class LabNodesArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    nodes: Optional[List[str]] = Field(default=None, description="Node names or IDs. Leave empty for every running node.")
    pod: Optional[int] = Field(default=None, description="EVE-NG POD. Default: EVENG_CONSOLE_POD env, else 0 (admin)")


class BackupArgs(LabNodesArgs):
    label: Optional[str] = Field(default=None, description="Optional short name added to the backup folder, e.g. 'ospf-done'")


class ResetArgs(LabNodesArgs):
    confirm: bool = Field(default=False, description="Must be true to actually reset. ONLY set true after the user explicitly approved losing unsaved changes on these nodes.")


def register_snapshot_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:

    async def _running(lab_path: str, refs: Optional[List[str]], pod: int):
        host = eveng_client.config.eveng.host
        targets = await _resolve_nodes(eveng_client, lab_path, refs)
        states = await asyncio.gather(*(is_running(host, int(n), pod) for n, _ in targets))
        up = [t for t, s in zip(targets, states) if s]
        down = [t for t, s in zip(targets, states) if not s]
        return host, up, down

    @mcp.tool()
    async def save_lab_configs(arguments: LabNodesArgs) -> list[TextContent]:
        """
        Save every running node's config and store it in the lab file as its
        startup-config, so the lab boots with these configs (and reset_lab returns
        to them). Per node, in parallel: `write memory` + `show running-config` over
        the console, then upload through the EVE-NG API and read it back to verify.
        """
        try:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            pod = _pod(arguments.pod)
            host, up, down = await _running(arguments.lab_path, arguments.nodes, pod)
            if not up:
                return [TextContent(type="text", text="No running nodes to save.")]

            async def one(nid, name):
                try:
                    t, errs = await run_commands(host, console_port(int(nid), pod),
                                                 ["write memory", "show running-config"], timeout=30)
                except ConsoleError as e:
                    return f"⚠️ {name}: {e}"
                if errs:
                    return f"⚠️ {name}: {'; '.join(errs)}"
                cfg = running_config_from_transcript(t)
                if not cfg:
                    return f"⚠️ {name}: saved on the device, but couldn't read its running-config"
                try:
                    stored = await eveng_client.set_startup_config(arguments.lab_path, nid, cfg)
                except Exception as e:
                    return f"⚠️ {name}: saved on the device, lab upload failed: {e}"
                if stored < len(cfg) * 0.9:
                    return f"⚠️ {name}: uploaded, but the lab returned {stored} of {len(cfg)} characters"
                return f"✅ {name}: saved and stored in the lab ({len(cfg.splitlines())} lines)"

            lines = list(await asyncio.gather(*(one(n, nm) for n, nm in up)))
            if down:
                lines.append("Skipped (not running): " + ", ".join(n for _, n in down))
            lines.append(f"({loop.time() - t0:.0f}s)")
            return [TextContent(type="text", text=f"{arguments.lab_path}\n" + "\n".join(lines))]
        except Exception as e:
            logger.error(f"save_lab_configs failed: {e}")
            return [TextContent(type="text", text=f"Failed to save lab configs: {e}")]

    @mcp.tool()
    async def backup_lab_configs(arguments: BackupArgs) -> list[TextContent]:
        """
        Copy every running node's `show running-config` into dated files on this PC
        (EVENG_BACKUP_DIR, default ~/eveng-mcp-backups/<lab>/<timestamp>[-label]/<node>.cfg).
        Read-only on the devices.
        """
        try:
            pod = _pod(arguments.pod)
            host, up, down = await _running(arguments.lab_path, arguments.nodes, pod)
            if not up:
                return [TextContent(type="text", text="No running nodes to back up.")]
            stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            if arguments.label:
                stamp += "-" + _safe(arguments.label).replace(" ", "-")
            lab = _safe(os.path.splitext(os.path.basename(arguments.lab_path))[0])
            folder = os.path.join(backup_root(), lab, stamp)
            os.makedirs(folder, exist_ok=True)

            async def grab(nid):
                try:
                    t, errs = await run_commands(host, console_port(int(nid), pod), ["show running-config"], timeout=40)
                    body = t.split("\n", 1)[1] if "\n" in t else ""
                    return body, errs
                except ConsoleError as e:
                    return None, [str(e)]
            results = await asyncio.gather(*(grab(n) for n, _ in up))
            lines = []
            for (nid, name), (body, errs) in zip(up, results):
                if body and not errs:
                    path = os.path.join(folder, f"{_safe(name)}.cfg")
                    with open(path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(body.rstrip() + "\n")
                    lines.append(f"✅ {name}: {len(body.splitlines())} lines")
                else:
                    lines.append(f"⚠️ {name}: {'; '.join(errs) or 'no output'}")
            if down:
                lines.append("Skipped (not running): " + ", ".join(n for _, n in down))
            return [TextContent(type="text", text=f"Backed up to {folder}\n" + "\n".join(lines))]
        except Exception as e:
            logger.error(f"backup_lab_configs failed: {e}")
            return [TextContent(type="text", text=f"Failed to back up lab configs: {e}")]

    @mcp.tool()
    async def reset_lab(arguments: ResetArgs) -> list[TextContent]:
        """
        Reset nodes to the lab's exported configs: stop, wipe, start.
        DESTRUCTIVE - anything not exported with save_lab_configs is lost.
        Without confirm=true this only reports what would happen. Show that to the
        user and set confirm=true only if they approve.
        """
        try:
            targets = await _resolve_nodes(eveng_client, arguments.lab_path, arguments.nodes)
            names = ", ".join(n for _, n in targets)
            if not arguments.confirm:
                return [TextContent(type="text", text=(
                    f"NEEDS CONFIRMATION: reset_lab would stop, wipe and restart: {names}.\n"
                    "Unsaved and un-exported changes on those nodes will be lost; they will boot from "
                    "the configs last exported with save_lab_configs. Ask the user; re-run with "
                    "confirm=true only if they approve."))]
            refs = [n for n, _ in targets]
            report = []
            for action in ("stop", "wipe", "start"):
                report.append(await do_power(eveng_client, arguments.lab_path, action, refs, arguments.pod))
            return [TextContent(type="text", text="\n\n".join(report))]
        except PowerError as e:
            return [TextContent(type="text", text=f"Power/SSH error: {e}")]
        except Exception as e:
            logger.error(f"reset_lab failed: {e}")
            return [TextContent(type="text", text=f"Failed to reset lab: {e}")]
