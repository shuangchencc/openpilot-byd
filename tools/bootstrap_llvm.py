#!/usr/bin/env python3
"""Explicit online bootstrap for the pinned project-local macOS LLVM runtime.

Only the archive members named in host-toolchain-lock.json are extracted. No
Homebrew installation, shell profile, system library, or upstream source changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="Recreate runtime from verified cached archives, downloading only if missing")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64" or Path("/AGNOS").exists():
        raise SystemExit("This host toolchain is for macOS arm64 only, never a comma device")
    lock = json.loads((ROOT / "host-toolchain-lock.json").read_text())
    required = tuple(map(int, lock["minimum_macos"].split(".")))
    actual = tuple(map(int, platform.mac_ver()[0].split(".")[:2]))
    if actual < required:
        raise SystemExit(f"Pinned bottle requires macOS >= {lock['minimum_macos']}")
    if not args.rebuild and all((ROOT / f["path"]).is_file() and sha256(ROOT / f["path"]) == f["sha256"] for f in lock["files"]):
        print("PASS project-local LLVM runtime already matches the lock")
        return
    for archive in lock["archives"]:
        path = ROOT / archive["archive_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or sha256(path) != archive["sha256"]:
            token_url = "https://ghcr.io/token?" + urllib.parse.urlencode({"service": "ghcr.io", "scope": archive["scope"]})
            with urllib.request.urlopen(token_url, timeout=30) as response:
                token = json.load(response)["token"]
            request = urllib.request.Request(archive["url"], headers={"Authorization": "Bearer " + token})
            temporary = path.with_suffix(path.suffix + ".part")
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if sha256(temporary) != archive["sha256"]:
                raise SystemExit(f"Archive checksum mismatch: {archive['name']}")
            temporary.replace(path)
        with tarfile.open(path) as source:
            for entry in archive["files"]:
                member = source.getmember(entry["member"])
                if not member.isfile():
                    raise SystemExit(f"Expected a regular archive member: {member.name}")
                with source.extractfile(member) as stream:
                    data = stream.read()
                if hashlib.sha256(data).hexdigest() != entry["source_sha256"]:
                    raise SystemExit(f"Archive member checksum mismatch: {member.name}")
                (ROOT / entry["path"]).write_bytes(data)
    for item in lock["relocations"]:
        path = ROOT / item["path"]
        command = ["/usr/bin/install_name_tool", "-id", item["id"]]
        for original, local in item["changes"].items():
            command.extend(["-change", original, local])
        subprocess.run([*command, str(path)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(path)], check=True)
    for entry in lock["files"]:
        if sha256(ROOT / entry["path"]) != entry["sha256"]:
            raise SystemExit(f"Relocated toolchain checksum differs: {entry['path']}; review local Xcode/codesign version")
    print("PASS pinned LLVM and zstd runtime extracted, relocated and verified in .bootstrap/llvm-host")


if __name__ == "__main__":
    main()
