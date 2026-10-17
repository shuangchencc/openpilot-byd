#include "../control_session.h"

#include <algorithm>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

using namespace openpilot::pandad::compatibility;
namespace {
std::size_t assertions = 0;
void check(bool value, int line) {
  ++assertions;
  if (!value) throw std::runtime_error("assertion at line " + std::to_string(line));
}
#define CHECK(value) check(static_cast<bool>(value), __LINE__)
SessionProfile fixture(std::uint32_t dos_slot = 0) {
  SessionProfile p;
  p.binding_status = "SYNTHETIC_BOUND";
  SessionIdentity dos{"fixture:dos", "DOS", "F4", "fixture:unknown-F4", "SPI", kSessionProtocol,
                     {{0, 1, 2}, false, true, false, true, true, true, false}, "fixture:dos-app"};
  SessionIdentity red{"fixture:external", "RED_PANDA", "H7", "fixture:unknown-H7", "USB", kSessionProtocol,
                     {{0, 1, 2}, true, true, true, true, false, false, false}, "fixture:red-app"};
  const auto red_slot = 1U - dos_slot;
  p.slots[dos_slot] = {dos_slot, dos, "noOutput"};
  p.slots[red_slot] = {red_slot, red, "noOutput"};
  p.roles = {static_cast<std::int32_t>(red_slot), static_cast<std::int32_t>(red_slot), static_cast<std::int32_t>(red_slot),
             {dos_slot, red_slot}, static_cast<std::int32_t>(dos_slot), static_cast<std::int32_t>(dos_slot), kRoleUnsupported};
  return p;
}
void put16(std::vector<std::uint8_t> &b, std::size_t i, std::uint16_t value) {
  b[i] = static_cast<std::uint8_t>(value); b[i + 1] = static_cast<std::uint8_t>(value >> 8);
}
void put32(std::vector<std::uint8_t> &b, std::size_t i, std::uint32_t value) {
  for (std::size_t n = 0; n < 4; ++n) b[i + n] = static_cast<std::uint8_t>(value >> (8 * n));
}
struct Rig {
  SessionProfile profile;
  SessionRegistry registry;
  ControlSession control;
  std::uint64_t now = 1000;
  std::vector<SlotSafetyConfig> configs{{0, 19, 0x1234, 2}, {1, 19, 0x4321, 24}};
  std::vector<ControlCommand> transcript;
  explicit Rig(std::uint32_t dos_slot = 0) : profile(fixture(dos_slot)), registry(profile), control(registry) {
    connect(1, 1);
  }
  void connect(std::uint64_t t0, std::uint64_t t1) {
    registry.reconcile({{profile.slots[1].expected, t1}, {profile.slots[0].expected, t0}});
  }
  std::vector<std::uint8_t> reply(const ControlCommand &command) const {
    if (command.kind == ControlKind::ReadHealth) {
      std::vector<std::uint8_t> bytes(48);
      put32(bytes, 0, 12345); put16(bytes, 4, 12001); put16(bytes, 6, 745);
      bytes[28] = 1; bytes[30] = 2; bytes[31] = 19;
      put16(bytes, 32, static_cast<std::uint16_t>(command.exit_command ? 0 : configs[command.token.slot].param));
      put16(bytes, 35, static_cast<std::uint16_t>(configs[command.token.slot].alternative_experience));
      bytes[37] = 127; bytes[38] = 23; put16(bytes, 41, 1234); put16(bytes, 43, 4321);
      put16(bytes, 45, 258); bytes[47] = 75;
      return bytes;
    }
    if (command.kind == ControlKind::ReadCanHealth) {
      std::vector<std::uint8_t> bytes(64); put16(bytes, 41, 500); put16(bytes, 43, 2000);
      return bytes;
    }
    return {};
  }
  void start() { CHECK(control.configure(configs, true, registry.snapshot().generation, now)); }
  std::vector<ControlCommand> poll() { auto c = control.poll(now); transcript.insert(transcript.end(), c.begin(), c.end()); return c; }
  void finish(const std::vector<ControlCommand> &commands) {
    for (const auto &c : commands) {
      CHECK(!c.real_device_authorized); CHECK(control.validate_command(c, now));
      CHECK(control.complete(c, true, reply(c), now));
    }
  }
  void pump(std::size_t rounds = 10) { for (std::size_t i = 0; i < rounds; ++i) finish(poll()); }
  void ready() { start(); pump(); CHECK(control.snapshot(now).configured); }
  void engage() {
    ++now; CHECK(control.update_selfdrive({true, false, now, registry.snapshot().generation}, now));
    ++now; CHECK(control.update_selfdrive({true, true, now, registry.snapshot().generation}, now));
    CHECK(control.snapshot(now).simulated_tx_allowed); CHECK(!control.snapshot(now).real_tx_authorized);
  }
  ControlCommand next(ControlKind kind, std::uint32_t slot = 0, std::uint32_t bus = 0) {
    for (int n = 0; n < 16; ++n) {
      auto cmds = poll();
      for (const auto &c : cmds) {
        if (!c.exit_command && c.kind == kind && c.token.slot == slot && (kind != ControlKind::ReadCanHealth || c.value == bus)) return c;
        CHECK(control.complete(c, true, reply(c), now));
      }
    }
    throw std::runtime_error("command not found");
  }
};
void denied(Rig &r) {
  const auto s = r.control.snapshot(r.now);
  CHECK(!s.simulated_tx_allowed); CHECK(!s.real_tx_authorized); CHECK(s.recovery_required);
}
}  // namespace

