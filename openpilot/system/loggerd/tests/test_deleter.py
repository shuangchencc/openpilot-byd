from collections import namedtuple
from pathlib import Path
from collections.abc import Sequence

import openpilot.system.loggerd.deleter as deleter
from openpilot.common.hardware.hw import Paths
from openpilot.system.loggerd.tests.loggerd_tests_common import UploaderTestCase

Stats = namedtuple("Stats", ['f_bavail', 'f_blocks', 'f_frsize'])


class TestDeleter(UploaderTestCase):
  # Deletion behavior is independent of file size; use smaller files to keep these tests fast.
  def make_file_with_data(self, f_dir: str, fn: str, size_mb: float = .001, lock: bool = False,
                          upload_xattr: bytes | None = None, preserve_xattr: bytes | None = None) -> Path:
    return super().make_file_with_data(f_dir, fn, size_mb, lock, upload_xattr, preserve_xattr)

  def fake_statvfs(self, d):
    return self.fake_stats

  def setup_method(self):
    # rlog, not fcamera.hevc: video of unarchived segments is deleted first (tier 2), so the
    # stock whole-segment order is what these tests check through tier 3.
    self.f_type = "rlog.zst"
    super().openpilot_setup_method()
    self.fake_stats = Stats(f_bavail=0, f_blocks=10, f_frsize=4096)
    deleter.os.statvfs = self.fake_statvfs  # ty: ignore[invalid-assignment]  # test double

  def test_delete(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not f_path.exists()

  def assertDeleteOrder(self, f_paths: Sequence[Path]) -> None:
    deleted_order = []
    for _ in f_paths:
      out_of_space, deleted_path = deleter.deleter_step()
      assert out_of_space and deleted_path is not None
      deleted_order.append(next(f for f in f_paths if f.parent == Path(deleted_path)))

    assert deleted_order == f_paths, "Files not deleted in expected order"

  def test_delete_order(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(1), self.f_type),
      self.make_file_with_data(self.seg_format2.format(0), self.f_type),
    ])

  def test_delete_many_preserved(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(1), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE),
      self.make_file_with_data(self.seg_format.format(2), self.f_type),
    ] + [
      self.make_file_with_data(self.seg_format2.format(i), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE)
      for i in range(5)
    ])

  def test_delete_last(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(1), self.f_type),
      self.make_file_with_data(self.seg_format2.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(0), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE),
      self.make_file_with_data("boot", self.seg_format[:-4]),
      self.make_file_with_data("crash", self.seg_format2[:-4]),
    ])

  def test_no_delete_when_available_space(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)

    block_size = 4096
    available = (10 * 1024 * 1024 * 1024) / block_size  # 10GB free
    self.fake_stats = Stats(f_bavail=available, f_blocks=10, f_frsize=block_size)

    assert deleter.deleter_step() == (False, None)
    assert f_path.exists(), "File deleted with available space"

  def test_no_delete_with_lock_file(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type, lock=True)

    assert deleter.deleter_step() == (True, None)
    assert f_path.exists(), "File deleted when locked"


class TestArchiveAwareDeleter(TestDeleter):
  def ack(self, *entries):
    with open(deleter.archive_ack_path(), "a") as f:
      for d, n in entries:
        f.write(f"{d}\t{n}\t{(Path(Paths.log_root()) / d / n).stat().st_size}\n")

  def setup_method(self):
    super().openpilot_setup_method()
    Path(deleter.archive_ack_path()).unlink(missing_ok=True)

  def test_archived_segment_deleted_before_older_unarchived(self):
    old = self.make_file_with_data(self.seg_format.format(0), "rlog.zst")
    new = self.make_file_with_data(self.seg_format.format(1), "rlog.zst")
    self.ack((new.parent.name, "rlog.zst"))
    assert deleter.deleter_step() == (True, str(new.parent))
    assert old.exists()

  def test_partly_archived_segment_is_not_tier1(self):
    old = self.make_file_with_data(self.seg_format.format(0), "rlog.zst")
    new = self.make_file_with_data(self.seg_format.format(1), "rlog.zst")
    self.make_file_with_data(self.seg_format.format(1), "qlog.zst")
    self.ack((new.parent.name, "rlog.zst"))
    assert deleter.deleter_step() == (True, str(old.parent))

  def test_size_mismatch_is_not_archived(self):
    old = self.make_file_with_data(self.seg_format.format(0), "rlog.zst")
    new = self.make_file_with_data(self.seg_format.format(1), "rlog.zst")
    with open(deleter.archive_ack_path(), "w") as f:
      f.write(f"{new.parent.name}\trlog.zst\t1\n")
    assert deleter.deleter_step() == (True, str(old.parent))

  def test_unarchived_video_before_logs(self):
    log0 = self.make_file_with_data(self.seg_format.format(0), "rlog.zst")
    q0 = self.make_file_with_data(self.seg_format.format(0), "qcamera.ts")
    vid1 = self.make_file_with_data(self.seg_format.format(1), "fcamera.hevc")
    log1 = self.make_file_with_data(self.seg_format.format(1), "rlog.zst")
    assert deleter.deleter_step() == (True, str(vid1))
    assert log0.exists() and q0.exists() and log1.exists()
    # no video left: stock order, oldest whole segment
    assert deleter.deleter_step() == (True, str(log0.parent))

  def test_archived_whole_segment_beats_video(self):
    vid0 = self.make_file_with_data(self.seg_format.format(0), "fcamera.hevc")
    log1 = self.make_file_with_data(self.seg_format.format(1), "rlog.zst")
    self.ack((log1.parent.name, "rlog.zst"))
    assert deleter.deleter_step() == (True, str(log1.parent))
    assert vid0.exists()

  def test_locked_segment_untouched(self):
    vid = self.make_file_with_data(self.seg_format.format(0), "fcamera.hevc", lock=True)
    assert deleter.deleter_step() == (True, None)
    assert vid.exists()

  def test_garbage_ack_file(self):
    log0 = self.make_file_with_data(self.seg_format.format(0), "rlog.zst")
    with open(deleter.archive_ack_path(), "w") as f:
      f.write("junk\n\t\t\nx\ty\tnotanumber\n")
    assert deleter.deleter_step() == (True, str(log0.parent))
