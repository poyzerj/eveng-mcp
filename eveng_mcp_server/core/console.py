"""Telnet console driver for Cisco IOS-style devices running in EVE-NG.

EVE-NG exposes each running node's serial console as a raw telnet server on the
EVE-NG host. Port = 32768 + 128 * pod + node_id, where pod is the EVE-NG user's
POD number (admin is 0). Telnet consoles need no login.

Pure asyncio sockets (no telnetlib, which was removed in Python 3.13).
"""

import asyncio
import re
from typing import Dict, List, Optional, Tuple

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3

PROMPT_RE = re.compile(r"(?:^|[\r\n])([A-Za-z0-9_.\-]+(?:\([^)\r\n]*\))?[>#])\s*$")
SPECIAL = [
    (re.compile(r"initial configuration dialog\?\s*\[yes/no\]:\s*$", re.I), "no\r"),
    (re.compile(r"terminate autoinstall\?\s*\[yes\]:?\s*$", re.I), "yes\r"),
    (re.compile(r"Press RETURN to get started", re.I), "\r"),
    (re.compile(r"--More--\s*$"), " "),
    (re.compile(r"\[confirm\]\s*$"), "\r"),
    (re.compile(r"Destination filename \[[^\]]*\]\?\s*$"), "\r"),
]
ERROR_RE = re.compile(r"^\s*(% Invalid input|% Incomplete command|% Ambiguous command|% Unknown command)", re.M)


def console_port(node_id: int, pod: int = 0) -> int:
    return 32768 + 128 * int(pod) + int(node_id)


class ConsoleError(Exception):
    pass


