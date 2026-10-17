"""Read bounded disk metadata and installed-file hashes. Never repair or mount."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import zlib


def read_text(path, limit=65536):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace").strip().strip("\x00")
    except OSError as e:
        return {"error": type(e).__name__}


def gpt_header(f, lba, sector_size, disk_bytes):
    if lba < 0 or (lba + 1) * sector_size > disk_bytes:
        raise ValueError("header outside disk")
    f.seek(lba * sector_size)
    data = f.read(sector_size)
    if data[:8] != b"EFI PART":
        return {"lba": lba, "signature_valid": False}
    header_size, stored_crc = struct.unpack_from("<II", data, 12)
    if not 92 <= header_size <= sector_size:
        raise ValueError("invalid GPT header size")
    current, alternate, first, last = struct.unpack_from("<QQQQ", data, 24)
    entries_lba, count, entry_size, stored_entries_crc = struct.unpack_from("<QIII", data, 72)
    table_size = count * entry_size
    if not 128 <= entry_size <= 4096 or table_size > 2 * 1024 * 1024 or entries_lba * sector_size + table_size > disk_bytes:
        raise ValueError("invalid or oversized GPT table")
    header = bytearray(data[:header_size]); header[16:20] = b"\0" * 4
    f.seek(entries_lba * sector_size)
    entries = f.read(table_size)
    if len(entries) != table_size:
        raise ValueError("short GPT table")
    labels = []
    for i in range(count):
        entry = entries[i * entry_size:(i + 1) * entry_size]
        if entry[:16] == b"\0" * 16:
            continue
        start, end = struct.unpack_from("<QQ", entry, 32)
        labels.append({"partition": i + 1, "name": entry[56:128].decode("utf-16le", "replace").rstrip("\0"),
                       "first_lba": start, "last_lba": end})
    return {"lba": lba, "signature_valid": True, "current_lba": current, "alternate_lba": alternate,
            "first_usable_lba": first, "last_usable_lba": last,
            "header_crc_valid": zlib.crc32(header) & 0xffffffff == stored_crc,
            "entries_lba": entries_lba, "entry_count": count, "entry_size": entry_size,
            "entries_crc_valid": zlib.crc32(entries) & 0xffffffff == stored_entries_crc,
            "stored_entries_crc32": stored_entries_crc,
            "actual_entries_crc32": zlib.crc32(entries) & 0xffffffff,
            "entries_sha256": hashlib.sha256(entries).hexdigest(), "partitions": labels}


out = {"collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "method": "O_RDONLY bounded GPT metadata and installed software hashes; no mount, repair, ioctl, abctl or updater",
       "disks": {}, "files": {}, "space": {}}
for name in ("sda", "sdb", "sdc", "sdd", "sde", "sdf"):
    node, sysdir = Path("/dev") / name, Path("/sys/class/block") / name
    try:
        if not stat.S_ISBLK(node.stat().st_mode):
            raise ValueError("not a block device")
        size = int((sysdir / "size").read_text()) * 512
        sector = int((sysdir / "queue/logical_block_size").read_text())
        if sector not in (512, 4096) or size % sector:
            raise ValueError("unexpected block size")
        with node.open("rb", buffering=0) as f:
            primary = gpt_header(f, 1, sector, size)
            secondary = gpt_header(f, size // sector - 1, sector, size)
        out["disks"][name] = {"bytes": size, "sector_bytes": sector, "primary": primary, "secondary": secondary,
                              "tables_identical": primary.get("entries_sha256") is not None and primary.get("entries_sha256") == secondary.get("entries_sha256")}
    except (OSError, ValueError) as e:
        out["disks"][name] = {"error": type(e).__name__, "reason": str(e)}
for path in ("/", "/data", "/persist"):
    try:
        s = os.statvfs(path)
        out["space"][path] = {"total_bytes": s.f_frsize * s.f_blocks, "available_bytes": s.f_frsize * s.f_bavail}
    except OSError as e:
        out["space"][path] = {"error": type(e).__name__}
out["mounts"] = [line for line in Path("/proc/mounts").read_text().splitlines()
                 if line.split()[1] in ("/", "/data", "/persist", "/firmware", "/dsp", "/systemrw")]
for relative in ("build.json", "common/version.h", "launch_env.sh", "panda/board/obj/version",
                 "panda/board/obj/panda.bin.signed", "panda/board/obj/panda_h7.bin.signed",
                 "panda/board/obj/bootstub.panda.bin", "panda/board/obj/bootstub.panda_h7.bin",
                 "panda/python/__init__.py", "system/hardware/tici/agnos.json", "system/camerad/camerad"):
    p = Path("/data/openpilot") / relative
    try:
        size = p.stat().st_size
        if not p.is_file() or size > 64 * 1024 * 1024:
            raise ValueError("not a bounded regular file")
        h = hashlib.sha256()
        with p.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        entry = {"bytes": size, "sha256": h.hexdigest()}
        if relative in ("common/version.h", "panda/board/obj/version"):
            entry["declared_text"] = read_text(p, 2048)
        out["files"][relative] = entry
    except (OSError, ValueError) as e:
        out["files"][relative] = {"error": type(e).__name__}
out["installed_tree_has_git_metadata"] = Path("/data/openpilot/.git").exists()
print(json.dumps(out, ensure_ascii=False, indent=2))
