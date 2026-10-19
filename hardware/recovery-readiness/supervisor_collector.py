"""Bounded passive supervision/storage metadata; Python 3.8 stdlib stdin.

Import and construction are inert. collect(root) requires an explicit root.
No commands, target imports, ioctl, device data opens, locks, writes, cmdline,
environment, params, keys, or custom_op_script content. /dev and process FDs
are stat only. Reads pin regular inodes using Linux O_PATH; application and
unit parent symlinks fail closed. Only fixed kernel sysfs directory aliases
and /proc/self may be followed. Nothing observed authorizes isolation/launch.
"""
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time


SCHEMA = "songplus-recovery-supervisor-passive/v1"
UNIT_NAMES = ("comma.service", "openpilot.service", "pandad.service", "panda.service",
              "manager.service", "tmux.service", "rc-local.service")
UNIT_PATHS = tuple(directory + "/" + unit for directory in
                   ("/etc/systemd/system", "/usr/lib/systemd/system") for unit in UNIT_NAMES)
SCRIPT_PATHS = ("/usr/comma/comma.sh", "/data/continue.sh", "/data/openpilot/launch_openpilot.sh",
                "/data/openpilot/launch_chffrplus.sh", "/data/openpilot/launch_env.sh",
                "/data/openpilot/sunnypilot/system/hardware/c3/launch_chffrplus.sh", "/etc/rc.local")
STARTUP_FILES = UNIT_PATHS + SCRIPT_PATHS
INTERPRETERS = ("/bin/sh", "/bin/bash", "/usr/bin/sh", "/usr/bin/bash", "/usr/bin/python3",
                "/usr/bin/python3.8", "/usr/bin/python3.12", "/usr/bin/env", "/usr/bin/tmux", "/bin/tmux")
COMM_CANDIDATES = {"pandad", "selfdrive.panda", "manager", "system.manager", "system.manager.",
                   "tmux", "tmux: server", "tmux: client"}
MOUNTS = ("/", "/data", "/systemrw", "/dsp", "/firmware", "/persist")
LIMITS = {"total_seconds": 20, "process_scan_seconds": 6, "read_bytes": 4 * 1024 * 1024,
          "directory_entries": 100000, "pids": 4096, "fds": 65536, "seeds": 32,
          "ancestor_nodes": 128, "ancestor_depth": 24, "startup_file_bytes": 256 * 1024,
          "block_entries": 256, "usb_entries": 512, "output_bytes": 1024 * 1024}


