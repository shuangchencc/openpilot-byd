#include "../can_router.h"
#include "../transport.h"

#include <algorithm>
#include <array>
#include <deque>
#include <functional>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;

namespace {
std::size_t assertions = 0;
void check(bool condition, int line) {
  ++assertions;
  if (!condition) throw std::runtime_error("assertion at line " + std::to_string(line));
}
#define CHECK(expression) check(static_cast<bool>(expression), __LINE__)

SessionProfile profile_for(unsigned dos_slot = 0, bool same_model = false) {
  SessionProfile profile;
  profile.binding_status = "SYNTHETIC_BOUND";
  for (unsigned slot = 0; slot < 2; ++slot) {
    const bool dos = slot == dos_slot;
    auto &binding = profile.slots[slot];
    auto &id = binding.expected;
    id.identity = dos ? "fixture:dos" : "fixture:external";
    id.board_type = dos && !same_model ? "DOS" : "RED_PANDA";
    id.mcu_family = dos && !same_model ? "F4" : "H7";
    id.exact_mcu = dos && !same_model ? "fixture:unknown-F4" : "fixture:unknown-H7";
    id.transport = "USB";
    id.protocol_profile = kSessionProtocol;
    id.capabilities = {{0, 1, 2}, !dos, !dos, !dos, true, dos, dos, false};
    id.application_signature = dos ? "fixture:dos-app" : "fixture:red-app";
    binding.safety_config_semantics = "noOutput";
  }
  profile.roles = {static_cast<std::int32_t>(1U - dos_slot), static_cast<std::int32_t>(1U - dos_slot),
                   static_cast<std::int32_t>(1U - dos_slot), {0, 1}, static_cast<std::int32_t>(dos_slot),
                   static_cast<std::int32_t>(dos_slot), kRoleUnsupported};
  return profile;
}

std::vector<SessionObservation> inventory_for(const SessionProfile &profile,
                                            std::uint64_t token0 = 1, std::uint64_t token1 = 1) {
  return {{profile.slots[0].expected, token0}, {profile.slots[1].expected, token1}};
}
CanCapability capability(const SessionProfile &profile, unsigned slot) {
  return profile.slots[slot].expected.capabilities.can_fd ? CanCapability::CanFd : CanCapability::ClassicOnly;
}
HostTxFrame host_frame(unsigned source) {
  return {0x321U + source, source, false, false,
          {static_cast<std::uint8_t>(source), static_cast<std::uint8_t>(source % 4), static_cast<std::uint8_t>(source / 4)}};
}
TxBatch six_routes(const SessionRegistry &registry, std::uint64_t timestamp) {
  TxBatch batch;
  batch.valid = true;
  batch.source_timestamp_us = timestamp;
  batch.group_generation = registry.snapshot().generation;
  for (const unsigned source : {0U, 1U, 2U, 4U, 5U, 6U}) batch.frames.push_back(host_frame(source));
  return batch;
}
CanFrame wire_frame(unsigned slot, unsigned local, bool returned = false, bool rejected = false) {
  const auto host = host_frame(slot * 4U + local);
  return {host.address, local, host.extended, host.fd, returned, rejected, host.payload};
}
Bytes encoded(unsigned slot, unsigned local, bool returned = false, bool rejected = false) {
  const auto result = encode_can_frame(wire_frame(slot, local, returned, rejected), CanCapability::CanFd, FrameDirection::Rx);
  CHECK(result);
  return result.value;
}

class FakeClock final : public Clock {
public:
  std::uint64_t time = 1000000;
  std::uint64_t now_us() override { return time; }
  void wait_us(std::uint32_t duration) override { time += duration; }
};
class FakeBackend final : public Backend {
public:
  std::deque<Result> script;
  std::vector<Request> calls;
  std::vector<std::uint32_t> timeouts;
  Result transact(const Request &request, std::uint32_t timeout_ms) override {
    calls.push_back(request);
    timeouts.push_back(timeout_ms);
    CHECK(timeout_ms > 0 && timeout_ms <= kBackendSliceMs);
    if (!script.empty()) {
      Result result = std::move(script.front());
      script.pop_front();
      return result;
    }
    if (request.kind == Kind::BulkWrite) return {Status::Ok, request.payload.size(), {}, true, false};
    throw std::runtime_error("unscripted fake read");
  }
};

RouterResult<std::size_t> fake_receive(CanRouter &router, Transport &transport, FakeBackend &backend,
                                     const RouteToken &token, const Bytes &data,
                                     std::uint64_t received, std::uint64_t now) {
  backend.script.push_back({Status::Short, data.size(), data, true, false});
  Request request;
  request.kind = Kind::BulkRead;
  request.endpoint = 0x81;
  request.read_length = data.size() + 1;
  const auto result = transport.perform(request);
  CHECK(result.status == Status::Short);
  CHECK(result.actual_known && !result.delivery_uncertain && result.actual == data.size());
  return router.receive(token, result.data.data(), result.data.size(), received, now);
}

// Test-only glue: production CanRouter has no I/O capability. A real adapter
// would need a separately reviewed P4 authorization/health/safety boundary.
Result fake_send(SessionRegistry &registry, CanRouter &router, const SimulatedTxIntent &intent, Transport &transport, std::uint64_t now) {
  CHECK(!intent.real_tx_authorized);
  if (!router.validate_intent(intent, now)) return {Status::Invalid};
  const auto current = registry.snapshot();
  const auto &slot = current.slots.at(intent.token.slot);
  CHECK(current.group_available && current.generation == intent.token.group_generation);
  CHECK(slot.epoch == intent.token.epoch && slot.connection_token == intent.token.connection_token);
  Request request;
  request.kind = Kind::BulkWrite;
  request.endpoint = 3;
  request.payload = intent.encoded;
  const auto result = transport.perform(request);
  if (result.status != Status::Ok || !result.actual_known || result.delivery_uncertain) {
    CHECK(registry.disconnect(intent.token.slot, intent.token.epoch, intent.token.connection_token));
  }
  return result;
}

std::string hex(const Bytes &data) {
  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (const auto byte : data) output << std::setw(2) << static_cast<unsigned>(byte);
  return output.str();
}
void json_string(const std::string &value) { std::cout << std::quoted(value); }
void bool_json(bool value) { std::cout << (value ? "true" : "false"); }
void capabilities_json(const SessionCapabilities &caps) {
  std::cout << "{\"can_buses\":[" << caps.can_buses[0] << ',' << caps.can_buses[1] << ',' << caps.can_buses[2]
            << "],\"can_fd\":"; bool_json(caps.can_fd);
  std::cout << ",\"diagnostic\":"; bool_json(caps.diagnostic);
  std::cout << ",\"harness\":"; bool_json(caps.harness);
  std::cout << ",\"ignition\":"; bool_json(caps.ignition);
  std::cout << ",\"fan\":"; bool_json(caps.fan);
  std::cout << ",\"power_monitor\":"; bool_json(caps.power_monitor);
  std::cout << ",\"ir\":"; bool_json(caps.ir); std::cout << '}';
}
void identity_json(const SessionIdentity &id, bool binding) {
  std::cout << (binding ? "\"identity_binding\":" : "\"identity\":"); json_string(id.identity);
  std::cout << (binding ? ",\"expected_board_type\":" : ",\"board_type\":"); json_string(id.board_type);
  std::cout << ",\"mcu_family\":"; json_string(id.mcu_family);
  std::cout << ",\"exact_mcu\":"; json_string(id.exact_mcu);
  std::cout << ",\"transport\":"; json_string(id.transport);
  std::cout << ",\"protocol_profile\":"; json_string(id.protocol_profile);
  std::cout << ",\"capabilities\":"; capabilities_json(id.capabilities);
  std::cout << ",\"application_signature\":"; json_string(id.application_signature);
}
void role_json(std::int32_t role) {
  if (role == kRoleUnsupported) json_string("UNSUPPORTED");
  else if (role == kRoleUnassigned) std::cout << "null";
  else std::cout << role;
}
void roles_json(const SessionRoles &roles) {
  std::cout << "{\"vehicle_can\":"; role_json(roles.vehicle_can);
  std::cout << ",\"diagnostic\":"; role_json(roles.diagnostic);
  std::cout << ",\"harness\":"; role_json(roles.harness);
  std::cout << ",\"ignition_sources\":[";
  for (std::size_t i = 0; i < roles.ignition_sources.size(); ++i) {
    if (i) std::cout << ',';
    std::cout << roles.ignition_sources[i];
  }
  std::cout << "],\"fan_control\":"; role_json(roles.fan_control);
  std::cout << ",\"power_monitor\":"; role_json(roles.power_monitor);
  std::cout << ",\"ir_control\":"; role_json(roles.ir_control); std::cout << '}';
}
void profile_json(const SessionProfile &profile) {
  std::cout << "{\"schema\":"; json_string(profile.schema);
  std::cout << ",\"purpose\":"; json_string(profile.purpose);
  std::cout << ",\"binding_status\":"; json_string(profile.binding_status);
  std::cout << ",\"device_access_enabled\":"; bool_json(profile.device_access_enabled);
  std::cout << ",\"can_tx_enabled\":"; bool_json(profile.can_tx_enabled);
  std::cout << ",\"firmware_install_enabled\":"; bool_json(profile.firmware_install_enabled);
  std::cout << ",\"expected_board_count\":" << profile.expected_board_count << ",\"required_slots\":["
            << profile.required_slots[0] << ',' << profile.required_slots[1] << "],\"bus_stride\":" << profile.bus_stride
            << ",\"ordinary_local_can_buses\":[" << profile.ordinary_local_can_buses[0] << ','
            << profile.ordinary_local_can_buses[1] << ',' << profile.ordinary_local_can_buses[2] << "],\"slots\":[";
  for (unsigned slot = 0; slot < 2; ++slot) {
    if (slot) std::cout << ',';
    const auto &binding = profile.slots[slot];
    std::cout << "{\"slot\":" << binding.slot << ','; identity_json(binding.expected, true);
    std::cout << ",\"safety_config_semantics\":"; json_string(binding.safety_config_semantics); std::cout << '}';
  }
  std::cout << "],\"roles\":"; roles_json(profile.roles); std::cout << '}';
}
void role_identities_json(const SessionProfile &profile) {
  const auto &roles = profile.roles;
  const std::vector<std::pair<std::string, std::vector<std::int32_t>>> bindings = {
    {"vehicle_can", {roles.vehicle_can}}, {"diagnostic", {roles.diagnostic}}, {"harness", {roles.harness}},
    {"ignition_sources", std::vector<std::int32_t>(roles.ignition_sources.begin(), roles.ignition_sources.end())},
    {"fan_control", {roles.fan_control}}, {"power_monitor", {roles.power_monitor}}, {"ir_control", {roles.ir_control}}};
  std::cout << '{';
  for (std::size_t i = 0; i < bindings.size(); ++i) {
    if (i) std::cout << ',';
    json_string(bindings[i].first); std::cout << ":[";
    bool first = true;
    for (const auto slot : bindings[i].second) {
      if (slot < 0) continue;
      if (!first) std::cout << ',';
      json_string(profile.slots.at(static_cast<unsigned>(slot)).expected.identity); first = false;
    }
    std::cout << ']';
  }
  std::cout << '}';
}

void oracle() {
  std::cout << "{\"device_access\":false,\"real_can_tx\":false,\"scope\":\"synthetic_session_routes\",\"fixtures\":[";
  unsigned count = 0;
  for (const unsigned dos_slot : {0U, 1U}) for (const bool same : {false, true}) for (const bool reversed : {false, true}) {
    const auto profile = profile_for(dos_slot, same);
    auto inventory = inventory_for(profile);
    if (reversed) std::reverse(inventory.begin(), inventory.end());
    SessionRegistry registry(profile);
    const auto snapshot = registry.reconcile(inventory);
    CanRouter router(registry);
    CHECK(snapshot.group_available);
    CHECK(router.enqueue_tx(six_routes(registry, 1000), 1000));
    if (count++) std::cout << ',';
    std::cout << "{\"dos_slot\":" << dos_slot << ",\"same_model\":"; bool_json(same);
    std::cout << ",\"reversed_inventory\":"; bool_json(reversed);
    std::cout << ",\"profile\":"; profile_json(registry.profile());
    std::cout << ",\"inventory\":[";
    for (unsigned i = 0; i < 2; ++i) {
      if (i) std::cout << ',';
      std::cout << '{'; identity_json(inventory[i].identity, false); std::cout << '}';
    }
    std::cout << "],\"registered_identities\":["; json_string(snapshot.slots[0].binding.expected.identity);
    std::cout << ','; json_string(snapshot.slots[1].binding.expected.identity);
    std::cout << "],\"available\":"; bool_json(snapshot.group_available);
    std::cout << ",\"permissions\":{\"device_access_enabled\":"; bool_json(registry.device_access_enabled());
    std::cout << ",\"can_tx_enabled\":"; bool_json(registry.can_tx_enabled());
    std::cout << ",\"firmware_install_enabled\":"; bool_json(registry.firmware_install_enabled());
    std::cout << "},\"role_identities\":"; role_identities_json(registry.profile());
    std::cout << ",\"routes\":[";
    unsigned route_count = 0;
    for (unsigned slot = 0; slot < 2; ++slot) for (unsigned local = 0; local < 3; ++local) {
      const auto intent = router.take_tx(slot, 1000);
      CHECK(intent);
      CHECK(intent.value.token.slot == slot && intent.value.frame.local_bus == local);
      std::array<unsigned, 3> sources{};
      for (unsigned flag = 0; flag < 3; ++flag) {
        const auto bytes = encoded(slot, local, flag == 1, flag == 2);
        CHECK(router.receive(intent.value.token, bytes.data(), bytes.size(), 1000, 1000));
        const auto received = router.take_rx(slot, 1000);
        CHECK(received);
        sources[flag] = received.value.source;
      }
      if (route_count++) std::cout << ',';
      std::cout << "{\"host_source\":" << slot * 4U + local << ",\"slot\":" << intent.value.token.slot
                << ",\"local_bus\":" << intent.value.frame.local_bus << ",\"identity\":";
      json_string(snapshot.slots[intent.value.token.slot].binding.expected.identity);
      std::cout << ",\"wire_hex\":"; json_string(hex(intent.value.encoded));
      std::cout << ",\"rx_sources\":[" << sources[0] << ',' << sources[1] << ',' << sources[2] << "]}";
    }
    std::cout << "]}";
  }
  std::cout << "]}\n";
}
}  // namespace

