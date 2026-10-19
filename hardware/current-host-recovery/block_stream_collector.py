"""Explicit bounded host-block acquisition; import is inert, never a restore tool.

Protocol: stdout is ONE gzip stream (level 1); stderr has bounded JSONL begin/end
records (plus bounded numeric progress records) only. A consumer MUST require successful exit, successful end, and local
full gzip decode length/SHA256 verification before accepting any archive. Failure
may leave partial compressed bytes. Source is a running host: matching pre/post
metadata does not establish an atomic or application-consistent snapshot.

CLI selects one embedded object ID. No caller-selected path, offset, size or
manifest exists. O_RDONLY/O_NOFOLLOW/O_NONBLOCK; no ioctl, subprocess, device
writes, target Python imports, USB/SPI/Panda or CAN access. sysfs reads and fixed
GPT metadata reads validate node identity and scope before content is streamed.
"""
import datetime
import hashlib
import json
import os
import stat
import struct
import sys
import time
import zlib

SCHEMA = "songplus-current-host-block-stream/v1"
SECTOR = 4096
CHUNK = 1024 * 1024
MAX_SYSFS = 128
MAX_RECORD = 16384
MAX_STDERR = 65536
PROGRESS_SECONDS = 30
# Sole retained CRC anomaly: 2026-10-19 evidence, freshly re-observed 2026-10-20.
# No other mismatch is an acceptable source layout for this fixed capture.
KNOWN_SDE_TABLE_SHA256 = "799842357d5111d451fe234f30dbbf4636bf6fb6b8dc263aa6a499adef20fcc5"
KNOWN_SDE_SECONDARY_STORED_CRC32 = 0xbfd6628d
KNOWN_SDE_SECONDARY_CALCULATED_CRC32 = 0x6f40dffc
# Values are frozen from recovery-readiness/recovery-requirements.json. Runtime
# geometry and GPT names/ranges must still match; labels/symlinks are not followed.
LUN_BYTES = {"sda": 57948504064, "sdb": 4194304, "sdc": 4194304,
             "sdd": 134217728, "sde": 4294967296, "sdf": 1610612736}
PARTITIONS = (
    ('system_a', 'sda6', 12437225472, 8712),
    ('system_b', 'sda7', 12437225472, 3045144),
    ('xbl_a', 'sdb1', 3670016, 6),
    ('xbl_config_a', 'sdb2', 364544, 902),
    ('xbl_b', 'sdc1', 3670016, 6),
    ('xbl_config_b', 'sdc2', 364544, 902),
    ('aop_a', 'sde1', 524288, 6),
    ('tz_a', 'sde2', 2097152, 134),
    ('hyp_a', 'sde3', 524288, 646),
    ('modem_a', 'sde4', 125829120, 774),
    ('bluetooth_a', 'sde5', 1048576, 31494),
    ('mdtpsecapp_a', 'sde6', 4194304, 31750),
    ('mdtp_a', 'sde7', 33554432, 32774),
    ('abl_a', 'sde8', 1048576, 40966),
    ('dsp_a', 'sde9', 33554432, 41222),
    ('boot_a', 'sde11', 67108864, 49542),
    ('cmnlib_a', 'sde12', 524288, 65926),
    ('cmnlib64_a', 'sde13', 524288, 66054),
    ('devcfg_a', 'sde14', 131072, 66182),
    ('qupfw_a', 'sde15', 65536, 66214),
    ('storsec_a', 'sde16', 131072, 66230),
    ('ImageFv_a', 'sde17', 1048576, 66262),
    ('aop_b', 'sde18', 524288, 66518),
    ('tz_b', 'sde19', 2097152, 66646),
    ('hyp_b', 'sde20', 524288, 67158),
    ('modem_b', 'sde21', 125829120, 67286),
    ('bluetooth_b', 'sde22', 1048576, 98006),
    ('mdtpsecapp_b', 'sde23', 4194304, 98262),
    ('mdtp_b', 'sde24', 33554432, 99286),
    ('abl_b', 'sde25', 1048576, 107478),
    ('dsp_b', 'sde26', 33554432, 107734),
    ('boot_b', 'sde28', 67108864, 116054),
    ('cmnlib_b', 'sde29', 524288, 132438),
    ('cmnlib64_b', 'sde30', 524288, 132566),
    ('devcfg_b', 'sde31', 131072, 132694),
    ('qupfw_b', 'sde32', 65536, 132726),
    ('storsec_b', 'sde33', 131072, 132742),
    ('ImageFv_b', 'sde34', 1048576, 132774),
    ('splash', 'sde42', 34226176, 135595),
    ('limits', 'sde43', 4096, 143951),
    ('toolsfv', 'sde44', 1048576, 143952),
    ('sti', 'sde46', 2097152, 146256),
)
OBJECTS = {
    "partition_" + name: {"name": name, "kind": "partition", "node": node, "lun": node[:3],
           "bytes": size, "start_lba": start, "offset": 0,
           "partition_number": int(node[3:]),
           "budget_seconds": 1800 if name.startswith("system_") else 180}
    for name, node, size, start in PARTITIONS
}
for _lun, _size in LUN_BYTES.items():
    for _side, _offset, _count in (("primary", 0, 6), ("secondary", _size - 5 * SECTOR, 5)):
        OBJECTS["gpt_" + _lun + "_" + _side] = {
            "kind": "gpt_metadata", "node": _lun, "lun": _lun,
            "bytes": _count * SECTOR, "offset": _offset,
            "budget_seconds": 180,
        }
