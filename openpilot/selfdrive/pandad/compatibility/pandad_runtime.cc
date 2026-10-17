#include "pandad_runtime.h"

#include <limits>

namespace openpilot::pandad::compatibility {
namespace {
void count(std::uint64_t &counter) { if (counter != UINT64_MAX) ++counter; }
std::uint16_t u16(const std::vector<std::uint8_t> &b, std::size_t n) {
  return std::uint16_t(b[n]) | (std::uint16_t(b[n + 1]) << 8);
}
Request control_request(const ControlCommand &command) {
  Request request;
  request.kind = Kind::ControlWrite;
  request.value = command.value;
  request.index = command.param;
  switch (command.kind) {
    case ControlKind::ReadHealth: request.kind = Kind::ControlRead; request.request = 0xd2; request.read_length = 48; break;
    case ControlKind::ReadCanHealth: request.kind = Kind::ControlRead; request.request = 0xc2; request.read_length = 64; break;
    case ControlKind::SetAlternativeExperience: request.request = 0xdf; break;
    case ControlKind::SetSafety: request.request = 0xdc; break;
    case ControlKind::Heartbeat: request.request = 0xf3; request.value = command.engaged ? 1 : 0; break;
  }
  return request;
}
}
PandadRuntime::PandadRuntime(const SessionProfile &profile, Clock &clock)
  : registry_(profile), router_(registry_), control_(registry_), clock_(clock) {}

bool PandadRuntime::reconcile(const std::vector<SessionObservation> &inventory,
                             const std::array<Transport *, 2> &transports) {
  if (stopped_) return false;
  const auto before = registry_.snapshot();
  const auto after = registry_.reconcile(inventory);
  if (transports[0] && transports[0] == transports[1]) registry_.invalidate_group("ALIASED_TRANSPORTS");
  for (unsigned slot = 0; slot < 2; ++slot) {
    const auto token = after.slots[slot].connection_token;
    const bool changed_pointer = before.slots[slot].connected && after.slots[slot].connected &&
      token == attached_tokens_[slot] && transports[slot] != transports_[slot];
    bool reused_handle = false;
    for (unsigned previous = 0; previous < 2; ++previous) {
      reused_handle |= after.slots[slot].connected && transports[slot] && transports[slot] == transports_[previous] &&
        (previous != slot || token != attached_tokens_[previous]);
    }
    if (changed_pointer || reused_handle || (after.slots[slot].connected &&
        (!transports[slot] || !transports[slot]->healthy()))) {
      registry_.invalidate_group("INVALID_OR_RECYCLED_TRANSPORT");
      break;
    }
  }
  const auto current = registry_.snapshot();
  for (unsigned slot = 0; slot < 2; ++slot) {
    if (transports_[slot] && (!current.slots[slot].connected ||
        current.slots[slot].connection_token != attached_tokens_[slot])) transports_[slot]->cancel();
    transports_[slot] = current.slots[slot].connected ? transports[slot] : nullptr;
    attached_tokens_[slot] = current.slots[slot].connected ? current.slots[slot].connection_token : 0;
  }
  synchronize();
  return registry_.snapshot().group_available;
}
void PandadRuntime::synchronize() {
  const auto state = control_.snapshot(clock_.now_us());
  if (seen_generation_ != state.generation || seen_control_epoch_ != state.control_epoch) {
    router_.discard_pending();
    last_selfdrive_.reset(); last_sendcan_.reset(); fan_time_.reset(); fan_rpm_.reset();
    device_control_epoch_ = 0;
    if (seen_generation_ != state.generation) { device_.reset(); camera_.reset(); }
    seen_generation_ = state.generation;
    seen_control_epoch_ = state.control_epoch;
    control_started_us_ = clock_.now_us();
  }
}
InputEpoch PandadRuntime::capture() {
  synchronize();
  return {seen_generation_, seen_control_epoch_};
}
bool PandadRuntime::epoch_matches(InputEpoch epoch) {
  const auto current = capture();
  return !stopped_ && current.generation != 0 && current.control_epoch != 0 &&
    current.generation == epoch.generation && current.control_epoch == epoch.control_epoch;
}
bool PandadRuntime::fresh(std::uint64_t source, std::uint64_t limit) const {
  const auto now = clock_.now_us();
  return source > 0 && source <= now && now - source < limit;
}
bool PandadRuntime::device_input(const DeviceInput &input, InputEpoch epoch) {
  if (!epoch_matches(epoch)) { count(counters_.discarded_inputs); return false; }
  if (!input.valid || !input.thermal_ok || input.fan_percent > 100 ||
      !fresh(input.timestamp_us, kDeviceInputMaxAgeUs) || input.timestamp_us < control_started_us_ ||
      (device_ && input.timestamp_us <= device_->timestamp_us)) {
    invalid_input("INVALID_DEVICE_STATE"); return false;
  }
  if (device_ && device_->started && !input.started) {
    // Offroad immediately retires every queued/control intent, even if an
    // enabled producer is still publishing. Retain this newly accepted offroad
    // observation as evidence for the next explicit configuration.
    control_.fail_group("DEVICE_OFFROAD", clock_.now_us());
    router_.discard_pending(); camera_.reset(); synchronize();
  }
  device_ = input;
  device_control_epoch_ = seen_control_epoch_;
  return true;
}
bool PandadRuntime::camera_input(const CameraInput &input, InputEpoch epoch) {
  if (!epoch_matches(epoch)) { count(counters_.discarded_inputs); return false; }
  if (!input.valid || input.ir_percent > 100 || !fresh(input.timestamp_us, kDeviceInputMaxAgeUs) ||
      (camera_ && input.timestamp_us <= camera_->timestamp_us)) {
    invalid_input("INVALID_CAMERA_STATE"); return false;
  }
  camera_ = input;
  return true;
}
bool PandadRuntime::device_fresh() {
  return device_ && device_->valid && device_->thermal_ok && fresh(device_->timestamp_us, kDeviceInputMaxAgeUs);
}
bool PandadRuntime::fan_observable() const {
  return registry_.profile().roles.fan_control < 0 ||
    (fan_time_ && fan_rpm_ && fresh(*fan_time_, kPeripheralMaxAgeUs));
}
bool PandadRuntime::configure(const std::vector<SlotSafetyConfig> &configs, InputEpoch epoch) {
  if (!epoch_matches(epoch) || !device_fresh() || device_->started || device_control_epoch_ != seen_control_epoch_) return false;
  const bool accepted = control_.configure(configs, true, seen_generation_, clock_.now_us());
  synchronize();
  return accepted;
}
bool PandadRuntime::selfdrive(bool valid, bool enabled, std::uint64_t timestamp_us, InputEpoch epoch) {
  if (!epoch_matches(epoch)) { count(counters_.discarded_inputs); return false; }
  if (timestamp_us < control_started_us_ || (last_selfdrive_ && timestamp_us <= *last_selfdrive_)) {
    invalid_input("REPLAYED_SELFDRIVE_STATE"); return false;
  }
  if (enabled && (!device_fresh() || !device_->started)) { invalid_input("ONROAD_DEVICE_STATE_REQUIRED"); return false; }
  if (enabled && !fan_observable()) { invalid_input("FAN_OBSERVABILITY_REQUIRED"); return false; }
  last_selfdrive_ = timestamp_us;
  return control_.update_selfdrive({valid, enabled, timestamp_us, epoch.generation}, clock_.now_us());
}
bool PandadRuntime::sendcan(TxBatch batch, InputEpoch epoch) {
  if (!epoch_matches(epoch)) { count(counters_.denied_tx); return false; }
  const auto state = snapshot();
  if (!state.simulated_tx_allowed || !device_fresh() || !device_->started ||
      batch.source_timestamp_us < control_started_us_ ||
      (last_sendcan_ && batch.source_timestamp_us <= *last_sendcan_)) {
    count(counters_.denied_tx); return false;
  }
  // Ingress metadata comes from capture before deserialization, never from a
  // refreshed connection after a reconnect or control barrier transition.
  batch.group_generation = epoch.generation;
  const auto queued = router_.enqueue_tx(batch, clock_.now_us());
  if (!queued) { count(counters_.denied_tx); return false; }
  last_sendcan_ = batch.source_timestamp_us;
  return true;
}
Result PandadRuntime::perform(std::uint32_t slot, const Request &request) {
  if (stopped_ || slot > 1 || !transports_[slot]) return {};
  return transports_[slot]->perform(request, kRuntimeIoBudgetMs);
}
bool PandadRuntime::exact(const Result &result, std::size_t expected) const {
  return result.status == Status::Ok && result.actual_known && !result.delivery_uncertain &&
    result.actual == expected && result.data.size() == expected;
}
void PandadRuntime::retire(std::uint32_t slot, const char *reason) {
  const auto current = registry_.snapshot();
  if (slot < 2) {
    const auto &s = current.slots[slot];
    registry_.disconnect(slot, s.epoch, s.connection_token);
    if (transports_[slot]) transports_[slot]->cancel();
  }
  count(counters_.io_failures);
  control_.fail_group(reason, clock_.now_us());
  synchronize();
}
void PandadRuntime::tick() {
  if (stopped_) return;
  snapshot();
  const auto commands = control_.poll(clock_.now_us());
  for (const auto &command : commands) {
    if (!control_.validate_command(command, clock_.now_us())) continue;
    const Request request = control_request(command);
    const Result result = perform(command.token.slot, request);
    const bool ok = exact(result, request.read_length);
    control_.complete(command, ok, result.data, clock_.now_us());
    if (!ok) {
      count(counters_.io_failures);
      if (transports_[command.token.slot]) transports_[command.token.slot]->cancel();
    }
  }
  synchronize();
}
bool PandadRuntime::peripheral_step() {
  if (stopped_) return false;
  const auto state = snapshot();
  const auto profile = registry_.profile();
  if (!state.panda_states_valid || !device_fresh()) {
    if (device_ && !device_fresh()) invalid_input("THERMAL_OBSERVABILITY_LOST");
    return false;
  }
  // There is deliberately no inferred GPIO/power-saving output. In particular
  // an internal board reporting ignition=false never powers down a live group.
  Request request;
  request.kind = Kind::ControlWrite;
  int slot = profile.roles.fan_control;
  const auto phase = peripheral_phase_++ % 3;
  if (phase < 2) {
    if (slot < 0) return false;
    if (phase == 0) { request.request = 0xb1; request.value = device_->fan_percent; }
    else { request.kind = Kind::ControlRead; request.request = 0xb2; request.read_length = 2; }
  } else {
    slot = profile.roles.ir_control;
    if (slot < 0) return false;  // no driver-camera/IR feature is fabricated
    if (!camera_ || !fresh(camera_->timestamp_us, kDeviceInputMaxAgeUs)) {
      invalid_input("IR_INPUT_OBSERVABILITY_LOST"); return false;
    }
    request.request = 0xb0; request.value = camera_->ir_percent / 2;
  }
  const auto result = perform(static_cast<unsigned>(slot), request);
  if (!exact(result, request.read_length)) { retire(slot, "PERIPHERAL_IO_FAILED"); return false; }
  if (request.kind == Kind::ControlRead) { fan_rpm_ = u16(result.data, 0); fan_time_ = clock_.now_us(); }
  return true;
}
bool PandadRuntime::receive_step(std::uint32_t slot) {
  if (stopped_ || slot > 1) return false;
  const auto token = router_.current_token(slot, clock_.now_us());
  if (!token) return false;
  Request request; request.kind = Kind::BulkRead; request.endpoint = 0x81; request.read_length = 16384;
  const auto started = clock_.now_us();
  const auto result = perform(slot, request);
  const bool benign = (result.status == Status::Ok || result.status == Status::Short ||
                       (result.status == Status::Timeout && result.actual == 0)) &&
                      result.actual_known && !result.delivery_uncertain && result.actual == result.data.size();
  if (!benign) { retire(slot, "CAN_READ_FAILED"); return false; }
  if (result.data.empty()) return true;
  return static_cast<bool>(router_.receive(token.value, result.data.data(), result.data.size(), started, clock_.now_us()));
}
bool PandadRuntime::transmit_step(std::uint32_t slot) {
  if (stopped_ || slot > 1) return false;
  const auto state = snapshot();
  if (!state.simulated_tx_allowed || !device_fresh() || !device_->started) {
    router_.discard_pending(); count(counters_.denied_tx); return false;
  }
  auto intent = router_.take_tx(slot, clock_.now_us());
  if (!intent || !router_.validate_intent(intent.value, clock_.now_us())) return false;
  Request request; request.kind = Kind::BulkWrite; request.endpoint = 3; request.payload = intent.value.encoded;
  const auto result = perform(slot, request);
  // Bulk writes report count, not echo bytes. Never retry uncertain or partial
  // delivery, and never route the same command through a sibling/diagnostics.
  if (result.status != Status::Ok || !result.actual_known || result.delivery_uncertain ||
      result.actual != request.payload.size() || !result.data.empty()) {
    retire(slot, "CAN_WRITE_FAILED"); return false;
  }
  count(counters_.simulated_tx); return true;
}
std::vector<RoutedRxFrame> PandadRuntime::receive_frames() {
  std::vector<RoutedRxFrame> frames;
  if (!snapshot().panda_states_valid) return frames;
  for (unsigned slot = 0; slot < 2; ++slot) {
    for (std::size_t n = 0; n < kRxFramesPerSlot; ++n) {
      const auto frame = router_.take_rx(slot, clock_.now_us());
      if (!frame) break;
      if (router_.validate_rx_frame(frame.value, clock_.now_us())) frames.push_back(frame.value);
    }
  }
  return frames;
}
ControlSnapshot PandadRuntime::snapshot() {
  synchronize();
  if (device_ && !device_fresh()) invalid_input("STALE_DEVICE_STATE");
  if (fan_time_ && !fan_observable()) invalid_input("FAN_OBSERVABILITY_EXPIRED");
  return control_.snapshot(clock_.now_us());
}
PeripheralSnapshot PandadRuntime::peripheral_snapshot() {
  const auto state = snapshot(); const auto profile = registry_.profile();
  PeripheralSnapshot out; out.fan_slot = profile.roles.fan_control;
  if (!state.panda_states_valid) return out;
  bool ignition = false;
  for (const auto slot : profile.roles.ignition_sources) ignition |= (u16(state.slots[slot].health, 28) & 3) != 0;
  out.ignition = ignition;
  if (profile.roles.harness >= 0) out.harness = state.slots[profile.roles.harness].health[30];
  if (profile.roles.power_monitor >= 0) {
    const auto &health = state.slots[profile.roles.power_monitor].health;
    out.voltage = u16(health, 4); out.current = u16(health, 6);
    out.power_timestamp_us = state.slots[profile.roles.power_monitor].health_timestamp_us;
  }
  if (out.fan_slot >= 0 && fan_time_ && fresh(*fan_time_, kPeripheralMaxAgeUs)) {
    out.fan_rpm = fan_rpm_; out.timestamp_us = *fan_time_;
    out.valid = out.voltage.has_value() && out.current.has_value() &&
      fresh(out.power_timestamp_us, kHealthMaxAgeUs);
  }
  return out;
}
void PandadRuntime::invalid_input(const char *reason) {
  control_.fail_group(reason, clock_.now_us()); router_.discard_pending();
  device_.reset(); camera_.reset(); synchronize();
}
void PandadRuntime::shutdown() {
  if (stopped_) return;
  control_.fail_group("PLANNED_SHUTDOWN", clock_.now_us());
  router_.discard_pending();
  // Three bounded scheduler rounds: noOutput -> readback -> heartbeat(false).
  // A failed/uncertain operation retires only that handle; no retry or assertion
  // that this code runs after abrupt power/process loss.
  device_.reset(); camera_.reset();
  for (unsigned round = 0; round < 3; ++round) tick();
  for (auto *transport : transports_) if (transport) transport->cancel();
  registry_.invalidate_group("PLANNED_SHUTDOWN"); stopped_ = true;
}
}  // namespace openpilot::pandad::compatibility
