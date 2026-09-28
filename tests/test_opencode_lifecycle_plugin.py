from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "adapters" / "opencode" / "amb-lifecycle.js"


def _require_node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to exercise the OpenCode lifecycle plugin")
    return node


def _install_fake_python(bin_dir: Path) -> Path:
    fake = bin_dir / "fake_hook.py"
    fake.write_text(
        """\
import os
import sys
import time
from pathlib import Path

Path(os.environ["AMB_HOOK_CAPTURE"]).write_bytes(sys.stdin.buffer.read())
pid_path = os.environ.get("AMB_HOOK_PID")
if pid_path:
    Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
if os.environ.get("AMB_HOOK_STDERR_FLOOD") == "1":
    sys.stderr.buffer.write(b"x" * 300_000)
    sys.stderr.flush()
if os.environ.get("AMB_HOOK_HANG") == "1":
    time.sleep(60)
sys.stdout.write("{}\\n")
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        return fake
    launcher = bin_dir / "python3"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return fake


def _run_plugin(tmp_path: Path, script: str, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = _install_fake_python(bin_dir)
    node = _require_node()
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["AMB_CWD"] = str(tmp_path / "repo")
    if os.name == "nt":
        # spawn("py") selects py.exe before a PATH py.cmd, so the Windows test
        # cannot intercept the production launcher. This override is test-only.
        child_env["AMB_LIFECYCLE_HOOK_COMMAND"] = json.dumps([sys.executable, str(fake)])
    else:
        child_env.pop("AMB_LIFECYCLE_HOOK_COMMAND", None)
        child_env["PATH"] = str(bin_dir) + os.pathsep + child_env.get("PATH", "")
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        env=child_env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_opencode_session_created_reads_info_id_and_drains_stderr(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    script = """
import { AmbLifecycle } from "./adapters/opencode/amb-lifecycle.js";
const started = Date.now();
const plugin = await AmbLifecycle({ directory: process.env.AMB_CWD, worktree: process.env.AMB_CWD });
await plugin.event({
  event: { type: "session.created", properties: { info: { id: "ses_from_info" } } },
});
console.log(JSON.stringify({ elapsed_ms: Date.now() - started }));
"""
    completed = _run_plugin(
        tmp_path,
        script,
        {"AMB_HOOK_CAPTURE": str(capture), "AMB_HOOK_STDERR_FLOOD": "1"},
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    elapsed = json.loads(completed.stdout)["elapsed_ms"]
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["session_id"] == "ses_from_info"
    assert payload["hook_event_name"] == "SessionStart"
    assert payload["host"] == "opencode"
    # The hook kills a stuck child at 10s. A drained child must finish before that.
    assert elapsed < 8000


def test_opencode_hook_kills_a_child_that_never_exits(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    pid_path = tmp_path / "child.pid"
    script = """
import { AmbLifecycle } from "./adapters/opencode/amb-lifecycle.js";
const started = Date.now();
const plugin = await AmbLifecycle({ directory: process.env.AMB_CWD });
await plugin.event({
  event: { type: "session.created", properties: { info: { id: "ses_hang" } } },
});
console.log(JSON.stringify({ elapsed_ms: Date.now() - started }));
"""
    completed = _run_plugin(
        tmp_path,
        script,
        {
            "AMB_HOOK_CAPTURE": str(capture),
            "AMB_HOOK_HANG": "1",
            "AMB_HOOK_PID": str(pid_path),
        },
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    elapsed = json.loads(completed.stdout)["elapsed_ms"]
    assert 8000 <= elapsed < 12000
    assert json.loads(capture.read_text(encoding="utf-8"))["session_id"] == "ses_hang"
    pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.05)
    assert not _process_alive(pid)


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
