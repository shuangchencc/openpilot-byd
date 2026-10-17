#!/usr/bin/env python3
"""Run only this directory's offline tests with a machine-readable result."""
import json
from pathlib import Path
import sys
import unittest


class RecordedResult(unittest.TextTestResult):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.passed_ids = []

  def addSuccess(self, test):
    super().addSuccess(test)
    self.passed_ids.append(test.id())


suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
report = dict(tests_run=result.testsRun, passed=len(result.passed_ids), failures=len(result.failures),
              errors=len(result.errors), skipped=len(result.skipped), passed_ids=result.passed_ids,
              failed_ids=[test.id() for test, _ in result.failures + result.errors])
Path(sys.argv[1]).write_text(json.dumps(report, indent=2) + "\n")
sys.exit(0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1)
