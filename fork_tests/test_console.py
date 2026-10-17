"""Console driver tests against fake IOS consoles. Each test pins a bug fixed in this fork."""

import asyncio
import time

import pytest

from eveng_mcp_server.core.console import (
    DeviceBusy, StillBooting, console_port, run_commands,
)


def run(coro):
    return asyncio.run(coro)


# ---- fake_ios: boot dialog, modes, errors ---------------------------------

def test_boot_dialog_answered_and_config_applied(fake_ios):
    port = console_port(1, fake_ios["pod"])
    t, errs = run(run_commands(fake_ios["host"], port,
                               ["hostname R1", "interface Gi0/0", " ip address 10.0.12.1 255.255.255.252"],
                               mode="config", save=True))
    assert not errs
    assert "write memory" in t
    t, _ = run(run_commands(fake_ios["host"], port, ["show ip interface brief"]))
    assert "10.0.12.1" in t


def test_still_booting_returns_quickly_and_sends_nothing(slow_ios):
    start = time.time()
    with pytest.raises(StillBooting):
        run(run_commands(slow_ios["host"], console_port(1, slow_ios["pod"]), ["show version"], boot_wait=3))
    assert time.time() - start < 10


def test_config_stops_at_first_rejected_line_and_does_not_save(fake_ios):
    port = console_port(2, fake_ios["pod"])
    t, errs = run(run_commands(fake_ios["host"], port,
                               ["interface Gi0/1", " bogus thing", " ip address 10.0.14.2 255.255.255.252"],
                               mode="config", save=True))
    assert any("Invalid input" in e for e in errs)
    assert any("NOT sent" in e and "10.0.14.2" in e for e in errs)
    assert any("NOT saved" in e for e in errs)
    assert "write memory" not in t


def test_one_job_per_device(fake_ios):
    """Two jobs on one console must not interleave (the SW1 incident)."""
    port = console_port(3, fake_ios["pod"])

    async def both():
        cfg = run_commands(fake_ios["host"], port, ["hostname R3", "interface Gi0/0"], mode="config")
        show = run_commands(fake_ios["host"], port, ["show ip interface brief"], lock_wait=30)
        return await asyncio.gather(cfg, show)
    (t1, e1), (t2, e2) = run(both())
    assert not e1 and not e2
    assert "R3#" in t2 or "R3(" in t1


def test_device_busy_when_lock_wait_expires(slow_ios):
    port = console_port(4, slow_ios["pod"])

    async def both():
        a = asyncio.create_task(run_commands(slow_ios["host"], port, ["show clock"], boot_wait=4))
        await asyncio.sleep(0.3)
        b = run_commands(slow_ios["host"], port, ["show clock"], lock_wait=1)
        return await asyncio.gather(a, b, return_exceptions=True)
    ra, rb = run(both())
    assert isinstance(ra, StillBooting)
    assert isinstance(rb, DeviceBusy)


# ---- fake_ios2: questions, debug, long output, async logs -----------------

def test_question_cancelled_without_permission(fake_ios2):
    t, errs = run(run_commands("127.0.0.1", fake_ios2["port"], ["reload", "show clock"]))
    assert any("NEEDS CONFIRMATION" in e and "Save? [yes/no]" in e for e in errs)
    assert any("NOT sent" in e and "show clock" in e for e in errs)
    log = fake_ios2["log"].read_text()
    assert "RX-ANS '^C'" in log            # cancelled with Ctrl+C
    assert "'show clock'" not in log.split("'reload'")[1].split("RX ''")[0]  # never typed into the question


def test_question_answered_with_permission(fake_ios2):
    t, errs = run(run_commands("127.0.0.1", fake_ios2["port"], ["clear counters"], allow_confirm=True))
    assert not errs
    assert "Answered with permission" in t


def test_reload_with_permission_returns_promptly(fake_ios2):
    start = time.time()
    t, errs = run(run_commands("127.0.0.1", fake_ios2["port"], ["reload"], allow_confirm=True, timeout=5))
    assert "(device is reloading)" in t
    assert time.time() - start < 15


def test_debug_capture_then_undebug(fake_ios2):
    t, errs = run(run_commands("127.0.0.1", fake_ios2["port"], ["debug ip packet"], capture_seconds=3))
    assert not errs
    assert "captured 3s" in t and "IP: s=" in t
    assert t.rstrip().endswith("All possible debugging has been turned off")


def test_endless_ping_stopped_with_ctrl_shift_6(fake_ios2):
    t, errs = run(run_commands("127.0.0.1", fake_ios2["port"], ["ping 3.3.3.3 repeat 100000", "show clock"], timeout=3))
    assert "stopped with Ctrl+Shift+6" in t
    assert "UTC" in t                      # next command still ran


def test_logs_after_prompt_do_not_stall_and_are_kept(fake_ios2):
    start = time.time()
    t, errs = run(run_commands("127.0.0.1", fake_ios2["port"],
                               ["configure terminal", "interface GigabitEthernet0/0", "shutdown", "no shutdown", "end"]))
    assert time.time() - start < 15        # used to wait 30s per command
    assert "FULL to DOWN" in t             # log lines that landed between commands are kept
