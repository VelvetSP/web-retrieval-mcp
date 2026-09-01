#!/usr/bin/env python3
"""Safe archive extraction and installed-tree integrity for the Valkey test fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile


MARKER = ".web-retrieval-install.json"
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024


def _tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"installed root is not a real directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if rel == MARKER:
            continue
        if path.is_symlink():
            raise ValueError(f"installed tree contains a symlink: {rel}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            digest.update(f"D\0{rel}\0{mode:o}\0".encode())
        elif path.is_file():
            digest.update(f"F\0{rel}\0{mode:o}\0{path.stat().st_size}\0".encode())
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        else:
            raise ValueError(f"installed tree contains a special node: {rel}")
    return digest.hexdigest()


def extract_archive(archive: Path, dest: Path, version: str, distro: str, arch: str) -> Path:
    expected_root = f"valkey-{version}-{distro}-{arch}"
    if not archive.is_file() or archive.is_symlink():
        raise ValueError("archive is not a real file")
    if not dest.is_dir() or dest.is_symlink() or any(dest.iterdir()):
        raise ValueError("extraction destination must be an empty real directory")
    seen: set[str] = set()
    expanded = 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("archive member count is empty or excessive")
        for member in members:
            name = member.name.rstrip("/")
            posix = PurePosixPath(name)
            if (not name or posix.is_absolute() or ".." in posix.parts
                    or not posix.parts or posix.parts[0] != expected_root):
                raise ValueError(f"archive member escapes expected root: {member.name!r}")
            if name in seen:
                raise ValueError(f"archive contains a duplicate member: {name!r}")
            seen.add(name)
            if not (member.isdir() or member.isreg()):
                raise ValueError(f"archive contains a link or special node: {name!r}")
            expanded += member.size
            if expanded > MAX_EXPANDED_BYTES:
                raise ValueError("archive expanded-size limit exceeded")
        bundle.extractall(dest, members=members, filter="data")
    root = dest / expected_root
    for rel in ("bin/valkey-server", "bin/valkey-cli"):
        binary = root / rel
        if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
            raise ValueError(f"archive lacks executable {rel}")
    return root


def write_marker(root: Path, version: str, archive_sha256: str) -> dict[str, str]:
    payload = {
        "archive_sha256": archive_sha256,
        "tree_sha256": _tree_digest(root),
        "version": version,
    }
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=root, prefix=f".{MARKER}.", delete=False
    ) as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, root / MARKER)
    return payload


def verify_installed(root: Path, version: str, archive_sha256: str | None) -> dict[str, str]:
    if root.name != f"valkey-{version}":
        raise ValueError("installed directory name does not match marker version")
    marker_path = root / MARKER
    if not marker_path.is_file() or marker_path.is_symlink():
        raise ValueError("installed marker is absent or not a real file")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("installed marker is unreadable") from exc
    if set(marker) != {"archive_sha256", "tree_sha256", "version"}:
        raise ValueError("installed marker has an unknown shape")
    if marker["version"] != version:
        raise ValueError("installed marker version mismatch")
    if archive_sha256 is not None and marker["archive_sha256"] != archive_sha256:
        raise ValueError("installed archive digest mismatch")
    actual = _tree_digest(root)
    if marker["tree_sha256"] != actual:
        raise ValueError("installed tree content drift")
    return marker


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--dest", type=Path, required=True)
    extract.add_argument("--version", required=True)
    extract.add_argument("--distro", required=True)
    extract.add_argument("--arch", required=True)
    marker = sub.add_parser("write-marker")
    marker.add_argument("--root", type=Path, required=True)
    marker.add_argument("--version", required=True)
    marker.add_argument("--archive-sha256", required=True)
    verify = sub.add_parser("verify-installed")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--archive-sha256")
    tree = sub.add_parser("tree-digest")
    tree.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "extract":
        print(extract_archive(args.archive, args.dest, args.version, args.distro, args.arch))
    elif args.command == "write-marker":
        print(json.dumps(write_marker(args.root, args.version, args.archive_sha256), sort_keys=True))
    elif args.command == "verify-installed":
        print(json.dumps(
            verify_installed(args.root, args.version, args.archive_sha256), sort_keys=True
        ))
    else:
        print(_tree_digest(args.root))


if __name__ == "__main__":
    main()
