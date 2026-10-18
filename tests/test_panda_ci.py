"""CI command/security contract fixtures; no submodules, compiler or device."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("panda_ci", Path(__file__).resolve().parents[1] / "tools/check_panda_ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class Pipeline:
    def __init__(self, gate=None, failure=None):
        self.calls = []
        self.gate = gate if gate is not None else {
            "offline_compatibility_complete": True, "deployment_ready": False,
            "deployment_blockers": [{"id": "physical-identity", "status": "BLOCKED"}],
        }
        self.failure = failure

    def __call__(self, name, argv, expected=0):
        self.calls.append((name, argv, expected))
        if name == self.failure:
            raise ValueError("fixture failure")
        return json.dumps(self.gate) if name == "release-check" else ""


class WorkflowTests(unittest.TestCase):
    def write_workflow(self, text):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        target = root / ci.WORKFLOW
        target.parent.mkdir(parents=True)
        target.write_text(text)
        return root

    def test_checked_in_workflow_matches_reviewed_contract(self):
        ci.check_workflow(ci.ROOT)

    def test_fixture_exact_contract_passes(self):
        ci.check_workflow(self.write_workflow(ci.workflow_text()))

    def test_action_must_remain_pinned(self):
        root = self.write_workflow(ci.workflow_text().replace(ci.CHECKOUT_SHA, "v6"))
        with self.assertRaises(ValueError):
            ci.check_workflow(root)

    def test_permissions_cannot_expand(self):
        root = self.write_workflow(ci.workflow_text().replace("contents: read", "contents: write"))
        with self.assertRaises(ValueError):
            ci.check_workflow(root)

    def test_self_hosted_cannot_run_for_pull_requests(self):
        root = self.write_workflow(ci.workflow_text().replace("== 'workflow_dispatch'", "== 'pull_request'"))
        with self.assertRaises(ValueError):
            ci.check_workflow(root)

    def test_protected_environment_cannot_disappear(self):
        root = self.write_workflow(ci.workflow_text().replace("    environment: panda-offline-maintainers\n", ""))
        with self.assertRaises(ValueError):
            ci.check_workflow(root)

    def test_network_namespace_cannot_disappear(self):
        changed = ci.workflow_text().replace(" --net --mount", " --mount")
        self.assertNotEqual(changed, ci.workflow_text())
        root = self.write_workflow(changed)
        with self.assertRaises(ValueError):
            ci.check_workflow(root)

    def test_publish_or_install_step_is_rejected(self):
        for extra in ("      - run: pip install dependency\n", "      - uses: actions/upload-artifact@v6\n"):
            with self.subTest(extra=extra):
                root = self.write_workflow(ci.workflow_text() + extra)
                with self.assertRaises(ValueError):
                    ci.check_workflow(root)


class CommandTests(unittest.TestCase):
    def test_hosted_commands_are_real_cli_and_stdlib_tests(self):
        run = Pipeline()
        ci.contract_commands(run, sys.executable)
        self.assertEqual(len(run.calls), 9)
        self.assertEqual(run.calls[0][1][-2:], ["verify-lock", "--metadata-only"])
        self.assertEqual([x[0] for x in run.calls[-2:]], ["maintenance-tests", "ci-tests"])
        self.assertFalse(any("validate" in argv and "--help" not in argv for _, argv, _ in run.calls))

    def test_full_pipeline_uses_fresh_report_and_explicit_local_vm(self):
        run = Pipeline()
        gate = ci.full_commands(run, sys.executable, Path("/fixture/output"), "fixture-vm", "/fixture/deps")
        self.assertTrue(gate["offline_compatibility_complete"])
        self.assertEqual([x[0] for x in run.calls], ["verify-lock", "replay", "validate", "release-check"])
        self.assertEqual(run.calls[-1][2], 3)
        self.assertEqual(run.calls[-1][1][-1], "/fixture/output/validation/result.json")
        self.assertIn("fixture-vm", run.calls[2][1])

    def test_pipeline_stops_at_each_failure(self):
        names = ["verify-lock", "replay", "validate", "release-check"]
        for index, name in enumerate(names):
            with self.subTest(name=name):
                run = Pipeline(failure=name)
                with self.assertRaises(ValueError):
                    ci.full_commands(run, sys.executable, Path("/fixture/output"), "vm", "/deps")
                self.assertEqual([x[0] for x in run.calls], names[:index + 1])

    def test_exit_three_without_release_evidence_is_not_pass(self):
        for gate in ({}, [], {"offline_compatibility_complete": False},
                     {"offline_compatibility_complete": True, "deployment_ready": True},
                     {"offline_compatibility_complete": True, "deployment_ready": False, "deployment_blockers": []},
                     {"offline_compatibility_complete": True, "deployment_ready": False, "deployment_blockers": [""]},
                     {"offline_compatibility_complete": True, "deployment_ready": False, "deployment_blockers": ["missing"], "status": "INVALID_EVIDENCE"}):
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                ci.full_commands(Pipeline(gate=gate), sys.executable, Path("/fixture/output"), "vm", "/deps")

    def test_booleans_must_be_actual_json_booleans(self):
        for value in (1, "true", "false", None):
            gate = {"offline_compatibility_complete": value, "deployment_ready": False, "deployment_blockers": ["missing"]}
            with self.subTest(value=value), self.assertRaises(ValueError):
                ci.full_commands(Pipeline(gate=gate), sys.executable, Path("/fixture/output"), "vm", "/deps")

    def test_instance_and_dependency_root_fail_before_commands(self):
        for instance, root in (("", "/deps"), ("--option", "/deps"), ("vm\nother", "/deps"), ("vm", "relative"), ("vm", "/deps\nother")):
            run = Pipeline()
            with self.subTest(instance=instance, root=root), self.assertRaises(ValueError):
                ci.full_commands(run, sys.executable, Path("/fixture/output"), instance, root)
            self.assertEqual(run.calls, [])

    def test_prepared_checkout_checks_commit_and_tracked_cleanliness(self):
        calls = []
        def run(name, argv):
            calls.append(name)
            return "a" * 40 if name == "prepared-head" else ""
        ci.check_prepared_commit(run, "a" * 40)
        self.assertEqual(calls, ["prepared-head", "prepared-status"])

    def test_prepared_checkout_rejects_commit_drift_and_dirty_tree(self):
        for observed, status in (("b" * 40, ""), ("a" * 40, " M tools/check_panda_ci.py")):
            def run(name, argv, observed=observed, status=status):
                return observed if name == "prepared-head" else status
            with self.subTest(observed=observed, status=status), self.assertRaises(ValueError):
                ci.check_prepared_commit(run, "a" * 40)

    def test_prepared_commit_requires_complete_sha(self):
        run = Pipeline()
        with self.assertRaises(ValueError):
            ci.check_prepared_commit(run, "main")
        self.assertEqual(run.calls, [])

    def test_runner_real_subprocess_uses_offline_environment_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = ci.Runner(ci.ROOT, Path(directory))
            code = "import os;print(os.environ['UV_OFFLINE'],os.environ['PIP_NO_INDEX'])"
            self.assertEqual(runner.run("environment", [sys.executable, "-c", code]).strip(), "1 1")
            self.assertTrue((Path(directory) / "environment.stdout.log").is_file())

    def test_runner_real_subprocess_rejects_unexpected_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = ci.Runner(ci.ROOT, Path(directory))
            with self.assertRaises(ValueError):
                runner.run("exit", [sys.executable, "-c", "raise SystemExit(3)"])

    def test_runner_rejects_zero_or_skipped_tests(self):
        for output in ("Ran 0 tests in 0.0s\nOK", "Ran 4 tests in 0.1s\nOK (skipped=1)"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(output=output):
                runner = ci.Runner(ci.ROOT, Path(directory))
                with patch.object(ci.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", output)), self.assertRaises(ValueError):
                    runner.run("fixture-tests", ["synthetic"])

    def test_runner_records_nonempty_test_count(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = ci.Runner(ci.ROOT, Path(directory))
            with patch.object(ci.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "Ran 7 tests in 0.1s\nOK")):
                runner.run("fixture-tests", ["synthetic"])
            self.assertEqual(runner.commands[-1]["tests_passed"], 7)


if __name__ == "__main__":
    unittest.main()
