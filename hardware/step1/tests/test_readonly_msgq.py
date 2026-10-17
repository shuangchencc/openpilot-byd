"""Synthetic queues only: validate passive reads, bounds, and race rejection."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "collectors/messages.py"
spec = importlib.util.spec_from_file_location("readonly_msgq", SOURCE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def record(payload):
    data = struct.pack("<q", len(payload)) + payload
    return data + b"\0" * (-len(data) % 8)


class ReadonlyMsgqTests(unittest.TestCase):
    def make_queue(self, path, payloads, cycles=0):
        data = b"".join(record(p) for p in payloads)
        header = struct.pack("<QQQ", 2, (cycles << 32) | len(data), 123456)
        path.write_bytes(header + b"\0" * (collector.HEADER_BYTES - len(header)) + data
                         + b"\0" * (collector.RING_BYTES - len(data)))

    def test_latest_frame_and_alignment(self):
        (offset, frame), count = collector.latest_committed_frame(record(b"old") + record(b"latest"))
        self.assertEqual((offset, frame, count), (16, b"latest", 2))

    def test_invalid_lengths_rejected(self):
        for size in (-1, 0, collector.MAX_FRAME_BYTES + 1, 48):
            with self.subTest(size=size), self.assertRaises(ValueError):
                collector.latest_committed_frame(struct.pack("<q", size) + b"12345678")

    def test_empty_and_partial_prefix_rejected(self):
        for data in (b"", b"x", record(b"payload")[:-1]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                collector.latest_committed_frame(data)

    def test_snapshot_wrap_cycle_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pandaStates"
            self.make_queue(path, [b"old", b"new"], cycles=7)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = collector.snapshot(path)
            self.assertEqual(result["write_cycles"], 7)
            self.assertEqual(result["frame_sha256"], hashlib.sha256(b"new").hexdigest())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertIsNone(collector.snapshot(path, tuple(result["writer_guard"])))

    def test_changed_writer_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deviceState"
            self.make_queue(path, [b"sample"])
            with patch.object(collector, "writer_guard", side_effect=[(16, 42), (32, 42)]):
                with self.assertRaisesRegex(ValueError, "publisher changed"):
                    collector.snapshot(path)

    def test_publication_after_snapshot_does_not_corrupt_local_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pandaStates"
            self.make_queue(path, [b"captured"])
            original = collector.latest_committed_frame

            def advance_after_copy(prefix):
                self.make_queue(path, [b"captured", b"published-later"])
                return original(prefix)

            with patch.object(collector, "latest_committed_frame", side_effect=advance_after_copy):
                result = collector.snapshot(path)
            self.assertEqual(result["frame_sha256"], hashlib.sha256(b"captured").hexdigest())

    def test_bounded_header_diagnostic_does_not_guess_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pandaStates"
            path.write_bytes(struct.pack("<QQQ", 1, 2, 3) + b"unknown-layout")
            result = collector.inspect_queue_header(path)
            self.assertEqual(result["file_bytes"], 38)
            self.assertEqual(result["first_three_le_u64"], [1, 2, 3])

    def test_symlink_and_wrong_size_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_bytes(b"short")
            link = Path(directory) / "pandaStates"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                collector.snapshot(link)
            with self.assertRaises(ValueError):
                collector.snapshot(target)

    def test_invalid_writer_header_rejected(self):
        for readers, pointer, uid in [(24, 16, 1), (1, 3, 1), (1, collector.RING_BYTES + 8, 1), (1, 8, 0)]:
            header = struct.pack("<QQQ", readers, pointer, uid) + b"\0" * (collector.HEADER_BYTES - 24)
            with self.subTest(header=header[:24]), self.assertRaises(ValueError):
                collector.writer_guard(header)


if __name__ == "__main__":
    unittest.main()
