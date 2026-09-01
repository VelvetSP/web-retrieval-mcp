#!/usr/bin/env python3
"""Mechanically validate the isolated Valkey test fixture, including readiness.

The probe starts the candidate binary with the tracked production configuration,
overrides only paths and the test memory ceiling, requires an sd_notify READY=1
datagram, and proves the private Unix socket answers with the bundled CLI.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import select
import socket
import stat
import subprocess
import tempfile
import time


VERSION_RE = re.compile(r"\bv=(\d+\.\d+\.\d+)\b")


def _run(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def verify_binary(root: Path, version: str, config: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("candidate root is not a real directory")
    if not config.is_file() or config.is_symlink():
        raise RuntimeError("tracked Valkey config is not a real file")
    server = root / "bin" / "valkey-server"
    cli = root / "bin" / "valkey-cli"
    for binary in (server, cli):
        if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"missing executable: {binary}")

    version_result = _run(str(server), "--version")
    match = VERSION_RE.search(version_result.stdout + version_result.stderr)
    if version_result.returncode != 0 or match is None or match.group(1) != version:
        raise RuntimeError(
            f"candidate version mismatch: expected {version}, got "
            f"{(version_result.stdout + version_result.stderr).strip()!r}"
        )

    with tempfile.TemporaryDirectory(prefix="wr-valkey-readiness-") as temporary:
        runtime = Path(temporary)
        notify_path = runtime / "notify.sock"
        valkey_socket = runtime / "valkey.sock"
        pidfile = runtime / "valkey.pid"
        notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        notify.bind(str(notify_path))
        notify.setblocking(False)
        environment = os.environ.copy()
        environment["NOTIFY_SOCKET"] = str(notify_path)
        process = subprocess.Popen(
            [
                str(server), str(config),
                "--daemonize", "no",
                "--port", "0",
                "--unixsocket", str(valkey_socket),
                "--unixsocketperm", "600",
                "--dir", str(runtime),
                "--pidfile", str(pidfile),
                "--maxmemory", "32mb",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        messages: list[str] = []
        try:
            deadline = time.monotonic() + 8.0
            ready = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                readable, _, _ = select.select([notify], [], [], 0.1)
                if readable:
                    message = notify.recv(8192).decode("utf-8", "replace")
                    messages.append(message)
                    if "READY=1" in message.split("\n"):
                        ready = True
                        break
            if not ready:
                output = ""
                if process.poll() is not None and process.stdout is not None:
                    output = process.stdout.read(4096)
                raise RuntimeError(
                    f"candidate did not emit READY=1; notifications={messages!r}; "
                    f"output={output!r}"
                )
            if not valkey_socket.is_socket():
                raise RuntimeError("candidate reported ready without creating its Unix socket")
            mode = stat.S_IMODE(valkey_socket.stat().st_mode)
            if mode != 0o600:
                raise RuntimeError(f"candidate socket mode is {mode:o}, expected 600")
            ping = _run(str(cli), "-s", str(valkey_socket), "PING")
            if ping.returncode != 0 or ping.stdout.strip() != "PONG":
                raise RuntimeError(f"candidate private-socket PING failed: {ping.stdout!r}")
            values = _run(
                str(cli), "-s", str(valkey_socket), "--raw",
                "CONFIG", "GET", "port", "save", "appendonly",
                "maxmemory-policy", "maxmemory-samples", "supervised",
            )
            fields = values.stdout.splitlines()
            if values.returncode != 0 or len(fields) < 2 or len(fields) % 2 != 0:
                raise RuntimeError(
                    f"candidate config probe failed: rc={values.returncode}; "
                    f"stdout={values.stdout!r}; stderr={values.stderr!r}"
                )
            settings = dict(zip(fields[0::2], fields[1::2], strict=True))
            expected = {
                "port": "0",
                "save": "",
                "appendonly": "no",
                "maxmemory-policy": "allkeys-lfu",
                "maxmemory-samples": "10",
                "supervised": "systemd",
            }
            if set(settings) != set(expected) or any(
                    settings.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"candidate config probe mismatch: {settings!r}")
        finally:
            try:
                if process.poll() is None and valkey_socket.exists():
                    _run(str(cli), "-s", str(valkey_socket), "SHUTDOWN", "NOSAVE", timeout=3)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                notify.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    verify_binary(args.root, args.version, args.config)
    print(f"verified Valkey {args.version}: version, READY=1, private socket, config")


if __name__ == "__main__":
    main()
