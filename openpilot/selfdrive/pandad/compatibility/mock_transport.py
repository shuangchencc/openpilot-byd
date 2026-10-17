"""Bounded scripted memory transport. A write is only recorded command intent.

No device files, clocks, waits, threads, sockets, USB, Panda, or GPIO are used.
These limits are fixture resource limits, not validated USB timing budgets.
"""

from dataclasses import dataclass, asdict


MAX_TRANSFER_BYTES = 4096
MAX_SCRIPT_STEPS = 512
MAX_SCRIPT_BYTES = 1024 * 1024
MAX_TIMEOUT_MS = 1000


class MockTransportError(Exception):
  pass


class ScriptError(MockTransportError):
  pass


class MockTimeout(MockTransportError):
  pass


class MockDisconnected(MockTransportError):
  pass


class MockCancelled(MockTransportError):
  pass


ERRORS = {"timeout": MockTimeout, "disconnected": MockDisconnected, "cancelled": MockCancelled}


@dataclass(frozen=True)
class ScriptStep:
  operation: str
  payload: bytes = b""
  returned_count: int | None = None
  error: str | None = None

  def __post_init__(self) -> None:
    if type(self.operation) is not str or self.operation not in ("read", "write"):
      raise ScriptError("operation must be read or write")
    if type(self.payload) is not bytes or len(self.payload) > MAX_TRANSFER_BYTES:
      raise ScriptError("payload must be bounded immutable bytes")
    if self.returned_count is not None and (self.operation != "write" or type(self.returned_count) is not int
                                           or not 0 <= self.returned_count <= len(self.payload)):
      raise ScriptError("invalid returned_count")
    if self.error is not None and (type(self.error) is not str or self.error not in ERRORS):
      raise ScriptError("invalid scripted error")
    if self.error is not None and (self.returned_count is not None or (self.operation == "read" and self.payload)):
      raise ScriptError("error cannot also return data or a count")


@dataclass(frozen=True)
class TraceEvent:
  operation: str
  requested_count: int
  timeout_ms: int
  payload_hex: str
  returned_count: int | None
  result: str
  actual_device_access: bool = False
  actual_can_tx: bool = False


class ScriptedTransport:
  def __init__(self, steps: tuple[ScriptStep, ...]):
    if type(steps) is not tuple or not all(type(s) is ScriptStep for s in steps):
      raise ScriptError("script must be an immutable tuple of ScriptStep")
    if len(steps) > MAX_SCRIPT_STEPS or sum(len(s.payload) for s in steps) > MAX_SCRIPT_BYTES:
      raise ScriptError("script exceeds resource limit")
    self._steps = steps
    self._cursor = 0
    self._trace: list[TraceEvent] = []
    self._closed = False

  @property
  def trace(self) -> tuple[TraceEvent, ...]:
    return tuple(self._trace)

  def trace_json(self) -> list[dict]:
    return [asdict(event) for event in self._trace]

  @property
  def remaining_steps(self) -> int:
    return len(self._steps) - self._cursor

  @staticmethod
  def _arguments(size: int, timeout_ms: int) -> None:
    if type(size) is not int or not 0 <= size <= MAX_TRANSFER_BYTES:
      raise ScriptError("transfer size out of range")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
      raise ScriptError("timeout must be a bounded positive integer")

  def _next(self, operation: str, size: int, timeout_ms: int, payload: bytes = b"") -> ScriptStep:
    self._arguments(size, timeout_ms)
    if self._closed:
      raise MockCancelled("transport is closed")
    if self._cursor == len(self._steps):
      raise ScriptError("script exhausted")
    step = self._steps[self._cursor]
    if step.operation != operation:
      raise ScriptError("unexpected operation")
    if (operation == "read" and len(step.payload) > size) or (operation == "write" and step.payload != payload):
      raise ScriptError("request does not match script")
    self._cursor += 1
    count = None if step.error else (len(step.payload) if step.returned_count is None else step.returned_count)
    self._trace.append(TraceEvent(operation, size, timeout_ms, (payload if operation == "write" else step.payload).hex(),
                                  count, step.error or "ok"))
    if step.error:
      raise ERRORS[step.error](f"scripted {step.error}")
    return step

  def read(self, size: int, *, timeout_ms: int = 100) -> bytes:
    return self._next("read", size, timeout_ms).payload

  def write(self, payload: bytes, *, timeout_ms: int = 100) -> int:
    if type(payload) is not bytes:
      raise ScriptError("write payload must be immutable bytes")
    step = self._next("write", len(payload), timeout_ms, payload)
    return len(payload) if step.returned_count is None else step.returned_count

  def close(self) -> None:
    # Closing an in-memory script never emits a physical reset/cancel operation.
    self._closed = True