class IOSConsole:
    def __init__(self, host: str, port: int, timeout: float = 20.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.prompt = ""

    # ---- transport -------------------------------------------------------
    async def open(self):
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=8)
        except (OSError, asyncio.TimeoutError) as e:
            raise ConsoleError(
                f"Can't reach console {self.host}:{self.port} ({e.__class__.__name__}). "
                "The node is probably not running for this POD, or someone else holds its console.")

    async def close(self):
        if self.writer:
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except Exception:
                pass

    def _send_raw(self, b: bytes):
        self.writer.write(b)

    async def send(self, text: str):
        self._send_raw(text.encode("utf-8", "replace"))
        await self.writer.drain()

    def _strip_telnet(self, data: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b != IAC:
                out.append(b); i += 1; continue
            if i + 1 >= len(data):
                break
            cmd = data[i + 1]
            if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                opt = data[i + 2]
                if cmd == DO:
                    self._send_raw(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    self._send_raw(bytes([IAC, DO if opt in (ECHO, SGA) else DONT, opt]))
                i += 3
            elif cmd == SB:
                j = data.find(bytes([IAC, SE]), i)
                i = len(data) if j < 0 else j + 2
            elif cmd == IAC:
                out.append(IAC); i += 2
            else:
                i += 2
        return bytes(out)

    async def read_until_prompt(self, timeout: Optional[float] = None, poke_every: float = 0) -> str:
        """Read until an IOS prompt; auto-answers boot/setup questions."""
        timeout = timeout or self.timeout
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        next_poke = loop.time() + poke_every if poke_every else None
        buf = ""
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                raise ConsoleError(f"No prompt within {int(timeout)}s. Last output:\n{buf[-400:]}")
            wait = min(remaining, 1.0)
            try:
                chunk = await asyncio.wait_for(self.reader.read(4096), timeout=wait)
                if not chunk:
                    raise ConsoleError("Console connection closed by the server.")
                buf += self._strip_telnet(chunk).decode("utf-8", "replace")
            except asyncio.TimeoutError:
                pass
            tail = buf[-300:]
            answered = False
            for rx, reply in SPECIAL:
                if rx.search(tail):
                    await self.send(reply)
                    buf = rx.sub("", buf)
                    answered = True
                    break
            if answered:
                continue
            m = PROMPT_RE.search(buf)
            if m:
                self.prompt = m.group(1)
                return buf
            if next_poke and loop.time() >= next_poke:
                await self.send("\r")
                next_poke = loop.time() + poke_every

    # ---- IOS session -----------------------------------------------------
    async def prepare(self, boot_wait: float = 120):
        """Get to privileged exec with paging off. Waits for boot if needed."""
        await self.send("\r")
        await self.read_until_prompt(timeout=boot_wait, poke_every=5)
        if "(" in self.prompt:          # stuck in config mode
            await self.send("end\r")
            await self.read_until_prompt()
        if self.prompt.endswith(">"):
            await self.send("enable\r")
            out = await self.read_until_prompt()
            if "Password" in out and not self.prompt.endswith("#"):
                raise ConsoleError("enable needs a password; not supported yet.")
        for c in ("terminal length 0", "terminal width 0"):
            await self.send(c + "\r")
            await self.read_until_prompt()

    async def command(self, cmd: str, timeout: Optional[float] = None) -> str:
        await self.send(cmd + "\r")
        raw = await self.read_until_prompt(timeout=timeout)
        lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # drop echoed command and trailing prompt
        if lines and cmd.strip() and cmd.strip() in lines[0]:
            lines = lines[1:]
        while lines and (not lines[-1].strip() or PROMPT_RE.search("\n" + lines[-1])):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n")


# One job per device at a time. Two sessions typing into the same console
# garble each other (an `end` from one knocks the other out of config mode).
_LOCKS: Dict[Tuple[str, int], asyncio.Lock] = {}


class DeviceBusy(ConsoleError):
    pass


class StillBooting(ConsoleError):
    pass


async def run_commands(host: str, port: int, commands: List[str], mode: str = "exec",
                       save: bool = False, timeout: float = 30, boot_wait: float = 40,
                       lock_wait: float = 15) -> Tuple[str, List[str]]:
    """Run commands on one device. Returns (transcript, errors).

    - Waits up to lock_wait seconds if another job holds this device, then gives up.
    - Waits up to boot_wait seconds for a prompt, then reports StillBooting (retry later).
    - Config mode stops at the first rejected line, exits config mode, and skips the save.
    """
    lock = _LOCKS.setdefault((host, port), asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), timeout=lock_wait)
    except asyncio.TimeoutError:
        raise DeviceBusy("Another job is still running on this device. Try again in a moment.")
    con = IOSConsole(host, port, timeout=timeout)
    transcript, errors = [], []
    try:
        await con.open()
        try:
            await con.prepare(boot_wait=boot_wait)
        except ConsoleError as e:
            if str(e).startswith("No prompt within"):
                raise StillBooting(
                    f"No CLI prompt after {int(boot_wait)}s - the device is probably still booting. "
                    "Nothing was sent. Try again in a minute.")
            raise
        if mode == "config":
            await con.command("configure terminal")
        sent = [c for c in commands if c.strip()]
        for i, cmd in enumerate(sent):
            before = con.prompt
            out = await con.command(cmd)
            transcript.append(f"{before} {cmd}" + (f"\n{out}" if out else ""))
            if ERROR_RE.search(out):
                last = out.strip().splitlines()[-1] if out.strip() else "error"
                errors.append(f"{cmd} -> {last}")
                if mode == "config":
                    skipped = sent[i + 1:]
                    if skipped:
                        errors.append(f"Stopped here. {len(skipped)} remaining line(s) NOT sent: " + "; ".join(s.strip() for s in skipped))
                    break
        if mode == "config":
            await con.command("end")
            if save and not errors:
                out = await con.command("write memory", timeout=60)
                transcript.append(f"{con.prompt} write memory\n{out}")
            elif save:
                errors.append("Config was NOT saved because of the error above.")
        return "\n".join(transcript), errors
    finally:
        await con.close()
        lock.release()
