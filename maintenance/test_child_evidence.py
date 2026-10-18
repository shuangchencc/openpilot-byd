"""Child result tampering tests using a synthetic, nonexecuting schema fixture."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance.child_evidence import validate_child_evidence
from maintenance.core import HOST, PANDA, sha, write_json


def fixture(root):
  packed = json.loads(Path(__file__).with_name("fixtures").joinpath("child-evidence-shape.json").read_text())

  def expand(value):
    if type(value) is dict:
      if set(value) == {"$fixture_ref"}:
        return expand(packed["shared"][value["$fixture_ref"]])
      return {key: expand(item) for key, item in value.items()}
    if type(value) is list:
      return [expand(item) for item in value]
    return value

  shape = expand(packed["root"])
  shape["host_manifest"]["baseline_inputs"].update({
    "upstream-openpilot/" + name: digest for name, digest in shape["p6"]["daemon_macos"]["source_files_sha256"].items()
  })
  for name, value in ((HOST, shape["host_manifest"]), (PANDA, shape["panda_manifest"])):
    write_json(root / name, value)
  for name in shape["host_manifest"]["validation_tools"]:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(shape["tool_payload"])
  p5, p6 = shape["p5"], shape["p6"]
  p5["manifest_sha256"] = p6["p5_manifest_sha256"] = sha(root / PANDA)
  p6["manifest_sha256"] = sha(root / HOST)
  return p5, p6


class ChildEvidenceTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix="songplus-child-contract-")
    self.root = Path(self.temp.name).resolve()
    self.p5, self.p6 = fixture(self.root)

  def tearDown(self):
    self.temp.cleanup()

  def check(self):
    return validate_child_evidence(self.p5, self.p6, self.root)

  def test_complete_synthetic_projection_reports_exact_counts_without_execution(self):
    with patch("subprocess.run", side_effect=AssertionError("must not execute")):
      counts = self.check()
    self.assertEqual(counts["p5"]["arm_app_builds"], 4)
    self.assertEqual(counts["p5"]["protocol_groups"], 30)
    self.assertEqual(counts["p6"]["python_tests"], 94)
    self.assertEqual(counts["p6"]["per_platform"]["native_groups_total"], 222)
    self.assertEqual(counts["p6"]["per_platform"]["safety_assertions"], 5086)

  def test_empty_or_malformed_reports_cannot_pass(self):
    for p5, p6 in (({}, {}), (None, self.p6), (self.p5, []), (self.p5, {"status": "PASS_OFFLINE_HOST_ONLY"})):
      with self.assertRaises(ValueError):
        validate_child_evidence(p5, p6, self.root)

  def test_summary_counts_false_zero_and_nonzero_failures_rejected(self):
    original = copy.deepcopy(self.p6)
    for field, value in (("tests_run", 0), ("tests_run", 93), ("failures", False), ("errors", 1), ("skipped", 1)):
      self.p6 = copy.deepcopy(original)
      self.p6["python_tests"][field] = value
      with self.assertRaises(ValueError):
        self.check()

  def test_each_arm_build_is_required(self):
    original = copy.deepcopy(self.p5)
    for name in ("f4-debug", "f4-release", "h7-debug", "h7-release"):
      self.p5 = copy.deepcopy(original)
      del self.p5["reports"][name]
      with self.assertRaises(ValueError):
        self.check()

  def test_source_protocol_or_signature_drift_rejected(self):
    original = copy.deepcopy(self.p5)
    for field, value in (("source_identity", "0" * 64), ("version", "P5-other"), ("signature_key", "release-key"),
                         ("protocol_inputs", {}), ("board_inputs", {}), ("safety_inputs", {})):
      self.p5 = copy.deepcopy(original)
      self.p5["reports"]["f4-debug"]["build"][field] = value
      with self.assertRaises(ValueError):
        self.check()

  def test_missing_duplicate_and_failed_commands_rejected(self):
    original = copy.deepcopy(self.p6)
    for action in ("missing", "duplicate", "failed"):
      self.p6 = copy.deepcopy(original)
      rows = self.p6["commands"]
      if action == "missing":
        rows.pop()
      elif action == "duplicate":
        rows[-1] = copy.deepcopy(rows[0])
      else:
        rows[0]["exit_code"] = 1
      with self.assertRaises(ValueError):
        self.check()

  def test_negative_compile_must_fail_and_positive_must_succeed(self):
    rows = self.p5["reports"]["protocol"]["commands"]
    negative = next(row for row in rows if row["name"].startswith("negative-"))
    negative["exit_code"] = 0
    with self.assertRaises(ValueError):
      self.check()

  def test_omitted_or_duplicated_native_group_cannot_hide_behind_total(self):
    original = copy.deepcopy(self.p6)
    for platform in ("macos", "linux"):
      self.p6 = copy.deepcopy(original)
      rows = self.p6["native_" + platform]["suites"]["p6_faults"]["groups"]
      rows[-1] = rows[0]
      with self.assertRaises(ValueError):
        self.check()

  def test_real_linux_build_cannot_be_replaced_by_macos_fixture(self):
    self.p6["daemon_linux"]["actual_main_cc_compiled_and_linked"] = False
    with self.assertRaises(ValueError):
      self.check()

  def test_safety_case_and_parity_require_executed_unique_evidence(self):
    original = copy.deepcopy(self.p6)
    for action in ("omit-case", "skip-case", "zero-assertions", "omit-parity", "failed-parity"):
      self.p6 = copy.deepcopy(original)
      safety = self.p6["p6_safety"]["linux"]
      if action == "omit-case":
        safety["configurations"][0]["cases"].pop()
      elif action == "skip-case":
        safety["configurations"][0]["cases"][0]["skipped"] = 1
      elif action == "zero-assertions":
        safety["configurations"][0]["cases"][0]["assertions"] = 0
      elif action == "omit-parity":
        safety["parity_comparisons"].pop()
      else:
        safety["parity_comparisons"][0]["equal"] = False
      with self.assertRaises(ValueError):
        self.check()

  def test_source_and_tool_evidence_is_bound_to_current_manifest(self):
    self.p6["daemon_linux"]["source_files_sha256"]["openpilot/selfdrive/pandad/main.cc"] = "0" * 64
    with self.assertRaises(ValueError):
      self.check()

  def test_scope_and_cereal_claims_cannot_be_promoted(self):
    original = copy.deepcopy(self.p6)
    for key in ("device_access", "real_transport_executed", "MCU_build", "deployment_ready"):
      self.p6 = copy.deepcopy(original)
      self.p6[key] = True
      with self.assertRaises(ValueError):
        self.check()
    self.p6 = copy.deepcopy(original)
    self.p6["runtime_message_oracles"]["linux"]["native_cereal_messages"] = 0
    with self.assertRaises(ValueError):
      self.check()


if __name__ == "__main__":
  unittest.main(verbosity=2)
