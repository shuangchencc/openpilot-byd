#!/usr/bin/env python3
"""Explicit passive exit supplement; preview unless --connect is supplied.

Reuse the frozen host receiver without editing it. Raw payloads/query output and
source snapshots go only to a new loaded-exit subdirectory in private external
storage. No service/state operations, target files, Panda access or retries.
"""
import argparse
import base64
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_PATH = ROOT / "hardware/current-host-recovery/loaded_exit_collector.py"
SHARED_PATH = ROOT / "tools/capture_current_host.py"
WRAPPER_PATH = Path(__file__).resolve()


def load_local_module(path, name):
    """Only this project's fixed, inert local modules; never target-provided code."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load_local_module(SHARED_PATH, "exit_supplement_shared_capture")
collector = load_local_module(COLLECTOR_PATH, "exit_supplement_local_collector")
ssh_command = shared.ssh_command
receive = shared.receive
private_write = shared.private_write
strict_json = shared.strict_json
sha = shared.sha
require = shared.require


def timestamp(value):
    require(isinstance(value, str) and len(value) <= 64, "timestamp_type")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "timestamp_timezone")
    return parsed


def validate_payload(result, path):
    require(result["exit_code"] == 0, "ssh_or_collector_failed")
    require(result["stderr"] == b"", "unexpected_remote_stderr")
    payload = path.read_bytes()
    require(len(payload) <= collector.LIMITS["output_bytes"], "payload_budget")
    require(len(payload) == result["bytes"] and sha(payload) == result["sha256"], "transport_hash")
    data = strict_json(payload)
    require(data["schema"] == collector.SCHEMA, "schema")
    require(timestamp(data["finished_utc"]) >= timestamp(data["collected_utc"]), "timestamp_order")
    boundary = data["boundary"]
    require(boundary["profile"] == "UNBOUND", "profile")
    for key in ("services_changed", "target_files_written", "device_data_read", "target_programs_imported_or_executed",
                "slot_changed", "panda_reset_or_flash", "can_tx", "boot_marker_content_read"):
        require(boundary[key] is False, "boundary")
    require(boundary["private_payload_requires_external_storage"] is True, "private_payload")
    queries, raws = {}, {}
    for position in ("before", "after"):
        item = data["systemd_loaded"][position]
        require(item["status"] == "collected" and type(item["returncode"]) is int and item["returncode"] == 0, "query_failed")
        require(item["argv"] == list(collector.SYSTEMCTL_ARGV), "query_argv")
        decoded = {}
        for stream in ("stdout", "stderr"):
            encoded = item[stream + "_base64"]
            require(isinstance(encoded, str) and len(encoded) <= collector.LIMITS["query_output_bytes"] * 2, "query_encoded_budget")
            value = base64.b64decode(encoded, validate=True)
            require(type(item[stream + "_bytes"]) is int and len(value) == item[stream + "_bytes"] and
                    sha(value) == item[stream + "_sha256"], "query_stream_hash")
            decoded[stream] = value
        require(len(decoded["stdout"]) + len(decoded["stderr"]) <= collector.LIMITS["query_output_bytes"], "query_output_budget")
        require(decoded["stderr"] == b"", "query_stderr")
        projection = collector.systemd_projection(decoded["stdout"])
        require(item["projection"] == projection, "query_projection")
        require(projection["duplicate_properties"] == [], "duplicate_property")
        raws[position] = decoded["stdout"]
        queries[position] = {"status": "VERIFIED_PASSIVE_QUERY", "returncode": 0,
                             "query_kind": "fixed_systemctl_show_all_properties",
                             "stdout_bytes": len(decoded["stdout"]), "stderr_bytes": 0,
                             "stdout_sha256": sha(decoded["stdout"]), "stderr_sha256": sha(b""),
                             "projection": projection}
    same = raws["before"] == raws["after"]
    require(data["systemd_loaded"]["same_output_across_collection"] is same, "reported_query_consistency")
    require(same, "loaded_state_changed")
    marker = data["boot_marker"]
    if marker.get("status") == "metadata_only":
        require(marker["path"] == "/tmp/booted" and marker["content_read"] is False and marker["symlink_followed"] is False, "marker_boundary")
        meta = marker["metadata"]
        keys = ("device", "inode", "mode", "uid", "gid", "bytes", "mtime_ns", "ctime_ns")
        require(set(meta) == {"kind", *keys}, "marker_metadata_fields")
        require(all(type(meta[key]) is int and meta[key] >= 0 for key in keys), "marker_metadata_types")
        kind = "regular" if stat.S_ISREG(meta["mode"]) else "symlink" if stat.S_ISLNK(meta["mode"]) else "other"
        require(meta["kind"] == kind, "marker_metadata_kind")
        safe_marker = {"status": "metadata_only", "path": "/tmp/booted", "metadata": meta,
                       "content_read": False, "symlink_followed": False}
    else:
        require(set(marker) == {"status", "error"} and marker["status"] == "unknown" and marker["error"] in
                {"FileNotFoundError", "PermissionError", "NotADirectoryError", "OSError", "ValueError", "total_time_budget"}, "marker_unknown")
        safe_marker = dict(marker)
    for key in ("atomic_snapshot", "all_external_launch_sources_proven", "process_cleanup_or_board_exit_state_verified"):
        require(data["consistency"][key] is False, "consistency_claim")
    return {"schema": data["schema"], "collected_utc": data["collected_utc"], "finished_utc": data["finished_utc"],
            "systemd_loaded": dict(queries, same_output_across_collection=True), "boot_marker": safe_marker,
            "boundary_verified": True, "raw_payload_private_only": True,
            "consistency": {"atomic_snapshot": False, "all_external_launch_sources_proven": False,
                            "process_cleanup_or_board_exit_state_verified": False}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--connection", type=Path, default=ROOT / "local/c3xl-connection.json")
    args = parser.parse_args(argv)
    source = COLLECTOR_PATH.read_bytes()
    wrapper = WRAPPER_PATH.read_bytes()
    helper = SHARED_PATH.read_bytes()
    sources = {"collector.py": source, "wrapper.py": wrapper, "shared_capture_helper.py": helper}
    source_hashes = {name: sha(value) for name, value in sources.items()}
    if not args.connect:
        print(json.dumps({"mode": "PREVIEW_NO_CONNECTION", "phase": "loaded-exit", "config_read": False,
                          "schema": collector.SCHEMA, "source_sha256": source_hashes,
                          "private_subdirectory": "loaded-exit", "max_received_bytes": collector.LIMITS["output_bytes"]}))
        return 0
    destination = args.destination
    require(destination is not None and destination.is_absolute(), "private_destination_required")
    require(not any(p.is_symlink() for p in (destination, *destination.parents)), "symlink_destination")
    require(destination.is_dir() and not destination.resolve().is_relative_to(ROOT), "destination_must_be_external")
    require(stat.S_IMODE(destination.stat().st_mode) == 0o700, "private_directory_mode")
    require(not (destination / "loaded-exit").exists() and not (destination / "loaded-exit").is_symlink(), "destination_already_exists")
    free = os.statvfs(destination)
    available = free.f_bavail * free.f_frsize
    require(available >= 32 * 1024 * 1024, "destination_capacity")
    command = ssh_command(args.connection, "startup", None)
    directory = destination / "loaded-exit"
    directory.mkdir(mode=0o700)
    for name, value in sources.items():
        private_write(directory / name, value)
    partial = directory / "loaded-exit.json.partial"
    receipt = {"schema": "songplus-current-loaded-exit-receipt/v1", "phase": "loaded-exit", "started_utc": shared.utc(),
               "source_sha256": source_hashes, "private_archive_directory": str(directory),
               "destination_available_bytes_at_start": available, "target_writes": False, "service_control": False,
               "slot_change": False, "panda_active_io_reset_flash": False, "can_tx": False, "profile": "UNBOUND",
               "automatic_retries": 0, "remote_service_logging_possible": True,
               "local_ssh_timeout_seconds": 45, "max_received_bytes": collector.LIMITS["output_bytes"]}
    try:
        result = receive(command, source, partial, collector.LIMITS["output_bytes"], 45, None)
        private_write(directory / "stderr.log", result["stderr"])
        receipt.update({key: value for key, value in result.items() if key != "stderr"})
        receipt["validation"] = validate_payload(result, partial)
        require(COLLECTOR_PATH.read_bytes() == source and WRAPPER_PATH.read_bytes() == wrapper and
                SHARED_PATH.read_bytes() == helper, "source_changed")
        final = directory / "loaded-exit.json"
        partial.rename(final)
        receipt.update(status="VERIFIED_PASSIVE_EXIT_SUPPLEMENT", artifact=final.name)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if isinstance(error, shared.ReceiveError):
            private_write(directory / "stderr.log", error.partial["stderr"])
            receipt.update({key: value for key, value in error.partial.items() if key != "stderr"})
        receipt.update(status="FAILED_PARTIAL_RETAINED", error_type=type(error).__name__)
    receipt["finished_utc"] = shared.utc()
    private_write(directory / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())
    print(json.dumps({"status": receipt["status"], "phase": "loaded-exit", "receipt": str(directory / "receipt.json")}))
    return 0 if receipt["status"] == "VERIFIED_PASSIVE_EXIT_SUPPLEMENT" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "UNAVAILABLE", "error_type": type(error).__name__}))
        raise SystemExit(2)
