#include "control_session.h"

#include <algorithm>
#include <limits>

namespace openpilot::pandad::compatibility {
namespace {
bool same_token(const RouteToken &a, const RouteToken &b) {
  return a.slot == b.slot && a.epoch == b.epoch && a.connection_token == b.connection_token &&
         a.group_generation == b.group_generation;
}
bool same_command(const ControlCommand &a, const ControlCommand &b) {
  return a.id == b.id && same_token(a.token, b.token) && a.kind == b.kind && a.value == b.value &&
         a.param == b.param && a.engaged == b.engaged && a.issued_us == b.issued_us &&
         !a.real_device_authorized && !b.real_device_authorized && a.exit_command == b.exit_command;
}
bool fresh(bool seen, std::uint64_t timestamp, std::uint64_t now, std::uint64_t budget) {
  return seen && timestamp <= now && now - timestamp < budget;
}
std::uint16_t u16(const std::vector<std::uint8_t> &b, std::size_t offset) {
  return static_cast<std::uint16_t>(b[offset]) | (static_cast<std::uint16_t>(b[offset + 1]) << 8);
}
std::uint32_t u32(const std::vector<std::uint8_t> &b, std::size_t offset) {
  return static_cast<std::uint32_t>(b[offset]) | (static_cast<std::uint32_t>(b[offset + 1]) << 8) |
         (static_cast<std::uint32_t>(b[offset + 2]) << 16) | (static_cast<std::uint32_t>(b[offset + 3]) << 24);
}
bool valid_health(const std::vector<std::uint8_t> &b) {
  if (b.size() != 48) return false;
  constexpr std::uint32_t known_faults = (1U << 0) | (1U << 1) | (1U << 2) | (1U << 3) | (1U << 4) |
    (1U << 5) | (1U << 7) | (1U << 8) | (1U << 15) | (1U << 18) | (1U << 20) | (1U << 21) |
    (1U << 22) | (1U << 23) | (1U << 25) | (1U << 26) | (1U << 27);
  constexpr std::uint8_t modes[] = {0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15, 17, 18,
                                  19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 30, 31, 33, 34, 35, 38};
  return (u32(b, 24) & ~known_faults) == 0 && (u16(b, 28) & ~0x1ffU) == 0 && b[30] <= 2 &&
    std::find(std::begin(modes), std::end(modes), b[31]) != std::end(modes) && b[34] <= 2 &&
    (u16(b, 35) & ~26U) == 0 && b[38] <= 100;
}
bool unhealthy(const std::vector<std::uint8_t> &b) {
  // In this noOutput-only profile controls_allowed must remain false. Historical
  // reset flags and power-save are not fabricated into an active healthy state.
  return u32(b, 24) != 0 || b[34] != 0 || (u16(b, 28) & ~3U) != 0;
}
bool valid_can_health(const std::vector<std::uint8_t> &b, bool fd) {
  if (b.size() != 64) return false;
  for (auto i : {0, 5, 6, 45, 46, 47}) if (b[i] > 1) return false;
  for (auto i : {7, 8, 9, 10}) if (b[i] > 7) return false;
  if (b[11] > 127) return false;
  return fd || (b[45] == 0 && b[46] == 0 && b[47] == 0);
}
bool counters_changed(const std::vector<std::uint8_t> &old, const std::vector<std::uint8_t> &now,
                      bool can) {
  if (old.empty()) return false;
  if (can) {
    for (auto i : {1, 13, 17, 21, 37, 60}) if (u32(old, i) != u32(now, i)) return true;
  } else {
    if (u32(now, 0) < u32(old, 0) || u16(old, 39) != u16(now, 39)) return true;
    for (auto i : {8, 12, 16, 20}) if (u32(old, i) != u32(now, i)) return true;
  }
  return false;
}
std::string bounded_reason(const std::string &reason) {
  if (reason.empty() || reason.size() > 128 ||
      !std::all_of(reason.begin(), reason.end(), [](unsigned char c) { return c >= 32 && c <= 126; })) {
    return "EXTERNAL_GROUP_FAILURE";
  }
  return reason;
}
}  // namespace

const char *control_kind_name(ControlKind kind) {
  switch (kind) {
    case ControlKind::ReadHealth: return "ReadHealth";
    case ControlKind::ReadCanHealth: return "ReadCanHealth";
    case ControlKind::SetAlternativeExperience: return "SetAlternativeExperience";
    case ControlKind::SetSafety: return "SetSafety";
    case ControlKind::Heartbeat: return "Heartbeat";
  }
  return "Unknown";
}

ControlSession::ControlSession(SessionRegistry &registry, ControlCounterSeed seed)
  : registry_(registry), next_command_id_(seed.next_command_id), control_epoch_(seed.control_epoch) {
  if (next_command_id_ == 0) clock_failed_ = true;
}

void ControlSession::synchronize(std::uint64_t now_us) {
  const auto next = registry_.snapshot();
  if (initialized_ && next.generation == registry_state_.generation) return;
  const bool previous_generation = initialized_;
  initialized_ = true;
  advance_control_epoch();
  registry_state_ = next;
  generation_started_us_ = now_us;
  boards_ = {};
  for (std::size_t i = 0; i < boards_.size(); ++i) {
    const auto &slot = next.slots[i];
    boards_[i].view.token = {static_cast<std::uint32_t>(i), slot.epoch, slot.connection_token, next.generation};
    boards_[i].view.connected = slot.connected;
    boards_[i].exit_phase = slot.connected ? (previous_generation ? 3 : 1) : 0;
  }
  recovery_required_ = true;
  armed_ = disabled_edge_seen_ = selfdrive_seen_ = false;
  reason_ = next.group_available ? "OFFROAD_CONFIGURATION_REQUIRED" : next.reason;
}

void ControlSession::advance_control_epoch() {
  if (control_epoch_ == std::numeric_limits<std::uint64_t>::max()) {
    clock_failed_ = true;
    registry_.invalidate_group("CONTROL_EPOCH_EXHAUSTED");
  } else {
    ++control_epoch_;
  }
}

void ControlSession::trip(const std::string &reason) {
  advance_control_epoch();
  recovery_required_ = true;
  armed_ = disabled_edge_seen_ = selfdrive_seen_ = false;
  reason_ = bounded_reason(reason);
  for (auto &board : boards_) {
    board.stage = Stage::Unconfigured;
    board.view.safety_readback_matched = false;
    board.pending.reset();
    board.exit_phase = board.view.connected ? 3 : 0;
  }
}

bool ControlSession::prepare(std::uint64_t now_us) {
  synchronize(now_us);
  if (clock_failed_) return false;
  if (last_now_ && now_us < *last_now_) {
    clock_failed_ = true;
    trip("CLOCK_ROLLBACK");
    registry_.invalidate_group("CONTROL_CLOCK_ROLLBACK");
    synchronize(now_us);
    reason_ = "CLOCK_ROLLBACK";
    return false;
  }
  last_now_ = now_us;
  if (armed_ && !fresh(selfdrive_seen_, selfdrive_.timestamp_us, now_us, kSelfdriveMaxAgeUs)) {
    trip("SELFDRIVE_EXPIRED");
  }
  if (!recovery_required_) {
    for (const auto &board : boards_) {
      if (!board.view.connected) { trip("REQUIRED_BOARD_MISSING"); break; }
      if ((board.heartbeat_seen && !fresh(true, board.heartbeat_timestamp_us, now_us, kHeartbeatMaxAgeUs)) ||
          (!board.heartbeat_seen && now_us - generation_started_us_ >= kHeartbeatMaxAgeUs)) {
        trip("HEARTBEAT_EXPIRED"); break;
      }
      if (!board.view.health.empty() && !fresh(true, board.view.health_timestamp_us, now_us, kHealthMaxAgeUs)) {
        trip("HEALTH_EXPIRED"); break;
      }
      bool stale = false;
      for (std::size_t bus = 0; bus < 3; ++bus) {
        stale |= !board.view.can_health[bus].empty() &&
                 !fresh(true, board.view.can_health_timestamp_us[bus], now_us, kHealthMaxAgeUs);
      }
      if (stale) { trip("CAN_HEALTH_EXPIRED"); break; }
    }
  }
  return true;
}

bool ControlSession::current(const RouteToken &token) const {
  const auto actual = registry_.snapshot();
  if (token.slot >= 2 || token.group_generation != actual.generation) return false;
  const auto &slot = actual.slots[token.slot];
  return slot.connected && slot.epoch == token.epoch && slot.connection_token == token.connection_token;
}

bool ControlSession::configure(const std::vector<SlotSafetyConfig> &configs, bool offroad,
                               std::uint64_t generation, std::uint64_t now_us) {
  // Stale envelopes cannot poison a new generation with old clocks or policy.
  if (generation != registry_.snapshot().generation) return false;
  if (!prepare(now_us)) return false;
  if (!offroad || !registry_state_.group_available || configs.size() != 2) {
    trip("INVALID_OFFROAD_CONFIGURATION"); return false;
  }
  for (std::size_t i = 0; i < 2; ++i) {
    if (configs[i].slot != i || configs[i].model != kNoOutputSafetyModel || configs[i].param > 65535 ||
        configs[i].alternative_experience > 65535 || (configs[i].alternative_experience & ~26U) != 0) {
      trip("UNSUPPORTED_SAFETY_CONFIGURATION"); return false;
    }
  }
  trip("CONFIGURATION_PENDING");
  recovery_required_ = false;
  for (std::size_t i = 0; i < 2; ++i) {
    auto &b = boards_[i];
    b.config = configs[i];
    b.stage = Stage::NeedAlternative;
    b.view.health.clear();
    for (auto &bytes : b.view.can_health) bytes.clear();
    b.view.health_valid = false;
    b.view.can_health_valid = {};
  }
  return true;
}

bool ControlSession::health_complete(std::uint64_t now_us) const {
  if (!registry_state_.group_available || recovery_required_) return false;
  for (const auto &b : boards_) {
    if (!fresh(!b.view.health.empty(), b.view.health_timestamp_us, now_us, kHealthMaxAgeUs)) return false;
    for (std::size_t bus = 0; bus < 3; ++bus) {
      if (!fresh(!b.view.can_health[bus].empty(), b.view.can_health_timestamp_us[bus], now_us, kHealthMaxAgeUs)) return false;
    }
  }
  return true;
}
bool ControlSession::ready(std::uint64_t now_us) const {
  if (!health_complete(now_us)) return false;
  for (const auto &b : boards_) {
    if (b.stage != Stage::Configured || !b.view.safety_readback_matched ||
        !fresh(b.heartbeat_seen, b.heartbeat_timestamp_us, now_us, kHeartbeatMaxAgeUs)) return false;
  }
  return true;
}

bool ControlSession::update_selfdrive(const SelfdriveSample &sample, std::uint64_t now_us) {
  if (sample.generation != registry_.snapshot().generation) return false;
  if (!prepare(now_us)) return false;
  if (!sample.valid || !fresh(true, sample.timestamp_us, now_us, kSelfdriveMaxAgeUs) ||
      (selfdrive_seen_ && sample.timestamp_us <= selfdrive_.timestamp_us)) {
    trip("INVALID_SELFDRIVE_STATE"); return false;
  }
  selfdrive_ = sample;
  selfdrive_seen_ = true;
  if (!sample.enabled) {
    if (armed_) {
      advance_control_epoch();
      for (auto &b : boards_) { b.exit_phase = b.view.connected ? 1 : 0; b.pending.reset(); }
    }
    armed_ = false;
    disabled_edge_seen_ = ready(now_us);
    return true;
  }
  if (!armed_ && (!disabled_edge_seen_ || !ready(now_us))) {
    reason_ = "FRESH_DISABLED_ENABLED_EDGE_REQUIRED";
    return false;
  }
  if (!ready(now_us)) { trip("GROUP_NOT_READY"); return false; }
  armed_ = true;
  disabled_edge_seen_ = false;
  reason_ = "SIMULATED_READY_NOOUTPUT";
  return true;
}

ControlCommand ControlSession::issue(std::size_t slot, ControlKind kind, std::uint64_t now_us,
                                    std::uint32_t value, std::uint32_t param, bool engaged, bool exit_command) {
  ControlCommand command{next_command_id_++, boards_[slot].view.token, kind, value, param, engaged, now_us, false, exit_command};
  boards_[slot].pending = command;
  return command;
}

std::vector<ControlCommand> ControlSession::poll(std::uint64_t now_us) {
  std::vector<ControlCommand> result;
  if (!prepare(now_us)) return result;
  // Bound the pending lifetime even if an adapter fails to deliver completion.
  for (std::size_t i = 0; i < 2; ++i) {
    const auto &pending = boards_[i].pending;
    if (pending && now_us - pending->issued_us >= kControlCommandMaxAgeUs) {
      retire_command_slot(i, "CONTROL_COMPLETION_EXPIRED");
    }
  }
  if (next_command_id_ >= std::numeric_limits<std::uint64_t>::max() - 2) {
    clock_failed_ = true; trip("COMMAND_ID_EXHAUSTED"); registry_.invalidate_group("COMMAND_ID_EXHAUSTED"); return result;
  }
  for (std::size_t i = 0; i < 2; ++i) {
    auto &b = boards_[i];
    if (!b.view.connected || b.pending) continue;
    const bool engaged = armed_ && ready(now_us) && fresh(selfdrive_seen_, selfdrive_.timestamp_us, now_us, kSelfdriveMaxAgeUs);
    if (b.exit_phase == 3) {
      result.push_back(issue(i, ControlKind::SetSafety, now_us, kNoOutputSafetyModel, 0, false, true));
    } else if (b.exit_phase == 2) {
      result.push_back(issue(i, ControlKind::ReadHealth, now_us, 0, 0, false, true));
    } else if (b.exit_phase == 1) {
      result.push_back(issue(i, ControlKind::Heartbeat, now_us, 0, 0, false, true));
    } else if (!b.heartbeat_seen || now_us - b.heartbeat_timestamp_us >= kHeartbeatPeriodUs ||
        b.view.heartbeat_engaged != engaged) {
      result.push_back(issue(i, ControlKind::Heartbeat, now_us, engaged ? 1 : 0, 0, engaged));
    } else if (!recovery_required_ && b.stage == Stage::NeedAlternative) {
      result.push_back(issue(i, ControlKind::SetAlternativeExperience, now_us, b.config.alternative_experience));
    } else if (!recovery_required_ && b.stage == Stage::NeedSafety) {
      result.push_back(issue(i, ControlKind::SetSafety, now_us, b.config.model, b.config.param));
    } else if (!recovery_required_ && (b.stage == Stage::NeedReadback || b.view.health.empty() ||
               now_us - b.view.health_timestamp_us >= kHealthPeriodUs)) {
      result.push_back(issue(i, ControlKind::ReadHealth, now_us));
    } else if (!recovery_required_) {
      for (std::uint32_t bus = 0; bus < 3; ++bus) {
        if (b.view.can_health[bus].empty() || now_us - b.view.can_health_timestamp_us[bus] >= kHealthPeriodUs) {
          result.push_back(issue(i, ControlKind::ReadCanHealth, now_us, bus)); break;
        }
      }
    }
  }
  return result;
}

bool ControlSession::validate_command(const ControlCommand &command, std::uint64_t now_us) {
  if (!current(command.token)) return false;
  if (!prepare(now_us)) return false;
  const auto &b = boards_[command.token.slot];
  return b.pending && same_command(command, *b.pending) &&
    fresh(true, command.issued_us, now_us, kControlCommandMaxAgeUs) &&
    (!command.engaged || (armed_ && ready(now_us)));
}

void ControlSession::retire_command_slot(std::size_t slot, const std::string &reason) {
  const auto token = boards_[slot].view.token;
  trip(reason);
  registry_.disconnect(token.slot, token.epoch, token.connection_token);
  synchronize(last_now_.value_or(0));
  reason_ = bounded_reason(reason);
}

bool ControlSession::complete(const ControlCommand &command, bool success,
                              const std::vector<std::uint8_t> &response, std::uint64_t now_us) {
  if (!current(command.token)) return false;
  if (!prepare(now_us)) return false;
  const auto slot = command.token.slot;
  auto &b = boards_[slot];
  if (!b.pending || !same_command(command, *b.pending)) return false;
  if (!fresh(true, command.issued_us, now_us, kControlCommandMaxAgeUs)) {
    retire_command_slot(slot, "CONTROL_COMPLETION_EXPIRED"); return false;
  }
  if (!success) { retire_command_slot(slot, "CONTROL_TRANSFER_FAILED"); return false; }
  b.pending.reset();
  if (command.kind != ControlKind::ReadHealth && command.kind != ControlKind::ReadCanHealth && !response.empty()) {
    retire_command_slot(slot, "UNEXPECTED_WRITE_RESPONSE"); return false;
  }
  if (command.exit_command && command.kind != ControlKind::Heartbeat) {
    if (command.kind == ControlKind::SetSafety) {
      b.exit_phase = 2;
    } else if (command.kind == ControlKind::ReadHealth) {
      if (!valid_health(response) || unhealthy(response) || response[31] != kNoOutputSafetyModel || u16(response, 32) != 0) {
        retire_command_slot(slot, "EXIT_READBACK_FAILED"); return false;
      }
      b.view.health = response;
      b.view.health_timestamp_us = now_us;
      b.exit_phase = 1;
    }
    return true;
  }
  switch (command.kind) {
    case ControlKind::Heartbeat:
      b.heartbeat_seen = true;
      b.heartbeat_timestamp_us = now_us;
      b.view.heartbeat_engaged = command.engaged;
      b.exit_phase = 0;
      break;
    case ControlKind::SetAlternativeExperience:
      b.stage = Stage::NeedSafety;
      break;
    case ControlKind::SetSafety:
      b.stage = Stage::NeedReadback;
      break;
    case ControlKind::ReadHealth: {
      if (!valid_health(response)) { b.view.health.clear(); trip("INVALID_HEALTH"); return false; }
      if (unhealthy(response) || counters_changed(b.view.health, response, false)) {
        b.view.health = response; b.view.health_timestamp_us = now_us;
        trip("BOARD_HEALTH_FAULT"); return false;
      }
      if (b.stage == Stage::NeedReadback || b.stage == Stage::Configured) {
        if (response[31] != b.config.model || u16(response, 32) != b.config.param ||
            u16(response, 35) != b.config.alternative_experience) {
          b.view.health = response; b.view.health_timestamp_us = now_us;
          trip("SAFETY_READBACK_MISMATCH"); return false;
        }
        b.stage = Stage::Configured;
        b.view.safety_readback_matched = true;
      }
      b.view.health = response;
      b.view.health_timestamp_us = now_us;
      break;
    }
    case ControlKind::ReadCanHealth: {
      const auto bus = command.value;
      if (!valid_can_health(response, registry_state_.slots[slot].binding.expected.capabilities.can_fd)) {
        b.view.can_health[bus].clear(); trip("INVALID_CAN_HEALTH"); return false;
      }
      if (response[0] || response[5] || response[6] || counters_changed(b.view.can_health[bus], response, true)) {
        b.view.can_health[bus] = response; b.view.can_health_timestamp_us[bus] = now_us;
        trip("CAN_HEALTH_FAULT"); return false;
      }
      b.view.can_health[bus] = response;
      b.view.can_health_timestamp_us[bus] = now_us;
      break;
    }
  }
  return true;
}

ControlSnapshot ControlSession::snapshot(std::uint64_t now_us) {
  const bool clock_ok = prepare(now_us);
  ControlSnapshot result;
  result.generation = registry_state_.generation;
  result.control_epoch = control_epoch_;
  for (std::size_t i = 0; i < 2; ++i) {
    result.slots[i] = boards_[i].view;
    auto &view = result.slots[i];
    view.health_valid = clock_ok && fresh(!view.health.empty(), view.health_timestamp_us, now_us, kHealthMaxAgeUs);
    for (std::size_t bus = 0; bus < 3; ++bus) {
      view.can_health_valid[bus] = clock_ok && fresh(!view.can_health[bus].empty(), view.can_health_timestamp_us[bus], now_us, kHealthMaxAgeUs);
    }
    view.heartbeat_fresh = clock_ok && fresh(boards_[i].heartbeat_seen, boards_[i].heartbeat_timestamp_us, now_us, kHeartbeatMaxAgeUs);
  }
  result.panda_states_valid = clock_ok && health_complete(now_us);
  result.configured = clock_ok && ready(now_us);
  result.armed = clock_ok && armed_;
  result.simulated_tx_allowed = result.configured && result.armed &&
    fresh(selfdrive_seen_, selfdrive_.timestamp_us, now_us, kSelfdriveMaxAgeUs);
  result.recovery_required = recovery_required_;
  result.reason = reason_;
  return result;
}

void ControlSession::fail_group(const std::string &reason, std::uint64_t now_us) {
  if (prepare(now_us)) trip(reason);
}

}  // namespace openpilot::pandad::compatibility
