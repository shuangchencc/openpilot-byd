#include "pandad_loop.h"

namespace openpilot::pandad::compatibility {
namespace {
bool due(const std::optional<std::uint64_t> &last, std::uint64_t now, std::uint64_t period) {
  return !last || (now >= *last && now - *last >= period);
}
}
LoopCycleResult PandadLoop::cycle() {
  LoopCycleResult out;
  const auto started = clock_.now_us();
  auto observed = started;
  if (clock_failed_ || (last_cycle_ && started < *last_cycle_)) {
    clock_failed_ = out.clock_failed = true;
    runtime_.invalid_input("LOOP_CLOCK_ROLLBACK");
  } else if (last_cycle_ && started - *last_cycle_ >= kLoopMaxGapUs) {
    out.overdue = true;
    runtime_.invalid_input("LOOP_CYCLE_OVERDUE");
  }
  last_cycle_ = started;
  const auto within_budget = [&]() {
    const auto now = clock_.now_us();
    if (now < observed) {
      clock_failed_ = out.clock_failed = true;
      runtime_.invalid_input("LOOP_CLOCK_ROLLBACK");
    }
    observed = now;
    out.elapsed_us = now >= started ? now - started : 0;
    if (out.elapsed_us >= kLoopBudgetUs && !out.budget_exceeded) {
      out.budget_exceeded = true;
      runtime_.invalid_input("LOOP_CYCLE_BUDGET_EXCEEDED");
    }
    return !out.clock_failed && !out.budget_exceeded;
  };
  try {
    if (shutdown_requested_) {
      // Shutdown has its own bounded scheduler allowance; no ingress/CAN work.
      runtime_.shutdown();
      out.shutdown = true;
    } else {
      // Health, heartbeat and explicit exit intents precede all queued messages.
      // Even an overdue cycle gets this bounded opportunity to exit healthy peers.
      runtime_.tick();
      for (std::size_t n = 0; n < kLoopMaxInputs && within_budget(); ++n) {
        const auto captured = runtime_.capture();
        auto input = source_.next(captured);
        if (!input) { within_budget(); break; }
        ++out.inputs;
        if (!within_budget()) { ++out.rejected; break; }
        bool accepted = false;
        switch (input->kind) {
          case InputKind::Event: accepted = ingest_cereal_event(runtime_, input->bytes, input->epoch); break;
          case InputKind::CarParams: accepted = configure_cereal_params(runtime_, input->bytes, input->epoch); break;
          default: runtime_.invalid_input("UNKNOWN_INGRESS_KIND"); break;
        }
        if (accepted) ++out.accepted; else ++out.rejected;
      }
      for (std::uint32_t slot = 0; slot < 2 && within_budget(); ++slot) {
        ++out.receive_attempts;
        runtime_.receive_step(slot);
        if (!within_budget()) break;
        ++out.transmit_attempts;
        runtime_.transmit_step(slot);
      }
      const auto now = clock_.now_us();
      if (within_budget() && due(last_peripheral_, now, kLoopPeripheralStepUs)) {
        last_peripheral_ = now;
        out.peripheral_attempted = true;
        runtime_.peripheral_step();
      }
      within_budget();
    }
  } catch (...) {
    out.callback_failed = true;
    runtime_.invalid_input("LOOP_CALLBACK_EXCEPTION");
  }
  // Publish invalid observations after a fault instead of retaining healthy
  // placeholders. Publication failure itself closes the group before the next
  // topic is serialized. Sink callbacks are subject to the same observed budget.
  const auto publish = [&](OutputTopic topic, const auto &serialize) {
    try {
      const auto bytes = serialize();
      if (!sink_.publish(topic, bytes)) {
        out.callback_failed = true;
        runtime_.invalid_input("LOOP_PUBLICATION_FAILED");
      } else ++out.publications;
    } catch (...) {
      out.callback_failed = true;
      runtime_.invalid_input("LOOP_PUBLICATION_EXCEPTION");
    }
    within_budget();
  };
  const auto force = [&]() { return out.shutdown || out.overdue || out.clock_failed || out.budget_exceeded || out.callback_failed; };
  publish(OutputTopic::Can, [&] { return serialize_can(runtime_, clock_.now_us()); });
  auto now = clock_.now_us();
  if (force() || due(last_panda_publish_, now, kLoopPandaPublishUs)) {
    last_panda_publish_ = now;
    publish(OutputTopic::PandaStates, [&] { return serialize_panda_states(runtime_, now); });
  }
  now = clock_.now_us();
  if (force() || due(last_peripheral_publish_, now, kLoopPeripheralPublishUs)) {
    last_peripheral_publish_ = now;
    publish(OutputTopic::PeripheralState, [&] { return serialize_peripheral_state(runtime_, now); });
  }
  within_budget();
  return out;
}
}  // namespace openpilot::pandad::compatibility
