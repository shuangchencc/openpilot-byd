#pragma once

#include <optional>
#include "cereal_bridge.h"

namespace openpilot::pandad::compatibility {
inline constexpr std::size_t kLoopMaxInputs = 16;
inline constexpr std::uint64_t kLoopBudgetUs = 10000;
inline constexpr std::uint64_t kLoopMaxGapUs = 250000;
inline constexpr std::uint64_t kLoopPeripheralStepUs = 50000;
inline constexpr std::uint64_t kLoopPandaPublishUs = 100000;
inline constexpr std::uint64_t kLoopPeripheralPublishUs = 500000;
enum class InputKind { Event, CarParams };
enum class OutputTopic { Can, PandaStates, PeripheralState };
struct InputEnvelope {
  InputKind kind = InputKind::Event;
  MessageBytes bytes;
  InputEpoch epoch{};
};
class EventSource {
public:
  virtual ~EventSource() = default;
  // Nonblocking cooperative callback. New acquisition may use this epoch,
  // captured BEFORE fetching. Buffered envelopes MUST retain their first epoch;
  // adapters cannot re-tag queued messages after reconnect or recovery.
  virtual std::optional<InputEnvelope> next(InputEpoch before_fetch) = 0;
};
class EventSink {
public:
  virtual ~EventSink() = default;
  // Nonblocking cooperative callback; false/exception closes the group gate.
  virtual bool publish(OutputTopic topic, const MessageBytes &bytes) = 0;
};
struct LoopCycleResult {
  std::size_t inputs = 0, accepted = 0, rejected = 0;
  std::size_t receive_attempts = 0, transmit_attempts = 0, publications = 0;
  bool peripheral_attempted = false, overdue = false, budget_exceeded = false;
  bool clock_failed = false, callback_failed = false, shutdown = false;
  std::uint64_t elapsed_us = 0;
};
// Actual reusable serialized pandad cycle, with injected ingress/publication.
// Runtime and loop MUST share the same Clock. No socket/device factory exists.
// 10ms is an observed cooperative deadline, not a kernel timing guarantee. A
// callback/transport cannot be preempted; failure closes TX at the next check.
class PandadLoop {
public:
  PandadLoop(PandadRuntime &runtime, Clock &clock, EventSource &source, EventSink &sink)
    : runtime_(runtime), clock_(clock), source_(source), sink_(sink) {}
  LoopCycleResult cycle();
  // Serialized owner-thread handoff for a normal exit or an observed signal.
  // Not async-signal-safe: an actual handler must only set its own signal flag.
  // The next cycle drains bounded exit commands before any ingress/CAN work.
  void request_shutdown() { shutdown_requested_ = true; }
private:
  PandadRuntime &runtime_;
  Clock &clock_;
  EventSource &source_;
  EventSink &sink_;
  std::optional<std::uint64_t> last_cycle_, last_peripheral_, last_panda_publish_, last_peripheral_publish_;
  bool clock_failed_ = false;
  bool shutdown_requested_ = false;
};
}  // namespace openpilot::pandad::compatibility
