"""Dependency and version-manifest drift checks."""

from __future__ import annotations

import importlib.metadata as metadata
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from web_retrieval_mcp._version import __version__

project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(label)
    print(f"{'OK  ' if condition else 'FAIL'}  {label}  {detail}")


check("package/server version matches pyproject", project["version"] == __version__)
check("mcp exact pin", "mcp==2.1.1" in project["dependencies"])
check("anyio exact pin", "anyio==4.14.2" in project["dependencies"])
extras = project["optional-dependencies"]
check("Camoufox exact pin", "camoufox[geoip]==0.5.5" in extras["render"])
check("Playwright supported minor", "playwright>=1.60,<1.61" in extras["render"])
check("Valkey client exact pin", extras["cache"] == ["valkey==6.1.1"])
check("Tavily SDK exact pin", extras["tavily"] == ["tavily-python==0.8.0"])
check(
    "Hatchling metadata-compatible pin",
    "hatchling==1.31.0" in project["optional-dependencies"]["dev"],
)

for distribution, expected in (
    ("mcp", "2.1.1"),
    ("anyio", "4.14.2"),
    ("camoufox", "0.5.5"),
    ("playwright", "1.60.0"),
    ("valkey", "6.1.1"),
    ("hatchling", "1.31.0"),
):
    actual = metadata.version(distribution)
    check(f"installed {distribution} matches release environment", actual == expected,
          f"got {actual}")

print(f"\n{'ALL PIN TESTS PASS' if not failures else str(len(failures)) + ' FAILURE(S)'}")
raise SystemExit(1 if failures else 0)
