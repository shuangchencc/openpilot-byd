"""Bounded passive follow-up, via system Python stdin. No target-side writes.

Reads cached sysfs/device-tree, selected system mount configuration, and GPT.
Never opens USB/TTY/video devices, imports target software, uses ioctl, or runs
external commands. Device serials, network addresses and provisioning excluded.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import zlib


def read_bytes(path, limit=65536):
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("not a regular/sysfs file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("read limit exceeded")
        return data
    finally:
        os.close(fd)


def text(path, limit=65536):
    try:
        return read_bytes(path, limit).decode("utf-8", "replace").rstrip("\x00\n")
    except (OSError, ValueError) as e:
        return {"error": type(e).__name__}


def fields(path, names):
    return {n: text(path / n, 4096) for n in names if (path / n).exists()}


def link(path):
    try:
        return str(path.resolve(strict=True))
    except OSError as e:
        return {"error": type(e).__name__}


def file_record(path, selected_line=None):
    try:
        data = read_bytes(path, 256 * 1024)
        result = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if selected_line:
            result["selected_lines"] = [{"line": i, "text": s} for i, s in
                enumerate(data.decode("utf-8", "replace").splitlines(), 1) if selected_line(s)]
        return result
    except (OSError, ValueError) as e:
        return {"error": type(e).__name__}


def gpt(fd, lba, sector, disk_bytes):
    if lba < 0 or (lba + 1) * sector > disk_bytes:
        raise ValueError("header out of range")
    data = os.pread(fd, sector, lba * sector)
    if len(data) != sector or data[:8] != b"EFI PART":
        raise ValueError("invalid GPT signature or read")
    hsize, stored_crc = struct.unpack_from("<II", data, 12)
    if not 92 <= hsize <= sector:
        raise ValueError("bad header length")
    entries_lba, count, size, entries_crc = struct.unpack_from("<QIII", data, 72)
    total = count * size
    if not 128 <= size <= 4096 or not 1 <= count <= 16384 or total > 2 * 1024 * 1024:
        raise ValueError("bad table dimensions")
    if entries_lba < 2 or entries_lba * sector + total > disk_bytes:
        raise ValueError("table outside disk")
    table = os.pread(fd, total, entries_lba * sector)
    if len(table) != total:
        raise ValueError("short table")
    head = bytearray(data[:hsize]); head[16:20] = b"\0" * 4
    parts = []
    for i in range(count):
        entry = table[i * size:(i + 1) * size]
        if entry[:16] == b"\0" * 16:
            continue
        name = entry[56:128].decode("utf-16le", "replace").rstrip("\0")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,36}", name):
            raise ValueError("unexpected partition label")
        start, end, attrs = struct.unpack_from("<QQQ", entry, 32)
        parts.append({"partition": i + 1, "name": name, "first_lba": start,
                      "last_lba": end, "attributes_hex": "0x%016x" % attrs})
    return {"header_lba": lba, "header_crc_valid": zlib.crc32(head) & 0xffffffff == stored_crc,
            "entries_crc_valid": zlib.crc32(table) & 0xffffffff == entries_crc,
            "entries_sha256": hashlib.sha256(table).hexdigest(), "partitions": parts,
            "read_bytes": sector + total}


def collect():
    out = {"schema": "songplus-c3xl-passive-followup/v1",
           "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "method": "O_RDONLY file/sysfs reads and bounded GPT pread; no commands or device controls"}
    out["soc"] = fields(Path("/sys/devices/soc0"), ["soc_id", "family", "machine", "revision", "platform_subtype", "platform_version", "platform_subtype_id"])
    dt = Path("/sys/firmware/devicetree/base")
    out["device_tree"] = fields(dt, ["model", "compatible"])
    for n in ["qcom,msm-id", "qcom,board-id", "qcom,pmic-id"]:
        if (dt / n).exists():
            b = read_bytes(dt / n, 1024)
            out["device_tree"][n + "_u32"] = list(struct.unpack(">" + "I" * (len(b) // 4), b))
    out["camera_dt"] = []
    for root, dirs, files in os.walk(str(dt)):
        p = Path(root)
        if not re.search(r"(cam-sensor|cam_sensor|camera|cci@|csiphy@|csid@)", p.name, re.I):
            continue
        item = {"path": str(p.relative_to(dt)), **fields(p, ["compatible", "status", "qcom,sensor-name"])}
        for name in ["reg", "cell-index", "qcom,cci-master", "qcom,slot-index", "qcom,sensor-position", "qcom,mount-angle"]:
            if (p / name).exists():
                b = read_bytes(p / name, 4096)
                if len(b) % 4 == 0:
                    item[name + "_u32"] = list(struct.unpack(">" + "I" * (len(b) // 4), b))
        out["camera_dt"].append(item)
    out["video_nodes"] = [{"node": p.name, **fields(p, ["name", "index"]), "device": link(p / "device")}
                          for p in sorted(Path("/sys/class/video4linux").glob("*"))]
    out["usb_devices"] = []
    for p in sorted(Path("/sys/bus/usb/devices").glob("*")):
        if not (p / "idVendor").exists():
            continue
        item = {"name": p.name, "path": link(p), **fields(p, ["idVendor", "idProduct", "bcdDevice", "product", "manufacturer", "speed", "bDeviceClass", "bNumInterfaces"])}
        item["interfaces"] = []
        for q in sorted(p.glob(p.name + ":*")):
            item["interfaces"].append({"name": q.name, **fields(q, ["bInterfaceNumber", "bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol", "interface"]), "driver": link(q / "driver") if (q / "driver").exists() else None})
        out["usb_devices"].append(item)
    out["communication_nodes"] = []
    for base, patterns in [("/sys/class/tty", ["ttyUSB*", "ttyACM*"]), ("/sys/class/usbmisc", ["cdc-wdm*"]), ("/sys/class/net", ["wwan*", "rmnet*", "usb*"])]:
        for pattern in patterns:
            for p in sorted(Path(base).glob(pattern)):
                out["communication_nodes"].append({"class": base, "name": p.name, "device": link(p / "device")})
    out["udc"] = {p.name: fields(p, ["state", "current_speed", "maximum_speed"]) for p in Path("/sys/class/udc").glob("*")}
    out["usb_roles"] = {p.name: fields(p, ["mode", "data_role", "power_role", "supported_modes"]) for p in Path("/sys/class/dual_role_usb").glob("*")}
    out["gadget_bindings"] = {str(p): text(p, 256) for p in Path("/sys/kernel/config/usb_gadget").glob("*/UDC")}
    allowed_mounts = {"/", "/dsp", "/firmware", "/persist", "/data", "/systemrw"}
    out["mounts"] = [s for s in text("/proc/mounts").splitlines() if len(s.split()) > 1 and s.split()[1] in allowed_mounts]
    out["boot_parameters"] = [s for s in text("/proc/cmdline").split() if s.startswith(("root=", "androidboot.slot_suffix=", "androidboot.verifiedbootstate="))]
    out["system_files"] = {
        "/etc/fstab": file_record("/etc/fstab", lambda s: bool(s.strip()) and not s.lstrip().startswith("#") and len(s.split()) > 1 and s.split()[1] in allowed_mounts),
        "/etc/initscripts/ab_mount.sh": file_record("/etc/initscripts/ab_mount.sh"),
        "/etc/initscripts/usb": file_record("/etc/initscripts/usb"),
        "/usr/lib/systemd/system/usb.service": file_record("/usr/lib/systemd/system/usb.service"),
        "/etc/systemd/system/systemrw.mount": file_record("/etc/systemd/system/systemrw.mount"),
        "/usr/lib/aarch64-linux-gnu/libabctl.so.0.0.0": file_record("/usr/lib/aarch64-linux-gnu/libabctl.so.0.0.0"),
        "/usr/sbin/abctl": file_record("/usr/sbin/abctl"),
    }
    out["partition_links"] = {}
    for p in [Path("/dev/disk/by-label/dsp")] + [Path("/dev/disk/by-partlabel") / n for n in ["dsp_a", "dsp_b", "modem_a", "modem_b", "system_a", "system_b", "boot_a", "boot_b", "misc", "persist", "userdata"]]:
        out["partition_links"][str(p)] = link(p)
    out["gpt"] = {}
    for name in ("sda", "sdb", "sdc", "sdd", "sde", "sdf"):
        try:
            sysdir = Path("/sys/class/block") / name
            disk_bytes = int(text(sysdir / "size")) * 512
            sector = int(text(sysdir / "queue/logical_block_size"))
            if sector not in (512, 4096) or disk_bytes % sector:
                raise ValueError("invalid block size")
            fd = os.open("/dev/" + name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISBLK(os.fstat(fd).st_mode):
                    raise ValueError("not a block device")
                out["gpt"][name] = {"bytes": disk_bytes, "sector_bytes": sector,
                    "primary": gpt(fd, 1, sector, disk_bytes),
                    "secondary": gpt(fd, disk_bytes // sector - 1, sector, disk_bytes)}
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError) as e:
            out["gpt"][name] = {"error": type(e).__name__}
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
