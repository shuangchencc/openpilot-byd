"""Integration CI must preserve isolation and reject incomplete release evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools import check_songplus_integration_ci as ci


GATE = {"status": "BLOCKED_FOR_DEPLOYMENT", "offline_integration_complete": True,
        "offline_compatibility_complete": True, "deployment_ready": False,
        "runtime_resource_proof_complete": False, "deployment_blockers": ["physical-identity"]}


class IntegrationCITests(unittest.TestCase):
  def test_committed_workflow_matches_contract(self):
    ci.check_workflow(ci.ROOT)

  def test_workflow_refuses_permission_isolation_or_runner_drift(self):
    mutations = (("contents: read", "contents: write"), (" --net --mount", " --mount"),
                 ("== 'workflow_dispatch'", "== 'pull_request'"),
                 ("    environment: panda-offline-maintainers\n", ""),
                 (ci.parent.CHECKOUT_SHA, "v6"),
                 ("group: panda-prepared-offline", "group: other-runner"))
    for old, new in mutations:
      with tempfile.TemporaryDirectory() as tmp, self.subTest(mutation=old):
        root = Path(tmp)
        path = root / ci.WORKFLOW
        path.parent.mkdir(parents=True)
        path.write_text(ci.workflow_text().replace(old, new))
        with self.assertRaises(ValueError):
          ci.check_workflow(root)

  def test_source_contract_does_not_execute_build_or_vehicle_replay(self):
    calls = []
    ci.contract_commands(lambda name, argv: calls.append((name, argv)), "python")
    self.assertEqual(len(calls), 8)
    self.assertEqual(calls[0][1][-2:], ["verify-lock", "--metadata-only"])
    for _, argv in calls:
      self.assertFalse("validate" in argv and "--help" not in argv)
    self.assertEqual([c[0] for c in calls[-2:]], ["maintenance-tests", "integration-ci-tests"])

  def run_pipeline(self, gate, failure=None):
    calls = []
    def run(name, argv, expected=0):
      calls.append((name, argv, expected))
      if name == failure:
        raise ValueError("fixture failure")
      return json.dumps(gate) if name == "release-check" else ""
    return run, calls

  def test_full_pipeline_accepts_only_valid_blocked_gate(self):
    run, calls = self.run_pipeline(GATE)
    self.assertEqual(ci.full_commands(run, "python", Path("/results"), "vm", "/home/user/deps"), GATE)
    self.assertEqual([c[0] for c in calls], ["verify-lock", "validate", "release-check"])
    self.assertEqual(calls[-1][2], 3)
    self.assertEqual(calls[-1][1][-1], "/results/validation/result.json")

  def test_incomplete_or_deployable_gate_fails(self):
    bad = [{}, [], {**GATE, "deployment_blockers": []}, {**GATE, "deployment_blockers": [""]}]
    for key in GATE:
      item = copy.deepcopy(GATE)
      del item[key]
      bad.append(item)
    for key in ("offline_integration_complete", "offline_compatibility_complete", "deployment_ready", "runtime_resource_proof_complete"):
      for value in (None, 0, 1, "false", not GATE[key]):
        bad.append({**GATE, key: value})
    for gate in bad:
      with self.subTest(gate=gate), self.assertRaises(ValueError):
        ci.full_commands(self.run_pipeline(gate)[0], "python", Path("/results"), "vm", "/home/user/deps")

  def test_first_failure_stops_pipeline(self):
    names = ["verify-lock", "validate", "release-check"]
    for index, name in enumerate(names):
      run, calls = self.run_pipeline(GATE, failure=name)
      with self.assertRaises(ValueError):
        ci.full_commands(run, "python", Path("/results"), "vm", "/home/user/deps")
      self.assertEqual([c[0] for c in calls], names[:index + 1])

  def test_invalid_vm_inputs_cannot_start_commands(self):
    for vm, root in (("--help", "/home/u"), ("vm\nother", "/home/u"), ("vm", "/home/u/../d"),
                     ("vm", "relative"), ("vm", "/home/u\nother")):
      run, calls = self.run_pipeline(GATE)
      with self.subTest(vm=vm, root=root), self.assertRaises(ValueError):
        ci.full_commands(run, "python", Path("/results"), vm, root)
      self.assertEqual(calls, [])


if __name__ == "__main__":
  unittest.main()
