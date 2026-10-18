"""Source-only regression tests for exact CI test execution evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from maintenance import core
from maintenance.integration_evidence import contract


class IntegrationContractTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name) / "source"
    self.base = Path(self.temp.name) / "logs"
    self.base.mkdir()
    self.inventory = core.read_json(core.ROOT / "maintenance/integration-test-inventory.json")
    for suite in self.inventory["suites"].values():
      for relative in suite["source_files"]:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((core.ROOT / relative).read_bytes())
    self.write_inventory()
    cli = ["/fixture/python", "-B", "tools/songplus_integration.py"]
    commands = [("integration-metadata", cli + ["verify-lock", "--metadata-only"])]
    commands += [("help-" + (name or "root"), cli + ([name] if name else []) + ["--help"])
                 for name in (None, "verify-lock", "replay", "validate", "release-check")]
    commands += [(name, ["/fixture/python", *suite["argv_suffix"]]) for name, suite in self.inventory["suites"].items()]
    self.report = {"schema": "songplus-integration-ci/v1", "status": "PASS", "mode": "contract",
                   "failed": 0, "skipped": 0, "passed_commands": 8,
                   "commands": [{"name": name, "argv": argv, "returncode": 0, "expected_returncode": 0}
                                for name, argv in commands]}
    for command in self.report["commands"]:
      name = command["name"]
      self.write_log(name, "stdout", "")
      self.write_log(name, "stderr", "")
      if name in self.inventory["suites"]:
        self.write_tests(name, self.inventory["suites"][name]["test_ids"])

  def write_inventory(self):
    path = self.root / "maintenance/integration-test-inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(self.inventory))

  def command(self, name):
    return next(command for command in self.report["commands"] if command["name"] == name)

  def write_log(self, name, stream, text):
    path = self.base / (name + "." + stream + ".log")
    path.write_text(text)
    self.command(name)[stream + "_sha256"] = core.sha(path)

  def write_tests(self, name, ids):
    text = "".join(identity.rsplit(".", 1)[-1] + " (" + identity + ") ... ok\n" for identity in ids)
    text += "\n----------------------------------------------------------------------\nRan " + str(len(ids)) + " tests in 0.001s\n\nOK\n"
    self.write_log(name, "stderr", text)
    self.command(name)["tests_passed"] = len(ids)

  def check(self):
    return contract(self.report, self.base, root=self.root)

  def test_complete_frozen_inventory_passes(self):
    self.assertEqual(self.check(), {"commands": 8, "tests": {
      name: len(suite["test_ids"]) for name, suite in self.inventory["suites"].items()}})

  def test_fewer_tests_with_consistent_summary_and_log_hash_are_rejected(self):
    for name, suite in self.inventory["suites"].items():
      with self.subTest(suite=name):
        self.write_tests(name, suite["test_ids"][:-1])
        with self.assertRaisesRegex(ValueError, "executed IDs"):
          self.check()
        self.write_tests(name, suite["test_ids"])

  def test_same_count_replaced_or_duplicated_test_is_rejected(self):
    name = "maintenance-tests"
    ids = self.inventory["suites"][name]["test_ids"]
    for altered in ([ids[0], *ids[:-1]], [*ids[:-1], "test_other.Case.test_unknown"]):
      with self.subTest(altered=altered[-1]):
        self.write_tests(name, altered)
        with self.assertRaisesRegex(ValueError, "executed IDs"):
          self.check()

  def test_missing_verbose_outcomes_cannot_use_only_summary(self):
    name = "maintenance-tests"
    count = self.command(name)["tests_passed"]
    self.write_log(name, "stderr", f"Ran {count} tests in 0.001s\n\nOK\n")
    with self.assertRaisesRegex(ValueError, "executed IDs"):
      self.check()

  def test_stdout_cannot_substitute_for_unittest_stderr(self):
    name = "maintenance-tests"
    self.write_log(name, "stdout", (self.base / (name + ".stderr.log")).read_text())
    self.write_log(name, "stderr", "")
    with self.assertRaisesRegex(ValueError, "executed IDs"):
      self.check()

  def test_method_label_must_match_full_id(self):
    name = "maintenance-tests"
    text = (self.base / (name + ".stderr.log")).read_text()
    self.write_log(name, "stderr", "test_wrong" + text[text.index(" ("):])
    with self.assertRaisesRegex(ValueError, "method/ID"):
      self.check()

  def test_wrong_duplicate_or_non_ok_summary_is_rejected(self):
    name = "maintenance-tests"
    text = (self.base / (name + ".stderr.log")).read_text()
    count = self.command(name)["tests_passed"]
    bad = (text.replace(f"Ran {count} tests", "Ran 1 test"), text + "Ran 1 test in 0.1s\n\nOK\n",
           text.replace("\nOK\n", "\nOK (skipped=1)\n"), text.replace("\nOK\n", "\nFAILED (errors=1)\n"),
           text.replace("\nOK\n", "\nOK (expected failures=1)\n"))
    for value in bad:
      self.write_log(name, "stderr", value)
      with self.subTest(summary=value[-80:]), self.assertRaisesRegex(ValueError, "accounting"):
        self.check()

  def test_extra_failure_or_skipped_outcome_is_rejected(self):
    name = "maintenance-tests"
    text = (self.base / (name + ".stderr.log")).read_text()
    for state in ("FAIL", "ERROR", "skipped 'reason'", "expected failure", "unexpected success"):
      self.write_log(name, "stderr", "test_extra (test_extra.Case.test_extra) ... " + state + "\n" + text)
      with self.subTest(state=state), self.assertRaisesRegex(ValueError, "accounting"):
        self.check()

  def test_changed_count_and_boolean_exit_cannot_pass(self):
    row = self.command("maintenance-tests")
    for field, value in (("tests_passed", 1), ("tests_passed", True), ("returncode", False),
                         ("expected_returncode", False), ("returncode", 1)):
      old = row[field]
      row[field] = value
      with self.subTest(field=field, value=value), self.assertRaises(ValueError):
        self.check()
      row[field] = old

  def test_each_command_requires_exact_argv_suffix(self):
    for row in self.report["commands"]:
      old = copy.deepcopy(row["argv"])
      row["argv"] += ["--unknown"]
      with self.subTest(command=row["name"]), self.assertRaisesRegex(ValueError, "argv suffix"):
        self.check()
      row["argv"] = old
    self.command("maintenance-tests")["argv"][-1] = "-q"
    with self.assertRaisesRegex(ValueError, "argv suffix"):
      self.check()

  def test_actual_python_path_may_vary_but_must_be_consistent(self):
    for command in self.report["commands"]:
      command["argv"][0] = "/another/prepared/python"
    self.check()
    self.command("maintenance-tests")["argv"][0] = "/different/python"
    with self.assertRaisesRegex(ValueError, "runner changed"):
      self.check()

  def test_missing_duplicate_and_unknown_commands_are_rejected(self):
    commands = self.report["commands"]
    for altered in (commands[:-1], [commands[0], *commands[:-1]],
                    [*commands[:-1], {**commands[-1], "name": "unknown"}]):
      self.report["commands"] = altered
      with self.subTest(commands=[item["name"] for item in altered]), self.assertRaisesRegex(ValueError, "commands missing"):
        self.check()

  def test_changed_test_source_and_new_uninventoried_test_fail(self):
    source = self.root / next(iter(self.inventory["suites"]["maintenance-tests"]["source_files"]))
    original = source.read_bytes()
    source.write_bytes(original + b"\n# changed\n")
    with self.assertRaisesRegex(ValueError, "current test files"):
      self.check()
    source.write_bytes(original)
    (self.root / "maintenance/test_uninventoried.py").write_text("# must not be silently added\n")
    with self.assertRaisesRegex(ValueError, "current test files"):
      self.check()

  def test_inventory_cannot_change_command_scope_or_duplicate_id(self):
    original = copy.deepcopy(self.inventory)
    changes = (lambda: self.inventory["suites"]["maintenance-tests"]["argv_suffix"].append("-q"),
               lambda: self.inventory["suites"]["maintenance-tests"]["test_ids"].append(
                 self.inventory["suites"]["maintenance-tests"]["test_ids"][0]),
               lambda: self.inventory["suites"].pop("integration-ci-tests"))
    for change in changes:
      self.inventory = copy.deepcopy(original)
      change()
      self.write_inventory()
      with self.assertRaises(ValueError):
        self.check()

  def test_changed_or_symlinked_evidence_fails(self):
    path = self.base / "maintenance-tests.stderr.log"
    original = path.read_text()
    path.write_text(original + "tampered\n")
    with self.assertRaisesRegex(ValueError, "transcript changed"):
      self.check()
    alternate = self.base / "alias.log"
    alternate.write_text(original)
    path.unlink()
    path.symlink_to(alternate)
    with self.assertRaisesRegex(ValueError, "Symlink forbidden"):
      self.check()

  def test_frozen_inventory_matches_actual_unittest_discovery(self):
    # Discovery imports the same source-only test modules but does not run them.
    # Keep its temporary sys.path changes out of the running parent test suite.
    def ids(suite):
      for item in suite:
        if isinstance(item, unittest.TestSuite):
          yield from ids(item)
        else:
          yield item.id()
    original_path = sys.path[:]
    try:
      for name, suite in self.inventory["suites"].items():
        argv = suite["argv_suffix"]
        loader = unittest.TestLoader()
        discovered = loader.discover(str(core.ROOT / argv[argv.index("-s") + 1]), pattern=argv[argv.index("-p") + 1])
        self.assertEqual(loader.errors, [])
        self.assertEqual(sorted(ids(discovered)), suite["test_ids"], name)
    finally:
      sys.path[:] = original_path


if __name__ == "__main__":
  unittest.main()