del _lun, _size, _side, _offset, _count


class CaptureError(Exception):
    """Only these fixed codes are serialized; never expose arbitrary exceptions."""


def _fail(code):
    raise CaptureError(code)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RealIO:
    """Small injectable OS surface; no target filesystem work during import."""
    open = staticmethod(os.open)
    close = staticmethod(os.close)
    fstat = staticmethod(os.fstat)
    read = staticmethod(os.read)
    pread = staticmethod(os.pread)
    monotonic = staticmethod(time.monotonic)


def _read_sysfs(io, path):
    fd = io.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(io.fstat(fd).st_mode):
            _fail("SYSFS_TYPE")
        value = io.read(fd, MAX_SYSFS + 1)
        if len(value) > MAX_SYSFS:
            _fail("SYSFS_LIMIT")
        try:
            text = value.decode("ascii").strip()
        except UnicodeError:
            _fail("SYSFS_ENCODING")
        if not text:
            _fail("SYSFS_EMPTY")
        return text
    finally:
        io.close(fd)


def _integer(io, path):
    text = _read_sysfs(io, path)
    if not text.isdigit() or len(text) > 20:
        _fail("SYSFS_INTEGER")
    return int(text)


def _dev(io, node):
    text = _read_sysfs(io, "/sys/class/block/" + node + "/dev")
    parts = text.split(":")
    if len(parts) != 2 or any(not x.isdigit() or len(x) > 10 for x in parts):
        _fail("SYSFS_DEV")
    return [int(x) for x in parts]


def _geometry(io, obj):
    lun, node = obj["lun"], obj["node"]
    result = {"parent_dev": _dev(io, lun), "node_dev": _dev(io, node),
              "parent_bytes": _integer(io, "/sys/class/block/" + lun + "/size") * 512,
              "sector_bytes": _integer(io, "/sys/class/block/" + lun + "/queue/logical_block_size"),
              "node_bytes": _integer(io, "/sys/class/block/" + node + "/size") * 512}
    if result["parent_bytes"] != LUN_BYTES[lun] or result["sector_bytes"] != SECTOR:
        _fail("PARENT_GEOMETRY")
    if obj["kind"] == "partition":
        result["start_512_sectors"] = _integer(io, "/sys/class/block/" + node + "/start")
        result["partition_number"] = _integer(io, "/sys/class/block/" + node + "/partition")
        if (result["node_bytes"] != obj["bytes"] or
                result["start_512_sectors"] * 512 != obj["start_lba"] * SECTOR or
                result["partition_number"] != obj["partition_number"]):
            _fail("PARTITION_GEOMETRY")
    elif result["node_bytes"] != LUN_BYTES[lun]:
        _fail("PARENT_GEOMETRY")
    return result


