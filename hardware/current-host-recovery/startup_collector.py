"""Explicit, bounded, passive startup/exit source collection; Python 3.8 stdlib.

Import and construction are inert. This result contains PRIVATE source bytes
and systemctl output in base64. Store the complete result outside the engineering
repository; only reviewed metadata/projections belong in versioned evidence.
No target imports, source execution, device reads, service state changes, or
arbitrary private config/key/parameter/environment reads. Only the fixed unit
and startup/exit source allowlist is read. A read can update filesystem atime.
"""
import base64
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time


SCHEMA = "songplus-current-host-startup-passive/v1"
SOURCE_PATHS = (
    "/usr/lib/systemd/system/comma.service", "/etc/systemd/system/comma.service",
    "/usr/comma/comma.sh", "/data/continue.sh",
    "/data/openpilot/launch_openpilot.sh", "/data/openpilot/launch_chffrplus.sh",
    "/data/openpilot/launch_env.sh",
    "/data/openpilot/sunnypilot/system/hardware/c3/launch_chffrplus.sh",
    "/data/openpilot/sunnypilot/system/hardware/c3/launch_env.sh",
    "/data/openpilot/system/manager/manager.py", "/data/openpilot/system/manager/process.py",
    "/data/openpilot/system/manager/process_config.py",
    "/data/openpilot/selfdrive/pandad/pandad.cc", "/data/openpilot/selfdrive/pandad/pandad.py",
    "/data/openpilot/selfdrive/panda/pandad.py", "/data/openpilot/selfdrive/panda/__init__.py",
    "/data/openpilot/system/hardware/base.py", "/data/openpilot/system/hardware/tici/hardware.py",
    "/data/openpilot/sunnypilot/system/hardware/c3/hardware.py",
)
DYNAMIC_METADATA_PATHS = ("/data/custom_op_script.sh", "/data/__system_reset__", "/data/__system_setup__")
PROPERTIES = (
    "ActiveState", "SubState", "LoadState", "UnitFileState", "NeedDaemonReload", "Result",
    "FragmentPath", "DropInPaths", "KillMode", "KillSignal", "FinalKillSignal", "SendSIGKILL",
    "Restart", "RestartUSec", "TimeoutStopUSec", "RemainAfterExit", "ExecMainPID", "ControlGroup",
    "Triggers", "TriggeredBy", "ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost",
)
SYSTEMCTL_ARGV = ("/bin/systemctl", "show", "comma.service", "--no-pager", "--property=" + ",".join(PROPERTIES))
LIMITS = {"file_bytes": 256 * 1024, "total_read_bytes": 4 * 1024 * 1024,
          "query_output_bytes": 256 * 1024, "query_seconds": 5, "total_seconds": 25,
          "output_bytes": 7 * 1024 * 1024}


