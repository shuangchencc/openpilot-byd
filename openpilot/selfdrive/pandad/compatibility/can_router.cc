#include "can_router.h"

#include <algorithm>
#include <limits>
#include <utility>

namespace openpilot::pandad::compatibility {
namespace {

void add(std::uint64_t &counter, std::size_t count = 1) {
  const auto remaining = std::numeric_limits<std::uint64_t>::max() - counter;
  counter += std::min<std::uint64_t>(remaining, count);
}

CanCapability capability(const SessionRegistry &registry, std::size_t slot) {
  return registry.profile().slots[slot].expected.capabilities.can_fd ? CanCapability::CanFd : CanCapability::ClassicOnly;
}

bool expired(std::uint64_t now, std::uint64_t timestamp, std::uint64_t budget) {
  // Never add a duration to a timestamp: that can wrap near UINT64_MAX.
  return timestamp > now || now - timestamp >= budget;
}

}  // namespace

const char *router_error_name(RouterError error) {
  switch (error) {
    case RouterError::None: return "none";
    case RouterError::Empty: return "empty";
    case RouterError::GroupUnavailable: return "group_unavailable";
    case RouterError::InvalidArgument: return "invalid_argument";
    case RouterError::InvalidEvent: return "invalid_event";
    case RouterError::InvalidRoute: return "invalid_route";
    case RouterError::Malformed: return "malformed";
    case RouterError::Expired: return "expired";
    case RouterError::FutureTimestamp: return "future_timestamp";
    case RouterError::StaleSession: return "stale_session";
    case RouterError::Overflow: return "overflow";
    case RouterError::ClockRollback: return "clock_rollback";
  }
  return "unknown_error";
}

CanRouter::CanRouter(SessionRegistry &registry)
    : registry_(registry), slots_{{SlotState(capability(registry, 0)), SlotState(capability(registry, 1))}} {
  synchronize();
}

void CanRouter::discard_tx_pending() {
  for (auto &slot : slots_) {
    add(counters_.epoch_tx_drops, slot.tx.size());
    slot.tx.clear();
  }
}

void CanRouter::clear_all() {
  for (auto &slot : slots_) {
    add(counters_.epoch_tx_drops, slot.tx.size());
    add(counters_.epoch_rx_drops, slot.rx.size());
    add(counters_.epoch_fragment_bytes, slot.parser.buffered_size());
    slot.tx.clear(); slot.rx.clear(); slot.parser.clear();
    slot.fragment_timestamp_us.reset();
    slot.last_receive_timestamp_us.reset();
  }
}

void CanRouter::synchronize() {
  const auto state = registry_.snapshot();
  bool changed = !initialized_ || generation_ != state.generation || group_available_ != state.group_available;
  for (std::size_t i = 0; i < 2; ++i) {
    changed = changed || epochs_[i] != state.slots[i].epoch || connections_[i] != state.slots[i].connection_token;
  }
  if (changed) clear_all();
  generation_ = state.generation;
  group_available_ = state.group_available;
  for (std::size_t i = 0; i < 2; ++i) {
    epochs_[i] = state.slots[i].epoch;
    connections_[i] = state.slots[i].connection_token;
  }
  initialized_ = true;
}

RouterError CanRouter::invalidate(RouterError error, const char *reason) {
  registry_.invalidate_group(reason);
  synchronize();
  return error;
}

void CanRouter::expire_queues(std::uint64_t now_us) {
  for (auto &slot : slots_) {
    for (auto it = slot.tx.begin(); it != slot.tx.end();) {
      if (expired(now_us, it->source_timestamp_us, kSendcanMaxAgeUs) ||
          expired(now_us, it->enqueue_timestamp_us, kTxQueueMaxAgeUs)) {
        add(counters_.expired_tx); it = slot.tx.erase(it);
      } else ++it;
    }
    for (auto it = slot.rx.begin(); it != slot.rx.end();) {
      if (expired(now_us, it->receive_timestamp_us, kRxMaxAgeUs)) {
        add(counters_.expired_rx); it = slot.rx.erase(it);
      } else ++it;
    }
  }
}

RouterError CanRouter::prepare(std::uint64_t now_us) {
  synchronize();
  if (clock_failed_) return RouterError::ClockRollback;
  if (last_now_us_ && now_us < *last_now_us_) {
    clock_failed_ = true; add(counters_.clock_faults);
    return invalidate(RouterError::ClockRollback, "ROUTER_CLOCK_ROLLBACK");
  }
  last_now_us_ = now_us;
  if (!group_available_) return RouterError::GroupUnavailable;
  for (const auto &slot : slots_) {
    if (slot.fragment_timestamp_us && expired(now_us, *slot.fragment_timestamp_us, kRxMaxAgeUs)) {
      add(counters_.expired_fragments);
      // Dropping a byte prefix would make its later suffix an unsafe new frame.
      return invalidate(RouterError::Expired, "ROUTER_EXPIRED_RX_FRAGMENT");
    }
  }
  expire_queues(now_us);
  return RouterError::None;
}

RouteToken CanRouter::token_for(std::size_t slot) const {
  return {static_cast<std::uint32_t>(slot), epochs_[slot], connections_[slot], generation_};
}

bool CanRouter::current(const RouteToken &token) const {
  return token.slot < 2 && group_available_ && token.group_generation == generation_ &&
         token.epoch == epochs_[token.slot] && token.connection_token == connections_[token.slot] &&
         token.connection_token != 0;
}

RouterResult<RouteToken> CanRouter::current_token(std::uint32_t slot, std::uint64_t now_us) {
  if (slot >= 2) return {{}, RouterError::InvalidArgument};
  const auto error = prepare(now_us);
  return error == RouterError::None ? RouterResult<RouteToken>{token_for(slot), error} : RouterResult<RouteToken>{{}, error};
}

RouterResult<std::size_t> CanRouter::enqueue_tx(const TxBatch &batch, std::uint64_t now_us) {
  synchronize();
  if (!group_available_) return {{}, RouterError::GroupUnavailable};
  if (batch.group_generation == 0 || batch.group_generation != generation_) {
    add(counters_.invalid_batches); return {{}, RouterError::StaleSession};
  }
  const auto error = prepare(now_us);
  if (error != RouterError::None) return {{}, error};
  const auto reject = [&](RouterError why) -> RouterResult<std::size_t> {
    add(counters_.invalid_batches); return {{}, why};
  };
  if (!batch.valid || batch.frames.empty()) return reject(RouterError::InvalidEvent);
  if (batch.frames.size() > kMaxTxBatchFrames) {
    add(counters_.tx_overflows); return reject(RouterError::Overflow);
  }
  if (batch.source_timestamp_us > now_us) return reject(RouterError::FutureTimestamp);
  if (expired(now_us, batch.source_timestamp_us, kSendcanMaxAgeUs)) return reject(RouterError::Expired);
  std::array<std::vector<SimulatedTxIntent>, 2> staged;
  for (const auto &frame : batch.frames) {
    // Bound the copy before constructing a local frame from caller memory.
    if (frame.payload.size() > kCanMaxFrameSize - kCanHeaderSize) {
      add(counters_.malformed); return reject(RouterError::Malformed);
    }
    const auto route = parse_host_source(frame.source, FrameDirection::Tx);
    if (!route) return reject(RouterError::InvalidRoute);
    CanFrame local{frame.address, route.value.local_bus, frame.extended, frame.fd, false, false, frame.payload};
    auto encoded = encode_can_frame(local, capability(registry_, route.value.slot), FrameDirection::Tx);
    if (!encoded) { add(counters_.malformed); return reject(RouterError::Malformed); }
    staged[route.value.slot].push_back({token_for(route.value.slot), std::move(local), std::move(encoded.value),
                                      batch.source_timestamp_us, now_us, false});
  }
  for (std::size_t i = 0; i < 2; ++i) {
    if (staged[i].size() > kTxFramesPerSlot - slots_[i].tx.size()) {
      add(counters_.tx_overflows); return reject(RouterError::Overflow);
    }
  }
  for (std::size_t i = 0; i < 2; ++i) {
    for (auto &intent : staged[i]) slots_[i].tx.push_back(std::move(intent));
  }
  return {batch.frames.size(), RouterError::None};
}

RouterResult<SimulatedTxIntent> CanRouter::take_tx(std::uint32_t slot, std::uint64_t now_us) {
  if (slot >= 2) return {{}, RouterError::InvalidArgument};
  const auto error = prepare(now_us);
  if (error != RouterError::None) return {{}, error};
  auto &queue = slots_[slot].tx;
  if (queue.empty()) return {{}, RouterError::Empty};
  if (!current(queue.front().token)) return {{}, invalidate(RouterError::StaleSession, "ROUTER_STALE_TX_QUEUE")};
  auto intent = std::move(queue.front()); queue.pop_front();
  return {std::move(intent), RouterError::None};
}

RouterResult<bool> CanRouter::validate_intent(const SimulatedTxIntent &intent, std::uint64_t now_us) {
  synchronize();
  if (!group_available_) return {false, RouterError::GroupUnavailable};
  if (!current(intent.token)) return {false, RouterError::StaleSession};
  const auto error = prepare(now_us);
  if (error != RouterError::None) return {false, error};
  if (!current(intent.token)) return {false, RouterError::StaleSession};
  if (intent.source_timestamp_us > now_us || intent.enqueue_timestamp_us > now_us ||
      intent.source_timestamp_us > intent.enqueue_timestamp_us) return {false, RouterError::FutureTimestamp};
  if (expired(now_us, intent.source_timestamp_us, kSendcanMaxAgeUs) ||
      expired(now_us, intent.enqueue_timestamp_us, kTxQueueMaxAgeUs)) return {false, RouterError::Expired};
  const auto encoded = encode_can_frame(intent.frame, capability(registry_, intent.token.slot), FrameDirection::Tx);
  if (intent.real_tx_authorized || !encoded || encoded.value != intent.encoded) return {false, RouterError::Malformed};
  return {true, RouterError::None};
}

RouterResult<std::size_t> CanRouter::receive(const RouteToken &token, const std::uint8_t *bytes,
                                            std::size_t size, std::uint64_t receive_timestamp_us,
                                            std::uint64_t now_us) {
  // Obsolete callbacks cannot invalidate a new session, even with corrupt bytes
  // or obsolete timestamps. Synchronize first, then reject the old token.
  synchronize();
  if (!current(token)) { add(counters_.stale_callbacks); return {{}, RouterError::StaleSession}; }
  const auto error = prepare(now_us);
  if (error != RouterError::None) return {{}, error};
  auto &slot = slots_[token.slot];
  if (receive_timestamp_us > now_us) {
    add(counters_.malformed);
    return {{}, invalidate(RouterError::FutureTimestamp, "ROUTER_FUTURE_RX_TIMESTAMP")};
  }
  if (slot.last_receive_timestamp_us && receive_timestamp_us < *slot.last_receive_timestamp_us) {
    add(counters_.clock_faults);
    return {{}, invalidate(RouterError::ClockRollback, "ROUTER_RX_CALLBACK_REORDER")};
  }
  const auto old_fragment = slot.fragment_timestamp_us;
  auto decoded = slot.parser.feed(bytes, size);
  if (!decoded) {
    add(counters_.malformed);
    return {{}, invalidate(RouterError::Malformed, "ROUTER_MALFORMED_RX_STREAM")};
  }
  slot.last_receive_timestamp_us = receive_timestamp_us;
  if (slot.parser.buffered_size() != 0) {
    slot.fragment_timestamp_us = old_fragment && decoded.value.empty() ? old_fragment : std::optional<std::uint64_t>(receive_timestamp_us);
  } else slot.fragment_timestamp_us.reset();
  if (slot.fragment_timestamp_us && expired(now_us, *slot.fragment_timestamp_us, kRxMaxAgeUs)) {
    add(counters_.expired_fragments);
    return {{}, invalidate(RouterError::Expired, "ROUTER_EXPIRED_RX_FRAGMENT")};
  }
  std::vector<RoutedRxFrame> staged;
  staged.reserve(std::min(decoded.value.size(), kRxFramesPerSlot));
  for (std::size_t i = 0; i < decoded.value.size(); ++i) {
    const auto timestamp = i == 0 && old_fragment ? *old_fragment : receive_timestamp_us;
    if (expired(now_us, timestamp, kRxMaxAgeUs)) { add(counters_.expired_rx); continue; }
    auto &frame = decoded.value[i];
    const auto source = host_source(token.slot, frame.local_bus, frame.returned, frame.rejected, FrameDirection::Rx);
    if (!source) {
      add(counters_.malformed);
      return {{}, invalidate(RouterError::Malformed, "ROUTER_INVALID_RX_ROUTE")};
    }
    if (staged.size() == kRxFramesPerSlot - slot.rx.size()) {
      add(counters_.rx_overflows);
      return {{}, invalidate(RouterError::Overflow, "ROUTER_RX_QUEUE_OVERFLOW")};
    }
    staged.push_back({token, std::move(frame), source.value, timestamp});
  }
  const auto accepted = staged.size();
  for (auto &frame : staged) {
    if (frame.frame.rejected) {
      add(counters_.safety_rejections);
      latest_rejection_ = RejectionEvidence{frame.token, frame.source, frame.frame.local_bus,
                                           frame.frame.address, frame.receive_timestamp_us};
    }
    slot.rx.push_back(std::move(frame));
  }
  return {accepted, RouterError::None};
}

RouterResult<RoutedRxFrame> CanRouter::take_rx(std::uint32_t slot, std::uint64_t now_us) {
  if (slot >= 2) return {{}, RouterError::InvalidArgument};
  const auto error = prepare(now_us);
  if (error != RouterError::None) return {{}, error};
  auto &queue = slots_[slot].rx;
  if (queue.empty()) return {{}, RouterError::Empty};
  if (!current(queue.front().token)) return {{}, invalidate(RouterError::StaleSession, "ROUTER_STALE_RX_QUEUE")};
  auto frame = std::move(queue.front()); queue.pop_front();
  return {std::move(frame), RouterError::None};
}

RouterResult<bool> CanRouter::validate_rx_frame(const RoutedRxFrame &frame, std::uint64_t now_us) {
  synchronize();
  if (!group_available_) return {false, RouterError::GroupUnavailable};
  if (!current(frame.token)) return {false, RouterError::StaleSession};
  const auto error = prepare(now_us);
  if (error != RouterError::None) return {false, error};
  if (!current(frame.token)) return {false, RouterError::StaleSession};
  if (frame.receive_timestamp_us > now_us) return {false, RouterError::FutureTimestamp};
  if (expired(now_us, frame.receive_timestamp_us, kRxMaxAgeUs)) return {false, RouterError::Expired};
  const auto &local = frame.frame;
  const auto source = host_source(frame.token.slot, local.local_bus, local.returned, local.rejected, FrameDirection::Rx);
  const auto encoded = encode_can_frame(local, capability(registry_, frame.token.slot), FrameDirection::Rx);
  if (!source || source.value != frame.source || !encoded) return {false, RouterError::Malformed};
  return {true, RouterError::None};
}

RouterResult<RouterDepths> CanRouter::inspect(std::uint64_t now_us) {
  const auto error = prepare(now_us);
  RouterDepths depths;
  for (std::size_t i = 0; i < 2; ++i) {
    depths.tx[i] = slots_[i].tx.size(); depths.rx[i] = slots_[i].rx.size();
    depths.fragment_bytes[i] = slots_[i].parser.buffered_size();
  }
  return {depths, error};
}

}  // namespace openpilot::pandad::compatibility