def _node_stat(io, fd, expected_dev):
    st = io.fstat(fd)
    if not stat.S_ISBLK(st.st_mode):
        _fail("NODE_TYPE")
    actual_dev = [os.major(st.st_rdev), os.minor(st.st_rdev)]
    if actual_dev != expected_dev:
        _fail("NODE_IDENTITY")
    return {"rdev": actual_dev, "inode": st.st_ino, "fs_dev": st.st_dev,
            "mode": st.st_mode, "uid": st.st_uid, "gid": st.st_gid,
            "size": st.st_size, "ctime_ns": st.st_ctime_ns,
            "mtime_ns": st.st_mtime_ns}


def _exact(io, fd, size, offset):
    data = io.pread(fd, size, offset)
    if len(data) != size:
        _fail("SHORT_READ")
    return data


def _header(data, disk_bytes, secondary):
    if len(data) != SECTOR or data[:8] != b"EFI PART":
        _fail("GPT_HEADER")
    revision, hsize, stored_crc, reserved = struct.unpack_from("<IIII", data, 8)
    current, alternate, first, last = struct.unpack_from("<QQQQ", data, 24)
    table_lba, count, size, table_crc = struct.unpack_from("<QIII", data, 72)
    total = disk_bytes // SECTOR
    if (revision != 65536 or hsize != 92 or reserved != 0 or
            current != (total - 1 if secondary else 1) or
            alternate != (1 if secondary else total - 1) or
            first != 6 or last != total - 6 or size != 128 or
            count < 1 or count > 128 or count * size > 4 * SECTOR):
        _fail("GPT_SCOPE")
    # These exact host metadata windows never overlap the declared usable range.
    low, high = (total - 5, total - 1) if secondary else (2, 6)
    if table_lba < low or table_lba * SECTOR + count * size > high * SECTOR:
        _fail("GPT_TABLE_SCOPE")
    header = bytearray(data[:hsize])
    header[16:20] = bytes(4)
    if zlib.crc32(header) & 0xffffffff != stored_crc:
        _fail("GPT_HEADER_CRC")
    return {"table_offset": table_lba * SECTOR, "count": count,
            "entry_size": size, "table_bytes": count * size,
            "stored_table_crc32": table_crc}


