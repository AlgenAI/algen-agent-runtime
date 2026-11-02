"""Ctrl-D and Ctrl-C at a real terminal prompt end the example cleanly (issue #96)."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty")

ROOT = Path(__file__).parents[2]
PROMPT = b"Approve or reject? "


def _read_until(fd: int, needle: bytes, timeout: float) -> bytes:
    output = b""
    deadline = time.monotonic() + timeout
    while needle not in output and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # the child closed the terminal
            break
        if not chunk:
            break
        output += chunk
    return output


def _default_sigint() -> None:
    # CI runners can start jobs with SIGINT ignored, and a child inherits that. An interactive
    # shell starts commands with the default disposition, so restore it.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _run_to_prompt() -> tuple[subprocess.Popen[bytes], int, bytes]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "examples.quickstart_approval.app"],
        cwd=ROOT,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=_default_sigint,
    )
    os.close(slave)
    output = _read_until(master, PROMPT, timeout=60)
    if PROMPT not in output:
        process.kill()
        process.wait()
        os.close(master)
        pytest.fail(f"no approval prompt; output was:\n{output.decode(errors='replace')}")
    return process, master, output


def _finish(process: subprocess.Popen[bytes], master: int) -> str:
    """Collect output until the example exits; fail if it is still running after 10 seconds."""
    output = b""
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        output += _read_until(master, b"\0", timeout=0.2)
    output += _read_until(master, b"\0", timeout=0.2)
    os.close(master)
    if process.poll() is None:
        process.kill()
        process.wait()
        pytest.fail(f"the example did not exit within 10 seconds; output was:\n{output!r}")
    return output.decode(errors="replace")


def test_ctrl_d_at_the_approval_prompt_exits_2_without_traceback() -> None:
    process, master, _ = _run_to_prompt()
    os.write(master, b"\x04")
    output = _finish(process, master)
    assert process.returncode == 2
    assert "No decision: end of input while awaiting approval" in output
    assert "Traceback" not in output


def test_ctrl_c_at_the_approval_prompt_exits_130_without_traceback() -> None:
    process, master, _ = _run_to_prompt()
    process.send_signal(signal.SIGINT)
    output = _finish(process, master)
    assert process.returncode == 130
    assert "Interrupted." in output
    assert "Traceback" not in output
