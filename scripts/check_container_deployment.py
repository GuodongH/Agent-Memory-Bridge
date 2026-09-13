"""Exercise an already-built image using only newly created test containers/volume.

No production container or database is accepted as input. The named test volume
is retained for inspection; application containers created here are stopped and
removed. Credentials and memory content are never included in the JSON report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from agent_mem_bridge.http_probe import run_http_probe


def docker(*args: str, input_bytes: bytes | None = None) -> str:
    result = subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=90, check=False)
    if result.returncode:
        # Docker errors can contain configuration; keep their text out of reports.
        raise RuntimeError(f"Docker {args[0]} failed with exit {result.returncode}")
    return result.stdout.decode().strip()


def readiness(url: str) -> int:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def wait_ready(url: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if readiness(url) == 200:
            return
        time.sleep(0.2)
    raise RuntimeError("Candidate readiness timeout")


async def marker_flow(url: str, token: str, marker: str, *, store: bool) -> str:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=20) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            if store:
                result = await client.call_tool(
                    "store",
                    {
                        "namespace": "project:container-acceptance",
                        "kind": "memory",
                        "content": f"claim: Retain decision {marker}.\nreason: Container replacement must preserve authority.",
                        "title": "Container persistence decision",
                    },
                )
                if result.is_error:
                    raise RuntimeError("Container marker store failed")
            result = await client.call_tool(
                "recall", {"namespace": "project:container-acceptance", "query": marker, "kind": "memory"}
            )
            payload = result.structured_content or {}
            items = payload.get("items", [])
            if result.is_error or len(items) != 1 or marker not in items[0]["content"]:
                raise RuntimeError("Container marker recall failed")
            return str(items[0]["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir.mkdir(parents=True, exist_ok=False)
    args.runtime_dir.chmod(0o700)
    report: dict[str, Any] = {"ok": False, "checks": {}, "image": args.image}
    suffix = secrets.token_hex(6)
    volume = f"amb-v034-acceptance-{suffix}"
    name = f"amb-v034-acceptance-{suffix}"
    token = secrets.token_hex(32)
    token_file = args.runtime_dir / "http-token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    active = False

    def start() -> str:
        nonlocal active
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=64m",
            "--mount",
            f"type=volume,src={volume},dst=/data/agent-memory-bridge",
            "--publish",
            "127.0.0.1::8000",
            args.image,
            "agent-memory-bridge",
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--token-file",
            "/data/agent-memory-bridge/http-token",
            "--allowed-host",
            "127.0.0.1:*",
            "--allowed-host",
            "localhost:*",
            "--allowed-origin",
            "http://127.0.0.1:*",
        )
        active = True
        ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))
        port = ports["8000/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        wait_ready(base + "/readyz")
        return base

    def provision_token() -> None:
        docker(
            "run",
            "--rm",
            "-i",
            "--mount",
            f"type=volume,src={volume},dst=/data/agent-memory-bridge",
            args.image,
            "python",
            "-c",
            "import os,sys,pathlib; os.umask(0o077); "
            "pathlib.Path('/data/agent-memory-bridge/http-token').write_bytes(sys.stdin.buffer.read())",
            input_bytes=token.encode(),
        )

    def stop() -> None:
        nonlocal active
        docker("stop", "--time", "30", name)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
        report["checks"]["graceful_termination"] = (
            report["checks"].get("graceful_termination", True) and state["ExitCode"] == 0 and not state["OOMKilled"]
        )
        docker("rm", name)
        active = False

    try:
        image = json.loads(docker("image", "inspect", args.image))[0]
        labels = image["Config"]["Labels"]
        report.update({"image_id": image["Id"], "version": args.expected_version, "revision": args.expected_revision})
        report["checks"]["provenance"] = (
            labels.get("org.opencontainers.image.version") == args.expected_version
            and labels.get("org.opencontainers.image.revision") == args.expected_revision
        )
        assert report["checks"]["provenance"], "Image label mismatch"
        uid = int(docker("run", "--rm", args.image, "python", "-c", "import os; print(os.getuid())"))
        report["checks"]["non_root"] = uid == 10001
        docker("volume", "create", "--label", "amb.scope=disposable-acceptance", volume)
        # The image's normal non-root user initializes only this fresh test volume.
        provision_token()
        report["retained_test_volume"] = volume
        base = start()
        report["http_probe"] = asyncio.run(run_http_probe(base + "/mcp", token_file=token_file))
        report["checks"]["http_contract"] = report["http_probe"]["ok"]
        marker = f"containerdecision{suffix}"
        original_id = asyncio.run(marker_flow(base + "/mcp", token, marker, store=True))
        epoch_command = (
            "import sqlite3; c=sqlite3.connect('file:/data/agent-memory-bridge/bridge.db?mode=ro',uri=True); "
            "print(c.execute(\"SELECT value FROM bridge_metadata WHERE key='database_epoch'\").fetchone()[0])"
        )
        epoch_before = docker("exec", name, "python", "-c", epoch_command)
        stop()
        unavailable = asyncio.run(run_http_probe(base + "/mcp", token_file=token_file, timeout=0.5))
        report["checks"]["no_fallback_when_unavailable"] = not unavailable["ok"] and not unavailable["local_fallback"]
        base = start()
        returned_id = asyncio.run(marker_flow(base + "/mcp", token, marker, store=False))
        epoch_after = docker("exec", name, "python", "-c", epoch_command)
        report["checks"]["container_replacement_persistence"] = (
            original_id == returned_id and epoch_before == epoch_after
        )
        for version in (999, 12):
            docker(
                "exec",
                name,
                "python",
                "-c",
                "import sqlite3; c=sqlite3.connect('/data/agent-memory-bridge/bridge.db'); "
                f"c.execute('PRAGMA user_version={version}'); c.close()",
            )
            expected_status = 503 if version == 999 else 200
            report["checks"][f"readiness_schema_{version}"] = readiness(base + "/readyz") == expected_status
        deadline = time.monotonic() + 35
        health = None
        while time.monotonic() < deadline:
            health = json.loads(docker("inspect", "--format", "{{json .State.Health}}", name))
            if health["Status"] == "healthy":
                break
            time.sleep(0.5)
        report["checks"]["docker_healthcheck"] = health is not None and health["Status"] == "healthy"
        stop()
        # Negative control: a different fresh volume must not masquerade as the
        # original authority, even with the same image and credential.
        volume = f"amb-v034-acceptance-empty-{suffix}"
        docker("volume", "create", "--label", "amb.scope=disposable-acceptance", volume)
        report["retained_negative_test_volume"] = volume
        provision_token()
        base = start()
        empty_epoch = docker("exec", name, "python", "-c", epoch_command)

        async def empty_authority() -> bool:
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False) as http:
                async with Client(streamable_http_client(base + "/mcp", http_client=http)) as client:
                    result = await client.call_tool(
                        "recall", {"namespace": "project:container-acceptance", "query": marker, "kind": "memory"}
                    )
                    return not result.is_error and (result.structured_content or {}).get("items") == []

        report["checks"]["different_volume_is_different_authority"] = empty_epoch != epoch_before and asyncio.run(
            empty_authority()
        )
        stop()
        report["ok"] = all(report["checks"].values())
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = "Container acceptance failed; inspect isolated runtime and retained volume."
    finally:
        if active:
            try:
                stop()
            except Exception:
                report["cleanup_incomplete"] = True
        report_path = args.runtime_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        report_path.chmod(0o600)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
