"""Fake EVE-NG SSH server: accepts root/toor, runs only a fake unl_wrapper that
opens/closes the node's console port (32768+128*T+D) on 127.0.0.1."""
import asyncio, asyncssh, re, shlex, sys
LISTENERS = {}
LOG = []
async def fake_console(r, w):
    w.write(b"\r\nR>"); await w.drain()
async def wrapper(argv):
    a = dict(zip(argv[1::2], argv[2::2]))
    act, T, D, F = a.get("-a"), int(a["-T"]), int(a["-D"]), a["-F"]
    if not F.startswith("/opt/unetlab/labs/") or not F.endswith(".unl"): return 1, "bad lab file\n"
    port = 32768 + 128 * T + D
    if act == "start" and port not in LISTENERS:
        LISTENERS[port] = await asyncio.start_server(fake_console, "127.0.0.1", port)
    elif act == "stop" and port in LISTENERS:
        LISTENERS.pop(port).close()
    LOG.append(f"{act} T{T} D{D} {F}")
    if act not in ("start", "stop", "wipe", "export"): return 1, "bad action\n"
    return 0, ""
class Srv(asyncssh.SSHServer):
    def begin_auth(self, u): return True
    def password_auth_supported(self): return True
    def validate_password(self, u, p): return u == "root" and p == "toor"
    def public_key_auth_supported(self): return True
    def validate_public_key(self, u, key):
        import os
        path = os.environ.get("FAKE_AUTHORIZED_KEYS")
        if not (u == "root" and path and os.path.exists(path)): return False
        return any(asyncssh.import_public_key(l) == key for l in open(path) if l.strip())
async def handle(proc):
    cmd = proc.command or ""
    print("CMD", cmd, flush=True)
    if "nohup sh -c" in cmd or cmd.startswith("cat ") or cmd.startswith("rm -f "):
        # shell mode: run for real, with the wrapper path pointed at a local stand-in
        import os, sys as _s
        stub = f"{_s.executable} {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_unl_wrapper.py')}"
        real = cmd.replace("/opt/unetlab/wrappers/unl_wrapper", stub)
        p = await asyncio.create_subprocess_shell(real, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out_b, _ = await p.communicate()
        out, rc = out_b.decode(), p.returncode or 0
    elif cmd.startswith("( "):
        # parallel form used by save_lab_configs: ( <wrapper ...> >/dev/null 2>&1; echo "RC <id> $?" ) & ... wait
        out, rc = "", 0
        for part in re.findall(r"\( (.+?) >/dev/null 2>&1; echo \"RC (\d+) \$\?\" \)", cmd):
            argv = shlex.split(part[0])
            r, _ = await wrapper(argv) if argv and argv[0] == "/opt/unetlab/wrappers/unl_wrapper" else (127, "")
            out += f"RC {part[1]} {r}\n"
    else:
        argv = shlex.split(cmd)
        if argv and argv[0] == "/opt/unetlab/wrappers/unl_wrapper":
            rc, out = await wrapper(argv)
        else:
            rc, out = 127, "not allowed\n"
    proc.stdout.write(out); proc.exit(rc)
async def main():
    key = asyncssh.generate_private_key("ssh-ed25519")
    await asyncssh.create_server(Srv, "127.0.0.1", int(__import__("os").environ.get("FAKE_SSH_PORT", "2222")), server_host_keys=[key], process_factory=handle)
    await asyncio.Event().wait()
asyncio.run(main())
