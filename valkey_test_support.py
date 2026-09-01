"""Shared real-Valkey fixture for cache adapter and black-box acceptance tests."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Iterator


REPO = Path(__file__).resolve().parent
FIXTURE = REPO / "tests" / "fixtures" / "valkey"
INSTALLER = FIXTURE / "install.sh"
CONFIG = FIXTURE / "valkey.conf"


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(kb|mb|gb)", value.casefold())
    if match is None:
        raise ValueError(f"unsupported Valkey test maxmemory: {value!r}")
    scale = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}[match.group(2)]
    return int(match.group(1)) * scale


def _real_valkey_root(root: Path, *, label: str) -> Path:
    """Resolve the installed stable link to the immutable version directory."""
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} cannot be resolved") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise RuntimeError(f"{label} does not resolve to a real directory")
    return resolved


@contextmanager
def valkey_root() -> Iterator[Path]:
    """Yield a verified pinned install without touching the production prefix."""
    supplied = os.environ.get("WEBRET_TEST_VALKEY_ROOT")
    if supplied:
        yield _real_valkey_root(
            Path(supplied), label="WEBRET_TEST_VALKEY_ROOT"
        )
        return
    with tempfile.TemporaryDirectory(prefix="wr-valkey-binary-") as prefix:
        result = subprocess.run(
            [str(INSTALLER), "--apply", "--prefix", prefix],
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "could not provision the pinned real Valkey test binary; "
                f"rc={result.returncode} stdout={result.stdout[-2000:]!r} "
                f"stderr={result.stderr[-2000:]!r}"
            )
        yield _real_valkey_root(
            Path(prefix) / "valkey", label="auto-provisioned Valkey root"
        )


class IsolatedValkey:
    """A no-TCP, no-persistence real Valkey process in a temporary directory."""

    def __init__(self, root: Path, *, maxmemory: str = "32mb") -> None:
        self.root = root
        self.maxmemory = maxmemory
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.runtime: Path | None = None
        self.socket: Path | None = None
        self.process: subprocess.Popen[str] | None = None

    @property
    def server(self) -> Path:
        return self.root / "bin" / "valkey-server"

    @property
    def cli_binary(self) -> Path:
        return self.root / "bin" / "valkey-cli"

    def cli(self, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        if self.socket is None:
            raise RuntimeError("Valkey fixture is not running")
        return subprocess.run(
            [str(self.cli_binary), "-s", str(self.socket), "--raw", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def __enter__(self) -> "IsolatedValkey":
        self._temporary = tempfile.TemporaryDirectory(prefix="wr-valkey-runtime-")
        self.runtime = Path(self._temporary.name)
        self.socket = self.runtime / "valkey.sock"
        self.process = subprocess.Popen(
            [
                str(self.server), str(CONFIG),
                "--supervised", "no",
                "--port", "0",
                "--unixsocket", str(self.socket),
                "--unixsocketperm", "600",
                "--dir", str(self.runtime),
                "--pidfile", str(self.runtime / "valkey.pid"),
                "--maxmemory", self.maxmemory,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                self.__exit__()
                raise RuntimeError(f"isolated Valkey exited early: {output[-2000:]}")
            if self.socket.exists():
                ping = self.cli("PING")
                if ping.returncode == 0 and ping.stdout.strip() == "PONG":
                    config = self.cli("CONFIG", "GET", "maxmemory", "maxmemory-policy")
                    fields = config.stdout.splitlines()
                    settings = (
                        dict(zip(fields[0::2], fields[1::2], strict=True))
                        if config.returncode == 0 and len(fields) == 4 else {}
                    )
                    expected = {
                        "maxmemory": str(_memory_bytes(self.maxmemory)),
                        "maxmemory-policy": "allkeys-lfu",
                    }
                    if settings != expected:
                        self.__exit__()
                        raise RuntimeError(
                            f"isolated Valkey config mismatch: {settings!r}, expected {expected!r}"
                        )
                    return self
            time.sleep(0.05)
        self.__exit__()
        raise RuntimeError("isolated Valkey did not become ready in 8 seconds")

    def __exit__(self, *_exc) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.cli("SHUTDOWN", "NOSAVE", timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdout is not None:
                self.process.stdout.close()
        if self._temporary is not None:
            self._temporary.cleanup()
