from dataclasses import FrozenInstanceError
import json
import unittest

from compatibility.mock_transport import (MAX_SCRIPT_BYTES, MAX_SCRIPT_STEPS, MAX_TRANSFER_BYTES,
                                           MockCancelled, MockDisconnected, MockTimeout, ScriptError, ScriptStep, ScriptedTransport)


class TestMockTransport(unittest.TestCase):
  def test_short_reads_and_writes_are_returned_without_retry(self):
    mock = ScriptedTransport((ScriptStep("read", b"a"), ScriptStep("read", b""), ScriptStep("write", b"abcd", 2)))
    self.assertEqual(mock.read(4096), b"a")
    self.assertEqual(mock.read(4), b"")
    self.assertEqual(mock.write(b"abcd"), 2)
    self.assertEqual(mock.remaining_steps, 0)
    trace = mock.trace_json()
    self.assertEqual([e["returned_count"] for e in trace], [1, 0, 2])
    self.assertTrue(all(not e["actual_device_access"] and not e["actual_can_tx"] for e in trace))
    self.assertEqual(json.loads(json.dumps(trace)), trace)

  def test_scripted_errors_are_explicit_and_do_not_retry(self):
    for error, cls in (("timeout", MockTimeout), ("disconnected", MockDisconnected), ("cancelled", MockCancelled)):
      mock = ScriptedTransport((ScriptStep("read", error=error),))
      with self.subTest(error=error), self.assertRaises(cls):
        mock.read(1)
      self.assertEqual(mock.remaining_steps, 0)
      self.assertEqual(mock.trace[0].result, error)
      self.assertIsNone(mock.trace[0].returned_count)

  def test_mock_write_error_records_only_intent(self):
    mock = ScriptedTransport((ScriptStep("write", b"abc", error="timeout"),))
    with self.assertRaises(MockTimeout):
      mock.write(b"abc")
    self.assertEqual(mock.trace[0].payload_hex, "616263")
    self.assertFalse(mock.trace[0].actual_can_tx)
    self.assertIsNone(mock.trace[0].returned_count)

  def test_strict_script_mismatch_does_not_consume(self):
    mock = ScriptedTransport((ScriptStep("write", b"expected"), ScriptStep("read", b"ab")))
    for call in (lambda: mock.read(1), lambda: mock.write(b"wrong")):
      with self.assertRaises(ScriptError):
        call()
    self.assertEqual(mock.remaining_steps, 2)
    self.assertEqual(mock.trace, ())
    self.assertEqual(mock.write(b"expected"), 8)
    with self.assertRaises(ScriptError):
      mock.read(1)
    self.assertEqual(mock.remaining_steps, 1)

  def test_argument_boundaries_and_types(self):
    for size in (-1, MAX_TRANSFER_BYTES + 1, True, 1.0):
      mock = ScriptedTransport((ScriptStep("read"),))
      with self.subTest(size=size), self.assertRaises(ScriptError):
        mock.read(size)
      self.assertEqual(mock.remaining_steps, 1)
    for timeout in (0, 1001, True, 1.0, None):
      mock = ScriptedTransport((ScriptStep("read"),))
      with self.subTest(timeout=timeout), self.assertRaises(ScriptError):
        mock.read(0, timeout_ms=timeout)
    for size, timeout in ((0, 1), (MAX_TRANSFER_BYTES, 1000)):
      self.assertEqual(ScriptedTransport((ScriptStep("read"),)).read(size, timeout_ms=timeout), b"")
    with self.assertRaises(ScriptError):
      ScriptedTransport(()).write(bytearray(b"x"))

  def test_step_contract_validation(self):
    for args in (("reset",), ("read", bytearray()), ("read", b"x" * 4097), ("read", b"", 0),
                 ("write", b"x", True), ("write", b"x", 2), ("read", b"", None, "unknown"),
                 ("read", b"x", None, "timeout"), ("write", b"x", 0, "timeout")):
      with self.subTest(args=args[:1]), self.assertRaises(ScriptError):
        ScriptStep(*args)

  def test_script_resources_are_bounded(self):
    for script in ([], (object(),), (ScriptStep("read"),) * (MAX_SCRIPT_STEPS + 1),
                   (ScriptStep("read", b"a" * MAX_TRANSFER_BYTES),) * (MAX_SCRIPT_BYTES // MAX_TRANSFER_BYTES + 1)):
      with self.assertRaises(ScriptError):
        ScriptedTransport(script)
    mock = ScriptedTransport((ScriptStep("read"),) * MAX_SCRIPT_STEPS)
    self.assertEqual(mock.remaining_steps, MAX_SCRIPT_STEPS)

  def test_instances_are_independent_and_close_is_local(self):
    first = ScriptedTransport((ScriptStep("read", error="timeout"),))
    second = ScriptedTransport((ScriptStep("read", b"ok"),))
    with self.assertRaises(MockTimeout):
      first.read(2)
    first.close()
    first.close()
    with self.assertRaises(MockCancelled):
      first.read(2)
    self.assertEqual(second.read(2), b"ok")
    self.assertEqual(len(first.trace), 1)
    self.assertEqual(len(second.trace), 1)

  def test_trace_is_immutable_and_export_is_detached(self):
    mock = ScriptedTransport((ScriptStep("read", b"a"),))
    mock.read(1)
    with self.assertRaises(FrozenInstanceError):
      mock.trace[0].result = "fake"
    exported = mock.trace_json()
    exported[0]["actual_can_tx"] = True
    self.assertFalse(mock.trace[0].actual_can_tx)

  def test_exhaustion_is_explicit(self):
    mock = ScriptedTransport(())
    with self.assertRaisesRegex(ScriptError, "script exhausted"):
      mock.read(0)
    self.assertEqual(mock.trace, ())


if __name__ == "__main__":
  unittest.main()
