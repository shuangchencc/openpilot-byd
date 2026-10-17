"""Synthetic-only integrity and statistics tests, with no device access."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from replay import canlog


def frame(timestamp_ns=0, bus=0, address=0x123, data="0000"):
    return dict(timestamp_ns=timestamp_ns, bus=bus, address=address, data=data)


class CANLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_recording(self, records=None, *, raw=None, manifest_changes=None):
        if raw is None:
            raw = ("\n".join(json.dumps(item) for item in (records if records is not None else [frame()])) + "\n").encode()
        (self.root / "synthetic.jsonl").write_bytes(raw)
        manifest = {
            "schema": canlog.MANIFEST_SCHEMA,
            "data_kind": "synthetic",
            "log_file": "synthetic.jsonl",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "timestamp_unit": "ns",
            "timestamp_clock": "monotonic",
            "description": "Synthetic unit-test bytes only; no vehicle or personal data.",
        }
        manifest.update(manifest_changes or {})
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        return path

    def test_mixed_buses_same_identifier_are_not_merged(self):
        path = self.write_recording([frame(0, 0), frame(1_000_000, 1),
                                     frame(10_000_000, 0), frame(11_000_000, 1)])
        result = canlog.analyze(path)
        self.assertEqual(result["bus_frame_counts"], {"0": 2, "1": 2})
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual([m["observed_interval_rate_hz"] for m in result["messages"]], [100., 100.])

    def test_global_time_regression_across_buses_fails(self):
        path = self.write_recording([frame(100, 0), frame(99, 1)])
        with self.assertRaisesRegex(canlog.ValidationError, "line 2.*backwards"):
            canlog.load_recording(path)

    def test_all_can_fd_wire_lengths_are_accepted(self):
        sizes = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]
        path = self.write_recording([frame(i, data="ab" * size) for i, size in enumerate(sizes)])
        _, frames = canlog.load_recording(path)
        self.assertEqual([len(item.data) for item in frames], sizes)

    def test_non_wire_payload_lengths_fail(self):
        for size in [9, 10, 11, 13, 15, 17, 31, 33, 63, 65]:
            with self.subTest(size=size):
                path = self.write_recording([frame(data="00" * size)])
                with self.assertRaisesRegex(canlog.ValidationError, "payload length"):
                    canlog.load_recording(path)

    def test_bad_hex_is_rejected_without_normalization(self):
        for data in ["0", "gg", "00 01", "0x01", "00_01", "éé", "00\n01", 12, None]:
            with self.subTest(data=data):
                path = self.write_recording([frame(data=data)])
                with self.assertRaisesRegex(canlog.ValidationError, "ASCII hex"):
                    canlog.load_recording(path)

    def test_duplicate_timestamps_with_positive_span_keep_qualified_rate(self):
        path = self.write_recording([frame(0), frame(0), frame(10_000_000)])
        message = canlog.analyze(path)["messages"][0]
        self.assertEqual(message["observed_interval_rate_hz"], 200.)
        self.assertEqual(message["rate_status"], "computed_with_duplicate_timestamps")
        self.assertEqual(message["interval_ns"]["zero_count"], 1)
        self.assertEqual(message["interval_ns"]["min_positive"], 10_000_000)

    def test_all_duplicate_timestamps_have_no_frequency(self):
        path = self.write_recording([frame(123), frame(123), frame(123)])
        message = canlog.analyze(path)["messages"][0]
        self.assertIsNone(message["observed_interval_rate_hz"])
        self.assertEqual(message["rate_status"], "zero_time_span")
        self.assertEqual(message["interval_ns"]["zero_count"], 2)
        self.assertIsNone(message["interval_ns"]["min_positive"])

    def test_single_frame_does_not_claim_frequency(self):
        message = canlog.analyze(self.write_recording())["messages"][0]
        self.assertIsNone(message["observed_interval_rate_hz"])
        self.assertEqual(message["rate_status"], "insufficient_frames")
        self.assertIsNone(message["interval_ns"]["mean"])

    def test_raw_byte_transitions_are_not_physical_signals(self):
        path = self.write_recording([frame(0, data="0000"), frame(1, data="0100"), frame(2, data="0302")])
        result = canlog.analyze(path)
        first, second = result["messages"][0]["raw_byte_candidates"]
        self.assertEqual(first["distinct_values"], [0, 1, 3])
        self.assertEqual(first["bit_flip_counts_lsb_first"], [1, 1, 0, 0, 0, 0, 0, 0])
        self.assertEqual(second["changed_comparisons"], 1)
        self.assertNotIn("CarState", result)
        self.assertNotIn("signal_name", first)

    def test_missing_byte_interrupts_adjacent_comparisons(self):
        path = self.write_recording([frame(0, data="01"), frame(1, data=""), frame(2, data="03")])
        byte = canlog.analyze(path)["messages"][0]["raw_byte_candidates"][0]
        self.assertEqual(byte["frames_without_byte"], 1)
        self.assertEqual(byte["adjacent_frame_comparisons"], 0)
        self.assertEqual(byte["bit_flip_counts_lsb_first"], [0] * 8)

    def test_hash_mismatch_produces_no_accepted_frames(self):
        path = self.write_recording()
        (self.root / "synthetic.jsonl").write_text(json.dumps(frame(10)) + "\n")
        with self.assertRaisesRegex(canlog.ValidationError, "SHA256"):
            canlog.load_recording(path)

    def test_unknown_and_missing_record_fields_fail(self):
        extra = frame()
        extra["steeringAngleDeg"] = 1
        missing = frame()
        del missing["bus"]
        for item in [extra, missing, [], None]:
            with self.subTest(item=item):
                with self.assertRaises(canlog.ValidationError):
                    canlog.load_recording(self.write_recording([item]))

    def test_unknown_manifest_fields_fail(self):
        path = self.write_recording(manifest_changes={"vehicle_confirmed": True})
        with self.assertRaisesRegex(canlog.ValidationError, "unknown fields"):
            canlog.load_recording(path)

    def test_unknown_origin_or_timebase_fails(self):
        for change in [{"data_kind": "unknown"}, {"timestamp_clock": "wall-clock"},
                       {"timestamp_unit": "seconds"}, {"schema": "v99"}]:
            with self.subTest(change=change):
                with self.assertRaises(canlog.ValidationError):
                    canlog.load_recording(self.write_recording(manifest_changes=change))

    def test_record_numeric_ranges_and_types(self):
        changes = [{"timestamp_ns": -1}, {"timestamp_ns": 1.2}, {"timestamp_ns": True},
                   {"timestamp_ns": 1 << 63}, {"bus": -1}, {"bus": 256}, {"bus": False},
                   {"address": -1}, {"address": 0x20000000}, {"address": "0x123"}]
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(canlog.ValidationError):
                    canlog.load_recording(self.write_recording([frame() | change]))

    def test_extended_id_upper_bound_accepted_without_inventing_flags(self):
        result = canlog.analyze(self.write_recording([frame(address=0x1FFFFFFF)]))
        self.assertTrue(result["messages"][0]["requires_29bit_identifier"])
        self.assertNotIn("ide", result["messages"][0])

    def test_duplicate_json_keys_and_nan_fail(self):
        for raw in [b'{"timestamp_ns":0,"timestamp_ns":1,"bus":0,"address":1,"data":""}\n',
                    b'{"timestamp_ns":NaN,"bus":0,"address":1,"data":""}\n']:
            with self.subTest(raw=raw):
                with self.assertRaises(canlog.ValidationError):
                    canlog.load_recording(self.write_recording(raw=raw))

    def test_empty_blank_invalid_json_and_non_utf8_logs_fail(self):
        for raw in [b"", b"\n", b"{broken}\n", b"\xff\n"]:
            with self.subTest(raw=raw):
                with self.assertRaises(canlog.ValidationError):
                    canlog.load_recording(self.write_recording(raw=raw))

    def test_line_length_is_bounded(self):
        with self.assertRaisesRegex(canlog.ValidationError, "exceeds"):
            canlog.load_recording(self.write_recording(raw=b" " * 65537))

    def test_relative_path_traversal_absolute_and_controls_fail(self):
        for name in ["../synthetic.jsonl", str(self.root / "synthetic.jsonl"), "./synthetic.jsonl",
                     "sub/../synthetic.jsonl", "synthetic\x00.jsonl", "folder\\synthetic.jsonl"]:
            with self.subTest(name=name):
                with self.assertRaises(canlog.ValidationError):
                    canlog.load_recording(self.write_recording(manifest_changes={"log_file": name}))

    def test_symlink_outside_manifest_directory_fails(self):
        path = self.write_recording()
        folder = self.root / "nested"
        folder.mkdir()
        (folder / "synthetic.jsonl").symlink_to(self.root / "synthetic.jsonl")
        nested_manifest = folder / "manifest.json"
        nested_manifest.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(canlog.ValidationError, "inside the manifest directory"):
            canlog.load_recording(nested_manifest)

    def test_missing_file_is_validation_error_in_public_loader(self):
        with self.assertRaises(canlog.ValidationError):
            canlog.load_recording(self.root / "absent.json")

    def test_manifest_must_be_a_regular_file(self):
        with self.assertRaisesRegex(canlog.ValidationError, "regular local file"):
            canlog.load_recording(self.root)

    def test_validate_and_summary_agree_on_integrity(self):
        path = self.write_recording([frame(0), frame(1)])
        validated = canlog.analyze(path, summarize=False)
        summarized = canlog.analyze(path)
        self.assertEqual(validated["source"], summarized["source"])
        self.assertEqual(validated["frame_count"], summarized["frame_count"])
        self.assertNotIn("messages", validated)

    def test_cli_failure_emits_only_error_and_nonzero_status(self):
        path = self.write_recording([frame(10), frame(9)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = canlog.main(["summarize", "--manifest", str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["validation"], "failed")

    def test_repository_fixture_is_explicitly_synthetic(self):
        path = Path(__file__).parents[1] / "fixtures/synthetic-can-data.manifest.json"
        manifest, frames = canlog.load_recording(path)
        self.assertEqual(manifest["data_kind"], "synthetic")
        self.assertEqual(len(frames), 10)
        result = canlog.analyze(path)
        self.assertEqual(result["frame_count"], 10)
        self.assertEqual(result["messages"][0]["observed_interval_rate_hz"], 150.)


if __name__ == "__main__":
    unittest.main()
