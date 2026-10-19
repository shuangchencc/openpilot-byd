"""Selected installed-file hashes only; Python 3.8 stdlib, no target imports.

Sent over stdin to python -I -S -B. Never executes installed software or opens
USB/SPI/block devices. A disk artifact is not proof of running MCU firmware.

Linux O_PATH pins metadata without calling a device driver's open operation.
Only a pinned regular inode is reopened for reading through /proc/self/fd; a
leaf symlink or device inode is rejected before any data-capable open. There is
no fallback on systems without O_PATH. Imports and function definitions do no I/O.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat


FILES = (
    "build.json", "common/version.h", "openpilot/common/version.h", "launch_env.sh",
    "panda/board/obj/version", "panda/board/obj/panda.bin.signed",
    "panda/board/obj/panda_h7.bin.signed", "panda/board/obj/bootstub.panda.bin",
    "panda/board/obj/bootstub.panda_h7.bin", "panda/python/__init__.py",
    "panda/python/spi.py", "panda/board/drivers/spi.h",
    "selfdrive/pandad/pandad.py", "selfdrive/pandad/pandad",
    "openpilot/selfdrive/pandad/pandad.py", "openpilot/selfdrive/pandad/pandad",
    "system/hardware/tici/agnos.json", "system/camerad/camerad",
)


def record(path):
    metadata_fd = None
    try:
        if not hasattr(os, "O_PATH"):
            return {"error": "safe_metadata_pinning_unavailable"}
        # O_PATH is deliberately used before regular-file validation: unlike
        # O_RDONLY it does not invoke a character/block device driver's open.
        # O_NOFOLLOW returns a symlink inode itself, which is then rejected.
        metadata_fd = os.open(str(path), os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(metadata_fd)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= 64 * 1024 * 1024:
            return {"error": "not_bounded_regular_file"}
        # Intentionally follow only this process's fd magic link, never reopen
        # the original pathname after validation. The single-threaded collector
        # retains metadata_fd until reading finishes, so path replacement cannot
        # redirect this open to another inode or to a device node.
        fd = os.open("/proc/self/fd/%d" % metadata_fd, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or
                    (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) !=
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)):
                return {"error": "file_changed_before_open"}
            digest, total = hashlib.sha256(), 0
            while total <= before.st_size:
                chunk = os.read(fd, min(1024 * 1024, before.st_size + 1 - total))
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(fd)
            stable = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if total != before.st_size or not stable:
                return {"error": "file_changed_during_read", "bytes_read": total}
            return {"bytes": total, "sha256": digest.hexdigest(), "stable_during_read": True}
        finally:
            os.close(fd)
    except OSError as error:
        return {"error": type(error).__name__}
    finally:
        if metadata_fd is not None:
            os.close(metadata_fd)


def collect():
    root = Path("/data/openpilot")
    result = {
        "schema": "songplus-p10-passive-software/v1",
        "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Selected disk artifact hashes; not live MCU flash/signature/ABI",
        "read_method": "Linux O_PATH|O_NOFOLLOW metadata pin; reopen only confirmed regular inode via /proc/self/fd",
        "files": {name: record(root / name) for name in FILES},
        "target_writes": False,
        "target_software_executed_or_imported": False,
    }
    try:
        result["application_path"] = str(root.resolve(strict=True))
    except OSError as error:
        result["application_path"] = {"error": type(error).__name__}
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
