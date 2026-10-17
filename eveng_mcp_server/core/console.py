"""Telnet console driver for Cisco IOS-style devices running in EVE-NG.

EVE-NG exposes each running node's serial console as a raw telnet server on the
EVE-NG host. Port = 32768 + 128 * pod + node_id, where pod is the EVE-NG user's
POD number (admin is 0). Telnet consoles need no login.

Pure asyncio sockets (no telnetlib, which was removed in Python 3.13).

Safety rules:
- Boot/setup prompts (initial config dialog, autoinstall, Press RETURN, --More--)
  are answered automatically; they never change anything.
- Device QUESTIONS ([confirm], [yes/no], [y/n], Destination filename [..]?) are
  NOT answered unless the caller passes allow_confirm=True, which the caller must
  only do after the user explicitly approved. Otherwise the question is cancelled
  with Ctrl+C and reported back, and nothing after it is sent.
- A command that never returns to a prompt (long ping/traceroute) is stopped
  with Ctrl+Shift+6 after its timeout.
- Any `debug` command is always followed by `undebug all` before disconnecting.
"""

import asyncio
import re
from typing import Dict, List, Optional, Tuple

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3
CTRL_C = "\x03"
CTRL_SHIFT_6 = "\x1e"

PROMPT_RE = re.compile(r"(?:^|[\r\n])([A-Za-z0-9_.\-]+(?:\([^)\r\n]*\))?[>#])\s*$")
ANY_PROMPT_RE = re.compile(r"[\r\n]([A-Za-z0-9_.\-]+(?:\([^)\r\n]*\))?[>#])[ \t]*[\r\n]")
# Harmless boot/paging prompts: always answered.
AUTO = [
    (re.compile(r"initial configuration dialog\?\s*\[yes/no\]:\s*$", re.I), "no\r"),
    (re.compile(r"terminate autoinstall\?\s*\[yes\]:?\s*$", re.I), "yes\r"),
    (re.compile(r"Press RETURN to get started", re.I), "\r"),
    (re.compile(r"--More--\s*$"), " "),
]
# Real questions: only answered with permission. (regex, answer)
QUESTIONS = [
    (re.compile(r"\[confirm\]\s*$", re.I), "\r"),
    (re.compile(r"\[yes/no\]:?\s*$", re.I), "yes\r"),
    (re.compile(r"\[y/n\]:?\s*$", re.I), "y\r"),
    (re.compile(r"Destination filename \[[^\]]*\]\?\s*$"), "\r"),
    (re.compile(r"(Source|Destination) filename \[[^\]]*\]\?\s*$"), "\r"),
]
ERROR_RE = re.compile(r"^\s*(% Invalid input|% Incomplete command|% Ambiguous command|% Unknown command)", re.M)
DEBUG_RE = re.compile(r"^\s*(debug|deb)\s", re.I)
UNDEBUG_RE = re.compile(r"^\s*(undebug|u|no debug)\s+all", re.I)
RELOAD_RE = re.compile(r"^\s*(reload|reboot)\b", re.I)

MAX_CAPTURE = 40


def console_port(node_id: int, pod: int = 0) -> int:
    return 32768 + 128 * int(pod) + int(node_id)


class ConsoleError(Exception):
    pass


class ConsoleTimeout(ConsoleError):
    def __init__(self, msg: str, buf: str):
        super().__init__(msg)
        self.buf = buf


class NeedsConfirmation(ConsoleError):
    def __init__(self, question: str, buf: str):
        super().__init__(f"Device asked: \"{question}\"")
        self.question = question
        self.buf = buf