int main() {
  std::size_t groups = 0;
  const auto test = [&](const char *name, const std::function<void()> &run) {
    run(); ++groups; std::cout << "PASS " << name << '\n';
  };
  try {
    test("unbound_no_device_permission_and_fixed_slots", [] {
      SessionRegistry registry(SessionProfile{}); ControlSession c(registry);
      const auto s = c.snapshot(0); CHECK(s.slots.size() == 2); CHECK(!s.panda_states_valid);
      CHECK(!s.configured); CHECK(!s.real_tx_authorized); CHECK(c.poll(0).empty());
      CHECK(!c.configure({{0}, {1}}, true, 0, 0));
    });
    test("exact_slot_config_cardinality_and_nooutput_only", [] {
      for (std::size_t count : {0U, 1U, 3U}) {
        Rig r; auto cfg = r.configs; cfg.resize(count); CHECK(!r.control.configure(cfg, true, r.registry.snapshot().generation, r.now)); denied(r);
      }
      for (std::uint32_t model : {0U, 3U, 17U, 35U, 65536U}) {
        Rig r; auto cfg = r.configs; cfg[1].model = model;
        CHECK(!r.control.configure(cfg, true, r.registry.snapshot().generation, r.now)); denied(r);
      }
      for (int mutation = 0; mutation < 4; ++mutation) {
        Rig r; auto cfg = r.configs;
        if (mutation == 0) cfg[1].slot = 0;
        if (mutation == 1) cfg[1].param = 65536;
        if (mutation == 2) cfg[1].alternative_experience = 1;
        if (mutation == 3) cfg[1].alternative_experience = 65536;
        CHECK(!r.control.configure(cfg, true, r.registry.snapshot().generation, r.now)); denied(r);
      }
      Rig r; CHECK(!r.control.configure(r.configs, false, r.registry.snapshot().generation, r.now)); denied(r);
    });
    test("per_slot_write_order_readback_and_role_permutations", [] {
      for (std::uint32_t dos : {0U, 1U}) {
        Rig r(dos); r.ready();
        CHECK(r.transcript.size() == 18);
        for (std::size_t slot = 0; slot < 2; ++slot) {
          std::vector<ControlCommand> own;
          for (const auto &c : r.transcript) if (c.token.slot == slot) own.push_back(c);
          CHECK(own.size() == 9); CHECK(own[0].kind == ControlKind::SetSafety && own[0].value == 19 && own[0].param == 0 && own[0].exit_command);
          CHECK(own[1].kind == ControlKind::ReadHealth && own[1].exit_command);
          CHECK(own[2].kind == ControlKind::Heartbeat && !own[2].engaged);
          CHECK(own[3].kind == ControlKind::SetAlternativeExperience); CHECK(own[3].value == r.configs[slot].alternative_experience);
          CHECK(own[4].kind == ControlKind::SetSafety); CHECK(own[4].value == 19); CHECK(own[4].param == r.configs[slot].param);
          CHECK(own[5].kind == ControlKind::ReadHealth);
          for (std::size_t bus = 0; bus < 3; ++bus) CHECK(own[6 + bus].kind == ControlKind::ReadCanHealth && own[6 + bus].value == bus);
        }
        const auto s = r.control.snapshot(r.now); CHECK(s.panda_states_valid); CHECK(!s.armed); CHECK(!s.simulated_tx_allowed);
        for (const auto &b : s.slots) { CHECK(b.health.size() == 48); CHECK(b.safety_readback_matched); CHECK(b.heartbeat_fresh); }
      }
    });
    test("incomplete_health_group_never_enables", [] {
      Rig r; r.start();
      for (int n = 0; n < 8; ++n) { r.finish(r.poll()); CHECK(!r.control.snapshot(r.now).configured); }
      auto last = r.poll(); CHECK(last.size() == 2);
      CHECK(r.control.complete(last[0], true, r.reply(last[0]), r.now)); CHECK(!r.control.snapshot(r.now).panda_states_valid);
      CHECK(r.control.complete(last[1], true, r.reply(last[1]), r.now)); CHECK(r.control.snapshot(r.now).configured);
    });
    test("health_every_short_prefix_and_legacy_rejected", [] {
      for (std::size_t size = 0; size < 48; ++size) {
        Rig r; r.start(); auto c = r.next(ControlKind::ReadHealth);
        CHECK(!r.control.complete(c, true, std::vector<std::uint8_t>(size), r.now)); denied(r);
      }
      for (std::size_t size : {49U, 58U, 4096U}) {
        Rig r; r.start(); auto c = r.next(ControlKind::ReadHealth);
        CHECK(!r.control.complete(c, true, std::vector<std::uint8_t>(size), r.now)); denied(r);
      }
    });
    test("can_health_every_short_prefix_and_oversize_rejected", [] {
      for (std::size_t size = 0; size <= 65; ++size) {
        if (size == 64) continue;
        Rig r; r.start(); auto c = r.next(ControlKind::ReadCanHealth);
        CHECK(!r.control.complete(c, true, std::vector<std::uint8_t>(size), r.now)); denied(r);
      }
    });
    test("health_enums_flags_faults_and_nooutput_controls_semantics", [] {
      const std::vector<std::pair<std::size_t, std::uint8_t>> mutations = {
        {27, 0x80}, {29, 2}, {30, 3}, {31, 7}, {34, 3}, {35, 1}, {38, 101},
        {24, 1}, {34, 1}, {28, 4}, {28, 8}, {28, 16}, {28, 32}, {28, 64}, {28, 128}, {29, 1}};
      for (auto [offset, value] : mutations) {
        Rig r; r.start(); auto c = r.next(ControlKind::ReadHealth); auto bytes = r.reply(c); bytes[offset] = value;
        CHECK(!r.control.complete(c, true, bytes, r.now)); denied(r);
      }
    });
    test("can_health_enums_busoff_and_f4_fd_capability", [] {
      const std::vector<std::pair<std::size_t, std::uint8_t>> mutations = {
        {0, 2}, {5, 2}, {6, 2}, {7, 8}, {8, 8}, {9, 8}, {10, 8}, {11, 128},
        {45, 2}, {46, 2}, {47, 2}, {45, 1}, {46, 1}, {47, 1}, {0, 1}, {5, 1}, {6, 1}};
      for (auto [offset, value] : mutations) {
        Rig r; r.start(); auto c = r.next(ControlKind::ReadCanHealth); auto bytes = r.reply(c); bytes[offset] = value;
        CHECK(!r.control.complete(c, true, bytes, r.now)); denied(r);
      }
      Rig r; r.start(); auto c = r.next(ControlKind::ReadCanHealth, 1); auto bytes = r.reply(c); bytes[45] = bytes[46] = bytes[47] = 1;
      CHECK(r.control.complete(c, true, bytes, r.now));
    });
    test("safety_model_param_and_alternative_readback_mismatch", [] {
      for (std::size_t offset : {31U, 32U, 35U}) {
        Rig r; r.start(); auto c = r.next(ControlKind::ReadHealth, 1); auto bytes = r.reply(c); bytes[offset] = 0;
        CHECK(!r.control.complete(c, true, bytes, r.now)); denied(r);
      }
    });
    test("fresh_disabled_enabled_edge_and_no_real_tx", [] {
      Rig r; r.ready();
      ++r.now; CHECK(!r.control.update_selfdrive({true, true, r.now, r.registry.snapshot().generation}, r.now));
      CHECK(!r.control.snapshot(r.now).simulated_tx_allowed);
      r.engage(); const auto epoch = r.control.snapshot(r.now).control_epoch;
      auto engaged = r.poll(); CHECK(engaged.size() == 2);
      for (const auto &c : engaged) CHECK(c.kind == ControlKind::Heartbeat && c.engaged);
      r.finish(engaged);
      ++r.now; CHECK(r.control.update_selfdrive({true, false, r.now, r.registry.snapshot().generation}, r.now));
      CHECK(r.control.snapshot(r.now).control_epoch > epoch); CHECK(!r.control.snapshot(r.now).simulated_tx_allowed);
      for (const auto &c : r.poll()) CHECK(c.kind == ControlKind::Heartbeat && !c.engaged);
    });
    test("selfdrive_freshness_invalid_future_duplicate_boundaries", [] {
      for (std::uint64_t age : {kSelfdriveMaxAgeUs - 1, kSelfdriveMaxAgeUs, kSelfdriveMaxAgeUs + 1}) {
        Rig r; r.ready(); r.engage(); const auto stamp = r.now; r.now += age;
        CHECK(r.control.snapshot(r.now).simulated_tx_allowed == (age < kSelfdriveMaxAgeUs));
        if (age >= kSelfdriveMaxAgeUs) { denied(r); for (const auto &c : r.poll()) CHECK(!c.engaged); }
        CHECK(stamp <= r.now);
      }
      for (int mode = 0; mode < 4; ++mode) {
        Rig r; r.ready(); r.engage(); auto sample = SelfdriveSample{true, true, r.now + 1, r.registry.snapshot().generation};
        if (mode == 0) sample.valid = false;
        if (mode == 1) sample.timestamp_us = r.now + 2;
        if (mode == 2) sample.timestamp_us = r.now;
        if (mode == 3) { r.now += kSelfdriveMaxAgeUs; sample.timestamp_us = r.now - kSelfdriveMaxAgeUs; }
        else ++r.now;
        CHECK(!r.control.update_selfdrive(sample, r.now)); denied(r);
      }
    });
    test("health_and_heartbeat_age_exact_boundary", [] {
      for (std::uint64_t age : {kHealthMaxAgeUs - 1, kHealthMaxAgeUs, kHealthMaxAgeUs + 1}) {
        Rig r; r.ready(); r.now += age;
        const auto s = r.control.snapshot(r.now);
        CHECK(s.panda_states_valid == (age < kHealthMaxAgeUs)); CHECK(s.configured == (age < kHealthMaxAgeUs));
        CHECK(s.slots[0].health_valid == (age < kHealthMaxAgeUs)); CHECK(s.slots[1].heartbeat_fresh == (age < kHeartbeatMaxAgeUs));
      }
    });
    test("heartbeat_period_fairness_and_no_duplicate_pending", [] {
      Rig r; r.ready(); CHECK(r.poll().empty());
      r.now += kHeartbeatPeriodUs - 1; CHECK(r.poll().empty());
      ++r.now; auto due = r.poll(); CHECK(due.size() == 2);
      for (const auto &c : due) CHECK(c.kind == ControlKind::Heartbeat);
      CHECK(r.poll().empty());
      CHECK(r.control.complete(due[1], true, {}, r.now));
      auto sibling = r.poll(); CHECK(sibling.size() == 1); CHECK(sibling[0].token.slot == 1);
      CHECK(sibling[0].kind == ControlKind::ReadHealth);
    });
    test("command_completion_age_boundary_and_uncertain_no_replay", [] {
      for (std::uint64_t age : {kControlCommandMaxAgeUs - 1, kControlCommandMaxAgeUs, kControlCommandMaxAgeUs + 1}) {
        Rig r; auto c = r.poll()[0]; r.now += age;
        CHECK(r.control.complete(c, true, {}, r.now) == (age < kControlCommandMaxAgeUs));
        if (age >= kControlCommandMaxAgeUs) CHECK(!r.registry.snapshot().slots[0].connected);
      }
      Rig r; r.start(); auto write = r.next(ControlKind::SetAlternativeExperience);
      CHECK(!r.control.complete(write, false, {}, r.now)); denied(r);
      CHECK(!r.control.complete(write, true, {}, r.now));
      auto surviving = r.poll(); CHECK(surviving.size() == 1); CHECK(surviving[0].token.slot == 1);
      CHECK(surviving[0].kind == ControlKind::SetSafety && surviving[0].value == 19 && surviving[0].param == 0);
    });
    test("pending_timeout_retires_slot_and_sibling_progresses", [] {
      Rig r; auto cmds = r.poll(); CHECK(cmds.size() == 2);
      CHECK(r.control.complete(cmds[1], true, {}, r.now)); r.now += kControlCommandMaxAgeUs;
      auto remaining = r.poll(); CHECK(!r.registry.snapshot().slots[0].connected);
      CHECK(remaining.size() == 1); CHECK(remaining[0].token.slot == 1); CHECK(!remaining[0].engaged);
    });
    test("forged_mutated_duplicate_and_unsolicited_completions", [] {
      Rig r; auto original = r.poll()[0];
      for (int m = 0; m < 9; ++m) {
        auto forged = original;
        if (m == 0) ++forged.id;
        if (m == 1) ++forged.token.epoch;
        if (m == 2) ++forged.token.connection_token;
        if (m == 3) ++forged.token.group_generation;
        if (m == 4) forged.kind = ControlKind::SetSafety;
        if (m == 5) ++forged.value;
        if (m == 6) ++forged.param;
        if (m == 7) ++forged.issued_us;
        if (m == 8) forged.real_device_authorized = true;
        CHECK(!r.control.validate_command(forged, r.now)); CHECK(!r.control.complete(forged, true, {}, r.now));
      }
      CHECK(r.control.complete(original, true, {}, r.now)); CHECK(!r.control.complete(original, true, {}, r.now));
    });
    test("disconnect_reconnect_requires_offroad_readback_and_new_edge", [] {
      Rig r; r.ready(); r.engage(); auto old = r.poll();
      const auto before = r.control.snapshot(r.now); const auto slot = r.registry.snapshot().slots[0];
      CHECK(r.registry.disconnect(0, slot.epoch, slot.connection_token)); denied(r);
      auto exits = r.poll(); CHECK(exits.size() == 1 && exits[0].token.slot == 1 && exits[0].kind == ControlKind::SetSafety && exits[0].value == 19);
      r.connect(2, 1); CHECK(!r.control.complete(old[0], true, {}, 0)); denied(r);
      CHECK(r.control.snapshot(r.now).control_epoch > before.control_epoch);
      ++r.now; CHECK(!r.control.update_selfdrive({true, true, r.now, r.registry.snapshot().generation}, r.now));
      r.ready(); CHECK(!r.control.snapshot(r.now).simulated_tx_allowed); r.engage();
    });
    test("stale_generations_are_rejected_before_clock_validation", [] {
      Rig r; r.ready(); auto old_generation = r.registry.snapshot().generation;
      const auto old = r.poll(); CHECK(old.empty());
      r.registry.invalidate_group("TEST"); r.connect(2, 2); r.ready();
      CHECK(!r.control.configure(r.configs, true, old_generation, 0));
      CHECK(!r.control.update_selfdrive({true, true, 0, old_generation}, 0));
      CHECK(r.control.snapshot(r.now).configured);
    });
    test("clock_rollback_and_wrap_permanently_close", [] {
      Rig r; r.ready(); r.engage(); --r.now; denied(r); CHECK(!r.registry.snapshot().group_available);
      r.now = 1000000; r.connect(2, 2); CHECK(!r.control.configure(r.configs, true, r.registry.snapshot().generation, r.now)); CHECK(r.poll().empty());
      Rig wrap; wrap.now = std::numeric_limits<std::uint64_t>::max() - 1000; wrap.ready(); wrap.now = 0;
      CHECK(wrap.control.poll(wrap.now).empty()); CHECK(!wrap.registry.snapshot().group_available);
    });
    test("counter_change_and_uptime_reset_close_group", [] {
      for (std::size_t offset : {0U, 8U, 12U, 16U, 20U, 39U}) {
        Rig r; r.ready(); r.now += kHealthPeriodUs; auto c = r.next(ControlKind::ReadHealth); auto bytes = r.reply(c);
        bytes[offset] = offset == 0 ? 0 : 1;
        CHECK(!r.control.complete(c, true, bytes, r.now)); denied(r);
      }
      for (std::size_t offset : {1U, 13U, 17U, 21U, 37U, 60U}) {
        Rig r; r.ready(); r.now += kHealthPeriodUs; auto c = r.next(ControlKind::ReadCanHealth); auto bytes = r.reply(c); bytes[offset] = 1;
        CHECK(!r.control.complete(c, true, bytes, r.now)); denied(r);
      }
    });
    test("external_fault_flushes_pending_and_epoch_with_sibling_exit", [] {
      Rig r; r.ready(); r.engage(); auto pending = r.poll(); const auto epoch = r.control.snapshot(r.now).control_epoch;
      r.control.fail_group("THERMAL_COMMAND_EXPIRED", r.now); denied(r);
      CHECK(r.control.snapshot(r.now).control_epoch > epoch);
      for (const auto &c : pending) CHECK(!r.control.validate_command(c, r.now));
      auto exit = r.poll(); CHECK(exit.size() == 2);
      for (const auto &c : exit) CHECK(c.kind == ControlKind::SetSafety && c.value == 19 && c.param == 0 && c.exit_command);
      r.finish(exit); r.pump(2); CHECK(r.poll().empty());
    });
    test("offroad_reconfiguration_never_implicitly_reengages", [] {
      Rig r; r.ready(); r.engage(); const auto epoch = r.control.snapshot(r.now).control_epoch;
      r.start(); CHECK(r.control.snapshot(r.now).control_epoch > epoch); CHECK(!r.control.snapshot(r.now).simulated_tx_allowed);
      r.pump(); CHECK(r.control.snapshot(r.now).configured);
      ++r.now; CHECK(!r.control.update_selfdrive({true, true, r.now, r.registry.snapshot().generation}, r.now)); r.engage();
    });
    test("unknown_replacement_and_extra_board_cannot_restore_control", [] {
      Rig r; r.ready(); r.engage(); auto changed = r.profile.slots[0].expected; changed.identity = "fixture:replacement";
      r.registry.reconcile({{changed, 2}, {r.profile.slots[1].expected, 1}}); denied(r);
      CHECK(!r.control.configure(r.configs, true, r.registry.snapshot().generation, r.now));
      r.registry.reconcile({{r.profile.slots[0].expected, 3}, {r.profile.slots[1].expected, 2}, {changed, 4}}); denied(r);
    });
    test("bounded_external_reason_and_exact_write_response", [] {
      Rig r; r.control.fail_group(std::string(1000, 'x'), r.now);
      CHECK(r.control.snapshot(r.now).reason == "EXTERNAL_GROUP_FAILURE");
      auto c = r.poll()[0]; CHECK(!r.control.complete(c, true, {0}, r.now)); CHECK(!r.registry.snapshot().slots[0].connected);
    });
    test("exit_safety_readback_and_failure_never_replay", [] {
      for (int mode = 0; mode < 4; ++mode) {
        Rig r; r.ready(); r.engage(); r.control.fail_group("FAULT", r.now);
        const auto exits = r.poll(); CHECK(exits.size() == 2);
        CHECK(exits[0].kind == ControlKind::SetSafety && exits[0].value == 19 && exits[0].param == 0);
        if (mode == 0) {
          CHECK(!r.control.complete(exits[0], false, {}, r.now));
        } else {
          r.finish(exits); const auto reads = r.poll(); CHECK(reads[0].exit_command);
          auto bytes = r.reply(reads[0]);
          if (mode == 1) bytes.pop_back();
          if (mode == 2) bytes[32] = 1;
          if (mode == 3) bytes[28] = 4;
          CHECK(!r.control.complete(reads[0], true, bytes, r.now));
        }
        CHECK(!r.registry.snapshot().slots[0].connected);
        for (const auto &c : r.poll()) CHECK(c.token.slot != 0);
        denied(r);
      }
    });
    test("independent_health_canhealth_and_heartbeat_deadlines", [] {
      {
        Rig r; r.ready(); r.now += kHeartbeatPeriodUs;
        r.finish(r.poll()); // Heartbeats refreshed, health intentionally frozen.
        r.now = 1000 + kHealthMaxAgeUs;
        CHECK(r.control.snapshot(r.now).reason == "HEALTH_EXPIRED"); denied(r);
      }
      {
        Rig r; r.ready(); r.now += kHeartbeatPeriodUs;
        r.finish(r.poll()); r.finish(r.poll()); // Heartbeats and health refreshed; CAN-health frozen.
        r.now = 1000 + kHealthMaxAgeUs;
        CHECK(r.control.snapshot(r.now).reason == "CAN_HEALTH_EXPIRED"); denied(r);
      }
      for (std::uint64_t age : {kHeartbeatMaxAgeUs - 1, kHeartbeatMaxAgeUs, kHeartbeatMaxAgeUs + 1}) {
        Rig r; r.start(); std::uint64_t heartbeat = 0;
        for (int n = 0; n < 9; ++n) {
          auto commands = r.poll();
          if (!commands.empty() && commands[0].kind == ControlKind::Heartbeat) heartbeat = r.now;
          r.finish(commands); ++r.now;
        }
        CHECK(heartbeat != 0); r.now = heartbeat + age;
        const auto s = r.control.snapshot(r.now);
        CHECK(s.slots[0].health_valid); CHECK(s.slots[0].can_health_valid[0]);
        CHECK(s.configured == (age < kHeartbeatMaxAgeUs));
        if (age >= kHeartbeatMaxAgeUs) CHECK(s.reason == "HEARTBEAT_EXPIRED");
      }
    });
    test("control_epoch_and_command_id_exhaustion_fail_closed", [] {
      for (auto seed : {ControlCounterSeed{std::numeric_limits<std::uint64_t>::max(), 1},
                        ControlCounterSeed{0, std::numeric_limits<std::uint64_t>::max() - 2},
                        ControlCounterSeed{0, 0}}) {
        auto p = fixture(); SessionRegistry registry(p);
        registry.reconcile({{p.slots[0].expected, 1}, {p.slots[1].expected, 1}});
        ControlSession c(registry, seed); CHECK(c.poll(1000).empty());
        CHECK(!c.snapshot(1000).simulated_tx_allowed); CHECK(!c.snapshot(1000).real_tx_authorized);
        CHECK(!c.configure({{0}, {1}}, true, registry.snapshot().generation, 1000));
      }
    });
    std::cout << "{\"suite\":\"control_session\",\"groups\":" << groups << ",\"assertions\":" << assertions
              << ",\"failures\":0,\"skips\":0,\"device_open\":false,\"real_can_tx\":false}" << '\n';
  } catch (const std::exception &e) {
    std::cerr << "FAIL " << e.what() << '\n'; return 1;
  }
  return 0;
}
