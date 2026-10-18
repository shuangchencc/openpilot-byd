"""Synthetic release-envelope tests; no historical report is counted as a run.

Child test accounting and candidate inspection are deliberately mocked here;
their independent suites test real schemas/files. This suite exercises the
release entry's actual JSON/path/hash/time/context/case/conclusion binding on
temporary files. It neither compiles firmware nor validates any device.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance import validation
from maintenance.runtime_guard import PROTOCOL, REQUIRED_CASE_IDS, evaluate_release


class ReleaseEnvelopeTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name).resolve()
    self.run = self.root / "run"
    self.run.mkdir()
    self.report_path = self.run / "result.json"
    lock = self.root / validation.LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text('{"fixture": "synthetic metadata lock, verification mocked"}\n')
    self.lock_hash = validation.sha(lock)
    self.run_id = "p7-synthetic-envelope-fixture"
    self.timestamp = "2026-01-02T03:04:05+00:00"
    self.inspection = {"inspection_valid": True, "offline_artifacts_available": True,
                       "runtime_available": False, "scope": "synthetic inspection fixture"}
    self.evidence = {
      "p5": {"utc": self.timestamp, "scope": "synthetic child fixture; accounting mocked"},
      "p6": {"tested_at_utc": self.timestamp, "scope": "synthetic child fixture; accounting mocked"},
      "contract": {"completed_at_utc": self.timestamp, "scope": "synthetic contract fixture"},
      "repro": {"scope": "synthetic comparison fixture"},
      "source_replay": {"lock_sha256": self.lock_hash},
      "gates": {"positive": self.inspection},
      "second_builds": {"scope": "synthetic build fixture; no compiler invoked"},
      "artifacts": {"scope": "synthetic artifacts fixture; inspection mocked"},
    }
    registry = {}
    for name, value in self.evidence.items():
      path = self.run / "evidence" / f"{name}.json"
      validation.write_json(path, value)
      registry[name] = {"path": str(path.relative_to(self.run)), "sha256": validation.sha(path),
                        "run_id": self.run_id, "lock_sha256": self.lock_hash}
    self.expected_cases = [{"id": case, "status": "PASS", "executed": 1, "failed": 0, "skipped": 0,
                            "evidence_sha256": registry["p6"]["sha256"], "run_id": self.run_id,
                            "lock_sha256": self.lock_hash} for case in REQUIRED_CASE_IDS]
    gate = evaluate_release(self.expected_cases, evidence_context={"run_id": self.run_id, "lock_sha256": self.lock_hash})
    gate["deployment_blockers"] = gate["blockers"]
    command_names = ["ci-contract", "p5", "p6", "reproducibility"]
    command_names.extend(prefix + name for name in ("f4-debug", "f4-release", "h7-debug", "h7-release")
                         for prefix in ("rebuild-", "resources-"))
    commands = []
    for name in command_names:
      log = self.run / f"{name}.log"
      log.write_text(f"SYNTHETIC log fixture only: {name}; no command executed.\n")
      commands.append({"name": name, "argv": ["synthetic-not-executed", name], "exit_code": 0,
                       "log_sha256": validation.sha(log)})
    self.report = {"schema": "songplus-p7-offline-validation/v1", "status": "PASS_OFFLINE_COMPATIBILITY",
                   "run_id": self.run_id, "lock_sha256": self.lock_hash,
                   "started_at_utc": "2026-01-02T03:04:00+00:00", "completed_at_utc": "2026-01-02T03:04:10+00:00",
                   "device_access": False, "deployment_ready": False, "evidence": registry,
                   "acceptance": copy.deepcopy(self.expected_cases), "release": gate, "commands": commands}
    for name, value in (("ROOT", self.root), ("verify_lock", None), ("acceptance", None), ("inspect_runtime", None)):
      args = {"new": value} if name == "ROOT" else {}
      context = patch.object(validation, name, **args)
      mocked = context.start()
      self.addCleanup(context.stop)
      if name == "verify_lock":
        mocked.return_value = {"status": "PASS", "lock_sha256": self.lock_hash}
        self.lock_verifier = mocked
      elif name == "acceptance":
        mocked.side_effect = lambda *_args: copy.deepcopy(self.expected_cases)
        self.accounting = mocked
      elif name == "inspect_runtime":
        mocked.return_value = copy.deepcopy(self.inspection)
        self.inspector = mocked

  def check(self):
    validation.write_json(self.report_path, self.report)
    return validation.release_check(self.report_path)

  def mutate_evidence(self, name, mutator, *, refresh_registry=True):
    path = self.run / self.report["evidence"][name]["path"]
    value = validation.read_json(path)
    mutator(value)
    validation.write_json(path, value)
    if refresh_registry:
      self.report["evidence"][name]["sha256"] = validation.sha(path)

  def test_valid_synthetic_envelope_remains_nondeployable(self):
    result = self.check()
    self.assertTrue(result["offline_compatibility_complete"])
    self.assertEqual(result["offline_cases_passed"], 41)
    self.assertFalse(result["deployment_ready"])
    self.assertFalse(result["release_installable"])
    self.assertTrue(all(value is False for value in result["permissions"].values()))
    self.lock_verifier.assert_called_once_with(metadata_only=True)
    self.accounting.assert_called_once()
    self.inspector.assert_called_once()

  def test_metadata_lock_failure_is_not_ignored(self):
    self.lock_verifier.side_effect = ValueError("fixture lock drift")
    with self.assertRaisesRegex(ValueError, "fixture lock drift"):
      self.check()
    self.accounting.assert_not_called()

  def test_changed_report_lock_rejected(self):
    self.report["lock_sha256"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "another maintenance lock"):
      self.check()

  def test_evidence_hash_drift_rejected_before_accounting(self):
    self.mutate_evidence("p5", lambda d: d.update(tampered=True), refresh_registry=False)
    with self.assertRaisesRegex(ValueError, "Evidence hash mismatch"):
      self.check()
    self.accounting.assert_not_called()

  def test_missing_evidence_registry_entry_rejected(self):
    del self.report["evidence"]["repro"]
    with self.assertRaisesRegex(ValueError, "Evidence registry incomplete"):
      self.check()

  def test_unexpected_evidence_registry_entry_rejected(self):
    self.report["evidence"]["unknown"] = self.report["evidence"]["p5"]
    with self.assertRaisesRegex(ValueError, "Evidence registry incomplete"):
      self.check()

  def test_foreign_evidence_run_rejected(self):
    self.report["evidence"]["p5"]["run_id"] = "other-run"
    with self.assertRaisesRegex(ValueError, "Stale evidence context"):
      self.check()

  def test_foreign_evidence_lock_rejected(self):
    self.report["evidence"]["p5"]["lock_sha256"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "Stale evidence context"):
      self.check()

  def test_evidence_path_escape_rejected(self):
    self.report["evidence"]["p5"]["path"] = "../outside.json"
    with self.assertRaisesRegex(ValueError, "Unsafe relative path"):
      self.check()

  def test_evidence_symlink_rejected(self):
    path = self.run / self.report["evidence"]["p5"]["path"]
    moved = self.run / "p5-real.json"
    path.rename(moved)
    path.symlink_to(moved)
    with self.assertRaisesRegex(ValueError, "Symlink forbidden"):
      self.check()

  def test_duplicate_json_keys_rejected(self):
    path = self.run / self.report["evidence"]["p5"]["path"]
    path.write_text('{"utc":"one", "utc":"two"}')
    self.report["evidence"]["p5"]["sha256"] = validation.sha(path)
    with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
      self.check()

  def test_rehashed_historical_child_evidence_rejected(self):
    for name, field in (("p5", "utc"), ("p6", "tested_at_utc"), ("contract", "completed_at_utc")):
      with self.subTest(name=name):
        self.mutate_evidence(name, lambda d, field=field: d.update({field: "2025-01-01T00:00:00+00:00"}))
        with self.assertRaisesRegex(ValueError, "Historical evidence"):
          self.check()
        self.mutate_evidence(name, lambda d, field=field: d.update({field: self.timestamp}))

  def test_rehashed_after_run_child_evidence_rejected(self):
    self.mutate_evidence("p6", lambda d: d.update(tested_at_utc="2026-01-02T04:00:00+00:00"))
    with self.assertRaisesRegex(ValueError, "Historical evidence"):
      self.check()

  def test_reversed_execution_interval_rejected(self):
    self.report["started_at_utc"], self.report["completed_at_utc"] = self.report["completed_at_utc"], self.report["started_at_utc"]
    with self.assertRaisesRegex(ValueError, "Invalid execution interval"):
      self.check()

  def test_rehashed_replay_lock_mismatch_rejected(self):
    self.mutate_evidence("source_replay", lambda d: d.update(lock_sha256="0" * 64))
    with self.assertRaisesRegex(ValueError, "Replay lock mismatch"):
      self.check()

  def test_case_mapping_or_count_edit_rejected(self):
    self.report["acceptance"][0]["executed"] += 1
    with self.assertRaisesRegex(ValueError, "Case records differ"):
      self.check()

  def test_stored_case_evidence_binding_edit_rejected(self):
    self.report["acceptance"][0]["evidence_sha256"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "Case records differ"):
      self.check()

  def test_false_complete_accounting_rejected_by_real_release_gate(self):
    self.expected_cases.pop()
    self.report["acceptance"] = copy.deepcopy(self.expected_cases)
    with self.assertRaisesRegex(ValueError, "Offline acceptance incomplete"):
      self.check()

  def test_runtime_artifact_drift_is_not_ignored(self):
    self.inspector.return_value = {"inspection_valid": False, "offline_artifacts_available": False}
    with self.assertRaisesRegex(ValueError, "artifact bundle missing/drifted"):
      self.check()

  def test_changed_runtime_inspection_result_rejected(self):
    self.inspector.return_value["scope"] = "different comparison result"
    with self.assertRaisesRegex(ValueError, "Artifact gate changed"):
      self.check()

  def test_edited_stored_release_conclusion_rejected(self):
    self.report["release"]["deployment_ready"] = True
    with self.assertRaisesRegex(ValueError, "Stored release conclusion differs"):
      self.check()

  def test_report_cannot_claim_hardware_access(self):
    self.report["device_access"] = True
    with self.assertRaisesRegex(ValueError, "Invalid report scope"):
      self.check()

  def test_command_log_drift_rejected(self):
    (self.run / "p5.log").write_text("changed after execution")
    with self.assertRaises(ValueError):
      self.check()

  def test_missing_command_log_rejected(self):
    (self.run / "p5.log").unlink()
    with self.assertRaises((ValueError, OSError)):
      self.check()

  def test_empty_or_duplicate_command_record_rejected(self):
    commands = self.report["commands"]
    for invalid in ([], [commands[0]] * 12):
      with self.subTest(invalid=len(invalid)):
        self.report["commands"] = invalid
        with self.assertRaises(ValueError):
          self.check()

  def test_failed_top_level_command_rejected(self):
    self.report["commands"][0]["exit_code"] = 1
    with self.assertRaises(ValueError):
      self.check()


class ChildOutputParserTests(unittest.TestCase):
  def test_only_final_json_object_is_accepted(self):
    output = 'worker log {not JSON}\n{"result": "/fixture/result.json"}\n'
    self.assertEqual(validation.json_tail(output), {"result": "/fixture/result.json"})

  def test_trailing_junk_does_not_authenticate_earlier_json(self):
    with self.assertRaisesRegex(ValueError, "no final JSON object"):
      validation.json_tail('{"result": "/fixture/result.json"}\ntrailing junk')


def acceptance_fixture():
  """Compact synthetic accounting data; no logs/artifacts claim real execution."""
  command_names = ("metadata-lock", "help-root", "help-verify-lock", "help-replay", "help-validate",
                   "help-release-check", "help-inspect-runtime", "maintenance-tests", "ci-tests")
  contract = {"status": "PASS", "failed": 0, "skipped": 0, "passed_commands": len(command_names),
              "commands": [{"name": name, "returncode": 0, "expected_returncode": 0,
                            "tests_passed": 93 if name == "maintenance-tests" else 21 if name == "ci-tests" else 0}
                           for name in command_names]}
  p5_cases = {"A50", "A51", "A52", "A53", "A54", "A55"}
  p7_cases = {"A70", "A71", "A72"}
  p6 = {"acceptance_results": [{"id": name, "python_evidence": ["fixture-only"]}
                                for name in REQUIRED_CASE_IDS if name not in p5_cases | p7_cases]}
  evidence = {"p5": {"reports": {}}, "p6": p6, "contract": contract,
              "source_replay": {"status": "PASS", "bundles_equal": True, "trees_equal": True},
              "gates": {"status": "PASS", "positive": {"inspection_valid": True, "offline_artifacts_available": True,
                                                        "runtime_available": False}, "negative_count": 5,
                        "negative": [{"inspection_valid": False, "runtime_available": False, "reasons": ["fixture"]}
                                     for _ in range(5)]},
              "second_builds": {"status": "PASS", "builds": 4, "reports": {}, "commands": []},
              "repro": {"status": "PASS", "build_variants": 4, "candidate_builds_compared": 8, "results": {}},
              "artifacts": {"artifacts": []}}
  for name in ("f4-debug", "f4-release", "h7-debug", "h7-release"):
    target, policy = name.split("-")
    artifact_meta = {file: {"sha256": str(index + 1) * 64, "bytes": 100 + index}
                     for index, file in enumerate(("main.elf", "main.bin", "main.bin.signed"))}
    identity = {"source_identity": "a" * 64, "version": f"fixture-{name}", "compiler": "synthetic fixture",
                "protocol_inputs": {"fixture": "b" * 64}, "elf": {"fixture_only": True},
                "signature_verified": True, "artifacts": artifact_meta}
    left, right = copy.deepcopy(identity), copy.deepcopy(identity)
    for side, data in (("first", left), ("second", right)):
      data["provenance"] = {field: f"/fixture/{side}/{field}" for field in ("source_root", "opendbc_root", "output_root")}
    evidence["repro"]["results"][name] = {"first": left, "second": right, "core_candidate_bytes_equal": True,
                                         "byte_equal": {"main.bin": True, "main.bin.signed": True}}
    build = {"source_identity": identity["source_identity"],
             "artifacts": {file: item["sha256"] for file, item in artifact_meta.items()}}
    evidence["p5"]["reports"][name] = {"build": copy.deepcopy(build)}
    evidence["second_builds"]["reports"][name] = {"build": build, "resources": {"status": "PASS"}}
    evidence["second_builds"]["commands"].extend({"name": prefix + name, "exit_code": 0}
                                                  for prefix in ("rebuild-", "resources-"))
    for file, kind in (("main.elf", "firmware-elf"), ("main.bin", "firmware-bin"), ("main.bin.signed", "firmware-signed-bin")):
      meta = artifact_meta[file]
      evidence["artifacts"]["artifacts"].append({"path": name + "/" + file, "sha256": meta["sha256"],
        "size_bytes": meta["bytes"], "kind": kind, "mcu": target.upper(), "build_variant": policy,
        "protocol": PROTOCOL, "signature": "public-development-key" if file.endswith(".signed") else "unsigned", "deployable": False})
  registry = {key: {"sha256": "e" * 64} for key in evidence}
  return evidence, registry


class AcceptanceAccountingTests(unittest.TestCase):
  def setUp(self):
    self.evidence, self.registry = acceptance_fixture()
    mock = patch.object(validation, "child_contract", return_value=None)
    self.child_contract = mock.start()
    self.addCleanup(mock.stop)

  def check(self):
    return validation.acceptance(self.evidence, self.registry, "p7-accounting-fixture", "f" * 64)

  def test_complete_synthetic_accounting_positive_control(self):
    cases = self.check()
    self.assertEqual({case["id"] for case in cases}, set(REQUIRED_CASE_IDS))
    self.assertEqual(len(cases), 41)
    self.assertTrue(all(case["executed"] > 0 for case in cases))
    self.child_contract.assert_called_once()

  def test_empty_or_duplicate_ci_commands_rejected(self):
    commands = self.evidence["contract"]["commands"]
    for invalid in ([], [commands[0]] * 9):
      with self.subTest(invalid=len(invalid)):
        self.evidence["contract"]["commands"] = invalid
        with self.assertRaisesRegex(ValueError, "CI commands incomplete"):
          self.check()

  def test_empty_maintenance_execution_rejected(self):
    for command in self.evidence["contract"]["commands"]:
      command["tests_passed"] = 0
    with self.assertRaisesRegex(ValueError, "Maintenance/CI tests incomplete"):
      self.check()

  def test_source_tree_mismatch_rejected(self):
    self.evidence["source_replay"]["trees_equal"] = False
    with self.assertRaisesRegex(ValueError, "Source replay mismatch"):
      self.check()

  def test_empty_negative_list_cannot_claim_five_cases(self):
    self.evidence["gates"]["negative"] = []
    with self.assertRaisesRegex(ValueError, "Runtime negative gate missing"):
      self.check()

  def test_negative_without_failure_reason_rejected(self):
    self.evidence["gates"]["negative"][0]["reasons"] = []
    with self.assertRaisesRegex(ValueError, "Runtime negative gate missing"):
      self.check()

  def test_empty_or_duplicate_second_build_commands_rejected(self):
    commands = self.evidence["second_builds"]["commands"]
    for invalid in ([], [commands[0]] * 8):
      with self.subTest(invalid=len(invalid)):
        self.evidence["second_builds"]["commands"] = invalid
        with self.assertRaisesRegex(ValueError, "Second compilation incomplete"):
          self.check()

  def test_failed_second_build_rejected(self):
    self.evidence["second_builds"]["commands"][0]["exit_code"] = 1
    with self.assertRaisesRegex(ValueError, "Second compilation incomplete"):
      self.check()

  def test_missing_second_build_report_rejected(self):
    del self.evidence["second_builds"]["reports"]["f4-debug"]
    with self.assertRaisesRegex(ValueError, "Second build set incomplete"):
      self.check()

  def test_empty_repro_results_cannot_claim_eight_builds(self):
    self.evidence["repro"]["results"] = {}
    with self.assertRaisesRegex(ValueError, "comparison set incomplete"):
      self.check()

  def test_unequal_raw_not_overridden_by_top_level_pass(self):
    self.evidence["repro"]["results"]["f4-debug"]["byte_equal"]["main.bin"] = False
    with self.assertRaisesRegex(ValueError, "Candidate bytes differ"):
      self.check()

  def test_reused_rebuild_source_directory_rejected(self):
    result = self.evidence["repro"]["results"]["f4-debug"]
    result["second"]["provenance"]["source_root"] = result["first"]["provenance"]["source_root"]
    with self.assertRaisesRegex(ValueError, "directory reused"):
      self.check()

  def test_signature_verification_cannot_be_omitted(self):
    self.evidence["repro"]["results"]["f4-debug"]["second"]["signature_verified"] = False
    with self.assertRaisesRegex(ValueError, "Signature verification missing"):
      self.check()

  def test_first_build_hash_must_match_comparison(self):
    self.evidence["p5"]["reports"]["f4-debug"]["build"]["artifacts"]["main.bin"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "First/second artifact mismatch"):
      self.check()

  def test_second_build_hash_must_match_comparison(self):
    self.evidence["second_builds"]["reports"]["f4-debug"]["build"]["artifacts"]["main.bin"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "Second artifact metadata mismatch"):
      self.check()

  def test_missing_retained_artifact_rejected(self):
    self.evidence["artifacts"]["artifacts"].pop()
    with self.assertRaisesRegex(ValueError, "compiled artifact set"):
      self.check()

  def test_wrong_retained_artifact_mcu_rejected(self):
    self.evidence["artifacts"]["artifacts"][0]["mcu"] = "H7"
    with self.assertRaisesRegex(ValueError, "compiled artifact set"):
      self.check()


if __name__ == "__main__":
  unittest.main()