class IOSConsole:
    def __init__(self, host: str, port: int, timeout: float = 20.0, allow_confirm: bool = False):
        self.host, self.port, self.timeout = host, port, timeout
        self.allow_confirm = allow_confirm
        self.answered: List[str] = []
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.prompt = ""
        self.between = ""

    # ---- transport -------------------------------------------------------
    async def open(self):
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=8)
        except (OSError, asyncio.TimeoutError) as e:
            raise ConsoleError(
                f"Can't reach console {self.host}:{self.port} ({e.__class__.__name__}). "
                "The node is probably not running for this POD.")

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

    async def _read_chunk(self, wait: float) -> str:
        try:
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout=wait)
        except asyncio.TimeoutError:
            return ""
        if not chunk:
            raise ConsoleError("Console connection closed by the server.")
        return self._strip_telnet(chunk).decode("utf-8", "replace")

    async def drain(self, quiet: float = 0.3, max_time: float = 3.0) -> str:
        """Discard pending output (stale prompts, late echoes) until the line is quiet."""
        loop = asyncio.get_running_loop()
        end = loop.time() + max_time
        junk = ""
        while loop.time() < end:
            chunk = await self._read_chunk(quiet)
            if not chunk:
                break
            junk += chunk
        return junk

    async def read_until_prompt(self, timeout: Optional[float] = None, poke_every: float = 0,
                                echo: Optional[str] = None, anywhere: bool = False) -> str:
        """Read until an IOS prompt. Answers boot prompts; handles questions per allow_confirm.

        echo: only accept a prompt/question that comes AFTER this echoed command text,
              so stale prompts from earlier output are never mistaken for completion.
        anywhere: accept a prompt even if more lines (e.g. debug logs) follow it.
        """
        timeout = timeout or self.timeout
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        echo_grace = loop.time() + 5          # if the device never echoes, stop requiring it
        next_poke = loop.time() + poke_every if poke_every else None
        buf = ""
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                raise ConsoleTimeout(f"No prompt within {int(timeout)}s. Last output:\n{buf[-400:]}", buf)
            buf += await self._read_chunk(min(remaining, 1.0))
            start = 0
            if echo:
                i = buf.find(echo)
                if i >= 0:
                    start = i + len(echo)
                elif loop.time() < echo_grace:
                    continue
            view = buf[start:]
            tail = view[-300:]
            handled = False
            for rx, reply in AUTO:
                if rx.search(tail):
                    await self.send(reply)
                    buf = rx.sub("", buf)
                    handled = True
                    break
            if handled:
                continue
            for rx, reply in QUESTIONS:
                if rx.search(tail):
                    question = tail.strip().splitlines()[-1].strip() if tail.strip() else "?"
                    if self.allow_confirm:
                        self.answered.append(question)
                        await self.send(reply)
                        buf = buf + "\n"  # keep transcript; avoid re-matching same tail
                        buf = rx.sub("", buf)
                        handled = True
                        break
                    await self.send(CTRL_C)          # cancel the question
                    try:
                        await self.read_until_prompt(timeout=8)
                    except ConsoleError:
                        await self.send("\r")
                    raise NeedsConfirmation(question, buf)
            if handled:
                continue
            m = PROMPT_RE.search(view)
            if not m and anywhere:
                m = ANY_PROMPT_RE.search(view)
            if m:
                self.prompt = m.group(1)
                return buf
            if next_poke and loop.time() >= next_poke:
                await self.send("\r")
                next_poke = loop.time() + poke_every

    async def capture(self, seconds: float) -> str:
        """Collect whatever the device prints (e.g. debug output) for N seconds."""
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        buf = ""
        while loop.time() < end:
            buf += await self._read_chunk(min(1.0, end - loop.time()))
        return buf.replace("\r\n", "\n").replace("\r", "\n").strip("\n")

    # ---- IOS session -----------------------------------------------------
    async def prepare(self, boot_wait: float = 120):
        """Get to privileged exec with paging off. Waits for boot if needed."""
        await self.send("\r")
        await self.read_until_prompt(timeout=boot_wait, poke_every=5)
        await self.drain()
        if "(" in self.prompt:          # left in config mode
            await self.send("end\r")
            await self.read_until_prompt(echo="end")
        if self.prompt.endswith(">"):
            await self.send("enable\r")
            out = await self.read_until_prompt(echo="enable")
            if "Password" in out and not self.prompt.endswith("#"):
                raise ConsoleError("enable needs a password; not supported yet.")
        for c in ("terminal length 0", "terminal width 0"):
            await self.command(c)

    @staticmethod
    def _keep(junk: str) -> str:
        """Output that arrived between commands (logs, debug): keep it, minus bare prompts."""
        lines = junk.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        kept = [l for l in lines if l.strip() and not PROMPT_RE.search("\n" + l.strip())]
        return "\n".join(kept)

    @staticmethod
    def _clean(raw: str, cmd: str) -> str:
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        c = cmd.strip()
        if c and c in text:
            text = text[text.find(c) + len(c):]
        lines = text.split("\n")
        while lines and (not lines[-1].strip() or PROMPT_RE.search("\n" + lines[-1])):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n")

    async def command(self, cmd: str, timeout: Optional[float] = None) -> str:
        """Send one command and return its output. Stops long-running output with
        Ctrl+Shift+6; returns right after a confirmed reload."""
        self.between = self._keep(await self.drain())
        await self.send(cmd + "\r")
        t = timeout or self.timeout
        streaming = bool(DEBUG_RE.match(cmd) or UNDEBUG_RE.match(cmd))
        try:
            raw = await self.read_until_prompt(timeout=t, echo=cmd.strip() or None, anywhere=streaming)
        except ConsoleTimeout as e:
            if self.answered and RELOAD_RE.match(cmd):
                return self._clean(e.buf, cmd) + "\n(device is reloading)"
            await self.send(CTRL_SHIFT_6 * 2)
            try:
                raw = e.buf + await self.read_until_prompt(timeout=6)
            except ConsoleTimeout:
                await self.send(CTRL_SHIFT_6 * 2 + "\r")
                raw = e.buf + await self.read_until_prompt(timeout=6)
            return self._clean(raw, cmd) + f"\n[stopped with Ctrl+Shift+6 after {int(t)}s]"
        return self._clean(raw, cmd)


