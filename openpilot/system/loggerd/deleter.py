#!/usr/bin/env python3
import os
import shutil
import threading
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 10

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5

# Song PLUS / C3XL data archive. The Mac archiver copies finished segments, verifies them by
# sha256 and then writes "<segment>\t<file>\t<size>" lines into <log_root>.archived (atomic
# rename). When space runs out the deleter frees space in this order:
#   1. whole segments whose every file is listed with the same size (safe: a copy exists),
#   2. full-size video (*.hevc) of not-yet-archived segments, keeping rlog/qlog/qcamera,
#   3. the stock rule (oldest segment first), only if nothing else is left.
# Within each tier the stock order applies (oldest first, boot/crash and preserved last).
ARCHIVE_ACK_SUFFIX = ".archived"
BIG_VIDEO_SUFFIXES = (".hevc",)
_ack_cache: dict = {"key": None, "acked": {}}


def archive_ack_path() -> str:
  return Paths.log_root().rstrip("/") + ARCHIVE_ACK_SUFFIX


def load_archive_acks() -> dict[str, dict[str, int]]:
  path = archive_ack_path()
  try:
    st = os.stat(path)
  except OSError:
    return {}
  key = (path, st.st_mtime_ns, st.st_size)
  if _ack_cache["key"] != key:
    acked: dict[str, dict[str, int]] = {}
    try:
      with open(path) as f:
        for line in f:
          parts = line.rstrip("\n").split("\t")
          if len(parts) == 3 and parts[2].isdigit():
            acked.setdefault(parts[0], {})[parts[1]] = int(parts[2])
    except OSError:
      cloudlog.exception(f"cannot read {path}")
      return {}
    _ack_cache["key"], _ack_cache["acked"] = key, acked
  return _ack_cache["acked"]


def is_archived(d: str, names: list[str], acked: dict[str, dict[str, int]]) -> bool:
  files = acked.get(d)
  if files is None:
    return False
  for n in names:
    try:
      size = os.path.getsize(os.path.join(Paths.log_root(), d, n))
    except OSError:
      return False
    if files.get(n) != size:
      return False
  return True


def has_preserve_xattr(d: str) -> bool:
  return getxattr(os.path.join(Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str]) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(has_preserve_xattr, reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def deleter_step() -> tuple[bool, str | None]:
  out_of_bytes = get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES
  out_of_percent = get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT
  out_of_space = out_of_percent or out_of_bytes
  if not out_of_space:
    return False, None

  dirs = listdir_by_creation(Paths.log_root())
  preserved_dirs = get_preserved_segments(dirs)

  acked = load_archive_acks()
  order = sorted(dirs, key=lambda d: (d in DELETE_LAST, d in preserved_dirs))
  unlocked = []
  for d in order:
    try:
      names = os.listdir(os.path.join(Paths.log_root(), d))
    except OSError:
      continue
    if not any(name.endswith(".lock") for name in names):
      unlocked.append((d, names))

  # tier 1: segments with a verified copy on the Mac
  for d, names in unlocked:
    if is_archived(d, names, acked):
      deleted = _rmtree(d)
      if deleted:
        return True, deleted

  # tier 2: big video of segments not archived yet; logs stay
  for d, names in unlocked:
    for n in sorted(names):
      if n.endswith(BIG_VIDEO_SUFFIXES):
        path = os.path.join(Paths.log_root(), d, n)
        try:
          cloudlog.info(f"deleting unarchived video {path}")
          os.remove(path)
          return True, path
        except OSError:
          cloudlog.exception(f"issue deleting {path}")

  # tier 3: stock behaviour
  for d, _ in unlocked:
    deleted = _rmtree(d)
    if deleted:
      return True, deleted
  return True, None


def _rmtree(d: str) -> str | None:
  delete_path = os.path.join(Paths.log_root(), d)
  try:
    cloudlog.info(f"deleting {delete_path}")
    shutil.rmtree(delete_path)
    return delete_path
  except OSError:
    cloudlog.exception(f"issue deleting {delete_path}")
  return None


def deleter_thread(exit_event: threading.Event):
  while not exit_event.is_set():
    out_of_space, _ = deleter_step()
    exit_event.wait(.1 if out_of_space else 30)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
