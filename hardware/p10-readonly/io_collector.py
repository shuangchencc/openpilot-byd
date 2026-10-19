"""P10 passive USB/SPI metadata, Python 3.8 stdlib, for stdin via -I -S -B.

Constructing/importing this module does no I/O. collect() requires an explicit
filesystem root; the CLI explicitly selects /. It opens only selected regular
sysfs/proc/OS files, never character devices. /dev and /proc/*/fd are stat/readlink
only. It does not issue commands, ioctl, USB requests, flock, CAN, or service
controls. Device-tree values are declarations, NOT current SPI ioctl settings.
"""
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import time


class LimitReached(Exception):
    pass


def proc_starttime(raw):
    """/proc/PID/stat comm may itself contain spaces and parentheses."""
    end = raw.rfind(")")
    if end < 0:
        raise ValueError("stat comm delimiter")
    fields = raw[end + 1:].split()
    if len(fields) < 20:
        raise ValueError("stat field count")
    return int(fields[19])  # Field 22; fields starts at field 3.


def fdinfo_selected(raw):
    result = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        value = value.strip()
        if separator and key in ("flags", "mnt_id", "ino"):
            if re.fullmatch(r"[0-9]{1,32}", value):
                result[key] = value
    return result


def serial_digest(raw):
    # sysfs serial is textual and ends in a kernel-generated newline.
    value = raw.rstrip(b"\r\n")
    return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value),
            "normalization": "strip trailing CR/LF only", "plaintext_retained": False}


def lock_key(line):
    """Lock dev is st_dev (node filesystem), NOT character-device st_rdev."""
    for token in line.split():
        match = re.fullmatch(r"([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9]+)", token)
        if match:
            return (int(match[1], 16), int(match[2], 16), int(match[3]))
    return None


def rdev_key(info):
    return (os.major(info.st_rdev), os.minor(info.st_rdev)) if stat.S_ISCHR(info.st_mode) else None


def inode_key(info):
    return (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)