class Unavailable(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def metadata(value):
    kind = "regular" if stat.S_ISREG(value.st_mode) else "symlink" if stat.S_ISLNK(value.st_mode) else "other"
    return {"kind": kind, "device": value.st_dev, "inode": value.st_ino,
            "mode": value.st_mode, "uid": value.st_uid, "gid": value.st_gid, "bytes": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def systemd_projection(stdout):
    """Safe loaded-manager projection; all raw output remains private."""
    raw_fields, duplicates = {}, []
    for line in stdout.decode("utf-8", "replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in PROPERTIES:
            continue
        if key in raw_fields:
            duplicates.append(key)
        raw_fields[key] = value
    result = {}
    enums = {
        "ActiveState": {"active", "inactive", "failed", "activating", "deactivating", "reloading"},
        "SubState": {"running", "dead", "failed", "exited", "auto-restart", "start", "stop", "stop-sigterm", "stop-sigkill"},
        "LoadState": {"loaded", "not-found", "error", "masked", "stub", "merged"},
        "UnitFileState": {"enabled", "disabled", "static", "masked", "indirect", "generated", "transient", "linked", "enabled-runtime"},
        "NeedDaemonReload": {"yes", "no"}, "SendSIGKILL": {"yes", "no"}, "RemainAfterExit": {"yes", "no"},
        "KillMode": {"control-group", "mixed", "process", "none"},
        "Restart": {"no", "always", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-abort"},
        "Result": {"success", "exit-code", "signal", "core-dump", "watchdog", "timeout", "resources", "start-limit-hit", "protocol"},
    }
    for key, value in raw_fields.items():
        field = {"value_sha256": sha256(value.encode()), "value_bytes": len(value.encode()), "empty": value == ""}
        if key in enums and value in enums[key]:
            field["value"] = value
        elif key in ("KillSignal", "FinalKillSignal", "ExecMainPID") and re.fullmatch(r"[0-9]{1,10}", value):
            field["value"] = int(value)
        elif key in ("RestartUSec", "TimeoutStopUSec") and len(value) <= 64 and re.fullmatch(
                r"(?:infinity|[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d|w|y)?(?: [0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d|w|y))*)", value):
            field["value"] = value
        elif key == "FragmentPath" and value in SOURCE_PATHS[:2]:
            field["value"] = value
        elif key == "ControlGroup" and value == "/system.slice/comma.service":
            field["value"] = value
        elif value == "":
            field["value"] = ""
        result[key] = field
    return {"fields": result, "duplicate_properties": sorted(set(duplicates)),
            "missing_properties": sorted(set(PROPERTIES) - set(raw_fields)),
            "raw_arguments_and_unknown_values_redacted": True}


def run_systemctl_query():
    """Only fixed read-only show; capped pipes, timeout, no shell or inherited env.

    On timeout/overflow only this collector-owned query process is killed and
    reaped; no service process, unit or target program receives a signal.
    """
    selector = selectors.DefaultSelector()
    proc = None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        proc = subprocess.Popen(list(SYSTEMCTL_ARGV), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                shell=False, close_fds=True, cwd="/",
                                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SYSTEMD_PAGER": "cat", "SYSTEMD_COLORS": "0"})
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + LIMITS["query_seconds"]
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unavailable("query_timeout")
            for key, _ in selector.select(min(remaining, 0.1)):
                budget = LIMITS["query_output_bytes"] - sum(len(v) for v in output.values())
                chunk = os.read(key.fd, min(4096, budget + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(chunk) > budget:
                    raise Unavailable("query_output_budget")
                output[key.data].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Unavailable("query_timeout")
        returncode = proc.wait(timeout=remaining)
        return {"returncode": returncode, "stdout": bytes(output["stdout"]), "stderr": bytes(output["stderr"])}
    finally:
        selector.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()


class Collector:
    def __init__(self, root, runner=None, monotonic=time.monotonic):
        if root is None or not Path(root).is_absolute():
            raise ValueError("explicit absolute root required")
        self.root = Path(root)
        self.runner = runner
        self.monotonic = monotonic
        self.deadline = None
        self.bytes_read = 0
        self.gaps = collections.Counter()

    def check(self):
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise Unavailable("total_time_budget")

    def failure(self, scope, exc):
        kind = str(exc) if isinstance(exc, Unavailable) else type(exc).__name__
        self.gaps[scope + ":" + kind] += 1
        return {"status": "unknown", "error": kind}

    def _pin(self, path):
        if path not in SOURCE_PATHS + DYNAMIC_METADATA_PATHS:
            raise Unavailable("path_not_allowlisted")
        if any(not hasattr(os, name) for name in ("O_PATH", "O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")):
            raise Unavailable("safe_pinning_unavailable")
        flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
        parents = []
        try:
            parents.append(os.open(str(self.root), flags | os.O_DIRECTORY))
            parts = path.lstrip("/").split("/")
            for part in parts[:-1]:
                self.check()
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parents[-1])
                parents.append(child)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise Unavailable("parent_not_directory")
            return os.open(parts[-1], flags, dir_fd=parents[-1])
        finally:
            for fd in reversed(parents):
                os.close(fd)

    def source(self, path):
        if path not in SOURCE_PATHS:
            raise Unavailable("source_path_not_allowlisted")
        self.check()
        pinned, fd = None, None
        try:
            pinned = self._pin(path)
            before = os.fstat(pinned)
            if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= LIMITS["file_bytes"]:
                raise Unavailable("not_bounded_regular_file")
            if before.st_size + self.bytes_read > LIMITS["total_read_bytes"]:
                raise Unavailable("total_read_budget")
            fd = os.open("/proc/self/fd/%d" % pinned, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            opened = os.fstat(fd)
            if identity(before) != identity(opened):
                raise Unavailable("inode_changed_before_read")
            chunks, count = [], 0
            while True:
                self.check()
                allowance = min(LIMITS["file_bytes"] + 1 - count,
                                LIMITS["total_read_bytes"] - self.bytes_read)
                if allowance <= 0:
                    # A full-size file cannot consume an extra byte beyond the
                    # aggregate cap; its EOF must fit the remaining read budget.
                    raise Unavailable("total_read_budget")
                chunk = os.read(fd, min(4096, allowance))
                self.bytes_read += len(chunk)
                count += len(chunk)
                if not chunk:
                    break
                chunks.append(chunk)
                if count > LIMITS["file_bytes"]:
                    raise Unavailable("file_read_budget")
            if identity(os.fstat(fd)) != identity(opened) or count != before.st_size:
                raise Unavailable("inode_or_length_changed_during_read")
            data = b"".join(chunks)
            return {"status": "collected", "metadata": metadata(before), "sha256": sha256(data),
                    "bytes": len(data), "content_base64": base64.b64encode(data).decode("ascii"),
                    "stable_during_read": True, "executed": False}
        finally:
            if fd is not None:
                os.close(fd)
            if pinned is not None:
                os.close(pinned)

    def dynamic_metadata(self, path):
        if path not in DYNAMIC_METADATA_PATHS:
            raise Unavailable("metadata_path_not_allowlisted")
        self.check()
        pinned = self._pin(path)
        try:
            return {"status": "metadata_only", "metadata": metadata(os.fstat(pinned)),
                    "content_read": False, "symlink_followed": False}
        finally:
            os.close(pinned)

    def query(self):
        self.check()
        if self.runner is None and self.root != Path("/"):
            raise Unavailable("fixture_requires_injected_runner")
        result = (self.runner or run_systemctl_query)()
        if not isinstance(result, dict) or set(result) != {"stdout", "stderr", "returncode"}:
            raise Unavailable("invalid_runner_result")
        stdout, stderr = result["stdout"], result["stderr"]
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes) or type(result["returncode"]) is not int:
            raise Unavailable("invalid_runner_types")
        if len(stdout) + len(stderr) > LIMITS["query_output_bytes"]:
            raise Unavailable("query_output_budget")
        return {"status": "collected" if result["returncode"] == 0 else "query_failed",
                "argv": list(SYSTEMCTL_ARGV), "returncode": result["returncode"],
                "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
                "stdout_sha256": sha256(stdout), "stderr_sha256": sha256(stderr),
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                "projection": systemd_projection(stdout) if result["returncode"] == 0 else None}

    def collect(self):
        self.deadline = self.monotonic() + LIMITS["total_seconds"]
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        loaded, sources, dynamic = {}, {}, {}
        for position in ("before", "after"):
            if position == "after":
                for path in SOURCE_PATHS:
                    try:
                        sources[path] = self.source(path)
                    except (OSError, Unavailable, ValueError) as exc:
                        sources[path] = self.failure("source", exc)
                for path in DYNAMIC_METADATA_PATHS:
                    try:
                        dynamic[path] = self.dynamic_metadata(path)
                    except (OSError, Unavailable, ValueError) as exc:
                        dynamic[path] = self.failure("dynamic_metadata", exc)
            try:
                loaded[position] = self.query()
            except (OSError, Unavailable, ValueError, subprocess.SubprocessError) as exc:
                loaded[position] = self.failure("systemd_query", exc)
        successful = all(loaded[p].get("status") == "collected" for p in ("before", "after"))
        loaded["same_output_across_collection"] = (loaded["before"]["stdout_sha256"] == loaded["after"]["stdout_sha256"]) if successful else None
        return {"schema": SCHEMA, "collected_utc": started,
                "finished_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "scope": "private current startup source and loaded manager metadata; no execution or atomic snapshot",
                "boundary": {"profile": "UNBOUND", "services_changed": False, "target_files_written": False,
                             "device_data_read": False, "target_programs_imported_or_executed": False,
                             "slot_changed": False, "panda_reset_or_flash": False, "can_tx": False,
                             "source_reads_may_update_atime": True, "private_payload_requires_external_storage": True},
                "files": sources, "dynamic_paths": dynamic, "systemd_loaded": loaded,
                "gaps": dict(sorted(self.gaps.items())), "limits": dict(LIMITS), "bytes_read": self.bytes_read,
                "consistency": {"atomic_across_objects": False, "loaded_disk_equivalence_proven": False,
                                "live_runtime_code_equivalence_proven": False, "dropin_contents_collected": False}}


def collect(root, runner=None):
    return Collector(root, runner=runner).collect()


if __name__ == "__main__":
    result = collect("/")
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > LIMITS["output_bytes"]:
        raise SystemExit("output_budget")
    print(encoded)
