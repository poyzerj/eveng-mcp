"""Fake IOS console with questions, debug streaming, and a long-running ping.
Node 1 on 127.0.0.1:32769. Logs every line/key it receives to stdout."""
import asyncio

IAC, WILL = 255, 251
S = {"debug": False, "reloaded": False, "cleared": 0}


async def readkey_line(reader, writer):
    """Return a typed line, or '^C' / '^^' for control keys."""
    buf = b""
    while True:
        ch = await reader.read(1)
        if not ch:
            return None
        if ch[0] == IAC:
            await reader.read(2); continue
        if ch == b"\x03":
            return "^C"
        if ch == b"\x1e":
            return "^^"
        if ch in (b"\r", b"\n"):
            return buf.decode()
        writer.write(ch); buf += ch


async def debug_pump(writer):
    n = 0
    while S["debug"]:
        n += 1
        writer.write(f"\r\n*Oct 17 20:00:{n:02d}: IP: s=10.0.12.2 (Gi0/0), d=224.0.0.10, len 60, rcvd 2".encode())
        await writer.drain()
        await asyncio.sleep(1)


async def handle(reader, writer):
    writer.write(bytes([IAC, WILL, 1, IAC, WILL, 3]) + b"\r\nR1#"); await writer.drain()
    mode = "priv"
    while True:
        line = await readkey_line(reader, writer)
        if line is None:
            return
        c = line.strip()
        print("RX", repr(c), flush=True)
        prompt = "R1#" if mode == "priv" else "R1(config)#"
        if c == "^C" or c == "^^":
            pass
        elif c in ("terminal length 0", "terminal width 0", "end"):
            mode = "priv"
        elif c == "configure terminal":
            mode = "config"
        elif c == "reload":
            writer.write(b"\r\nSystem configuration has been modified. Save? [yes/no]: "); await writer.drain()
            a = await readkey_line(reader, writer); print("RX-ANS", repr(a), flush=True)
            if a in ("^C", None):
                writer.write(b"\r\n" + prompt.encode()); await writer.drain(); continue
            writer.write(b"\r\nBuilding configuration...\r\n[OK]\r\nProceed with reload? [confirm]"); await writer.drain()
            a = await readkey_line(reader, writer); print("RX-ANS", repr(a), flush=True)
            if a == "^C":
                writer.write(b"\r\n" + prompt.encode()); await writer.drain(); continue
            S["reloaded"] = True
            writer.write(b"\r\n*Oct 17 20:01:00: %SYS-5-RELOAD: Reload requested\r\n"); await writer.drain()
            await asyncio.sleep(30); return
        elif c == "clear counters":
            writer.write(b"\r\nClear \"show interface\" counters on all interfaces [confirm]"); await writer.drain()
            a = await readkey_line(reader, writer); print("RX-ANS", repr(a), flush=True)
            if a != "^C":
                S["cleared"] += 1
        elif c.startswith("debug "):
            writer.write(b"\r\nIP packet debugging is on"); S["debug"] = True
            asyncio.create_task(debug_pump(writer))
        elif c in ("undebug all", "u all"):
            S["debug"] = False; writer.write(b"\r\nAll possible debugging has been turned off")
        elif c.startswith("ping") and "repeat" in c:
            writer.write(b"\r\nType escape sequence to abort.\r\nSending 100000, 100-byte ICMP Echos to 3.3.3.3:\r\n"); await writer.drain()
            stop = asyncio.Event()

            async def bangs():
                while not stop.is_set():
                    writer.write(b"!"); await writer.drain(); await asyncio.sleep(0.2)
            t = asyncio.create_task(bangs())
            while True:
                k = await readkey_line(reader, writer)
                print("RX-KEY", repr(k), flush=True)
                if k in ("^^", None):
                    break
            stop.set(); await t
            writer.write(b"\r\nSuccess rate is 100 percent (42/42)")
        elif c in ("shutdown", "no shutdown"):
            # real IOS: prompt first, then async logs right after it (no prompt reprint)
            writer.write(("\r\n" + prompt).encode()); await writer.drain()
            await asyncio.sleep(0.6)   # logs land after the tool's 0.3s drain window
            st = "administratively down" if c == "shutdown" else "up"
            writer.write(f"\r\n*Oct 17 20:04:20.232: %LINK-5-CHANGED: Interface GigabitEthernet0/0, changed state to {st}\r\n*Oct 17 20:04:20.234: %OSPF-5-ADJCHG: Process 1, Nbr 2.2.2.2 on GigabitEthernet0/0 from FULL to DOWN".encode())
            await writer.drain(); continue
        elif c == "interface GigabitEthernet0/0":
            mode = "config"
        elif c == "show running-config":
            # a GRUB log line lands mid-output, like IOSv right after write memory
            writer.write(b"\r\nBuilding configuration...\r\n\r\nCurrent configuration : 120 bytes\r\n!\r\nhostname R1\r\n!\r\n*Oct 17 21:00:52.834: %GRUB-5-CONFIG_WRITTEN: GRUB configuration was written to disk successfully.\r\ninterface GigabitEthernet0/0\r\n ip address 10.0.12.1 255.255.255.252\r\n!\r\nend")
        elif c == "show running-config | section ^interface":
            writer.write(b"\r\ninterface Loopback0\r\n ip address 1.1.1.1 255.255.255.255\r\n ip ospf 1 area 0\r\n"
                         b"interface GigabitEthernet0/0\r\n ip address 10.0.12.1 255.255.255.252\r\n ip ospf network point-to-point\r\n"
                         b"interface GigabitEthernet0/1\r\n ip address 10.0.14.2 255.255.255.252\r\n"
                         b"interface GigabitEthernet0/2\r\n description Trunk to SW1\r\n no ip address\r\n"
                         b"interface GigabitEthernet0/2.10\r\n encapsulation dot1Q 10\r\n ip address 172.16.10.1 255.255.255.0\r\n"
                         b"interface GigabitEthernet0/2.20\r\n encapsulation dot1Q 20\r\n ip address 172.16.20.1 255.255.255.0\r\n"
                         b"interface GigabitEthernet0/3\r\n no ip address\r\n shutdown")
        elif c == "write memory":
            writer.write(b"\r\nBuilding configuration...\r\n[OK]")
        elif c == "show clock":
            writer.write(b"\r\n*20:00:00.000 UTC Sat Oct 17 2026")
        writer.write(("\r\n" + ("R1#" if mode == "priv" else "R1(config)#")).encode()); await writer.drain()


async def main():
    await asyncio.start_server(handle, "127.0.0.1", 32768 + 128 * int(__import__("os").environ.get("FAKE_POD", "0")) + 1)
    await asyncio.Event().wait()

asyncio.run(main())
