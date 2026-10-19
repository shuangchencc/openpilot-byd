"""Read only two 1024-byte DSP ext4 superblocks; no mount/repair/blkid/ioctl."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct


def collect():
    result = {"schema": "songplus-p10-passive-dsp-labels/v1",
              "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "Two bounded ext4 metadata reads only; no filesystem contents or recovery action",
              "partitions": {}}
    for name in ("sde9", "sde26"):
        try:
            fd = os.open("/dev/" + name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                meta = os.fstat(fd)
                if not stat.S_ISBLK(meta.st_mode):
                    raise ValueError("not block device")
                data = os.pread(fd, 1024, 1024)
                if len(data) != 1024 or struct.unpack_from("<H", data, 56)[0] != 0xef53:
                    raise ValueError("not ext superblock")
                label = data[120:136].split(b"\0", 1)[0].decode("ascii", "strict")
                if not re.fullmatch(r"[A-Za-z0-9_-]{0,16}", label):
                    raise ValueError("unexpected label")
                result["partitions"][name] = {"label": label, "uuid_sha256": hashlib.sha256(data[104:120]).hexdigest(),
                                              "rdev": [os.major(meta.st_rdev), os.minor(meta.st_rdev)],
                                              "bytes_read": 1024, "offset": 1024}
            finally:
                os.close(fd)
        except (OSError, ValueError, UnicodeError) as error:
            result["partitions"][name] = {"error": type(error).__name__}
    try:
        result["by_label_dsp"] = os.readlink("/dev/disk/by-label/dsp")
    except OSError as error:
        result["by_label_dsp"] = {"error": type(error).__name__}
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
