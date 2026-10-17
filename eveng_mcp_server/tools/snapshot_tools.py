"""Lab snapshot tools: save, back up, and reset whole-lab device configs.

- save_lab_configs:   `write memory` on every running node, then export the
                      configs into the lab file (unl_wrapper -a export) so the
                      lab boots preconfigured - like EVE-NG's "Export all CFGs".
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
from ..core.power import PowerError, build_command, is_running, ssh_run, ssh_settings
from .power_tools import _pod, _resolve_nodes, do_power

logger = get_logger("SnapshotTools")


def backup_root() -> str:
    return os.path.expanduser(os.environ.get("EVENG_BACKUP_DIR", "~/eveng-mcp-backups"))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "lab"


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
        Save every running node's config and export it into the lab file, so the
        lab boots with these configs next time (and reset_lab returns to them).
        Runs `write memory` on each node (in parallel), then EVE-NG's export over SSH.
        """
        try:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            pod = _pod(arguments.pod)
            host, up, down = await _running(arguments.lab_path, arguments.nodes, pod)
            if not up:
                return [TextContent(type="text", text="No running nodes to save.")]

            async def wr(nid):
                try:
                    _, errs = await run_commands(host, console_port(int(nid), pod), ["write memory"], timeout=40)
                    return "ok" if not errs else "; ".join(errs)
                except ConsoleError as e:
                    return str(e)
            saved = await asyncio.gather(*(wr(n) for n, _ in up))

            t_saved = loop.time()
            user = ssh_settings()["user"]
            lines, ok_nodes = [], []
            for (nid, name), w in zip(up, saved):
                if w == "ok":
                    ok_nodes.append((nid, name))
                else:
                    lines.append(f"⚠️ {name}: write memory failed ({w}); not exported")
            if ok_nodes:
                # One SSH session; all exports run in parallel on EVE-NG (each one reads the
                # node's config over its console, which takes several seconds).
                parts = []
                for nid, name in ok_nodes:
                    c = build_command("export", pod, int(nid), arguments.lab_path, user)
                    parts.append(f"( {c} >/dev/null 2>&1; echo \"RC {nid} $?\" ) &")
                script = " ".join(parts) + " wait"
                rc, out = await ssh_run(host, script, timeout=45)
                codes = dict(re.findall(r"RC (\d+) (\d+)", out))
                for nid, name in ok_nodes:
                    code = codes.get(str(nid))
                    lines.append(f"✅ {name}: saved and exported" if code == "0"
                                 else f"⚠️ {name}: saved on the device, export {'exit ' + code if code else 'gave no result'}")
            lines.append(f"(write memory {t_saved - t0:.0f}s, export {loop.time() - t_saved:.0f}s)")
            if down:
                lines.append("Skipped (not running): " + ", ".join(n for _, n in down))
            return [TextContent(type="text", text=f"{arguments.lab_path}\n" + "\n".join(lines))]
        except PowerError as e:
            return [TextContent(type="text", text=f"Power/SSH error: {e}")]
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
