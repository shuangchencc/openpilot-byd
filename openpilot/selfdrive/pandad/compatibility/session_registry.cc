#include "session_registry.h"

#include <limits>
#include <stdexcept>
#include <tuple>

namespace openpilot::pandad::compatibility {
namespace {

bool text(const std::string &value) {
  if (value.empty() || value.size() > 128) return false;
  for (const auto c : value) if (static_cast<unsigned char>(c) <= 32 || static_cast<unsigned char>(c) >= 127) return false;
  return true;
}
bool fixture(const std::string &value) { return text(value) && value.compare(0, 8, "fixture:") == 0; }
bool slot(std::int32_t value) { return value == 0 || value == 1; }

void require(bool valid, const char *reason) {
  if (!valid) throw std::invalid_argument(reason);
}

bool hex(const std::string &s, std::size_t size) {
  if (s.size() != size) return false;
  for (const auto c : s) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  return true;
}
void validate_identity(const SessionIdentity &value, bool native) {
  if (native) {
    require(value.identity.rfind("uid:", 0) == 0 && hex(value.identity.substr(4), 24), "invalid native UID");
    require(text(value.exact_mcu) && value.exact_mcu.rfind("STM32", 0) == 0, "native exact MCU evidence required");
    require(hex(value.application_signature, 256), "native signature must preserve all observed bytes");
  } else {
  require(fixture(value.identity), "identity must be a bounded fixture string");
  require(fixture(value.exact_mcu), "exact MCU must be explicitly synthetic");
  require(fixture(value.application_signature), "application signature must be explicitly synthetic");
  }
  require((value.board_type == "DOS" && value.mcu_family == "F4") ||
          (value.board_type == "RED_PANDA" && value.mcu_family == "H7"), "unknown board/MCU pair");
  require(value.transport == "USB" || value.transport == "SPI", "unsupported transport");
  require(value.protocol_profile == kSessionProtocol, "unsupported protocol");
  require(value.capabilities.can_buses == std::array<std::uint32_t, 3>{{0, 1, 2}}, "invalid local buses");
  require(value.mcu_family != "F4" || !value.capabilities.can_fd, "F4 cannot declare CAN FD");
}

void validate_profile(const SessionProfile &p) {
  const bool native = p.schema == kNativeSessionProfileSchema && p.purpose == "native_read_only_candidate";
  require(native || (p.schema == kSessionProfileSchema && p.purpose == "offline_fixture"), "invalid profile schema/purpose");
  require(p.binding_status == "UNBOUND" || p.binding_status == (native ? "NATIVE_READ_ONLY_BOUND" : "SYNTHETIC_BOUND"), "invalid binding status");
  require(!p.device_access_enabled && !p.can_tx_enabled && !p.firmware_install_enabled, "all real permissions must remain false");
  require(p.expected_board_count == 2 && p.required_slots == std::array<std::uint32_t, 2>{{0, 1}}, "exactly two fixed required slots");
  require(p.bus_stride == 4 && p.ordinary_local_can_buses == std::array<std::uint32_t, 3>{{0, 1, 2}}, "invalid bus policy");
  require(p.slots[0].slot == 0 && p.slots[1].slot == 1, "noncanonical slots");
  const auto &r = p.roles;
  if (p.binding_status == "UNBOUND") {
    const SessionIdentity empty{};
    for (const auto &s : p.slots) require(s.expected == empty && s.safety_config_semantics.empty(), "partial unbound binding");
    require(r.vehicle_can == kRoleUnassigned && r.diagnostic == kRoleUnassigned && r.harness == kRoleUnassigned &&
            r.fan_control == kRoleUnassigned && r.power_monitor == kRoleUnassigned && r.ir_control == kRoleUnassigned &&
            r.ignition_sources.empty(), "unbound roles must remain unassigned");
    return;
  }
  for (const auto &s : p.slots) {
    validate_identity(s.expected, native);
    require(s.safety_config_semantics == "noOutput", "fixture safety must be noOutput");
  }
  require(p.slots[0].expected.identity != p.slots[1].expected.identity, "duplicate bound identity");
  const auto role = [&](std::int32_t owner, bool SessionCapabilities::*cap, bool unsupported) {
    if (unsupported && owner == kRoleUnsupported) {
      require(!(p.slots[0].expected.capabilities.*cap) && !(p.slots[1].expected.capabilities.*cap), "unsupported role hides capability");
    } else {
      require(slot(owner), "role needs an explicit slot");
      if (cap != nullptr) require(p.slots[static_cast<std::size_t>(owner)].expected.capabilities.*cap, "role owner lacks capability");
    }
  };
  role(r.vehicle_can, nullptr, false);
  role(r.diagnostic, &SessionCapabilities::diagnostic, false);
  role(r.harness, &SessionCapabilities::harness, false);
  role(r.fan_control, &SessionCapabilities::fan, true);
  role(r.power_monitor, &SessionCapabilities::power_monitor, true);
  role(r.ir_control, &SessionCapabilities::ir, true);
  require(!r.ignition_sources.empty() && r.ignition_sources.size() <= 2, "ignition sources must be explicit");
  if (r.ignition_sources.size() == 2) require(r.ignition_sources[0] != r.ignition_sources[1], "duplicate ignition source");
  for (const auto owner : r.ignition_sources) {
    require(owner < 2 && p.slots[owner].expected.capabilities.ignition, "ignition owner lacks capability");
  }
}

std::string mismatch(const SessionIdentity &a, const SessionIdentity &b) {
  if (a.board_type != b.board_type) return "MISMATCH_BOARD_TYPE";
  if (a.mcu_family != b.mcu_family) return "MISMATCH_MCU_FAMILY";
  if (a.exact_mcu != b.exact_mcu) return "MISMATCH_EXACT_MCU";
  if (a.transport != b.transport) return "MISMATCH_TRANSPORT";
  if (a.protocol_profile != b.protocol_profile) return "MISMATCH_PROTOCOL_PROFILE";
  if (!(a.capabilities == b.capabilities)) return "MISMATCH_CAPABILITIES";
  if (a.application_signature != b.application_signature) return "MISMATCH_APPLICATION_SIGNATURE";
  return {};
}

}  // namespace

bool operator==(const SessionCapabilities &a, const SessionCapabilities &b) {
  return std::tie(a.can_buses, a.can_fd, a.diagnostic, a.harness, a.ignition, a.fan, a.power_monitor, a.ir) ==
         std::tie(b.can_buses, b.can_fd, b.diagnostic, b.harness, b.ignition, b.fan, b.power_monitor, b.ir);
}
bool operator==(const SessionIdentity &a, const SessionIdentity &b) {
  return std::tie(a.identity, a.board_type, a.mcu_family, a.exact_mcu, a.transport, a.protocol_profile, a.application_signature) ==
         std::tie(b.identity, b.board_type, b.mcu_family, b.exact_mcu, b.transport, b.protocol_profile, b.application_signature) &&
         a.capabilities == b.capabilities;
}

SessionRegistry::SessionRegistry(const SessionProfile &profile, SessionCounterSeed seed) : profile_(profile) {
  validate_profile(profile_);
  state_.generation = seed.generation;
  state_.reason = profile_.binding_status == "UNBOUND" ? "UNBOUND_PROFILE" : "REQUIRED_BOARD_COUNT";
  for (std::size_t i = 0; i < 2; ++i) state_.slots[i] = {static_cast<std::uint32_t>(i), profile_.slots[i], false, seed.epochs[i], 0};
}

void SessionRegistry::exhaust() {
  state_.group_available = false;
  state_.permanently_closed = true;
  state_.reason = "COUNTER_EXHAUSTED";
  // No wrap, increment, or reusable live handle is permitted after exhaustion.
  for (auto &s : state_.slots) { s.connected = false; s.connection_token = 0; }
}

bool SessionRegistry::advance(std::uint64_t &counter) {
  if (counter == std::numeric_limits<std::uint64_t>::max()) { exhaust(); return false; }
  ++counter;
  return true;
}

bool SessionRegistry::close_slot(std::size_t slot_index) {
  auto &s = state_.slots[slot_index];
  if (!s.connected) return false;
  if (!advance(s.epoch)) return true;
  s.connected = false;
  s.connection_token = 0;
  return true;
}

void SessionRegistry::finalize(bool changed, bool available, const std::string &reason) {
  if (state_.permanently_closed) return;
  if (changed || state_.group_available != available) {
    if (!advance(state_.generation)) return;
  }
  state_.group_available = available;
  state_.reason = available ? "AVAILABLE_OFFLINE_ONLY" : reason;
}

SessionSnapshot SessionRegistry::reconcile(const std::vector<SessionObservation> &inventory) {
  if (state_.permanently_closed || profile_.binding_status == "UNBOUND") return state_;
  // Excess/ambiguous inventory cannot choose a winner. Retire all current
  // handles so deleting the extra entry cannot silently reauthorize old work.
  if (inventory.size() > 2) { invalidate_group("REQUIRED_BOARD_COUNT"); return state_; }
  if (inventory.size() == 2 && inventory[0].identity.identity == inventory[1].identity.identity) {
    // Two ambiguous observations are bounded work. Consume both known-identity
    // tokens so selecting one of these duplicate handles later cannot heal it.
    for (const auto &o : inventory) {
      for (std::size_t i = 0; i < 2; ++i) {
        if (o.identity.identity == profile_.slots[i].expected.identity && o.connection_token > token_high_watermark_[i]) {
          token_high_watermark_[i] = o.connection_token;
        }
      }
    }
    invalidate_group("DUPLICATE_IDENTITY"); return state_;
  }
  std::array<const SessionObservation *, 2> observed{{nullptr, nullptr}};
  std::string reason;
  if (inventory.size() != 2) reason = "REQUIRED_BOARD_COUNT";
  for (const auto &o : inventory) {
    if (o.identity.identity == profile_.slots[0].expected.identity) observed[0] = &o;
    else if (o.identity.identity == profile_.slots[1].expected.identity) observed[1] = &o;
    else reason = "UNEXPECTED_IDENTITY";
  }
  bool changed = false;
  for (std::size_t i = 0; i < 2 && !state_.permanently_closed; ++i) {
    auto &s = state_.slots[i];
    const auto *o = observed[i];
    if (o == nullptr) { changed = close_slot(i) || changed; continue; }
    auto invalid = mismatch(o->identity, profile_.slots[i].expected);
    const bool same_live_token = s.connected && o->connection_token == s.connection_token;
    if (o->connection_token == 0) invalid = "INVALID_CONNECTION_TOKEN";
    else if (!same_live_token && o->connection_token <= token_high_watermark_[i]) invalid = "RETIRED_CONNECTION_TOKEN";
    if (o->connection_token > token_high_watermark_[i]) token_high_watermark_[i] = o->connection_token;
    if (!invalid.empty()) {
      if (reason.empty()) reason = invalid;
      changed = close_slot(i) || changed;
      continue;
    }
    if (same_live_token) continue;
    // One epoch transition for direct replacement by a new handle of the same
    // validated identity; disconnect followed by reconnect is two transitions.
    if (!advance(s.epoch)) break;
    s.connected = true;
    s.connection_token = o->connection_token;
    changed = true;
  }
  const bool complete = reason.empty() && state_.slots[0].connected && state_.slots[1].connected;
  if (!complete && reason.empty()) reason = "REQUIRED_BOARD_COUNT";
  finalize(changed, complete, reason);
  return state_;
}

bool SessionRegistry::disconnect(std::uint32_t slot_index, std::uint64_t epoch, std::uint64_t connection_token) {
  if (slot_index >= 2 || state_.permanently_closed) return false;
  const auto &s = state_.slots[slot_index];
  if (!s.connected || s.epoch != epoch || s.connection_token != connection_token) return false;
  const bool changed = close_slot(slot_index);
  finalize(changed, false, "DISCONNECTED");
  return true;
}

void SessionRegistry::invalidate_group(const std::string &reason) {
  if (state_.permanently_closed || profile_.binding_status == "UNBOUND") return;
  bool changed = false;
  for (std::size_t i = 0; i < 2 && !state_.permanently_closed; ++i) changed = close_slot(i) || changed;
  // Diagnostic text is bounded and printable even when provided by a caller.
  finalize(changed, false, text(reason) ? reason : "INVALIDATED");
}

}  // namespace openpilot::pandad::compatibility
