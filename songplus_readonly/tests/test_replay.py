import json
import contextlib
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

from songplus_readonly.replay import (CanEvent, Comparison, OldState, causally_available,
                                     assert_reserved_unknowns, check_projection, ordered_can_events,
                                     replay, sha256, verify_candidate_research, verify_inputs)
from collections import Counter


class TestCausalMatching(unittest.TestCase):
  def test_requires_both_record_order_and_timestamp(self):
    state = OldState(1, 10, 100, {})
    self.assertTrue(causally_available(CanEvent(1, 9, 100, True, []), state))
    self.assertFalse(causally_available(CanEvent(1, 9, 101, True, []), state))
    self.assertFalse(causally_available(CanEvent(1, 11, 99, True, []), state))
    self.assertTrue(causally_available(CanEvent(0, 900, 99, True, []), state))
    self.assertFalse(causally_available(CanEvent(2, 0, 99, True, []), state))

  def test_unknown_is_not_false_and_old_invalid_is_not_compared(self):
    compare = Comparison(1e-5)
    compare.observe(None, False, old_valid=True, reason="unconfirmed", location=(0, 0, 1))
    compare.observe(False, False, old_valid=False, reason="available", location=(0, 1, 2))
    compare.observe(0.1, 0.1, old_valid=True, reason="available", location=(0, 2, 3))
    result = compare.result()
    self.assertEqual(result["compared"], 1)
    self.assertEqual(result["prototype_unavailable_reasons"], {"unconfirmed": 1})
    self.assertEqual(result["old_invalid"], 1)
    self.assertEqual(result["agreement_fraction"], 1)

  def test_boolean_confusion_excludes_unknown_invalid_and_nonboolean_labels(self):
    compare = Comparison(0, binary=True)
    for index, (value, reference, valid) in enumerate([
      (False, 0., True), (True, 0., True), (False, 1., True), (True, 1., True),
      (None, 0., True), (False, 1., False), (True, 2., True), (None, float('nan'), True),
    ]):
      compare.observe(value, reference, old_valid=valid,
                      reason="unseen" if value is None else "available", location=(0, index, index))
    result = compare.result()
    self.assertEqual(result["binary_confusion"], {"TN": 1, "FP": 1, "FN": 1, "TP": 1})
    self.assertEqual(result["compared"], 4)
    self.assertEqual(result["valid_reference_samples"], 5)
    self.assertEqual(result["prototype_unavailable_with_valid_reference"], 1)
    self.assertEqual(result["old_invalid"], 1)
    self.assertEqual(result["old_missing"], 1)
    self.assertEqual(result["old_not_boolean"], 1)
    self.assertEqual(result["quality_counts"], {"available": 6, "unseen": 2})
    with self.assertRaisesRegex(ValueError, "bool or None"):
      compare.observe(1., 1., old_valid=True, reason="available", location=(0, 9, 9))


class TestProjectionChecks(unittest.TestCase):
  def fixture(self):
    values = {"gasPressed": True, "brakePressed": False, "cruiseState.standstill": True,
              "cruiseState.enabled": None, "cruiseState.available": None}
    projected = types.SimpleNamespace(canValid=False, gasPressed=True, brakePressed=False,
                 cruiseState=types.SimpleNamespace(standstill=True, enabled=False, available=False))
    snapshot = types.SimpleNamespace(values=values,
      quality={k: "unconfirmed" if v is None else "available" for k, v in values.items()},
      to_opendbc=lambda: projected)
    return snapshot, projected

  def test_nested_candidate_projection_and_unknown_schema_defaults(self):
    snapshot, projected = self.fixture()
    result = check_projection(snapshot)
    self.assertTrue(result["candidate_fields"]["cruiseState.standstill"]["usable"])
    snapshot.values["gasPressed"] = None
    self.assertFalse(check_projection(snapshot)["candidate_fields"]["gasPressed"]["usable"])
    projected.cruiseState.standstill = False
    with self.assertRaisesRegex(ValueError, "projection differs"):
      check_projection(snapshot)

  def test_acc_unknowns_and_can_valid_cannot_be_promoted(self):
    snapshot, projected = self.fixture()
    snapshot.values["cruiseState.available"] = False
    with self.assertRaisesRegex(ValueError, "unconfirmed/None"):
      assert_reserved_unknowns(snapshot)
    snapshot.values["cruiseState.available"] = None
    projected.canValid = True
    with self.assertRaisesRegex(ValueError, "canValid"):
      check_projection(snapshot)