int main(int argc, char **argv) {
  std::size_t groups = 0;
  std::vector<std::string> names;
  auto test = [&](const char *name, const std::function<void()> &body) {
    body(); ++groups; names.emplace_back(name);
  };
  try {
    if (argc == 2 && std::string(argv[1]) == "--oracle") { oracle(); return 0; }
    CHECK(argc == 1);
    test("p0_contract_all_eight_profile_inventory_permutations", [] {
      for (const unsigned dos : {0U, 1U}) for (const bool same : {false, true}) for (const bool reverse : {false, true}) {
        const auto profile = profile_for(dos, same);
        SessionRegistry registry(profile);
        auto inventory = inventory_for(profile);
        if (reverse) std::reverse(inventory.begin(), inventory.end());
        const auto state = registry.reconcile(inventory);
        CHECK(state.group_available && !state.permanently_closed);
        CHECK(!registry.device_access_enabled() && !registry.can_tx_enabled() && !registry.firmware_install_enabled());
        for (unsigned slot = 0; slot < 2; ++slot) {
          CHECK(state.slots[slot].slot == slot && state.slots[slot].connected);
          CHECK(state.slots[slot].binding.expected == profile.slots[slot].expected);
        }
      }
    });
    test("p2_short_reads_every_split_keep_per_board_parser_isolated", [] {
      for (std::size_t split = 0; split <= 27; ++split) {
        const auto profile = profile_for(); SessionRegistry registry(profile);
        CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
        FakeClock clock; FakeBackend a, b; Transport ta(a, clock), tb(b, clock);
        const auto token0 = router.current_token(0, clock.time), token1 = router.current_token(1, clock.time);
        CHECK(token0 && token1);
        Bytes bytes;
        for (unsigned local = 0; local < 3; ++local) {
          const auto frame = encoded(0, local); bytes.insert(bytes.end(), frame.begin(), frame.end());
        }
        CHECK(bytes.size() == 27);
        const Bytes prefix(bytes.begin(), bytes.begin() + static_cast<std::ptrdiff_t>(split));
        const Bytes suffix(bytes.begin() + static_cast<std::ptrdiff_t>(split), bytes.end());
        CHECK(fake_receive(router, ta, a, token0.value, prefix, clock.time, clock.time));
        CHECK(fake_receive(router, tb, b, token1.value, encoded(1, 2, true), clock.time, clock.time));
        const auto independent = router.take_rx(1, clock.time);
        CHECK(independent && independent.value.source == 0x86 && independent.value.frame.local_bus == 2);
        CHECK(fake_receive(router, ta, a, token0.value, suffix, clock.time, clock.time));
        for (unsigned local = 0; local < 3; ++local) {
          const auto result = router.take_rx(0, clock.time);
          CHECK(result && result.value.source == local && result.value.frame.local_bus == local);
          CHECK(result.value.frame.payload == Bytes({static_cast<std::uint8_t>(local), static_cast<std::uint8_t>(local), 0}));
          CHECK(result.value.receive_timestamp_us == clock.time);
        }
        CHECK(router.take_rx(0, clock.time).error == RouterError::Empty);
        CHECK(a.calls.size() == 2 && b.calls.size() == 1);
        CHECK(ta.healthy() && tb.healthy());
      }
    });
    test("six_routes_reach_only_their_fake_p2_backend_with_mcu_local_buses", [] {
      for (const unsigned dos : {0U, 1U}) {
        const auto profile = profile_for(dos); SessionRegistry registry(profile);
        CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
        FakeClock clock; FakeBackend a, b; Transport ta(a, clock), tb(b, clock);
        CHECK(router.enqueue_tx(six_routes(registry, clock.time), clock.time));
        for (unsigned slot = 0; slot < 2; ++slot) for (unsigned local = 0; local < 3; ++local) {
          const auto intent = router.take_tx(slot, clock.time); CHECK(intent);
          CHECK(intent.value.token.slot == slot && intent.value.frame.local_bus == local);
          CHECK(fake_send(registry, router, intent.value, slot == 0 ? ta : tb, clock.time).status == Status::Ok);
        }
        CHECK(a.calls.size() == 3 && b.calls.size() == 3);
        for (unsigned slot = 0; slot < 2; ++slot) {
          const auto &calls = slot == 0 ? a.calls : b.calls;
          for (unsigned local = 0; local < 3; ++local) {
            CHECK(calls[local].kind == Kind::BulkWrite && calls[local].endpoint == 3);
            const auto &raw = calls[local].payload;
            const auto decoded = decode_can_frame(raw.data(), raw.size(), capability(profile, slot), FrameDirection::Tx);
            CHECK(decoded && decoded.value.local_bus == local);
            CHECK(decoded.value.address == 0x321U + 4U * slot + local);
            CHECK(decoded.value.payload == wire_frame(slot, local).payload);
          }
        }
        for (const unsigned invalid : {3U, 7U, 8U, 0x80U, 0xC4U, 0xFFFFFFFFU}) {
          TxBatch bad = six_routes(registry, clock.time);
          bad.frames = {host_frame(0), host_frame(invalid)};
          CHECK(!router.enqueue_tx(bad, clock.time));
          CHECK(router.take_tx(0, clock.time).error == RouterError::Empty);
          CHECK(router.take_tx(1, clock.time).error == RouterError::Empty);
        }
        CHECK(a.calls.size() == 3 && b.calls.size() == 3);
      }
    });
    test("disconnect_reconnect_discards_partial_and_both_queued_slots", [] {
      const auto profile = profile_for(); SessionRegistry registry(profile);
      CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
      FakeClock clock; FakeBackend a, b; Transport ta(a, clock), tb(b, clock);
      const auto old0 = router.current_token(0, clock.time), old1 = router.current_token(1, clock.time);
      CHECK(old0 && old1);
      const auto bytes = encoded(0, 1);
      CHECK(fake_receive(router, ta, a, old0.value, Bytes(bytes.begin(), bytes.begin() + 4), clock.time, clock.time));
      CHECK(fake_receive(router, tb, b, old1.value, encoded(1, 0), clock.time, clock.time));
      CHECK(router.enqueue_tx(six_routes(registry, clock.time), clock.time));
      CHECK(registry.disconnect(0, old0.value.epoch, old0.value.connection_token));
      CHECK(!registry.snapshot().group_available);
      const auto cleared = router.inspect(clock.time); CHECK(cleared.error == RouterError::GroupUnavailable);
      CHECK((cleared.value.tx == std::array<std::size_t, 2>({0, 0})));
      CHECK((cleared.value.rx == std::array<std::size_t, 2>({0, 0})));
      CHECK((cleared.value.fragment_bytes == std::array<std::size_t, 2>({0, 0})));
      CHECK(router.take_tx(1, clock.time).error == RouterError::GroupUnavailable);
      const auto reconnected = registry.reconcile(inventory_for(profile, 2, 1));
      CHECK(reconnected.group_available);
      const auto fresh = router.current_token(0, clock.time); CHECK(fresh);
      CHECK(fresh.value.epoch > old0.value.epoch);
      CHECK(!registry.disconnect(0, old0.value.epoch, old0.value.connection_token));
      CHECK(router.receive(old0.value, bytes.data() + 4, bytes.size() - 4, clock.time, clock.time).error == RouterError::StaleSession);
      CHECK(router.receive(old1.value, bytes.data(), bytes.size(), clock.time, clock.time).error == RouterError::StaleSession);
      CHECK(registry.snapshot().group_available);
      CHECK(router.receive(fresh.value, bytes.data(), bytes.size(), clock.time, clock.time));
      const auto fresh_rx = router.take_rx(0, clock.time); CHECK(fresh_rx && fresh_rx.value.source == 1);
      CHECK(router.take_rx(0, clock.time).error == RouterError::Empty);
      CHECK(router.counters().epoch_tx_drops == 6 && router.counters().epoch_rx_drops == 1);
      CHECK(router.counters().epoch_fragment_bytes == 4);
      CHECK(a.calls.size() == 1 && b.calls.size() == 1);
    });
    test("replacement_and_unknown_protocol_never_promote_survivor", [] {
      for (const bool wrong_protocol : {false, true}) {
        const auto profile = profile_for(); SessionRegistry registry(profile);
        CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
        CHECK(router.enqueue_tx(six_routes(registry, 1000), 1000));
        auto replacement = inventory_for(profile, 2, 1);
        if (wrong_protocol) replacement[0].identity.protocol_profile = "current:0000000000000000";
        else replacement[0].identity.identity = "fixture:replacement-at-reused-address";
        const auto state = registry.reconcile(replacement);
        CHECK(!state.group_available);
        CHECK(state.slots[0].binding.expected.identity == "fixture:dos");
        CHECK(state.slots[1].binding.expected.identity == "fixture:external");
        CHECK(router.take_tx(0, 1000).error == RouterError::GroupUnavailable);
        CHECK(router.take_tx(1, 1000).error == RouterError::GroupUnavailable);
        CHECK(!router.enqueue_tx(six_routes(registry, 1000), 1000));
      }
    });
    test("uncertain_p2_write_closes_group_without_replay_or_sibling_fallback", [] {
      const auto profile = profile_for(); SessionRegistry registry(profile);
      CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
      FakeClock clock; FakeBackend a, b; Transport ta(a, clock), tb(b, clock);
      CHECK(router.enqueue_tx(six_routes(registry, clock.time), clock.time));
      const auto intent = router.take_tx(0, clock.time); CHECK(intent);
      a.script.push_back({Status::IoError, 1, {}, false, true});
      const auto failure = fake_send(registry, router, intent.value, ta, clock.time);
      CHECK(failure.status == Status::IoError && !failure.actual_known && failure.delivery_uncertain);
      CHECK(!ta.healthy() && tb.healthy());
      CHECK(!registry.snapshot().group_available);
      CHECK(router.take_tx(0, clock.time).error == RouterError::GroupUnavailable);
      CHECK(router.take_tx(1, clock.time).error == RouterError::GroupUnavailable);
      CHECK(!router.enqueue_tx(six_routes(registry, clock.time), clock.time));
      CHECK(a.calls.size() == 1 && b.calls.empty());
      Request stale; stale.kind = Kind::BulkWrite; stale.endpoint = 3; stale.payload = intent.value.encoded;
      CHECK(ta.perform(stale).status == Status::Poisoned);
      CHECK(a.calls.size() == 1 && b.calls.empty());
      CHECK(registry.reconcile(inventory_for(profile, 2, 1)).group_available);
      CHECK(router.take_tx(0, clock.time).error == RouterError::Empty);
      CHECK(router.take_tx(1, clock.time).error == RouterError::Empty);
      CHECK(!registry.disconnect(0, intent.value.token.epoch, intent.value.token.connection_token));
      CHECK(registry.snapshot().group_available);
    });
    test("held_intent_and_ingress_batch_cannot_cross_connection_generation", [] {
      const auto profile = profile_for(); SessionRegistry registry(profile);
      CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
      FakeClock clock; FakeBackend backend; Transport transport(backend, clock);
      const auto old_batch = six_routes(registry, clock.time);
      CHECK(router.enqueue_tx(old_batch, clock.time));
      const auto held = router.take_tx(0, clock.time); CHECK(held);
      CHECK(registry.disconnect(0, held.value.token.epoch, held.value.token.connection_token));
      CHECK(fake_send(registry, router, held.value, transport, clock.time).status == Status::Invalid);
      CHECK(registry.reconcile(inventory_for(profile, 2, 1)).group_available);
      ++clock.time;
      CHECK(router.enqueue_tx(old_batch, clock.time).error == RouterError::StaleSession);
      CHECK(fake_send(registry, router, held.value, transport, clock.time).status == Status::Invalid);
      CHECK(backend.calls.empty());
      CHECK(router.take_tx(0, clock.time).error == RouterError::Empty);
      CHECK(router.take_tx(1, clock.time).error == RouterError::Empty);
      CHECK(registry.snapshot().group_available);
    });
    test("extra_inventory_retires_live_tokens_and_clears_both_queues", [] {
      const auto profile = profile_for(); SessionRegistry registry(profile);
      const auto original = inventory_for(profile);
      CHECK(registry.reconcile(original).group_available); CanRouter router(registry);
      CHECK(router.enqueue_tx(six_routes(registry, 1000), 1000));
      auto extra = original;
      extra.push_back({profile.slots[1].expected, 1});
      extra.back().identity.identity = "fixture:unexpected-third";
      CHECK(!registry.reconcile(extra).group_available);
      CHECK(router.take_tx(0, 1000).error == RouterError::GroupUnavailable);
      CHECK(router.take_tx(1, 1000).error == RouterError::GroupUnavailable);
      CHECK(!registry.reconcile(original).group_available);
      CHECK(registry.reconcile(inventory_for(profile, 2, 2)).group_available);
      CHECK(router.take_tx(0, 1000).error == RouterError::Empty);
      CHECK(router.take_tx(1, 1000).error == RouterError::Empty);
      CHECK(router.counters().epoch_tx_drops == 6);
    });
    test("held_rx_publication_checks_original_age_and_connection_token", [] {
      const auto profile = profile_for(); SessionRegistry registry(profile);
      CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
      const auto token = router.current_token(1, 1000); CHECK(token);
      const auto bytes = encoded(1, 0);
      CHECK(router.receive(token.value, bytes.data(), bytes.size(), 1000, 1000));
      const auto held = router.take_rx(1, 1000); CHECK(held);
      CHECK(router.validate_rx_frame(held.value, 1000));
      CHECK(router.validate_rx_frame(held.value, 1000 + kRxMaxAgeUs).error == RouterError::Expired);
      CHECK(registry.disconnect(1, token.value.epoch, token.value.connection_token));
      CHECK(registry.reconcile(inventory_for(profile, 1, 2)).group_available);
      CHECK(router.current_token(1, 1000 + kRxMaxAgeUs + 1));
      CHECK(router.validate_rx_frame(held.value, 999).error == RouterError::StaleSession);
      CHECK(registry.snapshot().group_available);
      CHECK(router.take_rx(1, 1000 + kRxMaxAgeUs + 1).error == RouterError::Empty);
    });
    test("held_intent_final_dispatch_checks_queue_and_source_age", [] {
      for (const bool source_age : {false, true}) for (const auto offset : {0U, 1U}) {
        const auto profile = profile_for(); SessionRegistry registry(profile);
        CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
        FakeClock clock; FakeBackend backend; Transport transport(backend, clock);
        const auto source = source_age ? clock.time - kSendcanMaxAgeUs + 1000 : clock.time;
        CHECK(router.enqueue_tx(six_routes(registry, source), clock.time));
        const auto held = router.take_tx(0, clock.time); CHECK(held);
        clock.time += (source_age ? 1000 : kTxQueueMaxAgeUs) + offset;
        CHECK(router.validate_intent(held.value, clock.time).error == RouterError::Expired);
        CHECK(fake_send(registry, router, held.value, transport, clock.time).status == Status::Invalid);
        CHECK(backend.calls.empty() && transport.healthy());
      }
    });
    test("original_fragment_receive_age_is_not_renewed_by_transport_or_publication", [] {
      const auto profile = profile_for(); SessionRegistry registry(profile);
      CHECK(registry.reconcile(inventory_for(profile)).group_available); CanRouter router(registry);
      FakeClock clock; FakeBackend backend; Transport transport(backend, clock);
      const auto token = router.current_token(1, clock.time); CHECK(token);
      const auto bytes = encoded(1, 2);
      const auto original = clock.time;
      CHECK(fake_receive(router, transport, backend, token.value, Bytes(bytes.begin(), bytes.begin() + 3), original, clock.time));
      clock.time += 20000;
      CHECK(fake_receive(router, transport, backend, token.value, Bytes(bytes.begin() + 3, bytes.end()), clock.time, clock.time));
      const auto published = router.take_rx(1, clock.time); CHECK(published);
      CHECK(published.value.receive_timestamp_us == original && published.value.source == 6);
      CHECK(router.take_rx(1, clock.time).error == RouterError::Empty);
      CHECK(fake_receive(router, transport, backend, token.value, bytes, clock.time, clock.time));
      clock.time += kRxMaxAgeUs + 1;
      CHECK(router.take_rx(1, clock.time).error == RouterError::Empty);
      CHECK(router.counters().expired_rx == 1);
      CHECK(backend.calls.size() == 3);
    });
    std::cout << "{\"suite\":\"session_router_integration\",\"passed\":" << groups
              << ",\"failed\":0,\"skipped\":0,\"assertions\":" << assertions
              << ",\"device_access\":false,\"real_can_tx\":false,\"transport\":\"P2_with_fake_backends_only\",\"cases\":[";
    for (std::size_t i = 0; i < names.size(); ++i) {
      if (i) std::cout << ',';
      json_string(names[i]);
    }
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "FAIL after " << groups << " groups: " << error.what() << '\n';
    return 1;
  }
}