# One job per device at a time. Two sessions typing into the same console
# garble each other (an `end` from one knocks the other out of config mode).
_LOCKS: Dict[Tuple[str, int], asyncio.Lock] = {}


class DeviceBusy(ConsoleError):
    pass


class StillBooting(ConsoleError):
    pass


async def run_commands(host: str, port: int, commands: List[str], mode: str = "exec",
                       save: bool = False, timeout: float = 30, boot_wait: float = 40,
                       lock_wait: float = 15, allow_confirm: bool = False,
                       capture_seconds: float = 0) -> Tuple[str, List[str]]:
    """Run commands on one device. Returns (transcript, errors).

    - Waits up to lock_wait seconds if another job holds this device, then gives up.
    - Waits up to boot_wait seconds for a prompt, then reports StillBooting (retry later).
    - Config mode stops at the first rejected line, exits config mode, and skips the save.
    - Device questions are cancelled and reported unless allow_confirm=True.
    - capture_seconds collects follow-on output (debug); `undebug all` always runs after debug.
    """
    lock = _LOCKS.setdefault((host, port), asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), timeout=lock_wait)
    except asyncio.TimeoutError:
        raise DeviceBusy("Another job is still running on this device. Try again in a moment.")
    con = IOSConsole(host, port, timeout=timeout, allow_confirm=allow_confirm)
    transcript, errors = [], []
    debug_used = False
    try:
        await con.open()
        try:
            await con.prepare(boot_wait=boot_wait)
        except ConsoleTimeout:
            raise StillBooting(
                f"No CLI prompt after {int(boot_wait)}s - the device is probably still booting. "
                "Nothing was sent. Try again in a minute.")
        if mode == "config":
            await con.command("configure terminal")
        sent = [c for c in commands if c.strip()]
        for i, cmd in enumerate(sent):
            before = con.prompt
            debug_used = debug_used or bool(DEBUG_RE.match(cmd))
            try:
                out = await con.command(cmd)
                if con.between:
                    transcript.append(con.between)
            except NeedsConfirmation as q:
                transcript.append(f"{before} {cmd}\n{con._clean(q.buf, cmd)}\n[question cancelled with Ctrl+C]")
                errors.append(f"NEEDS CONFIRMATION: '{cmd}' asked \"{q.question}\". It was cancelled; "
                              "nothing was changed by it. Ask the user, and only if they approve, "
                              "re-run with confirm=true.")
                skipped = sent[i + 1:]
                if skipped:
                    errors.append(f"{len(skipped)} later line(s) NOT sent: " + "; ".join(s.strip() for s in skipped))
                break
            transcript.append(f"{before} {cmd}" + (f"\n{out}" if out else ""))
            if RELOAD_RE.match(cmd) and "(device is reloading)" in out:
                transcript.append("Answered with permission: " + "; ".join(f'"{q}"' for q in con.answered))
                return "\n".join(transcript), errors
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
            if con.between:
                transcript.append(con.between)
        if capture_seconds and not errors:
            secs = min(float(capture_seconds), MAX_CAPTURE)
            captured = await con.capture(secs)
            transcript.append(f"--- captured {int(secs)}s of output ---" + (f"\n{captured}" if captured else "\n(nothing printed)"))
        if debug_used:
            out = await con.command("undebug all")
            if con.between:
                transcript.append(con.between)
            transcript.append(f"{con.prompt} undebug all" + (f"\n{out}" if out else ""))
        if mode == "config":
            if save and not errors:
                out = await con.command("write memory", timeout=60)
                transcript.append(f"{con.prompt} write memory\n{out}")
            elif save:
                errors.append("Config was NOT saved because of the error above.")
        if con.answered:
            transcript.append("Answered with permission: " + "; ".join(f'"{q}"' for q in con.answered))
        return "\n".join(transcript), errors
    finally:
        await con.close()
        lock.release()