class TestStreamSelection(unittest.TestCase):
  def test_raw_src_not_masked_and_invalid_event_preserved(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "segment-000.npz"
      dtype = [("t_ns", "<u8"), ("event_index", "<u4"), ("valid", "?"),
               ("address", "<u4"), ("src", "<u2"), ("length", "u1")]
      can = np.array([(10, 2, True, 287, 0, 5), (10, 2, True, 287, 128, 5),
                      (10, 2, True, 287, 192, 5), (10, 2, True, 287, 197, 5),
                      (20, 3, False, 287, 0, 5)], dtype=dtype)
      data = np.zeros((5, 64), dtype=np.uint8)
      data[:, 0] = np.arange(5)
      np.savez(path, can=can, can_data=data)
      cache = {"segments": [{"cache_path": str(path), "counts": {"can": 5, "sendcan": 7}, "original_can_events": 2}]}
      counts = Counter()
      events = list(ordered_can_events(cache, counts))
      self.assertEqual(len(events), 2)
      self.assertEqual(events[0].frames, [(287, bytes(5), 0)])
      self.assertEqual(events[1].frames, [(287, bytes([4, 0, 0, 0, 0]), 0)])
      self.assertFalse(events[1].valid)
      self.assertEqual(counts["selected_raw_src_0_frames"], 2)
      self.assertEqual(counts["sendcan_frames_excluded"], 7)
      can[4]["t_ns"] = 9
      np.savez(path, can=can, can_data=data)
      with self.assertRaisesRegex(ValueError, "regressed"):
        list(ordered_can_events(cache, Counter()))

  def test_missing_empty_event_is_rejected_instead_of_silently_dropped(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "segment-000.npz"
      dtype = [("t_ns", "<u8"), ("event_index", "<u4"), ("valid", "?"),
               ("address", "<u4"), ("src", "<u2"), ("length", "u1")]
      np.savez(path, can=np.empty(0, dtype=dtype), can_data=np.empty((0, 64), dtype=np.uint8))
      cache = {"segments": [{"cache_path": str(path), "counts": {"can": 0, "sendcan": 0}, "original_can_events": 1}]}
      with self.assertRaisesRegex(ValueError, "coverage mismatch"):
        list(ordered_can_events(cache, Counter()))

  def test_pending_events_cross_segments_and_tail_is_not_dropped(self):
    class Decoder:
      def __init__(self):
        self.value = None
        self.fed = 0

      def ingest_batch(self, t_ns, frames, event_valid=True):
        self.fed += 1
        for _, payload, _ in frames:
          self.value = payload[0]

      def snapshot(self, t_ns):
        unknown = {"brakePressed": None, "gasPressed": None, "cruiseState.standstill": None,
                   "cruiseState.enabled": None, "cruiseState.available": None}
        return types.SimpleNamespace(timestamp_ns=t_ns,
          values={"steeringAngleDeg": self.value, **unknown},
          quality={"steeringAngleDeg": "available", **{name: "unconfirmed" for name in unknown}},
          to_opendbc=lambda: types.SimpleNamespace(canValid=False, brakePressed=False, gasPressed=False,
            cruiseState=types.SimpleNamespace(standstill=False, enabled=False, available=False)))

      def diagnostics(self):
        return {"fed": self.fed}

    module = types.ModuleType("songplus_readonly.carstate")
    module.SongPlusReadonlyCarState = Decoder
    module.FIELD_EVIDENCE = {"steeringAngleDeg": {"tier": "supported_encoding"},
                             "brakePressed": {"tier": "unconfirmed"}}
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      can_dtype = [("t_ns", "<u8"), ("event_index", "<u4"), ("valid", "?"),
                   ("address", "<u4"), ("src", "<u2"), ("length", "u1")]
      state_dtype = [("t_ns", "<u8"), ("event_index", "<u4"), ("valid", "?"),
                     ("canValid", "<f8"), ("steeringAngleDeg", "<f8"), ("brakePressed", "<f8")]
      definitions = [([(10, 1, 1), (20, 2, 2), (25, 4, 3), (40, 6, 4)],
                      [(15, 3, True, 1, 1, 0), (30, 5, True, 1, 3, 0)]),
                     ([(50, 2, 5)], [(35, 1, True, 1, 3, 0)])]
      entries = []
      for segment, (can_defs, state_defs) in enumerate(definitions):
        can = np.array([(t, index, True, 287, 0, 5) for t, index, value in can_defs], dtype=can_dtype)
        data = np.zeros((len(can), 64), dtype=np.uint8)
        data[:, 0] = [value for t, index, value in can_defs]
        states = np.array(state_defs, dtype=state_dtype)
        path = root / f"segment-{segment:03d}.npz"
        np.savez(path, can=can, can_data=data, carState=states)
        entries.append({"cache_path": str(path), "counts": {"can": len(can), "sendcan": 0, "carState": len(states)},
                        "original_can_events": len(can)})
      with patch.dict("sys.modules", {"songplus_readonly.carstate": module}), \
           patch("songplus_readonly.replay.LAB", root), contextlib.redirect_stdout(io.StringIO()):
        result = replay({"segments": entries}, {})
      self.assertEqual(result["fields"]["steeringAngleDeg"]["equal_within_tolerance"], 3)
      self.assertEqual(result["fields"]["brakePressed"]["prototype_unavailable"], 3)
      self.assertEqual(result["counts"]["can_events_after_final_old_state"], 2)
      self.assertEqual(result["decoder_diagnostics"]["fed"], 5)
      self.assertEqual(result["final_snapshot"]["values"]["steeringAngleDeg"], 5)
      self.assertEqual(sum(item["omitted_can_events"] for item in result["can_event_coverage"]), 0)
      self.assertEqual(result["reserved_acc_unknown"]["snapshots_checked"], 4)
      self.assertEqual(result["official_projection"]["samples_checked"], 3)
      with patch.dict("sys.modules", {"songplus_readonly.carstate": module}), \
           patch("songplus_readonly.replay.LAB", root), contextlib.redirect_stdout(io.StringIO()), \
           patch("songplus_readonly.replay.runtime_source_hashes", side_effect=[{"a": "before"}, {"a": "after"}]):
        with self.assertRaisesRegex(ValueError, "source changed"):
          replay({"segments": entries}, {})


class TestProvenance(unittest.TestCase):
  def fixture(self, root: Path):
    def write(path, obj):
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(json.dumps(obj))
      return sha256(path)
    extractor = root / "offline-analysis/extract_route.py"
    extractor.parent.mkdir()
    extractor.write_text("# fixture extractor")
    schema = root / "schema.capnp"
    schema.write_text("fixture schema")
    rlog = root / "route--0/rlog.zst"
    rlog.parent.mkdir()
    rlog.write_bytes(b"fixture strictly validated rlog")
    cached = root / "cache/segment-000.npz"
    cached.parent.mkdir()
    cached.write_bytes(b"fixture cache")
    export = root / "export.json"
    export_hash = write(export, {"route": "route"})
    report = root / "validation.json"
    hashes = {str(schema): sha256(schema)}
    report_hash = write(report, {"schema": "c3xl-rlog-content-validation/v1", "route": "route",
      "status": "content_valid", "files_failed": 0, "files_passed": 1,
      "source_report": str(export), "source_report_sha256": export_hash, "schema_files_sha256": hashes,
      "files": [{"path": str(rlog), "sha256": sha256(rlog), "bytes": rlog.stat().st_size,
                 "events": 3, "event_types": {"can": 1}, "status": "content_valid"}]})
    manifest = cached.parent / "manifest.json"
    manifest_hash = write(manifest, {"schema": "song-local-rlog-cache/v1", "raw_src_preserved": True,
      "event_order_preserved": True, "source_report": str(report), "source_report_sha256": report_hash,
      "schema_sha256": hashes, "extractor_sha256": sha256(extractor), "segments": [
        {"source_path": str(rlog), "source_sha256": sha256(rlog), "source_events": 3,
         "cache_path": str(cached), "cache_sha256": sha256(cached)}]})
    candidate = root / "candidates.json"
    candidate_hash = write(candidate, {})
    analysis = root / "analysis.json"
    write(analysis, {"schema": "song-rlog-first-analysis/v1", "route": "route", "input_segments": 1,
      "cache_manifest_sha256": manifest_hash, "files": {
        "cache/manifest.json": {"path": str(manifest), "sha256": manifest_hash},
        "state-candidates-final.json": {"path": str(candidate), "sha256": candidate_hash}}})
    return analysis, rlog, cached

  def test_rehashes_original_and_cache(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      analysis, rlog, cached = self.fixture(root)
      with patch("songplus_readonly.replay.LAB", root):
        _, provenance = verify_inputs(analysis)
        self.assertEqual(provenance["rlog_hashes_verified"], 1)
        saved = rlog.read_bytes()
        rlog.write_bytes(b"x" * len(saved))
        with self.assertRaisesRegex(ValueError, "Original rlog"):
          verify_inputs(analysis)
        rlog.write_bytes(saved)
        cached.write_bytes(b"changed cache")
        with self.assertRaisesRegex(ValueError, "Cache hash"):
          verify_inputs(analysis)


class TestCandidateResearchProvenance(unittest.TestCase):
  def fixture(self, root):
    base = "results/songplus-pressed-acc-20261018/"
    files = {}

    def save(relative, obj):
      path = root / relative
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
      files[relative] = sha256(path)
      return path

    old = save("before/replay.py", "# preserved old verifier")
    sources = {"pedal-pressed.json": "offline-analysis/pedal_pressed.py",
               "brake-bit-screen.json": "offline-analysis/brake_bit_screen.py",
               "cruise-bit-screen.json": base + "cruise-bit-screen-source-before-metadata-correction.py",
               "cruise-bit-holdout.json": base + "cruise-bit-screen-source-before-metadata-correction.py"}
    for source in sources.values():
      save(source, "# historical research source")
    provenance = {key: "same recorded input" for key in ("analysis_manifest_sha256", "cache_manifest_sha256",
      "source_content_validation_sha256", "export_report_sha256", "extractor_sha256", "schema_sha256", "route", "segments")}
    for name, source in sources.items():
      report = {"script_sha256": files[source], "provenance": provenance}
      if name == "pedal-pressed.json":
        report["rules"] = {"gas_raw_gt_3": {}, "extra_bit_123_43_0": {}}
      if name == "cruise-bit-screen.json":
        report["segment_partition"] = [0, 24]
      if name == "cruise-bit-holdout.json":
        report.update({"segment_partition": [25, 97], "fixed_candidates": [{"target": "cruiseState.standstill",
          "raw_src": 0, "address": "0x220", "little_endian_bit_index": 8, "inverted": False}]})
      save(base + name, report)
    save(base + "cruise-bit-screen-metadata-correction.json", {
      "original_source": {"sha256": files[sources["cruise-bit-screen.json"]]},
      "reports": [{"path": str(root / (base + name)), "sha256": files[base + name]}
                  for name in ("cruise-bit-screen.json", "cruise-bit-holdout.json")]})
    manifest = save(base + "verification.json", {"result": "pass",
      "runtime_source_sha256": {"songplus_readonly/replay.py": sha256(old)}, "file_sha256": dict(files)})
    return provenance, manifest, old

  def test_historical_verifier_uses_preserved_source_and_rejects_input_drift(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      provenance, manifest, old = self.fixture(root)
      with patch("songplus_readonly.replay.LAB", root):
        result = verify_candidate_research(provenance, manifest, old)
        self.assertTrue(result["verified"])
        self.assertEqual(result["historical_replay_source_sha256"], sha256(old))
        with self.assertRaisesRegex(ValueError, "input chain differs"):
          verify_candidate_research({**provenance, "route": "another route"}, manifest, old)
        old.write_text("changed preserved source")
        with self.assertRaisesRegex(ValueError, "Historical replay source"):
          verify_candidate_research(provenance, manifest, old)

  def test_historical_script_tamper_is_rejected_without_execution(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      provenance, manifest, old = self.fixture(root)
      (root / "offline-analysis/pedal_pressed.py").write_text("raise RuntimeError('must not execute')")
      with patch("songplus_readonly.replay.LAB", root):
        with self.assertRaisesRegex(ValueError, "evidence hash mismatch"):
          verify_candidate_research(provenance, manifest, old)


if __name__ == "__main__":
  unittest.main()
