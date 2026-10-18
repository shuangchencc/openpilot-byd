"""Evidence rejection tests do not compile or load vehicle/private data."""
from pathlib import Path
import tempfile
import unittest

from maintenance import core
from maintenance.integration_evidence import check_commands, groups_passed


class CommandEvidenceTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.base = Path(self.temp.name)
    (self.base / "compile.log").write_text("compiler output\n")
    self.row = {"name": "compile", "argv": ["cc", "fixture.c"], "exit_code": 0,
                "log_sha256": core.sha(self.base / "compile.log")}

  def test_success_is_fixed_by_validator(self):
    check_commands([self.row], self.base, {"compile"})
    row = {**self.row, "exit_code": 1, "expected_exit_code": 1}
    with self.assertRaisesRegex(ValueError, "redefine"):
      check_commands([row], self.base, {"compile"})

  def test_missing_extra_and_duplicate_commands_fail(self):
    for rows, names in (([], {"compile"}), ([self.row], {"compile", "link"}),
                        ([self.row, self.row], {"compile"}), ([{**self.row, "name": "other"}], {"compile"})):
      with self.subTest(rows=rows), self.assertRaises(ValueError):
        check_commands(rows, self.base, names)

  def test_negative_exit_requires_exact_external_expectation(self):
    row = {**self.row, "exit_code": 3, "expected_exit_code": 3}
    check_commands([row], self.base, {"compile"}, {"compile": 3})
    for exit_code in (0, 1, 2, True):
      with self.subTest(exit_code=exit_code), self.assertRaises(ValueError):
        check_commands([{**row, "exit_code": exit_code}], self.base, {"compile"}, {"compile": 3})

  def test_changed_and_missing_transcript_fail(self):
    (self.base / "compile.log").write_text("")
    with self.assertRaises(ValueError):
      check_commands([self.row], self.base, {"compile"})
    (self.base / "compile.log").unlink()
    with self.assertRaises(OSError):
      check_commands([self.row], self.base, {"compile"})

  def test_no_empty_duplicate_skipped_or_self_inconsistent_groups(self):
    good = {"groups": ["a", "b"], "summary": {"passed": 2, "failed": 0, "skipped": 0}}
    self.assertEqual(groups_passed(good), 2)
    for groups, summary in (([], good["summary"]), (["a", "a"], good["summary"]),
                            (["a"], good["summary"]), (good["groups"], {"passed": 2, "skipped": 1}),
                            (good["groups"], {"passed": 2, "failed": 1})):
      with self.subTest(groups=groups, summary=summary), self.assertRaises(ValueError):
        groups_passed({"groups": groups, "summary": summary})


if __name__ == "__main__":
  unittest.main()
