"""Standard-library-only contract/fault tests; no private logs or runtime imports."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from maintenance.vehicle_readonly import (DENIED, LOCK, OFFLINE_FILES, ROOT, RUNTIME_FILES,
                                        canonical_hash, create_output_directory, read_json, sha256, verify_contract,
                                        verify_replay_report, write_new)


class SourceContractTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name).resolve()
    for name in RUNTIME_FILES + OFFLINE_FILES + (LOCK,):
      target = self.root / name
      target.parent.mkdir(parents=True, exist_ok=True)
      shutil.copyfile(ROOT / name, target)

  def change_lock(self, mutate):
    path = self.root / LOCK
    data = read_json(path)
    mutate(data)
    path.write_text(json.dumps(data))

  def change_source(self, name, text):
    path = self.root / name
    path.write_text(text)
    self.change_lock(lambda lock: lock["versioned_files_sha256"].__setitem__(name, sha256(path)))

  def test_source_only_without_dependencies_or_raw_logs(self):
    result = verify_contract(self.root)
    self.assertEqual(result["source_files"], 13)
    self.assertEqual(result["evidence_tiers"], {"supported_encoding": 5, "empirical_candidate": 10, "unconfirmed": 17})
    self.assertFalse(result["raw_logs_read"])
    self.assertFalse(result["replay_executed"])
    self.assertFalse((self.root / "upstream-openpilot").exists())

  def test_source_hash_drift(self):
    (self.root / RUNTIME_FILES[2]).write_text("# changed")
    with self.assertRaisesRegex(ValueError, "source hash"):
      verify_contract(self.root)

  def test_unmanifested_runtime_source(self):
    (self.root / "songplus_readonly/unreviewed.py").write_text("# hidden dependency")
    with self.assertRaisesRegex(ValueError, "unaccounted"):
      verify_contract(self.root)

  def test_missing_dbc(self):
    (self.root / RUNTIME_FILES[-1]).unlink()
    with self.assertRaisesRegex(ValueError, "source missing"):
      verify_contract(self.root)

  def test_symlink_source_rejected(self):
    path = self.root / RUNTIME_FILES[0]
    other = self.root / "alternate"
    shutil.copyfile(path, other)
    path.unlink()
    path.symlink_to(other)
    with self.assertRaisesRegex(ValueError, "symlink"):
      verify_contract(self.root)

  def test_parent_symlink_source_rejected(self):
    target = self.root / "songplus_readonly/dbc"
    alternative = self.root / "alternative-dbc"
    target.rename(alternative)
    target.symlink_to(alternative, target_is_directory=True)
    with self.assertRaisesRegex(ValueError, "symlink"):
      verify_contract(self.root)

  def test_duplicate_json_keys_rejected(self):
    path = self.root / LOCK
    path.write_text('{"schema":"first","schema":"second"}')
    with self.assertRaisesRegex(ValueError, "duplicate JSON"):
      verify_contract(self.root)

  def test_nonfinite_json_values_rejected(self):
    path = self.root / LOCK
    path.write_text('{"value": NaN}')
    with self.assertRaisesRegex(ValueError, "nonfinite JSON"):
      read_json(path)

  def test_new_output_directory_required(self):
    output = self.root / "new/nested"
    self.assertEqual(create_output_directory(output), output)
    with self.assertRaises(FileExistsError):
      create_output_directory(output)

  def test_symlink_output_parent_rejected(self):
    alias = self.root / "alias"
    alias.symlink_to(self.root, target_is_directory=True)
    with self.assertRaisesRegex(ValueError, "symlink"):
      create_output_directory(alias / "output")

  def test_official_pin_drift(self):
    self.change_lock(lambda lock: lock.__setitem__("opendbc_commit", "0" * 40))
    with self.assertRaisesRegex(ValueError, "pin changed"):
      verify_contract(self.root)

  def test_permission_escalation(self):
    self.change_lock(lambda lock: lock["permissions"].__setitem__("real_can_tx", True))
    with self.assertRaisesRegex(ValueError, "permission"):
      verify_contract(self.root)

  def test_integer_permissions_are_not_booleans(self):
    self.change_lock(lambda lock: lock["permissions"].__setitem__("real_can_tx", 0))
    with self.assertRaisesRegex(ValueError, "permission"):
      verify_contract(self.root)

  def test_canvalid_promotion_even_after_hash_update(self):
    name = "songplus_readonly/carstate.py"
    self.change_source(name, (self.root / name).read_text().replace("ret.canValid = False", "ret.canValid = True"))
    with self.assertRaisesRegex(ValueError, "canValid"):
      verify_contract(self.root)

  def test_runtime_must_not_depend_on_research(self):
    name = "songplus_readonly/runtime.py"
    self.change_source(name, (self.root / name).read_text() + "\nimport songplus_readonly.replay\n")
    with self.assertRaisesRegex(ValueError, "research or hardware dependency"):
      verify_contract(self.root)

  def test_runtime_must_not_import_panda(self):
    name = "songplus_readonly/runtime.py"
    self.change_source(name, (self.root / name).read_text() + "\nfrom panda import Panda\n")
    with self.assertRaisesRegex(ValueError, "research or hardware dependency"):
      verify_contract(self.root)

  def test_field_confirmation_is_not_allowed(self):
    name = "songplus_readonly/field-evidence.json"
    evidence = read_json(self.root / name)
    evidence["fields"]["gasPressed"]["vehicle_confirmed"] = True
    self.change_source(name, json.dumps(evidence))
    with self.assertRaisesRegex(ValueError, "field promoted"):
      verify_contract(self.root)

  def test_dbc_message_extension_requires_explicit_review(self):
    name = RUNTIME_FILES[-1]
    self.change_source(name, (self.root / name).read_text() + "\nBO_ 777 UNKNOWN: 8 XXX\n")
    with self.assertRaisesRegex(ValueError, "receive DBC inventory"):
      verify_contract(self.root)

  def test_segment_gap_rejected(self):
    self.change_lock(lambda lock: lock["external_evidence"]["segments"].pop())
    with self.assertRaisesRegex(ValueError, "segment coverage"):
      verify_contract(self.root)

  def test_evidence_path_escape_rejected(self):
    self.change_lock(lambda lock: lock["external_evidence"]["file_sha256"].__setitem__("../private", "0" * 64))
    with self.assertRaisesRegex(ValueError, "bounded"):
      verify_contract(self.root)

  def test_raw_logs_never_enter_runtime_boundary(self):
    self.change_lock(lambda lock: lock["external_evidence"].__setitem__("research_runtime_dependency", True))
    with self.assertRaisesRegex(ValueError, "raw/research boundary"):
      verify_contract(self.root)

  def test_existing_report_is_never_overwritten(self):
    path = self.root / "report.json"
    write_new(path, {"first": True})
    with self.assertRaises(FileExistsError):
      write_new(path, {"second": True})
    self.assertEqual(read_json(path), {"first": True})


class ReplayReportTests(unittest.TestCase):
  def setUp(self):
    # These synthetic records test validator branches, never impersonate a run.
    self.lock = read_json(ROOT / LOCK)
    external = self.lock["external_evidence"]
    self.fields = {"synthetic": True}
    self.lock["historical_replay_expectations"]["fields_sha256"] = canonical_hash(self.fields)
    reference = self.lock["historical_replay_expectations"]
    self.report = {
      "schema": "songplus-readonly-carstate-replay/v1", "status": "completed_offline_replay",
      **dict.fromkeys(("vehicle_semantics_independently_confirmed", "actuation_allowed", "network_or_device_access", "panda_safety_validated"), False),
      "provenance": {"segments": copy.deepcopy(external["segments"]), "rlog_hashes_verified": 98, "cache_hashes_verified": 98,
                     **external["provenance_sha256"], "schema_sha256": external["source_schema_sha256"]},
      "candidate_research": {"verified": True, "input_chain_matches_current_route_and_caches": True, **external["candidate_research"]},
      "runtime_source_unchanged_during_replay": True,
      "runtime_source_sha256": {n: h for n, h in self.lock["versioned_files_sha256"].items()
                                if n.endswith((".py", ".dbc")) and "/tests/" not in n},
      "counts": copy.deepcopy(reference["counts"]), "fields": self.fields,
      "can_event_coverage": [{"omitted_can_events": 0} for _ in range(98)],
      "official_projection": {"canValid": False, "unknown_defaults_usable": False, "nullable_sidecar_required": True,
                              "samples_checked": reference["projection_samples"]},
      "reserved_acc_unknown": {"all_checked_values_none": True, "quality_always_unconfirmed": True,
                               "snapshots_checked": reference["unknown_snapshots"]},
    }

  def test_complete_synthetic_report_is_accepted(self):
    self.assertEqual(verify_replay_report(self.report, self.lock)["status"], "PASS_REPLAY_REGRESSION")

  def test_missing_rehashed_input_is_rejected(self):
    self.report["provenance"]["rlog_hashes_verified"] = 97
    with self.assertRaisesRegex(ValueError, "unverified input"):
      verify_replay_report(self.report, self.lock)

  def test_omitted_event_is_rejected(self):
    self.report["can_event_coverage"][0]["omitted_can_events"] = 1
    with self.assertRaisesRegex(ValueError, "coverage loss"):
      verify_replay_report(self.report, self.lock)

  def test_changed_candidate_reference_is_rejected(self):
    self.report["candidate_research"]["manifest_sha256"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "candidate research changed"):
      verify_replay_report(self.report, self.lock)

  def test_changed_raw_evidence_is_rejected(self):
    self.report["provenance"]["segments"][1]["rlog_sha256"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "segment identity"):
      verify_replay_report(self.report, self.lock)

  def test_schema_drift_is_rejected(self):
    self.report["provenance"]["schema_sha256"] = {"log.capnp": "0" * 64}
    with self.assertRaisesRegex(ValueError, "schema identity"):
      verify_replay_report(self.report, self.lock)

  def test_source_drift_is_rejected(self):
    self.report["runtime_source_unchanged_during_replay"] = False
    with self.assertRaisesRegex(ValueError, "source changed"):
      verify_replay_report(self.report, self.lock)

  def test_count_regression_is_rejected(self):
    self.report["counts"]["can_events_fed"] -= 1
    with self.assertRaisesRegex(ValueError, "count regression"):
      verify_replay_report(self.report, self.lock)

  def test_field_semantics_regression_is_rejected(self):
    self.report["fields"] = {"synthetic": "changed"}
    with self.assertRaisesRegex(ValueError, "comparison/evidence"):
      verify_replay_report(self.report, self.lock)

  def test_control_validity_promotion_is_rejected(self):
    self.report["official_projection"]["canValid"] = True
    with self.assertRaisesRegex(ValueError, "nullable/control"):
      verify_replay_report(self.report, self.lock)

  def test_acc_unknown_false_coercion_is_rejected(self):
    self.report["reserved_acc_unknown"]["all_checked_values_none"] = False
    with self.assertRaisesRegex(ValueError, "ACC unknowns"):
      verify_replay_report(self.report, self.lock)

  def test_vehicle_validation_claim_is_rejected(self):
    self.report["vehicle_semantics_independently_confirmed"] = True
    with self.assertRaisesRegex(ValueError, "scope escalation"):
      verify_replay_report(self.report, self.lock)


if __name__ == "__main__":
  unittest.main()
