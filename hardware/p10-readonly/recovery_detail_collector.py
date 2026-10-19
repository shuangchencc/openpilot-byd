"""Bounded sde GPT fingerprints and selected mount configuration, never repair.

GUIDs are hashed, not emitted. No abctl, mount command, ioctl or target imports.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_file(name, limit=65536):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular metadata/config file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("file limit")
        return data
    finally:
        os.close(fd)


def entry_fingerprints(table, count, size):
    if len(table) != count * size or not 128 <= size <= 4096 or not 1 <= count <= 16384:
        raise ValueError("table shape")
    used, unused = [], bytearray()
    for index in range(count):
        entry = table[index * size:(index + 1) * size]
        if entry[:16] == bytes(16):
            unused.extend(struct.pack("<I", index)); unused.extend(entry)
            continue
        name = entry[56:128].decode("utf-16le", "strict").rstrip("\0")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", name):
            raise ValueError("unexpected label")
        used.append({"partition": index + 1, "name": name,
                     "type_guid_sha256": sha(entry[:16]), "unique_guid_sha256": sha(entry[16:32]),
                     "lba_sha256": sha(entry[32:48]), "attributes_hex": "0x%016x" % struct.unpack_from("<Q", entry, 48)[0],
                     "name_bytes_sha256": sha(entry[56:128]), "extension_sha256": sha(entry[128:]),
                     "entry_sha256": sha(entry)})
    return {"entries_sha256": sha(table), "count": count, "size": size, "used": used,
            "unused_indexed_entries_sha256": sha(unused), "raw_guids_emitted": False}


def gpt_details():
    sector = int(read_file("/sys/class/block/sde/queue/logical_block_size"))
    disk_bytes = int(read_file("/sys/class/block/sde/size")) * 512
    if sector not in (512, 4096) or disk_bytes % sector:
        raise ValueError("sector geometry")
    fd = os.open("/dev/sde", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISBLK(os.fstat(fd).st_mode):
            raise ValueError("not block device")
        result = {}
        for label, lba in (("primary", 1), ("secondary", disk_bytes // sector - 1)):
            header = os.pread(fd, sector, lba * sector)
            if len(header) != sector or header[:8] != b"EFI PART":
                raise ValueError("header")
            entries_lba, count, size = struct.unpack_from("<QII", header, 72)
            total = count * size
            if not 128 <= size <= 4096 or not 1 <= count <= 16384 or total > 2 * 1024 * 1024:
                raise ValueError("table dimensions")
            if entries_lba < 2 or entries_lba * sector + total > disk_bytes:
                raise ValueError("table bounds")
            result[label] = entry_fingerprints(os.pread(fd, total, entries_lba * sector), count, size)
        return result
    finally:
        os.close(fd)


def collect():
    result = {"schema": "songplus-p10-passive-recovery-detail/v1",
              "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "method": "O_RDONLY bounded sde GPT metadata and selected existing mount config; no commands/ioctl/writes",
              "mount_config": {}, "mountinfo": {}}
    paths = ["/run/systemd/generator/dsp.mount", "/run/systemd/generator.early/dsp.mount",
             "/run/systemd/generator.late/dsp.mount", "/etc/systemd/system/dsp.mount",
             "/usr/lib/systemd/system/dsp.mount", "/lib/systemd/system/dsp.mount",
             "/etc/initscripts/ab_mount.sh", "/etc/fstab"]
    for name in paths:
        try:
            data = read_file(name)
            lines = data.decode("utf-8", "replace").splitlines()
            selected = [{"line": i, "text": line} for i, line in enumerate(lines, 1)
                        if ("dsp" in line or "sde26" in line or "sde9" in line or
                            (name.endswith("dsp.mount") and line.startswith(("What=", "Where=", "Type=", "Options=", "SourcePath="))))]
            result["mount_config"][name] = {"bytes": len(data), "sha256": sha(data), "selected_lines": selected}
        except (OSError, ValueError) as error:
            result["mount_config"][name] = {"error": type(error).__name__}
    for name in ("/proc/self/mountinfo", "/proc/1/mountinfo"):
        try:
            data = read_file(name, 256 * 1024)
            result["mountinfo"][name] = [line for line in data.decode().splitlines()
                                               if len(line.split()) > 5 and line.split()[4] in ("/dsp", "/firmware", "/")]
        except (OSError, ValueError) as error:
            result["mountinfo"][name] = {"error": type(error).__name__}
    try:
        result["sde"] = gpt_details()
    except (OSError, ValueError, UnicodeError) as error:
        result["sde"] = {"error": type(error).__name__}
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