def _layout(io, fd, obj, object_id):
    disk_bytes = LUN_BYTES[obj["lun"]]
    primary_header = _exact(io, fd, SECTOR, SECTOR)
    secondary_header = _exact(io, fd, SECTOR, disk_bytes - SECTOR)
    primary = _header(primary_header, disk_bytes, False)
    secondary = _header(secondary_header, disk_bytes, True)
    if (primary_header[56:72] != secondary_header[56:72] or
            any(primary[key] != secondary[key] for key in ("count", "entry_size", "table_bytes"))):
        _fail("GPT_HEADER_DISAGREEMENT")
    head = _exact(io, fd, 6 * SECTOR, 0)
    tail = _exact(io, fd, 5 * SECTOR, disk_bytes - 5 * SECTOR)
    if head[SECTOR:2 * SECTOR] != primary_header or tail[-SECTOR:] != secondary_header:
        _fail("GPT_HEADER_DRIFT")
    tables = []
    for header, area, base in ((primary, head, 0), (secondary, tail, disk_bytes - 5 * SECTOR)):
        start = header["table_offset"] - base
        tables.append(area[start:start + header["table_bytes"]])
    if tables[0] != tables[1]:
        _fail("GPT_TABLE_DISAGREEMENT")
    crcs = [zlib.crc32(t) & 0xffffffff for t in tables]
    # Preserve the known sde secondary CRC anomaly. No correction or write exists.
    # The valid primary table and byte-identical secondary establish range lookup.
    if crcs[0] != primary["stored_table_crc32"]:
        _fail("GPT_PRIMARY_ENTRIES_CRC")
    if crcs[1] != secondary["stored_table_crc32"]:
        known_anomaly = (obj["lun"] == "sde" and
                         _sha(tables[1]) == KNOWN_SDE_TABLE_SHA256 and
                         secondary["stored_table_crc32"] == KNOWN_SDE_SECONDARY_STORED_CRC32 and
                         crcs[1] == KNOWN_SDE_SECONDARY_CALCULATED_CRC32)
        if not known_anomaly:
            _fail("GPT_SECONDARY_ENTRIES_CRC")
    record = {"disk_guid_sha256": _sha(primary_header[56:72]),
              "primary_metadata_sha256": _sha(head), "secondary_metadata_sha256": _sha(tail),
              "primary_table_sha256": _sha(tables[0]), "secondary_table_sha256": _sha(tables[1]),
              "primary_entries_crc_valid": True,
              "secondary_entries_crc_valid": crcs[1] == secondary["stored_table_crc32"],
              "metadata_read_bytes": 13 * SECTOR}
    if obj["kind"] == "partition":
        number = obj["partition_number"]
        if number > primary["count"]:
            _fail("GPT_PARTITION_NUMBER")
        entry = tables[0][(number - 1) * 128:number * 128]
        first, last = struct.unpack_from("<QQ", entry, 32)
        try:
            name = entry[56:128].decode("utf-16-le").rstrip("\x00")
        except UnicodeError:
            _fail("GPT_PARTITION_NAME")
        if (not any(entry[:16]) or first != obj["start_lba"] or
                (last - first + 1) * SECTOR != obj["bytes"] or name != obj["name"]):
            _fail("GPT_PARTITION_MAPPING")
        record["selected_entry_sha256"] = _sha(entry)
    return record


def _emit(stderr, record):
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text) > MAX_RECORD:
        _fail("RECORD_LIMIT")
    stderr.write(text + "\n")
    stderr.flush()
    return len(text.encode("ascii")) + 1


