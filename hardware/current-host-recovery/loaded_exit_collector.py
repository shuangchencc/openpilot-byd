"""Independent bounded read-only loaded exit semantics query; Python 3.8 stdlib.

Import/construction are inert. Complete output contains PRIVATE systemctl stdout
and stderr in base64 and must be archived outside the repository. Only a fixed
show --all query is executed; no target program import, service state operation,
device control, source/key/config/environment read, or target-file write exists.
The sole file observation is lstat of /tmp/booted; no content or link target read.
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

SCHEMA = "songplus-current-loaded-exit-passive/v1"
SOURCE_PATHS = ("/usr/lib/systemd/system/comma.service", "/etc/systemd/system/comma.service")
PROPERTIES = (
    "Type", "User", "ActiveState", "SubState", "LoadState", "UnitFileState", "NeedDaemonReload", "Result",
    "FragmentPath", "DropInPaths", "KillMode", "KillSignal", "FinalKillSignal", "SendSIGKILL",
    "Restart", "RestartUSec", "TimeoutStopUSec", "RemainAfterExit", "ExecMainPID", "ControlGroup",
    "Triggers", "TriggeredBy", "ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost",
)
SYSTEMCTL_ARGV = ("/bin/systemctl", "show", "comma.service", "--all", "--no-pager", "--property=" + ",".join(PROPERTIES))
LIMITS = {"query_output_bytes": 256 * 1024, "query_seconds": 5, "total_seconds": 15,
          "output_bytes": 1024 * 1024}


class Unavailable(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


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
        "Type": {"simple", "exec", "forking", "oneshot", "dbus", "notify", "idle"},
        "User": {"comma", "root"},
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
    def __init__(self, root, runner=None, lstat=os.lstat, monotonic=time.monotonic):
        if root is None or not Path(root).is_absolute():
            raise ValueError("explicit absolute root required")
        self.root = Path(root)
        self.runner = runner
        self.lstat = lstat
        self.monotonic = monotonic
        self.deadline = None
        self.gaps = collections.Counter()

    def check(self):
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise Unavailable("total_time_budget")

    def failure(self, scope, exc):
        kind = str(exc) if isinstance(exc, Unavailable) else type(exc).__name__
        self.gaps[scope + ":" + kind] += 1
        return {"status": "unknown", "error": kind}

    def boot_marker(self):
        self.check()
        value = self.lstat(str(self.root / "tmp/booted"))
        kind = "regular" if stat.S_ISREG(value.st_mode) else "symlink" if stat.S_ISLNK(value.st_mode) else "other"
        return {"status": "metadata_only", "path": "/tmp/booted",
                "metadata": {"kind": kind, "device": value.st_dev, "inode": value.st_ino,
                             "mode": value.st_mode, "uid": value.st_uid, "gid": value.st_gid,
                             "bytes": value.st_size, "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns},
                "content_read": False, "symlink_followed": False,
                "scope": "point-in-time lstat; a symlink target is not inspected"}

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
        loaded, marker = {}, None
        for position in ("before", "after"):
            if position == "after":
                try:
                    marker = self.boot_marker()
                except (OSError, Unavailable, ValueError) as exc:
                    marker = self.failure("boot_marker", exc)
            try:
                loaded[position] = self.query()
            except (OSError, Unavailable, ValueError, subprocess.SubprocessError) as exc:
                loaded[position] = self.failure("systemd_query", exc)
        successful = all(loaded[p].get("status") == "collected" for p in ("before", "after"))
        loaded["same_output_across_collection"] = (loaded["before"]["stdout_sha256"] == loaded["after"]["stdout_sha256"]) if successful else None
        return {"schema": SCHEMA, "collected_utc": started,
                "finished_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "scope": "fixed loaded-manager show --all properties and /tmp/booted lstat only",
                "boundary": {"profile": "UNBOUND", "services_changed": False, "target_files_written": False,
                             "device_data_read": False, "target_programs_imported_or_executed": False,
                             "slot_changed": False, "panda_reset_or_flash": False, "can_tx": False,
                             "boot_marker_content_read": False, "private_payload_requires_external_storage": True},
                "systemd_loaded": loaded, "boot_marker": marker,
                "gaps": dict(sorted(self.gaps.items())), "limits": dict(LIMITS),
                "consistency": {"atomic_snapshot": False, "all_external_launch_sources_proven": False,
                                "process_cleanup_or_board_exit_state_verified": False}}


def collect(root, runner=None, lstat=os.lstat):
    return Collector(root, runner=runner, lstat=lstat).collect()


if __name__ == "__main__":
    result = collect("/")
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > LIMITS["output_bytes"]:
        raise SystemExit("output_budget")
    print(encoded)
