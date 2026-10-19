"""Fixed, bounded read-only sde GPT fingerprints. No raw GUIDs or payload output.

Importing this module performs no I/O. Only collect() opens the fixed sde device;
the parent-reviewed diagnostic entry must invoke it explicitly. No repair API.
"""
import datetime
import hashlib
import json
import os
import stat
import struct
import zlib

MAX_TABLE_BYTES = 65536
SCOPE_SECTOR_BYTES = 4096
SCOPE_DISK_BYTES = 4294967296
HEADER_FIELDS = (
    ("revision", 8, "I"), ("header_size", 12, "I"),
    ("header_crc32_stored", 16, "I"), ("reserved", 20, "I"),
    ("current_lba", 24, "Q"), ("alternate_lba", 32, "Q"),
    ("first_usable_lba", 40, "Q"), ("last_usable_lba", 48, "Q"),
    ("entries_lba", 72, "Q"), ("entries_count", 80, "I"),
    ("entry_size", 84, "I"), ("entries_crc32_stored", 88, "I"),
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def parse_header(header, sector_bytes, disk_bytes, observed_lba):
    if sector_bytes not in (512, 4096) or disk_bytes <= 0 or disk_bytes % sector_bytes:
        raise ValueError("geometry")
    if not 0 <= observed_lba < disk_bytes // sector_bytes:
        raise ValueError("header LBA bounds")
    if len(header) != sector_bytes or header[:8] != b"EFI PART":
        raise ValueError("header signature or short read")
    result = {name: struct.unpack_from("<" + fmt, header, offset)[0]
              for name, offset, fmt in HEADER_FIELDS}
    hsize = result["header_size"]
    if not 92 <= hsize <= sector_bytes:
        raise ValueError("header length")
    count, size = result["entries_count"], result["entry_size"]
    total = count * size
    if not 1 <= count <= 16384 or not 128 <= size <= 4096 or size % 128 or total > MAX_TABLE_BYTES:
        raise ValueError("table shape or budget")
    if result["entries_lba"] < 2 or result["entries_lba"] * sector_bytes + total > disk_bytes:
        raise ValueError("table bounds")
    crc_input = bytearray(header[:hsize])
    crc_input[16:20] = bytes(4)
    calculated = zlib.crc32(crc_input) & 0xffffffff
    result.update({
        "observed_lba": observed_lba, "sector_bytes": sector_bytes,
        "header_sector_sha256": sha(header), "signature": "EFI PART",
        "header_crc32_calculated": calculated,
        "header_crc_valid": calculated == result["header_crc32_stored"],
        "disk_guid_sha256": sha(header[56:72]),
        "reserved_field_sha256": sha(header[20:24]),
        "header_extension_sha256": sha(header[92:hsize]),
        "sector_padding_sha256": sha(header[hsize:]),
        "entries_bytes": total,
    })
    return result


def describe_table(header_record, table):
    if len(table) != header_record["entries_bytes"]:
        raise ValueError("short table")
    calculated = zlib.crc32(table) & 0xffffffff
    return {"entries_sha256": sha(table), "entries_bytes": len(table),
            "entries_crc32_calculated": calculated,
            "entries_crc_valid": calculated == header_record["entries_crc32_stored"]}


def validate_collection_scope(record, label, disk_bytes):
    """Reject before table pread unless this is the approved sde metadata area.

    Generic parse_header() is useful for synthetic tests. Real collect() has no
    generic-disk mode: its observed machine geometry and GPT layout are pinned.
    """
    if label not in ("primary", "secondary") or disk_bytes != SCOPE_DISK_BYTES:
        raise ValueError("collection disk/label scope")
    primary = label == "primary"
    expected = {
        "sector_bytes": SCOPE_SECTOR_BYTES, "revision": 65536,
        "header_size": 92, "reserved": 0,
        "observed_lba": 1 if primary else 1048575,
        "current_lba": 1 if primary else 1048575,
        "alternate_lba": 1048575 if primary else 1,
        "first_usable_lba": 6, "last_usable_lba": 1048570,
        "entries_lba": 2 if primary else 1048571,
        "entries_count": 64, "entry_size": 128, "entries_bytes": 8192,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("outside approved fixed sde metadata scope")
    if record.get("header_crc_valid") is not True:
        raise ValueError("invalid header CRC; table read not authorized")
    # These assertions are implied by the fixed layout, and kept explicit to
    # make its table/header/usable-range separation reviewable before I/O.
    end = record["entries_lba"] + record["entries_bytes"] // SCOPE_SECTOR_BYTES
    if primary:
        valid_range = record["current_lba"] < record["entries_lba"] < end <= record["first_usable_lba"]
    else:
        valid_range = record["last_usable_lba"] < record["entries_lba"] < end <= record["current_lba"]
    if not valid_range:
        raise ValueError("metadata overlap")


def _geometry_integer(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not regular sysfs geometry")
        raw = os.read(fd, 65)
        if len(raw) > 64 or not raw.strip().isdigit():
            raise ValueError("geometry value")
        return int(raw)
    finally:
        os.close(fd)


def collect():
    result = {"schema": "songplus-recovery-gpt-header-passive/v1",
              "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "method": "fixed sde O_RDONLY bounded pread; metadata hashes only",
              "raw_guids_emitted": False, "raw_payloads_emitted": False,
              "target_writes": False, "ioctl": False, "sde": {}}
    sector = _geometry_integer("/sys/class/block/sde/queue/logical_block_size")
    disk_bytes = _geometry_integer("/sys/class/block/sde/size") * 512
    if sector != SCOPE_SECTOR_BYTES or disk_bytes != SCOPE_DISK_BYTES:
        raise ValueError("outside approved fixed sde geometry")
    result.update({"sector_bytes": sector, "disk_bytes": disk_bytes,
                   "non_atomic_snapshot": True})
    fd = os.open("/dev/sde", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISBLK(os.fstat(fd).st_mode):
            raise ValueError("not block device")
        for label, lba in (("primary", 1), ("secondary", disk_bytes // sector - 1)):
            header = os.pread(fd, sector, lba * sector)
            record = parse_header(header, sector, disk_bytes, lba)
            validate_collection_scope(record, label, disk_bytes)
            table = os.pread(fd, record["entries_bytes"], record["entries_lba"] * sector)
            record.update(describe_table(record, table))
            # Detect a header that changed during its own table read. This does
            # not make the two headers/tables an atomic disk snapshot.
            after = os.pread(fd, sector, lba * sector)
            record["header_stable_across_table_read"] = after == header
            record["header_after_sha256"] = sha(after)
            result["sde"][label] = record
    finally:
        os.close(fd)
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, sort_keys=True))