def stream(object_id, stdout, stderr, io=None):
    """Return 0 only for a complete single-object gzip stream with stable mapping.

    io is an offline-test injection surface, not a CLI option. No retries or
    resumptions: short reads, interruption or drift fail the entire object.
    """
    io = RealIO() if io is None else io
    started = io.monotonic()
    read_bytes = 0
    compressed_bytes = 0
    raw_hash = hashlib.sha256()
    gzip_hash = hashlib.sha256()
    fds = []
    began = False
    obj = None
    stderr_bytes = 0

    def emit(record):
        nonlocal stderr_bytes
        size = len(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)) + 1
        if stderr_bytes + size > MAX_STDERR:
            _fail("STDERR_BUDGET")
        stderr_bytes += _emit(stderr, record)
    try:
        if not isinstance(object_id, str) or object_id not in OBJECTS:
            _fail("OBJECT_NOT_ALLOWED")
        obj = OBJECTS[object_id]
        if obj["bytes"] > 12437225472:
            _fail("BYTE_BUDGET")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        before_geometry = _geometry(io, obj)
        parent_fd = io.open("/dev/" + obj["lun"], flags)
        fds.append(parent_fd)
        before_parent = _node_stat(io, parent_fd, before_geometry["parent_dev"])
        node_fd = parent_fd
        if obj["kind"] == "partition":
            node_fd = io.open("/dev/" + obj["node"], flags)
            fds.append(node_fd)
        before_node = _node_stat(io, node_fd, before_geometry["node_dev"])
        before_layout = _layout(io, parent_fd, obj, object_id)
        before = {"geometry": before_geometry, "parent_stat": before_parent,
                  "node_stat": before_node, "layout": before_layout}
        if io.monotonic() - started > obj["budget_seconds"]:
            _fail("TIME_BUDGET")
        emit({"schema": SCHEMA, "event": "begin", "object_id": object_id,
                       "utc": _utc(), "source": dict(obj), "expected_bytes": obj["bytes"],
                       "before": before, "consistency": "BEST_EFFORT_LIVE_READ",
                       "target_writes": False, "atomic_snapshot": False})
        began = True
        compressor = zlib.compressobj(1, zlib.DEFLATED, 31)

        def write_chunk(value):
            nonlocal compressed_bytes
            if value:
                if stdout.write(value) != len(value):
                    _fail("OUTPUT_SHORT_WRITE")
                compressed_bytes += len(value)
                gzip_hash.update(value)

        last_progress = io.monotonic()
        while read_bytes < obj["bytes"]:
            if io.monotonic() - started > obj["budget_seconds"]:
                _fail("TIME_BUDGET")
            count = min(CHUNK, obj["bytes"] - read_bytes)
            data = _exact(io, node_fd, count, obj["offset"] + read_bytes)
            read_bytes += len(data)
            raw_hash.update(data)
            write_chunk(compressor.compress(data))
            now = io.monotonic()
            if now - last_progress >= PROGRESS_SECONDS:
                emit({"schema": SCHEMA, "event": "progress", "object_id": object_id,
                      "actual_bytes": read_bytes, "elapsed_seconds": round(now - started, 6)})
                last_progress = now
        after_geometry = _geometry(io, obj)
        after_parent = _node_stat(io, parent_fd, before_geometry["parent_dev"])
        after_node = _node_stat(io, node_fd, before_geometry["node_dev"])
        after_layout = _layout(io, parent_fd, obj, object_id)
        after = {"geometry": after_geometry, "parent_stat": after_parent,
                 "node_stat": after_node, "layout": after_layout}
        if before != after:
            _fail("SOURCE_METADATA_DRIFT")
        if obj["kind"] == "gpt_metadata":
            side = "primary" if obj["offset"] == 0 else "secondary"
            if raw_hash.hexdigest() != before_layout[side + "_metadata_sha256"]:
                _fail("GPT_CONTENT_DRIFT")
        if io.monotonic() - started > obj["budget_seconds"]:
            _fail("TIME_BUDGET")
        write_chunk(compressor.flush())
        stdout.flush()
        emit({"schema": SCHEMA, "event": "end", "status": "COMPLETE", "object_id": object_id,
                       "utc": _utc(), "actual_bytes": read_bytes, "raw_sha256": raw_hash.hexdigest(),
                       "gzip_bytes": compressed_bytes, "gzip_sha256": gzip_hash.hexdigest(),
                       "after": after, "metadata_stable": True, "content_stability_proven": False,
                       "elapsed_seconds": round(io.monotonic() - started, 6)})
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, CaptureError):
            code = str(error)
        elif isinstance(error, KeyboardInterrupt):
            code = "INTERRUPTED"
        elif isinstance(error, PermissionError):
            code = "PERMISSION_DENIED"
        elif isinstance(error, BrokenPipeError):
            code = "OUTPUT_CLOSED"
        elif isinstance(error, OSError):
            code = "IO_ERROR"
        else:
            code = "INTERNAL_ERROR"
        try:
            emit({"schema": SCHEMA, "event": "end", "status": "FAILED",
                           "object_id": object_id if obj is not None else None,
                           "utc": _utc(), "error_code": code, "begin_emitted": began,
                           "actual_bytes": read_bytes, "gzip_bytes": compressed_bytes,
                           "raw_sha256_partial": raw_hash.hexdigest(),
                           "gzip_sha256_partial": gzip_hash.hexdigest()})
        except (Exception, KeyboardInterrupt):
            pass
        return 2
    finally:
        for fd in reversed(fds):
            try:
                io.close(fd)
            except OSError:
                pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        _emit(sys.stderr, {"schema": SCHEMA, "event": "end", "status": "FAILED",
                           "object_id": None, "error_code": "ONE_OBJECT_ID_REQUIRED"})
        return 2
    return stream(argv[0], sys.stdout.buffer, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
