"""Fake IOSv serial console over telnet, for testing the console driver.
Node N listens on 32768+N. Simulates: telnet negotiation, boot noise,
initial config dialog (first connect only), user/priv/config modes, hostname,
interfaces, a few show commands, invalid input."""
import asyncio, os, sys, time
POD = int(os.environ.get('FAKE_POD', '0'))
START = time.time()

IAC, WILL, DO = 255, 251, 253
STATE = {}

def st(n):
    return STATE.setdefault(n, {"host": "Router", "booted": False, "ifs": {}, "saved": False})

async def handle(reader, writer, n, boot_delay):
    s = st(n)
    writer.write(bytes([IAC, WILL, 1, IAC, WILL, 3]))
    mode = "user"; ctx = ""
    if not s["booted"]:
        writer.write(b"\r\nIOSv boot...\r\n%SYS-5-RESTART: System restarted\r\n")
        await writer.drain(); await asyncio.sleep(max(0, START + boot_delay - time.time()))
        writer.write(b"\r\n--- System Configuration Dialog ---\r\nWould you like to enter the initial configuration dialog? [yes/no]: ")
        await writer.drain()
        while True:
            line = await readline(reader, writer)
            if line is None: return
            if line.strip().lower() == "no": break
            writer.write(b"\r\n% Please answer 'yes' or 'no'.\r\nWould you like to enter the initial configuration dialog? [yes/no]: ")
            await writer.drain()
        writer.write(b"\r\nPress RETURN to get started!\r\n"); await writer.drain()
        await readline(reader, writer)
        s["booted"] = True
    def prompt():
        if mode == "user": return f"{s['host']}>"
        if mode == "priv": return f"{s['host']}#"
        return f"{s['host']}({ctx})#"
    writer.write(f"\r\n{prompt()}".encode()); await writer.drain()
    while True:
        line = await readline(reader, writer)
        if line is None: return
        c = line.strip(); out = ""
        if not c: pass
        elif mode == "user" and c == "enable": mode = "priv"
        elif c in ("terminal length 0", "terminal width 0") and mode == "priv": pass
        elif mode == "priv" and c in ("configure terminal", "conf t"): mode = "config"; ctx = "config"; out = "Enter configuration commands, one per line.  End with CNTL/Z."
        elif mode == "config" and c == "end": mode = "priv"
        elif mode == "config" and c.startswith("hostname "): s["host"] = c.split()[1]
        elif mode == "config" and c.startswith("interface "): ctx = "config-if"; cur = c.split(None, 1)[1]; s["ifs"].setdefault(cur, {}); s["cur"] = cur
        elif mode == "config" and c.startswith("router ospf"): ctx = "config-router"
        elif mode == "config" and ctx == "config-if" and c.startswith("ip address"): s["ifs"][s["cur"]]["ip"] = c.split()[2]
        elif mode == "config" and (c.startswith("ip ospf") or c.startswith("router-id") or c == "no shutdown"): pass
        elif mode == "priv" and c == "show ip interface brief":
            out = "Interface                  IP-Address      OK? Method Status                Protocol\n" + "\n".join(
                f"{i:<26} {v.get('ip','unassigned'):<15} YES manual up                    up" for i, v in s["ifs"].items())
        elif mode == "priv" and c == "write memory": out = "Building configuration...\n[OK]"; s["saved"] = True
        else:
            out = "                ^\n% Invalid input detected at '^' marker."
        if out: writer.write(("\r\n" + out.replace("\n", "\r\n")).encode())
        writer.write(f"\r\n{prompt()}".encode()); await writer.drain()

async def readline(reader, writer):
    buf = b""
    while True:
        ch = await reader.read(1)
        if not ch: return None
        if ch[0] == IAC:
            await reader.read(2); continue
        if ch in (b"\r", b"\n"):
            return buf.decode()
        writer.write(ch)  # echo
        buf += ch

async def main():
    boot = float(sys.argv[1]) if len(sys.argv) > 1 else 2
    for n in range(1, 5):
        await asyncio.start_server(lambda r, w, n=n: handle(r, w, n, boot), "127.0.0.1", 32768 + 128 * POD + n)
    await asyncio.Event().wait()

asyncio.run(main())