class Unavailable(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_stat(raw):
    start, end = raw.find("("), raw.rfind(")")
    if start < 1 or end <= start:
        raise ValueError("invalid stat delimiters")
    values = raw[end + 1:].split()
    if len(values) < 20:
        raise ValueError("short stat")
    comm = raw[start + 1:end]
    if len(comm) > 64 or any(ord(c) < 32 for c in comm):
        comm = "redacted_nonstandard_comm"
    return {"pid": int(raw[:start].strip()), "ppid": int(values[1]),
            "comm": comm, "starttime_ticks": int(values[19])}


def cgroup_fields(raw):
    result = []
    for line in raw.splitlines()[:64]:
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not re.fullmatch(r"[a-zA-Z0-9_,=-]*", parts[1]):
            continue
        path = parts[2]
        safe = True
        for segment in path.strip("/").split("/"):
            if segment in ("", "system.slice", "user.slice", "init.scope", "app.slice") + UNIT_NAMES:
                continue
            if re.fullmatch(r"(?:user-[0-9]+\.slice|user@[0-9]+\.service|session-[a-z]?[0-9]+\.scope)", segment):
                continue
            safe = False
        item = {"hierarchy": int(parts[0]), "controllers": parts[1].split(",") if parts[1] else [],
                "path_sha256": digest(path.encode()), "path_bytes": len(path.encode())}
        if safe and path.startswith("/") and len(path) <= 512:
            item["path"] = path
        else:
            item["path"] = "redacted_nonallowlisted_cgroup_path"
        result.append(item)
    return result


def startup_fields(path, data):
    """Hash all selected files; emit only literal allowlisted configuration."""
    result = {"bytes": len(data), "sha256": digest(data), "executed": False,
              "scope": "disk startup candidate, not proof this supervisor loaded it"}
    text = data.decode("utf-8", "replace")
    result["known_path_markers"] = [name for name in SCRIPT_PATHS + INTERPRETERS if name in text]
    result["static_token_markers"] = [token for token in
        ("tmux", "exec", "source", "launch_openpilot.sh", "launch_chffrplus.sh")
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", text)]
    result["dynamic_custom_script_reference"] = "custom_op_script" in text
    if path not in UNIT_PATHS:
        return result
    selected, commands = {}, []
    enums = {"Type": {"simple", "exec", "forking", "oneshot", "dbus", "notify", "idle"},
             "Restart": {"no", "always", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-abort"},
             "KillMode": {"control-group", "mixed", "process", "none"},
             "RemainAfterExit": {"yes", "no", "true", "false"}, "User": {"comma", "root"},
             "Group": {"comma", "root"}}
    for line in text.splitlines()[:2048]:
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in enums and value in enums[key]:
            selected[key] = value
        elif key in ("RestartSec", "TimeoutStopSec") and re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,3})?(?:ms|s|min)?", value):
            selected[key] = value
        elif key in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"):
            item = {"field": key, "value_sha256": digest(value.encode()), "arguments_redacted": True,
                    "line_projection_only": True}
            try:
                args = shlex.split(value)
            except ValueError:
                args = []
            if args:
                executable = args[0].lstrip("-+!:@")
                if executable in INTERPRETERS + SCRIPT_PATHS:
                    item["known_executable"] = executable
                item["known_literal_script_arguments"] = [a for a in args[1:] if a in SCRIPT_PATHS]
            commands.append(item)
    result["selected_unit_fields"] = selected
    result["commands_not_executed"] = commands[:32]
    result["unit_resolution_unknown"] = "drop-ins, generated units, enablement and loaded manager state not queried"
    return result


def parse_mounts(raw):
    result = []
    for line in raw.splitlines():
        words = line.split()
        if len(words) < 10 or words[4] not in MOUNTS or "-" not in words:
            continue
        separator = words.index("-")
        if separator + 3 >= len(words):
            continue
        source = words[separator + 2]
        result.append({"mount_point": words[4], "major_minor": words[2],
                       "source": source if re.fullmatch(r"/dev/sd[a-f][0-9]{1,3}", source) else "redacted_non_partition_source",
                       "filesystem_type": words[separator + 1] if words[separator + 1] in
                       ("ext4", "ext3", "vfat", "tmpfs", "overlay", "squashfs", "f2fs") else "other",
                       "flags": sorted(set(words[5].split(",")) & {"ro", "rw", "nosuid", "nodev", "noexec", "relatime"})})
    return result


class Collector:
    def __init__(self, root, monotonic=time.monotonic):
        if root is None or not Path(root).is_absolute():
            raise ValueError("explicit absolute filesystem root required")
        self.root = Path(root)
        self.monotonic = monotonic
        self.deadline = None
        self.bytes_read = 0
        self.entries_seen = 0
        self.gaps = collections.Counter()
        self.targets = {}

    def check(self):
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise Unavailable("time_budget")

    def error(self, scope, exc):
        kind = str(exc) if isinstance(exc, Unavailable) else type(exc).__name__
        self.gaps[scope + ":" + kind] += 1
        return {"status": "unknown", "error": kind}

    def path(self, absolute):
        if not absolute.startswith("/") or ".." in Path(absolute).parts:
            raise ValueError("invalid absolute path")
        return self.root / absolute.lstrip("/")

    def allowed_read(self, path):
        return (path in STARTUP_FILES or path == "/proc/self/mountinfo" or
                bool(re.fullmatch(r"/proc/[0-9]+/(stat|cgroup)", path)) or
                bool(re.fullmatch(r"/sys/class/block/sd[a-f][0-9]{0,3}/(?:size|dev|ro|queue/logical_block_size)", path)) or
                bool(re.fullmatch(r"/sys/bus/usb/devices/[0-9]+-[0-9]+(?:\.[0-9]+)*/(?:idVendor|idProduct|busnum|devnum)", path)))

    def _pin(self, path):
        if any(not hasattr(os, flag) for flag in ("O_PATH", "O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")):
            raise Unavailable("safe_metadata_pinning_unavailable")
        flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
        parents = []
        try:
            parents.append(os.open(str(self.root), flags | os.O_DIRECTORY))
            parts = path.lstrip("/").split("/")
            for i, part in enumerate(parts[:-1]):
                self.check()
                directory_flags = flags | os.O_DIRECTORY
                # These are fixed kernel-maintained alias directories only.
                if (parts[:i + 1] == ["proc", "self"] or
                        (path.startswith("/sys/") and i >= 1)):
                    directory_flags &= ~os.O_NOFOLLOW
                child = os.open(part, directory_flags, dir_fd=parents[-1])
                parents.append(child)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise Unavailable("parent_not_directory")
            return os.open(parts[-1], flags, dir_fd=parents[-1])
        finally:
            for fd in reversed(parents):
                os.close(fd)

    def read(self, path, limit=4096):
        self.check()
        if not self.allowed_read(path):
            raise Unavailable("path_not_allowlisted")
        pinned, fd = None, None
        virtual = path.startswith(("/proc/", "/sys/"))
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns)
        try:
            pinned = self._pin(path)
            before = os.fstat(pinned)
            if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
                raise Unavailable("not_bounded_regular_file")
            fd = os.open("/proc/self/fd/%d" % pinned, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            opened = os.fstat(fd)
            if identity(opened) != identity(before):
                raise Unavailable("inode_changed_before_read")
            chunks, total = [], 0
            while total <= limit:
                self.check()
                count = min(4096, limit + 1 - total, LIMITS["read_bytes"] - self.bytes_read)
                if count <= 0:
                    raise Unavailable("read_budget")
                chunk = os.read(fd, count)
                self.bytes_read += len(chunk)
                total += len(chunk)
                if not chunk:
                    break
                chunks.append(chunk)
            if identity(os.fstat(fd)) != identity(opened):
                raise Unavailable("inode_changed_during_read")
            if total > limit or (not virtual and total != before.st_size):
                raise Unavailable("read_length_changed_or_oversize")
            return b"".join(chunks)
        finally:
            if fd is not None:
                os.close(fd)
            if pinned is not None:
                os.close(pinned)

    def names(self, path, limit):
        result = []
        try:
            self.check()
            with os.scandir(str(self.path(path))) as entries:
                for entry in entries:
                    self.check()
                    self.entries_seen += 1
                    if self.entries_seen > LIMITS["directory_entries"] or len(result) >= limit:
                        raise Unavailable("directory_budget")
                    result.append(entry.name)
        except (OSError, Unavailable) as exc:
            self.error("list", exc)
        return sorted(result)

    def target_nodes(self):
        result = []
        nodes = ["/dev/spidev0.0"]
        for name in self.names("/sys/bus/usb/devices", LIMITS["usb_entries"]):
            if not re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", name):
                continue
            base = "/sys/bus/usb/devices/" + name
            try:
                vendor = self.read(base + "/idVendor").strip()
                product = self.read(base + "/idProduct").strip()
                if vendor not in (b"bbaa", b"3801") or product != b"ddcc":
                    continue
                bus, device = int(self.read(base + "/busnum")), int(self.read(base + "/devnum"))
                if not (0 < bus <= 999 and 0 < device <= 999):
                    raise ValueError("USB bus address")
                nodes.append("/dev/bus/usb/%03d/%03d" % (bus, device))
            except (OSError, ValueError, Unavailable) as exc:
                self.error("usb_target", exc)
        for path in nodes:
            try:
                self.check()
                info = os.stat(str(self.path(path)))
                if not stat.S_ISCHR(info.st_mode):
                    raise ValueError("target not character node")
                key = (os.major(info.st_rdev), os.minor(info.st_rdev))
                self.targets.setdefault(key, []).append(path)
                result.append({"node": path, "rdev": list(key), "method": "stat_only"})
            except (OSError, ValueError, Unavailable) as exc:
                result.append({"node": path, **self.error("target_stat", exc)})
        return result

    def process(self, pid):
        path = "/proc/%d" % pid
        before = parse_stat(self.read(path + "/stat").decode("utf-8", "strict"))
        if before["pid"] != pid:
            raise ValueError("PID mismatch")
        try:
            before["cgroup"] = cgroup_fields(self.read(path + "/cgroup", 16384).decode("utf-8", "strict"))
        except (OSError, UnicodeError, Unavailable) as exc:
            before["cgroup"] = self.error("cgroup", exc)
        after = parse_stat(self.read(path + "/stat").decode("utf-8", "strict"))
        if any(before[k] != after[k] for k in ("pid", "ppid", "starttime_ticks")):
            raise Unavailable("process_reused_or_reparented")
        before["non_atomic"] = True
        return before

    def processes(self):
        targets = self.target_nodes()
        started = self.monotonic()
        initial_gaps = self.gaps.copy()
        seeds, observations = [], {}
        scan = {"pids_seen": 0, "fds_seen": 0, "truncated": False}
        pids = [int(n) for n in self.names("/proc", LIMITS["pids"] + 512) if n.isdigit()]
        if len(pids) > LIMITS["pids"]:
            scan["truncated"] = True
        for pid in pids[:LIMITS["pids"]]:
            if self.monotonic() - started >= LIMITS["process_scan_seconds"]:
                scan["truncated"] = True
                break
            scan["pids_seen"] += 1
            try:
                before = parse_stat(self.read("/proc/%d/stat" % pid).decode("utf-8", "strict"))
                holders = []
                for fd in self.names("/proc/%d/fd" % pid, LIMITS["fds"]):
                    if not fd.isdigit():
                        continue
                    if scan["fds_seen"] >= LIMITS["fds"] or self.monotonic() - started >= LIMITS["process_scan_seconds"]:
                        scan["truncated"] = True
                        break
                    scan["fds_seen"] += 1
                    try:
                        info = os.stat(str(self.path("/proc/%d/fd/%s" % (pid, fd))))
                        if stat.S_ISCHR(info.st_mode):
                            key = (os.major(info.st_rdev), os.minor(info.st_rdev))
                            if key in self.targets:
                                holders.append({"fd": int(fd), "rdev": list(key), "target_nodes": self.targets[key]})
                    except OSError as exc:
                        self.error("fd_stat", exc)
                if holders or before["comm"] in COMM_CANDIDATES:
                    if len(seeds) >= LIMITS["seeds"]:
                        scan["truncated"] = True
                        break
                    current = self.process(pid)
                    if any(before[k] != current[k] for k in ("pid", "ppid", "starttime_ticks")):
                        raise Unavailable("process_changed_across_fd_scan")
                    current["matching_fds"] = holders
                    current["selection"] = "observed_device_holder" if holders else "known_comm_candidate_only"
                    observations[pid] = current
                    seeds.append(pid)
                if scan["truncated"]:
                    break
            except (OSError, UnicodeError, ValueError, Unavailable) as exc:
                self.error("process_scan", exc)
        chains = []
        for seed in seeds:
            chain, seen, current = [], set(), seed
            stop = "root_or_no_parent"
            for unused in range(LIMITS["ancestor_depth"]):
                if current <= 0:
                    break
                if current in seen:
                    stop = "cycle"
                    break
                if len(observations) >= LIMITS["ancestor_nodes"] and current not in observations:
                    stop = "ancestor_node_budget"
                    break
                seen.add(current)
                try:
                    observed = self.process(current)
                    prior = observations.get(current)
                    if prior and any(prior[k] != observed[k] for k in ("pid", "ppid", "starttime_ticks")):
                        raise Unavailable("ancestor_changed")
                    if prior:
                        observed.update({k: prior[k] for k in ("matching_fds", "selection") if k in prior})
                    observations[current] = observed
                    chain.append({"pid": current, "starttime_ticks": observed["starttime_ticks"]})
                    current = observed["ppid"]
                except (OSError, UnicodeError, ValueError, Unavailable) as exc:
                    stop = self.error("ancestor", exc)["error"]
                    break
            else:
                stop = "ancestor_depth_budget"
            chains.append({"seed_pid": seed, "chain": chain, "stop_reason": stop,
                           "same_time_parent_relationship_proven": False})
        scan["duration_seconds"] = self.monotonic() - started
        scan["gaps"] = dict(self.gaps - initial_gaps)
        scan["all_accessors_proven"] = False
        return {"target_nodes": targets, "scan": scan, "seeds": seeds,
                "observations": [observations[p] for p in sorted(observations)], "ancestors": chains,
                "target_scope": "spidev0.0 and cached bbaa/3801:ddcc USB candidates only; other devices/masters not excluded",
                "limitation": "Known comms are candidates; /proc visibility and non-atomic ancestry never prove complete supervision or exclusive access"}

    def startup_files(self):
        result = {}
        for path in STARTUP_FILES:
            try:
                result[path] = startup_fields(path, self.read(path, LIMITS["startup_file_bytes"]))
            except (OSError, ValueError, Unavailable) as exc:
                result[path] = self.error("startup_file", exc)
        return result

    def storage(self):
        result = {"statvfs": {}, "block_devices": {}}
        for path in MOUNTS:
            try:
                self.check()
                value = os.statvfs(str(self.path(path)))
                result["statvfs"][path] = {"capacity_bytes": value.f_blocks * value.f_frsize,
                    "free_bytes": value.f_bfree * value.f_frsize, "available_bytes": value.f_bavail * value.f_frsize,
                    "read_only_flag": bool(value.f_flag & getattr(os, "ST_RDONLY", 1)), "method": "statvfs_only"}
            except (OSError, Unavailable) as exc:
                result["statvfs"][path] = self.error("statvfs", exc)
        try:
            result["mounts"] = parse_mounts(self.read("/proc/self/mountinfo", 256 * 1024).decode("utf-8", "strict"))
        except (OSError, UnicodeError, Unavailable) as exc:
            result["mounts"] = self.error("mountinfo", exc)
        for name in self.names("/sys/class/block", LIMITS["block_entries"]):
            if not re.fullmatch(r"sd[a-f][0-9]{0,3}", name):
                continue
            item = {}
            for attr in ("size", "dev", "ro") + (("queue/logical_block_size",) if len(name) == 3 else ()):
                try:
                    value = self.read("/sys/class/block/" + name + "/" + attr).decode("ascii", "strict").strip()
                    if not re.fullmatch(r"[0-9]{1,20}(?::[0-9]{1,20})?", value):
                        raise ValueError("unexpected block metadata")
                    item[attr] = value
                    if attr == "size":
                        item["capacity_bytes"] = int(value) * 512
                except (OSError, UnicodeError, ValueError, Unavailable) as exc:
                    item[attr] = self.error("block_sysfs", exc)
            result["block_devices"][name] = item
        result["limitation"] = "Capacities and current mount metadata only; no filesystem scan, backup, block read or recoverability proof"
        return result

    def collect(self):
        self.deadline = self.monotonic() + LIMITS["total_seconds"]
        result = {"schema": SCHEMA, "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "scope": "Passive current supervision candidates and storage metadata; no isolation or recovery execution",
                  "profile_binding": "UNBOUND",
                  "boundary": {"device_data_opened": False, "ioctl": False, "writes": False,
                    "subprocess": False, "target_software_imported_or_started": False, "service_control": False,
                    "cmdline_environ_params_keys_read": False, "all_accessors_proven": False}}
        for name, function in (("processes", self.processes), ("startup_files", self.startup_files), ("storage", self.storage)):
            try:
                result[name] = function()
            except (OSError, ValueError, Unavailable) as exc:
                result[name] = self.error(name, exc)
        result["gaps"] = dict(self.gaps)
        result["limits"] = dict(LIMITS, actual_read_bytes=self.bytes_read, actual_directory_entries=self.entries_seen,
                                 hard_deadline_proven=False)
        return result


def collect(root):
    return Collector(root).collect()


if __name__ == "__main__":
    encoded = json.dumps(collect("/"), ensure_ascii=True, sort_keys=True)
    if len(encoded.encode()) > LIMITS["output_bytes"]:
        raise SystemExit("bounded_output_exceeded")
    print(encoded)
