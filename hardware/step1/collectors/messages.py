"""Snapshot three existing msgq files with O_RDONLY; no target-package imports.

Layout is tied to the archived c920589... binary and its 576-byte header.
No socket, mmap, reader registration, ftruncate, signal, or control API is used.
Raw frames belong only in ignored local results; decode/whitelist on the host.
"""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import time

HEADER_BYTES = 576
RING_BYTES = 10 * 1024 * 1024
MAX_FRAME_BYTES = 512 * 1024
SERVICES = ("deviceState", "pandaStates", "managerState")
EXPECTED_FILES = {
    "/data/openpilot/msgq/ipc_pyx.so": "c9205891508a42f147962b3dc1e8049353989e1ffa9ef32d326dae4e52da29e5",
    "/data/openpilot/selfdrive/pandad/pandad": "4875b20dc59d6c73c7f4017b8796f9a22746be179a75e7cb2417ab2e27506265",
    "/data/openpilot/msgq/msgq.h": "29ba87e7fe4aa00a694cc7abe82c93df2bcea3df5f9b743440ee5b1a498ebfff",
    "/data/openpilot/cereal/log.capnp": "5b2bcbf12be59203b9666b4d5193aecdb58e58852f713cfdf0cee1f7a4e8cdb0",
    "/data/openpilot/cereal/car.capnp": "4d3e7321bc9d8dc4aa77816bfe882ebfdbf9a19f2b07de640d1b2c26f04434b8",
    "/data/openpilot/cereal/custom.capnp": "d121160319662e953c34cc1458b361c002281b1a7ad628a2f60de29e932567fa",
    "/data/openpilot/cereal/legacy.capnp": "4ab15bad593a3433dab2d3c8f9951fa4fbadea2074ca0ebb082139ed38f25131",
    "/data/openpilot/cereal/include/c++.capnp": "fb306076cd38c27af1aed20fac6395e9a46fbe5b5df6a248b5f1b6845a079c44",
}


def verify_layout_files():
    for path, expected in EXPECTED_FILES.items():
        with open(path, "rb") as f:
            content = f.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("archived layout/schema hash mismatch")


def writer_guard(header):
    if len(header) != HEADER_BYTES:
        raise ValueError("short queue header")
    readers, pointer, uid = struct.unpack_from("<QQQ", header)
    end = pointer & 0xffffffff
    if readers > 23 or uid == 0 or end > RING_BYTES or end % 8:
        raise ValueError("invalid queue header")
    return pointer, uid


def latest_committed_frame(prefix):
    """Scan only [0, committed end), never the publisher's next write region."""
    position, count, last = 0, 0, None
    while position < len(prefix):
        if position + 8 > len(prefix):
            raise ValueError("short record length")
        size = struct.unpack_from("<q", prefix, position)[0]
        next_position = (position + 8 + size + 7) & ~7
        if not 0 < size <= MAX_FRAME_BYTES or next_position > len(prefix):
            raise ValueError("invalid or uncommitted record")
        last = (position, prefix[position + 8:position + 8 + size])
        position = next_position
        count += 1
        if count > 262144:
            raise ValueError("too many records")
    if last is None:
        raise ValueError("no committed record; publisher may be wrapping")
    return last, count


def snapshot(path, previous_guard=None):
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | os.O_NONBLOCK
    fd = os.open(str(path), flags)
    try:
        before_stat = os.fstat(fd)
        if not stat.S_ISREG(before_stat.st_mode) or before_stat.st_size != HEADER_BYTES + RING_BYTES:
            raise ValueError("not the expected existing regular queue")
        before = writer_guard(os.pread(fd, HEADER_BYTES, 0))
        if before == previous_guard:
            return None
        end = before[0] & 0xffffffff
        prefix = os.pread(fd, end, HEADER_BYTES)
        if len(prefix) != end:
            raise ValueError("short queue snapshot")
        after = writer_guard(os.pread(fd, HEADER_BYTES, 0))
        captured_at = time.monotonic_ns()
        if before != after or os.fstat(fd).st_size != before_stat.st_size:
            raise ValueError("publisher changed during snapshot")
        (offset, frame), count = latest_committed_frame(prefix)
        # prefix is an immutable local copy bounded by two identical writer
        # guards. A later publication cannot change it; do not require the
        # producer to remain idle during Python parsing. Wrap sets the next
        # cycle's zero pointer before overwriting, so full pointer equality
        # (including cycle count) rejects any wrap during the copy itself.
        return {"writer_guard": list(before), "write_cycles": before[0] >> 32,
                "committed_end": end, "frame_offset": offset, "records_in_current_cycle": count,
                "frame_bytes": len(frame), "frame_sha256": hashlib.sha256(frame).hexdigest(),
                "frame_base64": base64.b64encode(frame).decode("ascii"),
                "collected_monotonic_ns": captured_at}
    finally:
        os.close(fd)


def inspect_queue_header(path):
    """Bounded format diagnostic, without assuming a header or ring layout."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode):
            raise ValueError("queue path is not a regular file")
        prefix = os.pread(fd, 24, 0)
        return {"file_bytes": meta.st_size, "first_24_bytes_hex": prefix.hex(),
                "first_three_le_u64": list(struct.unpack("<QQQ", prefix)) if len(prefix) == 24 else None,
                "note": "Raw diagnostics; field interpretation requires confirmed publisher layout."}
    finally:
        os.close(fd)


def main():
    verify_layout_files()
    last, counts, errors = {}, dict.fromkeys(SERVICES, 0), dict.fromkeys(SERVICES, 0)
    samples = {name: [] for name in SERVICES}
    rejection_reasons = {name: {} for name in SERVICES}
    queue_headers = {}
    for name in SERVICES:
        try:
            queue_headers[name] = inspect_queue_header(Path("/dev/shm") / name)
        except (OSError, ValueError) as e:
            queue_headers[name] = {"error": type(e).__name__}
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        for name in SERVICES:
            if counts[name] >= 2:
                continue
            try:
                old = tuple(last[name]["writer_guard"]) if name in last else None
                result = snapshot(Path("/dev/shm") / name, old)
                if result is not None:
                    last[name] = result
                    samples[name].append(result)
                    counts[name] += 1
            except (OSError, ValueError) as e:
                errors[name] += 1
                reason = str(e) if isinstance(e, ValueError) else type(e).__name__
                rejection_reasons[name][reason] = rejection_reasons[name].get(reason, 0) + 1
        if all(counts[name] >= 2 for name in SERVICES):
            break
        time.sleep(1)
    print(json.dumps({"collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "method": "O_RDONLY pread of existing queues; no reader registration or package imports",
                      "layout_and_schema_hashes_verified": EXPECTED_FILES,
                      "device_files_written_by_collector": False, "ipc_reader_registration": False,
                      "distinct_snapshots": counts, "rejected_or_unavailable_snapshots": errors,
                      "rejection_reasons": rejection_reasons, "queue_headers": queue_headers,
                      "samples": samples}, indent=2))


if __name__ == "__main__":
    main()
