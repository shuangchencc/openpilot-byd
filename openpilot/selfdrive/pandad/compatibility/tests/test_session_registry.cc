#include "../session_registry.h"

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

std::vector<SessionObservation> inventory(const SessionProfile &p, std::uint64_t token0 = 1, std::uint64_t token1 = 1) {
  return {{p.slots[0].expected, token0}, {p.slots[1].expected, token1}};
}

bool rejects(const SessionProfile &p) {
  try { SessionRegistry r(p); return false; } catch (const std::invalid_argument &) { return true; }
}

void permissions(const SessionRegistry &r) {
  CHECK(!r.device_access_enabled()); CHECK(!r.can_tx_enabled()); CHECK(!r.firmware_install_enabled());
}
}  // namespace

int main() {
  std::size_t groups = 0;
  std::vector<std::string> names;
  const auto test = [&](const char *name, const std::function<void()> &run) {
    run(); ++groups; names.emplace_back(name); std::cout << "PASS " << name << '\n';
  };
  try {
    test("unbound_and_permission_contract", [] {
      SessionRegistry r(SessionProfile{});
      const auto initial = r.snapshot();
      CHECK(!initial.group_available); CHECK(initial.reason == "UNBOUND_PROFILE");
      CHECK(initial.slots[0].slot == 0 && initial.slots[1].slot == 1);
      CHECK(!r.reconcile(inventory(fixture())).group_available);
      r.invalidate_group("ANY_REASON"); CHECK(r.snapshot().generation == 0);
      CHECK(!r.disconnect(0, 0, 0)); permissions(r);
      auto p = fixture();
      for (auto member : {&SessionProfile::device_access_enabled, &SessionProfile::can_tx_enabled, &SessionProfile::firmware_install_enabled}) {
        auto changed = p; changed.*member = true; CHECK(rejects(changed));
      }
    });

    test("profile_schema_fixed_topology_and_binding_rejections", [] {
      std::vector<std::function<void(SessionProfile &)>> mutations = {
        [](auto &p) { p.schema = "other"; }, [](auto &p) { p.purpose = "runtime"; },
        [](auto &p) { p.binding_status = "BOUND"; }, [](auto &p) { p.expected_board_count = 1; },
        [](auto &p) { p.required_slots = {1, 0}; }, [](auto &p) { p.bus_stride = 3; },
        [](auto &p) { p.ordinary_local_can_buses = {0, 1, 3}; }, [](auto &p) { p.slots[0].slot = 1; },
        [](auto &p) { p.slots[1].slot = std::numeric_limits<std::uint32_t>::max(); },
        [](auto &p) { p.slots[1].expected.identity = p.slots[0].expected.identity; },
        [](auto &p) { p.slots[0].safety_config_semantics = "allOutput"; },
        [](auto &p) { p.slots[0].expected.identity = "real:serial"; },
        [](auto &p) { p.slots[0].expected.identity = std::string(129, 'x'); },
        [](auto &p) { p.slots[0].expected.exact_mcu = "STM32F4"; },
        [](auto &p) { p.slots[0].expected.application_signature = "fixture:bad\n"; },
        [](auto &p) { p.slots[0].expected.transport = "AUTO"; },
        [](auto &p) { p.slots[0].expected.protocol_profile = "current:unknown"; },
        [](auto &p) { p.slots[0].expected.mcu_family = "H7"; },
        [](auto &p) { p.slots[0].expected.board_type = "UNKNOWN"; },
        [](auto &p) { p.slots[0].expected.capabilities.can_fd = true; },
        [](auto &p) { p.slots[0].expected.capabilities.can_buses = {0, 1, 256}; },
      };
      for (const auto &mutate : mutations) { auto p = fixture(); mutate(p); CHECK(rejects(p)); }
      auto unbound = SessionProfile{}; unbound.slots[0].expected.identity = "fixture:dos"; CHECK(rejects(unbound));
      unbound = SessionProfile{}; unbound.roles.vehicle_can = 0; CHECK(rejects(unbound));
      unbound = SessionProfile{}; unbound.slots[0].expected.capabilities.can_fd = true; CHECK(rejects(unbound));
    });

    test("explicit_role_capability_validation", [] {
      std::vector<std::function<void(SessionProfile &)>> mutations = {
        [](auto &p) { p.roles.vehicle_can = kRoleUnassigned; },
        [](auto &p) { p.roles.vehicle_can = kRoleUnsupported; },
        [](auto &p) { p.roles.diagnostic = 2; },
        [](auto &p) { p.slots[1].expected.capabilities.diagnostic = false; },
        [](auto &p) { p.roles.harness = 0; },
        [](auto &p) { p.roles.fan_control = kRoleUnsupported; },
        [](auto &p) { p.roles.power_monitor = 1; },
        [](auto &p) { p.roles.ir_control = 0; },
        [](auto &p) { p.roles.ignition_sources.clear(); },
        [](auto &p) { p.roles.ignition_sources = {0, 0}; },
        [](auto &p) { p.roles.ignition_sources = {0, 1, 0}; },
        [](auto &p) { p.roles.ignition_sources = {256}; },
        [](auto &p) { p.slots[0].expected.capabilities.ignition = false; },
      };
      for (const auto &mutate : mutations) { auto p = fixture(); mutate(p); CHECK(rejects(p)); }
      auto p = fixture(); p.roles.vehicle_can = 0; p.roles.diagnostic = 0;
      SessionRegistry r(p); CHECK(r.reconcile(inventory(p)).group_available);
      CHECK(r.profile().roles.vehicle_can == 0); CHECK(r.profile().roles.harness == 1);
    });

    test("both_dos_red_permutations_and_reversed_discovery", [] {
      for (std::uint32_t dos_slot = 0; dos_slot < 2; ++dos_slot) {
        const auto p = fixture(dos_slot); SessionRegistry r(p); auto seen = inventory(p, 11, 22);
        std::reverse(seen.begin(), seen.end());
        const auto s = r.reconcile(seen);
        CHECK(s.group_available); CHECK(s.generation == 1);
        CHECK(s.slots[dos_slot].binding.expected.identity == "fixture:dos");
        CHECK(s.slots[1 - dos_slot].binding.expected.identity == "fixture:external");
        CHECK(s.slots[0].connection_token == 11 && s.slots[1].connection_token == 22);
        CHECK(s.slots[0].epoch == 1 && s.slots[1].epoch == 1); permissions(r);
      }
    });

    test("identical_models_distinct_identity_without_type_sorting", [] {
      for (std::uint32_t dos_slot = 0; dos_slot < 2; ++dos_slot) {
        auto p = fixture(dos_slot);
        for (auto &s : p.slots) { s.expected.board_type = "RED_PANDA"; s.expected.mcu_family = "H7"; s.expected.exact_mcu = "fixture:unknown-H7"; s.expected.transport = "USB"; }
        SessionRegistry r(p); auto seen = inventory(p, 7, 9); std::reverse(seen.begin(), seen.end());
        const auto s = r.reconcile(seen); CHECK(s.group_available);
        CHECK(s.slots[0].binding.expected.identity == p.slots[0].expected.identity);
        CHECK(s.slots[1].binding.expected.identity == p.slots[1].expected.identity);
        CHECK(s.slots[0].connection_token == 7); CHECK(s.slots[1].connection_token == 9);
      }
    });

    test("immutable_profile_and_snapshot_copies", [] {
      auto p = fixture(); SessionRegistry r(p); const auto observations = inventory(p);
      p.slots[0].expected.identity = "fixture:replacement"; p.roles.vehicle_can = 0; p.can_tx_enabled = true;
      CHECK(r.reconcile(observations).group_available);
      auto s = r.snapshot(); s.slots[0].epoch = 999; s.group_available = false;
      auto copy = r.profile(); copy.slots[0].expected.identity.clear(); copy.device_access_enabled = true;
      CHECK(r.snapshot().slots[0].epoch == 1); CHECK(r.snapshot().group_available);
      CHECK(r.profile().slots[0].expected.identity == "fixture:dos"); CHECK(r.profile().roles.vehicle_can == 1);
      permissions(r);
    });

    test("idempotent_healthy_inventory_does_not_consume_epochs", [] {
      const auto p = fixture(); SessionRegistry r(p); auto observed = inventory(p);
      r.reconcile(observed);
      for (int i = 0; i < 100; ++i) {
        std::reverse(observed.begin(), observed.end()); const auto s = r.reconcile(observed);
        CHECK(s.group_available && s.generation == 1);
        CHECK(s.slots[0].epoch == 1 && s.slots[1].epoch == 1);
      }
    });

    test("missing_slot_never_promotes_survivor_and_late_discovery", [] {
      for (std::uint32_t missing = 0; missing < 2; ++missing) {
        const auto p = fixture(); SessionRegistry r(p); const auto seen = inventory(p);
        auto s = r.reconcile({seen[1 - missing]});
        CHECK(!s.group_available); CHECK(!s.slots[missing].connected); CHECK(s.slots[1 - missing].connected);
        CHECK(s.slots[1 - missing].slot == 1 - missing); CHECK(s.slots[missing].epoch == 0);
        s = r.reconcile(seen); CHECK(s.group_available); CHECK(s.generation == 2);
        CHECK(s.slots[0].epoch == 1 && s.slots[1].epoch == 1);
      }
    });

    test("drop_reconnect_epoch_trace_and_peer_generation", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p, 10, 20);
      const auto start = r.reconcile(seen); const auto dropped = r.reconcile({seen[1]});
      CHECK(!dropped.group_available && !dropped.slots[0].connected && dropped.slots[1].connected);
      CHECK(dropped.slots[0].epoch == 2 && dropped.slots[1].epoch == 1);
      CHECK(dropped.generation == start.generation + 1);
      CHECK(!r.reconcile(seen).group_available); CHECK(r.snapshot().reason == "RETIRED_CONNECTION_TOKEN");
      seen[0].connection_token = 11; const auto restored = r.reconcile(seen);
      CHECK(restored.group_available); CHECK(restored.slots[0].epoch == 3); CHECK(restored.slots[1].epoch == 1);
      CHECK(restored.generation == dropped.generation + 1);
      std::cout << "TRACE drop_reconnect epochs0=1,2,3 epochs1=1,1,1 generations=1,2,3\n";
    });

    test("new_handle_same_identity_address_requires_new_token", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p, 5, 9); r.reconcile(seen);
      seen[0].connection_token = 6; const auto s = r.reconcile(seen);
      CHECK(s.group_available); CHECK(s.slots[0].epoch == 2); CHECK(s.slots[1].epoch == 1); CHECK(s.generation == 2);
      CHECK(!r.disconnect(0, 1, 5)); CHECK(!r.disconnect(0, 2, 5)); CHECK(!r.disconnect(0, 1, 6));
      CHECK(r.snapshot().group_available && r.snapshot().generation == 2);
      CHECK(r.disconnect(0, 2, 6)); CHECK(!r.snapshot().group_available);
      CHECK(!r.disconnect(0, 2, 6)); CHECK(r.snapshot().generation == 3);
      CHECK(!r.disconnect(256, 0, 0));
    });

    test("replacement_identity_never_binds_without_new_profile", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p); r.reconcile(seen);
      auto replacement = seen; replacement[0].identity.identity = "fixture:replacement"; replacement[0].connection_token = 2;
      const auto s = r.reconcile(replacement);
      CHECK(!s.group_available && !s.slots[0].connected && s.slots[1].connected);
      CHECK(s.reason == "UNEXPECTED_IDENTITY"); CHECK(s.slots[0].binding.expected.identity == "fixture:dos");
      CHECK(!r.reconcile(seen).group_available);
      seen[0].connection_token = 2; CHECK(r.reconcile(seen).group_available);
      auto rebound = p; rebound.slots[0].expected.identity = "fixture:replacement";
      SessionRegistry explicit_rebind(rebound); CHECK(explicit_rebind.reconcile(replacement).group_available);
      permissions(r); permissions(explicit_rebind);
    });

    test("all_observed_identity_fields_revalidate_every_reconnect", [] {
      std::vector<std::function<void(SessionIdentity &)>> changes = {
        [](auto &x) { x.board_type = "RED_PANDA"; }, [](auto &x) { x.mcu_family = "H7"; },
        [](auto &x) { x.exact_mcu = "fixture:other"; }, [](auto &x) { x.transport = "USB"; },
        [](auto &x) { x.protocol_profile = "legacy:100405"; }, [](auto &x) { x.application_signature = "fixture:other"; },
        [](auto &x) { x.capabilities.can_buses = {0, 1, 3}; }, [](auto &x) { x.capabilities.can_fd = true; },
        [](auto &x) { x.capabilities.diagnostic = false; }, [](auto &x) { x.capabilities.harness = true; },
        [](auto &x) { x.capabilities.ignition = false; }, [](auto &x) { x.capabilities.fan = false; },
        [](auto &x) { x.capabilities.power_monitor = false; }, [](auto &x) { x.capabilities.ir = true; },
      };
      for (const auto &mutate : changes) {
        const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p); r.reconcile(seen);
        auto wrong = seen; wrong[0].connection_token = 2; mutate(wrong[0].identity);
        const auto failed = r.reconcile(wrong);
        CHECK(!failed.group_available); CHECK(!failed.slots[0].connected && failed.slots[1].connected);
        CHECK(failed.slots[0].epoch == 2); CHECK(failed.slots[1].epoch == 1);
        seen[0].connection_token = 2; CHECK(!r.reconcile(seen).group_available);
        seen[0].connection_token = 3; CHECK(r.reconcile(seen).group_available);
        CHECK(r.snapshot().slots[0].epoch == 3);
      }
    });

    test("same_token_descriptor_change_retires_connection", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p); r.reconcile(seen);
      auto changed = seen; changed[1].identity.protocol_profile = "current:unknown";
      CHECK(!r.reconcile(changed).group_available); CHECK(!r.reconcile(seen).group_available);
      seen[1].connection_token = 2; CHECK(r.reconcile(seen).group_available);
      CHECK(r.snapshot().slots[1].epoch == 3);
    });

    test("extra_and_duplicate_inventory_retire_existing_group", [] {
      for (const bool extra : {false, true}) {
        const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p); r.reconcile(seen);
        auto wrong = seen;
        if (extra) wrong.push_back({SessionIdentity{}, 99}); else wrong[1] = wrong[0];
        const auto s = r.reconcile(wrong); CHECK(!s.group_available);
        CHECK(!s.slots[0].connected && !s.slots[1].connected); CHECK(s.generation == 2);
        CHECK(s.slots[0].epoch == 2 && s.slots[1].epoch == 2);
        CHECK(!r.reconcile(seen).group_available);
        seen[0].connection_token = 2; seen[1].connection_token = 2;
        CHECK(r.reconcile(seen).group_available);
      }
    });

    test("group_fault_requires_both_fresh_tokens_and_bounds_reason", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p); r.reconcile(seen);
      r.invalidate_group("RX_CHECKSUM"); const auto failed = r.snapshot();
      CHECK(!failed.group_available); CHECK(failed.reason == "RX_CHECKSUM"); CHECK(failed.generation == 2);
      CHECK(failed.slots[0].epoch == 2 && failed.slots[1].epoch == 2);
      r.invalidate_group(std::string(10000, 'x')); CHECK(r.snapshot().reason == "INVALIDATED");
      CHECK(r.snapshot().generation == failed.generation);
      CHECK(!r.reconcile(seen).group_available); seen[0].connection_token = 2;
      CHECK(!r.reconcile(seen).group_available); seen[1].connection_token = 2;
      CHECK(r.reconcile(seen).group_available); CHECK(r.snapshot().generation == 4);
    });

    test("duplicate_candidates_consume_both_known_identity_tokens", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p); r.reconcile(seen);
      auto a = seen[0]; auto b = seen[0]; a.connection_token = 2; b.connection_token = 7;
      CHECK(!r.reconcile({a, b}).group_available);
      seen[0].connection_token = 2; seen[1].connection_token = 2; CHECK(!r.reconcile(seen).group_available);
      seen[0].connection_token = 7; CHECK(!r.reconcile(seen).group_available);
      seen[0].connection_token = 8; CHECK(r.reconcile(seen).group_available);
      const auto s = r.snapshot(); CHECK(s.slots[0].connection_token == 8); CHECK(s.slots[1].connection_token == 2);
    });

    test("zero_older_and_wrap_tokens_cannot_reconnect", [] {
      const auto p = fixture(); SessionRegistry r(p); auto seen = inventory(p, 0, 1);
      CHECK(!r.reconcile(seen).group_available); CHECK(r.snapshot().reason == "INVALID_CONNECTION_TOKEN");
      seen[0].connection_token = 5; CHECK(r.reconcile(seen).group_available);
      seen[0].connection_token = 4; CHECK(!r.reconcile(seen).group_available);
      seen[0].connection_token = 5; CHECK(!r.reconcile(seen).group_available);
      seen[0].connection_token = std::numeric_limits<std::uint64_t>::max(); CHECK(r.reconcile(seen).group_available);
      CHECK(r.disconnect(0, r.snapshot().slots[0].epoch, seen[0].connection_token));
      seen[0].connection_token = 0; CHECK(!r.reconcile(seen).group_available);
      seen[0].connection_token = std::numeric_limits<std::uint64_t>::max(); CHECK(!r.reconcile(seen).group_available);
      CHECK(!r.snapshot().slots[0].connected); CHECK(r.snapshot().slots[1].connected);
    });

    test("epoch_counter_exhaustion_is_permanent_and_never_wraps", [] {
      const auto p = fixture(); const auto max = std::numeric_limits<std::uint64_t>::max();
      SessionRegistry r(p, {{max - 1, 0}, 0}); auto seen = inventory(p); CHECK(r.reconcile(seen).group_available);
      CHECK(r.snapshot().slots[0].epoch == max); CHECK(r.disconnect(0, max, 1));
      const auto failed = r.snapshot(); CHECK(failed.permanently_closed && !failed.group_available);
      CHECK(failed.reason == "COUNTER_EXHAUSTED"); CHECK(failed.slots[0].epoch == max);
      CHECK(!failed.slots[0].connected && !failed.slots[1].connected);
      seen[0].connection_token = 2; seen[1].connection_token = 2;
      CHECK(!r.reconcile(seen).group_available); r.invalidate_group("ANY_REASON");
      CHECK(r.snapshot().reason == "COUNTER_EXHAUSTED"); permissions(r);
    });

    test("generation_counter_exhaustion_closes_healthy_peer", [] {
      const auto p = fixture(); const auto max = std::numeric_limits<std::uint64_t>::max();
      SessionRegistry r(p, {{0, 0}, max - 1}); auto seen = inventory(p);
      CHECK(r.reconcile(seen).group_available); CHECK(r.snapshot().generation == max);
      CHECK(r.reconcile(seen).group_available); // Idempotent observation does not consume a counter.
      seen[0].connection_token = 2; const auto failed = r.reconcile(seen);
      CHECK(failed.permanently_closed && !failed.group_available); CHECK(failed.generation == max);
      CHECK(!failed.slots[0].connected && !failed.slots[1].connected);
      CHECK(!r.reconcile(seen).group_available); permissions(r);
      SessionRegistry immediately_exhausted(p, {{0, 0}, max}); CHECK(!immediately_exhausted.reconcile(inventory(p)).group_available);
      CHECK(immediately_exhausted.snapshot().permanently_closed);
    });

    std::cout << "{\"suite\":\"session_registry\",\"groups\":" << groups << ",\"cases\":[";
    for (std::size_t i = 0; i < names.size(); ++i) std::cout << (i ? "," : "") << '"' << names[i] << '"';
    std::cout << "],\"assertions\":" << assertions << ",\"failures\":0,\"skips\":0}\n";
  } catch (const std::exception &error) {
    std::cerr << "FAIL session_registry: " << error.what() << '\n'; return 1;
  }
}
