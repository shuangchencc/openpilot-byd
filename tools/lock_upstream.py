#!/usr/bin/env python3
"""Record or verify the pinned official source stack without executing its code.

Verification is local and read-only. This tool never fetches, starts openpilot,
loads model pickles, connects to a device, or installs/flashes anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstream-openpilot"
LOCK = ROOT / "upstream-lock.json"
SCHEMA = "songplus-upstream-lock/v1"
LFS_RE = re.compile(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?\Z")
PIN_FILES = {".gitmodules", ".gitattributes", ".python-version", "pyproject.toml", "uv.lock", "requirements.txt", "Cargo.lock", "Cargo.toml", "launch_env.sh", "SConstruct"}


def git(repo: Path, *args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], input=input_data, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_entries(repo: Path) -> list[dict]:
    result = []
    for record in git(repo, "ls-tree", "-r", "-l", "-z", "HEAD").split(b"\0"):
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, oid, size = header.decode().split()
        result.append({"path": os.fsdecode(name), "mode": mode, "kind": kind, "oid": oid, "size": None if size == "-" else int(size)})
    return result


def small_blobs(repo: Path, entries: list[dict]) -> dict[str, bytes]:
    oids = sorted({e["oid"] for e in entries if e["kind"] == "blob" and e["size"] <= 1024})
    data = git(repo, "cat-file", "--batch", input_data="".join(oid + "\n" for oid in oids).encode())
    result = {}
    cursor = 0
    for oid in oids:
        end = data.index(b"\n", cursor)
        head_oid, kind, size = data[cursor:end].decode().split()
        if head_oid != oid or kind != "blob":
            raise ValueError(f"Unexpected Git object {oid}")
        cursor = end + 1
        result[oid] = data[cursor:cursor + int(size)]
        cursor += int(size) + 1
    return result


def collect_repo(repo: Path) -> tuple[dict, list[dict]]:
    entries = tree_entries(repo)
    blobs = small_blobs(repo, entries)
    lfs = []
    pins = []
    for e in entries:
        if e["kind"] != "blob":
            continue
        path = repo / e["path"]
        pointer = LFS_RE.fullmatch(blobs.get(e["oid"], b""))
        if pointer:
            expected = pointer.group(1).decode()
            size = int(pointer.group(2))
            local_pointer = path.exists() and path.stat().st_size <= 1024 and path.read_bytes() == blobs[e["oid"]]
            available = path.is_file() and not local_pointer and path.stat().st_size == size and sha256(path) == expected
            lfs.append({"path": e["path"], "git_pointer_blob": e["oid"], "sha256": expected, "size": size,
                        "status": "downloaded_sha256_verified" if available else "upstream_pointer_only"})
            if not available and not local_pointer:
                raise ValueError(f"Missing or damaged LFS file: {path}")
        elif Path(e["path"]).name in PIN_FILES or Path(e["path"]).name.upper().startswith(("LICENSE", "COPYING")):
            pins.append({"path": e["path"], "git_blob": e["oid"], "sha256": sha256(path)})
    manifest = {"path": str(repo.relative_to(ROOT)), "origin": git(repo, "remote", "get-url", "origin").decode().strip(),
                "commit": git(repo, "rev-parse", "HEAD").decode().strip(), "tree": git(repo, "rev-parse", "HEAD^{tree}").decode().strip(),
                "tracked_blob_count": sum(e["kind"] == "blob" for e in entries),
                "gitlinks": [{"path": e["path"], "commit": e["oid"]} for e in entries if e["kind"] == "commit"],
                "pin_files": pins, "lfs_objects": lfs}
    return manifest, entries


def repos_recursive(repo: Path):
    yield repo
    for entry in tree_entries(repo):
        if entry["kind"] == "commit":
            child = repo / entry["path"]
            if not (child / ".git").exists():
                raise ValueError(f"Uninitialized submodule: {child}")
            actual = git(child, "rev-parse", "HEAD").decode().strip()
            if actual != entry["oid"]:
                raise ValueError(f"Gitlink mismatch: {child}: {actual} != {entry['oid']}")
            yield from repos_recursive(child)


def current_stack() -> list[dict]:
    return [collect_repo(repo)[0] for repo in repos_recursive(UPSTREAM)]


def verify_sources(repo: Path, manifest: dict, allow_lfs_pointers: bool) -> tuple[int, int]:
    """Compare every tracked source byte with Git, independent of LFS filters."""
    lfs = {item["path"]: item for item in manifest["lfs_objects"]}
    verified = pointers = 0
    for entry in tree_entries(repo):
        if entry["kind"] != "blob":
            continue
        path = repo / entry["path"]
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
        elif path.is_file():
            if entry["path"] in lfs:
                asset = lfs[entry["path"]]
                if path.stat().st_size == asset["size"] and sha256(path) == asset["sha256"]:
                    verified += 1
                    continue
                if not allow_lfs_pointers:
                    raise ValueError(f"LFS content missing or changed: {path}")
                if path.stat().st_size > 1024:
                    raise ValueError(f"Invalid LFS content: {path}")
                content = path.read_bytes()
                pointers += 1
            else:
                content = path.read_bytes()
        else:
            raise ValueError(f"Tracked file missing: {path}")
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if blob != entry["oid"]:
            raise ValueError(f"Tracked source differs from pinned commit: {path}")
        if entry["mode"] == "120000" and not path.is_symlink():
            raise ValueError(f"Tracked symlink replaced: {path}")
        if entry["mode"] in {"100644", "100755"} and bool(path.stat().st_mode & 0o100) != (entry["mode"] == "100755"):
            raise ValueError(f"Tracked executable mode changed: {path}")
    return verified, pointers


def agnos_declarations() -> dict:
    base = UPSTREAM / "openpilot/common/hardware/comma"
    launch = (UPSTREAM / "launch_env.sh").read_text()
    version = re.search(r'export AGNOS_VERSION="([^\"]+)"', launch).group(1)
    return {"version": version, "status": "upstream_manifests_only_no_partition_images_downloaded",
            "compatibility_with_c3xl": "not_validated",
            "hash_semantics": "Values below are copied unchanged from official manifests. They are not download SHA256s verified by this project.",
            "manifests": [{"path": str((base / name).relative_to(UPSTREAM)), "sha256": sha256(base / name),
                           "declared_partitions": json.loads((base / name).read_text())} for name in ("agnos.json", "all-partitions.json")]}


def dependency_declarations() -> dict:
    data = tomllib.loads((UPSTREAM / "uv.lock").read_text())
    return {"lock_path": "upstream-openpilot/uv.lock", "lock_sha256": sha256(UPSTREAM / "uv.lock"),
            "python_version": (UPSTREAM / ".python-version").read_text().strip(),
            "requires_python": data.get("requires-python"),
            "artifact_status": "versions_and_distribution_hashes_locked_by_uv_lock; host_installation_and_cache_coverage_reported_separately",
            "packages": [{"name": p["name"], "version": p.get("version"), "source": p.get("source"),
                          "locked_distribution_count": len(p.get("wheels", [])) + int("sdist" in p)} for p in data["package"]]}


def refresh(args) -> dict:
    if not args.expected_commit or not args.release_json or not args.release_commit or not args.checked_at:
        raise ValueError("--refresh requires --expected-commit, --release-json, --release-commit and --checked-at (explicit upstream query evidence)")
    actual = git(UPSTREAM, "rev-parse", "HEAD").decode().strip()
    if actual != args.expected_commit:
        raise ValueError("Current upstream HEAD differs from --expected-commit")
    release = json.loads(args.release_json.read_text())
    stack = current_stack()
    for repo in stack:
        verify_sources(ROOT / repo["path"], repo, allow_lfs_pointers=True)
    data = {"schema": SCHEMA, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "selection": {"policy": "official_master_pinned_at_query_time_not_automatic_latest",
                          "checked_at": args.checked_at, "remote": "https://github.com/commaai/openpilot.git", "ref": "refs/heads/master", "commit": actual,
                          "master_query": "git ls-remote https://github.com/commaai/openpilot.git refs/heads/master",
                          "release_query": "https://api.github.com/repos/commaai/openpilot/releases/latest",
                          "latest_release_observed": {"tag": release["tag_name"], "commit": args.release_commit,
                                                      "published_at": release["published_at"], "url": release["html_url"]}},
            "scope": {"hardware": "C3XL and existing Panda retained; compatibility not established",
                      "device_actions": "none: no device login, system modification, launcher execution, or firmware flashing",
                      "research_baseline": {"path": "upstream-opendbc", "lock": "baseline-lock.json", "relationship": "separate historical research baseline; not substituted for selected openpilot gitlink"}},
            "repos": stack, "python_dependencies": dependency_declarations(), "agnos": agnos_declarations()}
    LOCK.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return data


def verify(args) -> dict:
    expected = json.loads(LOCK.read_text())
    if expected["schema"] != SCHEMA:
        raise ValueError("Unknown lock schema")
    actual_repos = current_stack()
    if len(actual_repos) != len(expected["repos"]):
        raise ValueError("Recursive repository set changed")
    downloaded = pointers = 0
    for actual, locked in zip(actual_repos, expected["repos"], strict=True):
        # Availability may improve after fetching. Committed pointer identity cannot.
        actual_id = {k: v for k, v in actual.items() if k != "lfs_objects"}
        locked_id = {k: v for k, v in locked.items() if k != "lfs_objects"}
        if actual_id != locked_id:
            raise ValueError(f"Repository/lock metadata changed: {locked['path']}")
        if [{k: v for k, v in o.items() if k != "status"} for o in actual["lfs_objects"]] != [{k: v for k, v in o.items() if k != "status"} for o in locked["lfs_objects"]]:
            raise ValueError(f"LFS pointer manifest changed: {locked['path']}")
        v, p = verify_sources(ROOT / locked["path"], locked, args.allow_lfs_pointers)
        downloaded += v
        pointers += p
    if dependency_declarations() != expected["python_dependencies"] or agnos_declarations() != expected["agnos"]:
        raise ValueError("Dependency or AGNOS declaration changed")
    selection = expected["selection"]
    if selection["commit"] != actual_repos[0]["commit"]:
        raise ValueError("Selected commit differs from checkout")
    index = git(ROOT, "ls-files", "--stage", "upstream-openpilot").decode().split()
    if len(index) < 2 or index[0] != "160000" or index[1] != selection["commit"]:
        raise ValueError("Parent Git index does not pin the selected upstream gitlink")
    return {"status": "verified", "mode": "source_only_allowing_lfs_pointers" if args.allow_lfs_pointers else "strict",
            "network_used": False, "target_code_executed": False, "repositories": len(actual_repos),
            "lfs_objects_sha256_verified": downloaded, "lfs_pointer_only": pointers,
            "agnos": "declarations_verified_only; partition_images_not_downloaded; device_compatibility_not_validated"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--verify", action="store_true", help="Read-only verification, without network access")
    group.add_argument("--refresh", action="store_true", help="Explicitly replace lock from current local checkout and supplied query evidence")
    parser.add_argument("--allow-lfs-pointers", action="store_true", help="Accept intact LFS pointers for source-only CI; report missing assets")
    parser.add_argument("--expected-commit")
    parser.add_argument("--release-json", type=Path)
    parser.add_argument("--release-commit")
    parser.add_argument("--checked-at")
    args = parser.parse_args()
    try:
        result = refresh(args) if args.refresh else verify(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": "lock_written", "path": str(LOCK), "repositories": len(result["repos"])} if args.refresh else result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