class Collector:
    def __init__(self, root, monotonic=time.monotonic, sleep=time.sleep):
        if root is None:
            raise ValueError("an explicit filesystem root is required")
        self.root = Path(root)
        self.monotonic = monotonic
        self.sleep = sleep
        self.deadline = None
        self.read_bytes_total = 0
        self.directory_entries_total = 0
        self.errors = collections.Counter()
        self.targets = {}
        self.lock_nodes = {}
        self.limits = {"overall_seconds": 24, "scan_seconds_each": 5,
                       "read_bytes_total": 4 * 1024 * 1024,
                       "directory_entries_total": 100000, "pids_each_scan": 4096,
                       "fds_each_scan": 65536, "dev_entries": 8192,
                       "dev_depth": 5, "usb_entries": 512, "spi_entries": 256,
                       "sample_interval_seconds": 0.2}

    def path(self, absolute):
        if not absolute.startswith("/") or ".." in Path(absolute).parts:
            raise ValueError("fixed absolute path required")
        return self.root / absolute.lstrip("/")

    def check(self):
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise LimitReached("overall time limit")

    def failure(self, scope, exc):
        kind = type(exc).__name__
        self.errors[scope + ":" + kind] += 1
        return {"error": kind, "value": "unknown"}

    def read(self, absolute, limit=4096):
        self.check()
        # Strict read-path whitelist. No /dev or /proc/PID/fd may be opened.
        allowed = (absolute == "/VERSION" or absolute in
                   ("/proc/version", "/proc/cmdline", "/proc/locks") or
                   re.fullmatch(r"/proc/[0-9]+/(stat|comm|fdinfo/[0-9]+)", absolute) or
                   absolute.startswith(("/sys/bus/usb/devices/", "/sys/bus/spi/devices/",
                                        "/sys/class/spidev/")))
        if not allowed:
            raise ValueError("read path outside whitelist")
        p = self.path(absolute)
        # Attribute leaf must be ordinary, and must not be a symlink. Parent
        # sysfs links are kernel paths. O_NOFOLLOW closes the leaf symlink race.
        before = os.lstat(str(p))
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("only ordinary kernel metadata files may be opened")
        if self.read_bytes_total + limit + 1 > self.limits["read_bytes_total"]:
            raise LimitReached("total read budget")
        fd = os.open(str(p), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            after = os.fstat(fd)
            if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("metadata changed while opening")
            value = os.read(fd, limit + 1)
            self.read_bytes_total += len(value)
            if len(value) > limit:
                raise LimitReached("per-file read budget")
            return value
        finally:
            os.close(fd)

    def text(self, absolute, limit=4096):
        try:
            return self.read(absolute, limit).decode("utf-8", "replace").rstrip("\x00\r\n")
        except (OSError, ValueError, LimitReached) as exc:
            return self.failure("read", exc)

    def names(self, absolute, limit):
        self.check()
        result = []
        try:
            with os.scandir(str(self.path(absolute))) as entries:
                for entry in entries:
                    self.check()
                    self.directory_entries_total += 1
                    if self.directory_entries_total > self.limits["directory_entries_total"]:
                        raise LimitReached("global directory budget")
                    if len(result) >= limit:
                        raise LimitReached("directory entry budget")
                    result.append(entry.name)
        except (OSError, ValueError, LimitReached) as exc:
            self.failure("list", exc)
        return sorted(result)

    def link(self, absolute):
        try:
            self.check()
            return os.readlink(str(self.path(absolute)))
        except (OSError, ValueError, LimitReached) as exc:
            return self.failure("readlink", exc)

    def fields(self, path, names):
        return {name: self.text(path + "/" + name) for name in names}

    def node(self, absolute, target=False):
        """Metadata only: stat never opens the device or requests its driver."""
        try:
            self.check()
            info = os.stat(str(self.path(absolute)))
            key = rdev_key(info)
            result = {"node": absolute, "mode_octal": oct(stat.S_IMODE(info.st_mode)),
                      "uid": info.st_uid, "gid": info.st_gid,
                      "is_character_device": key is not None,
                      "filesystem_dev": [os.major(info.st_dev), os.minor(info.st_dev)],
                      "inode": info.st_ino}
            if key is not None:
                result["rdev"] = list(key)
                if target:
                    self.targets.setdefault(key, []).append(absolute)
                if target or key in self.targets:
                    self.lock_nodes[inode_key(info)] = key
            return result
        except (OSError, ValueError, LimitReached) as exc:
            return {"node": absolute, **self.failure("stat_node", exc)}

    def usb(self):
        root = "/sys/bus/usb/devices"
        entries = self.names(root, self.limits["usb_entries"])
        devices = []
        for name in entries:
            if not re.fullmatch(r"(?:usb[0-9]+|[0-9]+-[0-9]+(?:\.[0-9]+)*)", name):
                continue
            base = root + "/" + name
            item = {"topology_name": name, "sysfs_link": self.link(base),
                    **self.fields(base, ["idVendor", "idProduct", "bcdDevice", "product", "busnum", "devnum",
                                          "bConfigurationValue", "bDeviceClass", "bNumInterfaces", "speed"])}
            candidate = (item["product"] == "panda" or item["idProduct"] == "ddcc")
            item["panda_descriptor_candidate"] = candidate
            item["candidate_limitation"] = "Descriptor candidate only, not authenticated identity or binding authority"
            if candidate:
                try:
                    item["serial"] = serial_digest(self.read(base + "/serial", 512))
                except (OSError, ValueError, LimitReached) as exc:
                    item["serial"] = self.failure("usb_serial", exc)
            else:
                item["serial"] = {"read": False, "reason": "not a Panda descriptor candidate"}
            item["interfaces"] = []
            for iface in entries:
                if not iface.startswith(name + ":"):
                    continue
                path = root + "/" + iface
                interface = {"name": iface, **self.fields(path, ["bInterfaceNumber", "bAlternateSetting",
                             "bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol", "bNumEndpoints"]),
                             "driver_link": self.link(path + "/driver"), "endpoints": []}
                for endpoint in self.names(path, 128):
                    if re.fullmatch(r"ep_[0-9a-fA-F]{2}", endpoint):
                        interface["endpoints"].append({"name": endpoint, **self.fields(path + "/" + endpoint,
                            ["bEndpointAddress", "bmAttributes", "wMaxPacketSize", "bInterval", "type", "direction"])})
                item["interfaces"].append(interface)
            if isinstance(item["busnum"], str) and isinstance(item["devnum"], str):
                if item["busnum"].isdigit() and item["devnum"].isdigit():
                    bus, dev = int(item["busnum"]), int(item["devnum"])
                    if 0 < bus <= 999 and 0 < dev <= 999:
                        item["character_node"] = self.node("/dev/bus/usb/%03d/%03d" % (bus, dev), target=True)
            devices.append(item)
        return {"source": "kernel-cached sysfs attributes; no USB transaction initiated",
                "devices": devices}

    def spi(self):
        devices = []
        for name in self.names("/sys/bus/spi/devices", self.limits["spi_entries"]):
            if not re.fullmatch(r"spi[0-9]+\.[0-9]+", name):
                continue
            base = "/sys/bus/spi/devices/" + name
            item = {"name": name, "sysfs_link": self.link(base),
                    "driver_link": self.link(base + "/driver"),
                    "modalias": self.text(base + "/modalias"),
                    "of_node_link": self.link(base + "/of_node"), "device_tree": {},
                    "current_ioctl_settings": {"mode": "unknown", "bits_per_word": "unknown", "speed_hz": "unknown"}}
            for prop in ("compatible", "reg", "spi-max-frequency", "spi-cpha", "spi-cpol",
                         "spi-cs-high", "spi-3wire", "spi-lsb-first"):
                try:
                    value = self.read(base + "/of_node/" + prop, 1024)
                    if prop == "compatible":
                        decoded = [v.decode("utf-8", "replace") for v in value.split(b"\0") if v]
                    elif prop in ("reg", "spi-max-frequency"):
                        if len(value) % 4:
                            raise ValueError("invalid big-endian u32 property")
                        decoded = list(struct.unpack(">" + "I" * (len(value) // 4), value))
                    else:
                        decoded = {"present": True, "bytes": len(value)}
                    item["device_tree"][prop] = decoded
                except FileNotFoundError:
                    item["device_tree"][prop] = {"present": False, "meaning": "not declared; current setting unknown"}
                except (OSError, ValueError, LimitReached) as exc:
                    item["device_tree"][prop] = self.failure("spi_dt", exc)
            devices.append(item)
        classes = []
        for name in self.names("/sys/class/spidev", self.limits["spi_entries"]):
            if not re.fullmatch(r"spidev[0-9]+\.[0-9]+", name):
                continue
            base = "/sys/class/spidev/" + name
            classes.append({"name": name, "sysfs_link": self.link(base), "device_link": self.link(base + "/device"),
                            "declared_dev": self.text(base + "/dev"), "node": self.node("/dev/" + name, target=True)})
        return {"devices": devices, "class_spidev": classes,
                "device_tree_limitation": "Device-tree values are declarations, not current SPI ioctl mode/bits/speed. No ioctl performed."}

    def aliases(self):
        result = []
        stack = [("/dev", 0)]
        visited = 0
        truncated = False
        while stack:
            directory, depth = stack.pop()
            for name in self.names(directory, self.limits["dev_entries"]):
                visited += 1
                if visited > self.limits["dev_entries"]:
                    truncated = True
                    stack = []
                    break
                path = directory + "/" + name
                try:
                    self.check()
                    info = os.lstat(str(self.path(path)))
                    if stat.S_ISDIR(info.st_mode):
                        if depth < self.limits["dev_depth"]:
                            stack.append((path, depth + 1))
                        else:
                            truncated = True
                        continue
                    resolved = os.stat(str(self.path(path))) if stat.S_ISLNK(info.st_mode) else info
                    if rdev_key(resolved) in self.targets:
                        record = self.node(path)
                        if stat.S_ISLNK(info.st_mode):
                            record["link"] = self.link(path)
                        result.append(record)
                except (OSError, ValueError, LimitReached) as exc:
                    self.failure("alias", exc)
        return {"matching_nodes": result, "entries_seen": visited, "truncated": truncated,
                "scope": "/dev only, bounded depth/entries, symlink directories not traversed; aliases elsewhere unknown"}

    def lock_sample(self):
        try:
            raw = self.read("/proc/locks", 512 * 1024).decode("ascii", "replace")
            matches = []
            for line in raw.splitlines():
                key = lock_key(line)
                if key in self.lock_nodes:
                    matches.append({"line": line, "target_rdev": list(self.lock_nodes[key])})
            return {"matching_locks": matches,
                    "match_semantics": "lock st_dev+inode matched to sampled node/FD; target_rdev identifies character device",
                    "limitation": "No matching lock in a sample does not prove all masters cooperate or absence of access."}
        except (OSError, ValueError, LimitReached) as exc:
            return self.failure("proc_locks", exc)

    def process_sample(self):
        started = self.monotonic()
        scan_deadline = started + self.limits["scan_seconds_each"]
        before_errors = self.errors.copy()
        out = {"sample_monotonic": started, "holders": [], "pids_seen": 0, "fds_seen": 0,
               "pids_completed": 0, "truncated": False,
               "completed_definition": "PID starttime stable and no per-PID read/list/stat errors; still non-atomic"}
        pids = [name for name in self.names("/proc", self.limits["pids_each_scan"] + 512) if name.isdigit()]
        if len(pids) > self.limits["pids_each_scan"]:
            out["truncated"] = True
            pids = pids[:self.limits["pids_each_scan"]]
        for pid in pids:
            if self.monotonic() >= scan_deadline:
                out["truncated"] = True
                break
            out["pids_seen"] += 1
            base = "/proc/" + pid
            pid_errors = self.errors.copy()
            try:
                start = proc_starttime(self.read(base + "/stat", 4096).decode("utf-8", "replace"))
                matched = []
                completed = True
                for fd in self.names(base + "/fd", self.limits["fds_each_scan"]):
                    if not fd.isdigit():
                        continue
                    if self.monotonic() >= scan_deadline or out["fds_seen"] >= self.limits["fds_each_scan"]:
                        out["truncated"] = True
                        completed = False
                        break
                    out["fds_seen"] += 1
                    path = base + "/fd/" + fd
                    try:
                        info = os.stat(str(self.path(path)))
                        key = rdev_key(info)
                        if key in self.targets:
                            self.lock_nodes[inode_key(info)] = key
                            fdinfo = self.text(base + "/fdinfo/" + fd, 4096)
                            matched.append({"fd": int(fd), "target_rdev": list(key), "target_nodes": self.targets[key],
                                            "link": self.link(path), "filesystem_dev": list(inode_key(info)[:2]),
                                            "inode": info.st_ino,
                                            "fdinfo": fdinfo_selected(fdinfo) if isinstance(fdinfo, str) else fdinfo})
                    except (OSError, ValueError, LimitReached) as exc:
                        self.failure("proc_fd", exc)
                end = proc_starttime(self.read(base + "/stat", 4096).decode("utf-8", "replace"))
                if start != end:
                    self.errors["process:PidReused"] += 1
                    continue
                if matched:
                    out["holders"].append({"pid": int(pid), "comm": self.text(base + "/comm", 512),
                                           "exe_link": self.link(base + "/exe"), "starttime_ticks": start,
                                           "fds": matched, "not_atomic": True})
                out["pids_completed"] += int(completed and self.errors == pid_errors)
                if not completed:
                    break
            except (OSError, ValueError, LimitReached) as exc:
                self.failure("process", exc)
        out["locks"] = self.lock_sample()
        out["duration_seconds"] = self.monotonic() - started
        out["gaps"] = dict(self.errors - before_errors)
        out["visible_snapshot_complete"] = not out["truncated"] and not out["gaps"]
        return out

    def collect(self):
        self.deadline = self.monotonic() + self.limits["overall_seconds"]
        out = {"schema": "songplus-p10-passive-io/v1",
               "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "profile_binding": "UNBOUND",
               "method": "cached sysfs/proc ordinary-file reads plus stat/readlink; no character-device opens or control transactions",
               "identity_unknown": ["physical_board_revision", "MCU_identity", "firmware_UID", "firmware_signature",
                                    "protocol_version", "F4_capabilities", "physical_slot_role_mapping"],
               "limits": dict(self.limits),
               "limit_semantics": "Cooperative per-operation checks and bounded enumeration/read sizes; no kernel hard deadline claim",
               "namespace_links": {"pid": self.link("/proc/self/ns/pid"), "mount": self.link("/proc/self/ns/mnt")}}
        try:
            out["system"] = {"VERSION": self.text("/VERSION"), "kernel": self.text("/proc/version", 16384)}
            cmdline = self.text("/proc/cmdline", 16384)
            allowed = ("root=", "androidboot.slot_suffix=", "androidboot.verifiedbootstate=", "androidboot.bootreason=")
            out["system"]["boot_parameters_selected"] = [s for s in cmdline.split() if s.startswith(allowed)] if isinstance(cmdline, str) else cmdline
            out["usb"] = self.usb()
            out["spi"] = self.spi()
            out["aliases"] = self.aliases()
            out["process_samples"] = [self.process_sample()]
            self.check()
            self.sleep(self.limits["sample_interval_seconds"])
            out["process_samples"].append(self.process_sample())
        except (OSError, ValueError, LimitReached) as exc:
            out["collection_incomplete"] = self.failure("collect", exc)
        out["accounting"] = {"bytes_read": self.read_bytes_total, "directory_entries_seen": self.directory_entries_total,
                             "gaps": dict(self.errors)}
        out["limitations"] = [
            "Snapshots are non-atomic; visible processes/FDs may exit, reopen, or change between reads.",
            "Permissions, PID namespace, scan limits and aliases outside /dev prevent an all-accessors proof.",
            "USB cached descriptors are host observations, not authenticated physical board or firmware identities.",
            "USB descriptors and SPI device-tree declarations do not prove exclusive ownership, live firmware semantics or kernel deadlines.",
            "No matching FD/lock is not evidence that a device is idle or all masters obey flock.",
            "Only serial SHA256 and byte length are emitted; no cmdline except whitelisted boot parameters, process environment, keys or provisioning read."]
        return out


if __name__ == "__main__":
    print(json.dumps(Collector(Path("/")).collect(), ensure_ascii=False, indent=2))
