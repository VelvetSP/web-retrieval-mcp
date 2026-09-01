"""Installed-wheel acceptance for the public package and provider configuration."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import venv

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "web_search", "web_fetch", "research_papers", "research_paper",
    "research_similar", "research_github",
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_tcp(port: int, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


class ProviderLab:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self._lock = threading.Lock()
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                payload = json.loads(body or b"{}")
                record = {
                    "path": self.path,
                    "payload": payload,
                    "exa_key": self.headers.get("x-api-key"),
                    "authorization": self.headers.get("Authorization"),
                }
                with lab._lock:
                    lab.records.append(record)

                if self.path == "/exa/search":
                    if payload.get("query") == "force fallback":
                        self._json({"error": "synthetic"}, status=503)
                    else:
                        self._json({"results": [{
                            "title": "Exa result",
                            "url": "https://example.com/exa",
                            "text": "Exa excerpt",
                            "highlights": [],
                        }]})
                elif self.path == "/exa/contents":
                    self._json({
                        "results": [{"text": "thin"}],
                        "statuses": [{"status": "success", "source": "crawled"}],
                    })
                elif self.path == "/firecrawl/search":
                    self._json({"success": True, "data": {"web": [{
                        "title": "Firecrawl fallback",
                        "url": "https://example.com/firecrawl",
                        "description": "Firecrawl excerpt",
                    }]}})
                elif self.path == "/search":
                    self._json({"results": [{
                        "title": "Tavily result",
                        "url": "https://example.com/tavily",
                        "content": "Tavily search excerpt",
                        "score": 0.9,
                    }]})
                elif self.path == "/extract":
                    self._json({"results": [{
                        "url": "https://example.com/page",
                        "raw_content": "Tavily extracted body " * 30,
                    }], "failed_results": []})
                else:
                    self._json({"error": "not found"}, status=404)

            def _json(self, payload: dict, status: int = 200) -> None:
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class InstalledServer:
    def __init__(self, executable: Path, environment: dict[str, str]) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.process = subprocess.Popen(
            [str(executable), "--http", "--port", str(self.port)],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not wait_tcp(self.port):
            self.stop()
            raise RuntimeError(
                "installed MCP server did not start:\n"
                + self.stderr.decode("utf-8", "replace")[-3000:]
            )

    def stop(self) -> None:
        if getattr(self, "process", None) is None:
            return
        self.process.terminate()
        try:
            _stdout, self.stderr = self.process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _stdout, self.stderr = self.process.communicate(timeout=8)
        self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()


async def list_tools(url: str) -> set[str]:
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            return {tool.name for tool in response.tools}


async def call_tool(url: str, name: str, arguments: dict) -> str:
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
            return "\n".join(
                block.text for block in result.content
                if getattr(block, "type", None) == "text"
            )


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, text=True, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file():
        parser.error(f"wheel not found: {wheel}")

    with tempfile.TemporaryDirectory(prefix="web-retrieval-install-") as temporary:
        root = Path(temporary)
        environment = dict(os.environ)
        environment.update({
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
        })
        venv.EnvBuilder(with_pip=True, clear=True).create(root / "venv")
        bindir = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        pip = [str(python), "-m", "pip"]
        run([*pip, "install", str(wheel)], env=environment)
        run([*pip, "check"], env=environment)

        optional_probe = run(
            [str(python), "-c",
             "import importlib.util as u; "
             "print(all(u.find_spec(n) is None for n in "
             "('tavily','camoufox','playwright','valkey','keyring')))"] ,
            env=environment,
            capture_output=True,
        )
        assert optional_probe.stdout.strip() == "True", optional_probe.stdout

        base_env = dict(environment)
        base_env["WEB_RETRIEVAL_MCP_CACHE"] = "off"
        with InstalledServer(bindir / "web-retrieval-mcp", base_env) as installed:
            assert asyncio.run(list_tools(installed.url)) == EXPECTED_TOOLS

        run([*pip, "install", "tavily-python==0.8.0"], env=environment)
        run([*pip, "check"], env=environment)

        key_file = root / "keys.env"
        key_file.write_text("FIRECRAWL_API_KEY=file-firecrawl-key\n", encoding="utf-8")
        if os.name != "nt":
            key_file.chmod(0o600)

        with ProviderLab() as lab:
            daemon_env = dict(environment)
            daemon_env.update({
                "WEB_RETRIEVAL_MCP_CACHE": "off",
                "WEB_RETRIEVAL_MCP_ENV_FILE": str(key_file),
                "WEBRET_ACCEPTANCE_LAB": "1",
                "WEBRET_ACCEPTANCE_EXA_SEARCH_URL":
                    f"http://127.0.0.1:{lab.port}/exa/search",
                "WEBRET_ACCEPTANCE_EXA_CONTENTS_URL":
                    f"http://127.0.0.1:{lab.port}/exa/contents",
                "WEBRET_ACCEPTANCE_FIRECRAWL_SEARCH_URL":
                    f"http://127.0.0.1:{lab.port}/firecrawl/search",
                "WEBRET_ACCEPTANCE_TAVILY_API_BASE": f"http://127.0.0.1:{lab.port}",
                "WEBRET_ACCEPTANCE_DNS_JSON": json.dumps({"example.com": ["8.8.8.8"]}),
                "EXA_API_KEY": "env-exa-key",
                "TAVILY_API_KEY": "env-tavily-key",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            })
            daemon_env.pop("FIRECRAWL_API_KEY", None)
            for proxy in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy",
            ):
                daemon_env.pop(proxy, None)

            with InstalledServer(bindir / "web-retrieval-mcp", daemon_env) as installed:
                exa = asyncio.run(call_tool(
                    installed.url, "web_search", {"query": "exa", "provider": "exa"}
                ))
                fallback = asyncio.run(call_tool(
                    installed.url,
                    "web_search",
                    {"query": "force fallback", "provider": "exa"},
                ))
                tavily_search = asyncio.run(call_tool(
                    installed.url,
                    "web_search",
                    {"query": "tavily", "provider": "tavily"},
                ))
                tavily_fetch = asyncio.run(call_tool(
                    installed.url,
                    "web_fetch",
                    {"url": "https://example.com/page", "render": "never", "tavily": True},
                ))

        assert "Exa result" in exa
        assert "served by: firecrawl search" in fallback
        assert "[served by: tavily search]" in tavily_search
        assert "[served by: tavily]" in tavily_fetch
        assert any(
            record["path"] == "/exa/search" and record["exa_key"] == "env-exa-key"
            for record in lab.records
        )
        assert any(
            record["path"] == "/firecrawl/search"
            and record["authorization"] == "Bearer file-firecrawl-key"
            for record in lab.records
        )
        assert any(
            record["path"] in {"/search", "/extract"}
            and record["authorization"] == "Bearer env-tavily-key"
            for record in lab.records
        )

    print("installed-wheel acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
