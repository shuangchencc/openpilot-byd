#!/usr/bin/env python3
"""The gate that keeps upstream's hardware tests from running on a tici.

Runs anywhere: the device is faked, and nothing here is itself a hardware test.
"""
import unittest
from unittest import mock

import openpilot.common.test as optest
from openpilot.common.test import OpenpilotTestCase, refuse_hardware_test


def make_cases(ran: list[str]):
  # Built inside a function so that the loader does not collect these classes as tests.
  class Hw(OpenpilotTestCase):
    COMMA_HARDWARE_TEST = True

    @classmethod
    def setup_class(cls):
      ran.append("setup_class")

    def setup_method(self):
      ran.append("setup_method")

    def test_body(self):
      ran.append("test_body")

  class HwSafe(Hw):
    SAFE_ON_TICI = True

  class NotHw(OpenpilotTestCase):
    def test_body(self):
      ran.append("not_hw")

  return Hw, HwSafe, NotHw


class TestHardwareTestGate(unittest.TestCase):
  def setUp(self):
    self.ran: list[str] = []
    self.Hw, self.HwSafe, self.NotHw = make_cases(self.ran)
    self.hardware = mock.Mock()

  def _device(self, device_type: str | None):
    """Pretend to be a comma device of this type, or a PC for None. Nothing real is touched."""
    self.hardware.get_device_type.return_value = device_type if device_type is not None else "pc"
    patches = [
      mock.patch.object(optest, "COMMA_HARDWARE", device_type is not None),
      mock.patch.object(optest, "HARDWARE", self.hardware),
      mock.patch.object(optest.subprocess, "run"),   # setUp's `pkill -9 -f athena`
    ]
    for p in patches:
      p.start()
      self.addCleanup(p.stop)

  def _run_suite(self, cls) -> unittest.TestResult:
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(cls).run(result)
    return result

  def test_tici_refuses_before_any_fixture_runs(self):
    self._device("tici")
    result = self._run_suite(self.Hw)
    self.assertEqual(self.ran, [])
    self.assertEqual(result.testsRun, 0)
    self.assertEqual(len(result.errors), 1)        # an error, not a skip: "nothing ran" must not read as "passed"
    self.assertEqual(result.skipped, [])
    self.assertIn("refused on tici", result.errors[0][1])
    self.hardware.initialize_hardware.assert_not_called()
    self.hardware.set_power_save.assert_not_called()
    optest.subprocess.run.assert_not_called()

  def test_tici_refuses_a_test_run_without_its_class_fixtures(self):
    self._device("tici")
    result = unittest.TestResult()
    self.Hw("test_body").run(result)               # no suite, so setUpClass never gets the chance
    self.assertEqual(self.ran, [])
    self.assertEqual(len(result.errors), 1)
    self.assertIn("refused on tici", result.errors[0][1])
    self.hardware.initialize_hardware.assert_not_called()
    optest.subprocess.run.assert_not_called()

  def test_tici_leaves_a_decorator_skip_as_a_skip(self):
    self._device("tici")
    skipped = unittest.skip("not for this device")(type("Skipped", (self.Hw,), {}))
    result = self._run_suite(skipped)
    self.assertEqual(self.ran, [])
    self.assertEqual(result.errors, [])
    self.assertEqual(len(result.skipped), 1)

  def test_tici_runs_a_class_that_declares_itself_safe(self):
    self._device("tici")
    result = self._run_suite(self.HwSafe)
    self.assertEqual(self.ran, ["setup_class", "setup_method", "test_body"])
    self.assertTrue(result.wasSuccessful())
    self.assertEqual(result.testsRun, 1)

  def test_other_comma_devices_are_unchanged(self):
    self._device("tizi")
    result = self._run_suite(self.Hw)
    self.assertEqual(self.ran, ["setup_class", "setup_method", "test_body"])
    self.assertTrue(result.wasSuccessful())
    self.hardware.initialize_hardware.assert_called_once()

  def test_ordinary_tests_run_on_tici(self):
    self._device("tici")
    result = self._run_suite(self.NotHw)
    self.assertEqual(self.ran, ["not_hw"])
    self.assertTrue(result.wasSuccessful())

  def test_pc_still_skips_hardware_tests(self):
    self._device(None)
    result = self._run_suite(self.Hw)
    self.assertEqual(self.ran, [])
    self.assertEqual(result.errors, [])
    self.assertEqual(len(result.skipped), 1)

  def test_refuse_hardware_test_truth_table(self):
    for device_type, cls, refused in (("tici", self.Hw, True), ("tici", self.HwSafe, False), ("tici", self.NotHw, False),
                                      ("tizi", self.Hw, False), ("mici", self.Hw, False), (None, self.Hw, False)):
      with self.subTest(device_type=device_type, cls=cls.__name__):
        self.hardware.get_device_type.return_value = device_type or "pc"
        with mock.patch.object(optest, "COMMA_HARDWARE", device_type is not None), \
             mock.patch.object(optest, "HARDWARE", self.hardware):
          self.assertEqual(refuse_hardware_test(cls) is not None, refused)

  def test_no_hardware_test_claims_to_be_safe_yet(self):
    # SAFE_ON_TICI is an exception someone has to argue for, one class at a time. If this
    # fails, a class was marked safe: check that its tests neither touch a panda nor send CAN.
    import ast
    import pathlib
    root = pathlib.Path(optest.__file__).resolve().parents[2]
    marked = []
    for path in root.rglob("test*.py"):
      if ".venv" in path.parts or path.name == "test_hardware_test_gate.py":
        continue
      try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
      except (SyntaxError, UnicodeDecodeError):
        continue
      for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
          for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SAFE_ON_TICI" for t in stmt.targets):
              if isinstance(stmt.value, ast.Constant) and stmt.value.value is True:
                marked.append(f"{path.relative_to(root)}:{node.name}")
    self.assertEqual(marked, [])


if __name__ == "__main__":
  unittest.main()
