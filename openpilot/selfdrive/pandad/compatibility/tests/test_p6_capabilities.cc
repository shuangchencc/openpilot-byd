#include "../pandad_runtime.h"

#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace openpilot::pandad::compatibility;
namespace {
using Bytes = std::vector<std::uint8_t>;
std::size_t assertions = 0;
void check(bool value, int line) {
  ++assertions;
  if (!value) throw std::runtime_error("capability assertion line " + std::to_string(line));
}
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
const Bytes kCapabilities{'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};
void put16(Bytes &bytes, std::size_t offset, unsigned value) {
  bytes[offset] = static_cast<std::uint8_t>(value);
  bytes[offset + 1] = static_cast<std::uint8_t>(value >> 8);
}
SessionProfile profile(unsigned dos_slot = 0, bool h7_only = false) {
  SessionProfile p; p.binding_status = "SYNTHETIC_BOUND";
  for (unsigned slot = 0; slot < 2; ++slot) {
    const bool dos = !h7_only && slot == dos_slot;
    p.slots[slot].expected = {slot ? "fixture:second" : "fixture:first", dos ? "DOS" : "RED_PANDA",
      dos ? "F4" : "H7", dos ? "fixture:F413-reference" : "fixture:H725-reference", "USB", kSessionProtocol,
      {{0,1,2}, !dos, true, true, true, slot == dos_slot, slot == dos_slot, false}, "fixture:application"};
    p.slots[slot].safety_config_semantics = "noOutput";
  }
  p.roles = {int(1-dos_slot), int(1-dos_slot), int(1-dos_slot), {0,1}, int(dos_slot), int(dos_slot), kRoleUnsupported};
  return p;
}
struct FakeClock final : Clock {
  std::uint64_t now = 1000000;
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t amount) override { now += amount; }
};
struct Board final : Backend {
  bool f4;
  unsigned param = 0, alternative = 0, spi_count = 0;
  Bytes capabilities = kCapabilities;
  Status capability_status = Status::Ok;
  std::vector<Request> calls;
  bool cancelled = false;
  explicit Board(bool is_f4) : f4(is_f4) {}
  Result transact(const Request &request, std::uint32_t budget) override {
    CHECK(budget <= 5); calls.push_back(request);
    if (request.kind == Kind::ControlWrite) {
      CHECK(request.request == 0xdc || request.request == 0xdf || request.request == 0xf3 || request.request == 0xb1);
      if (request.request == 0xdc) { CHECK(request.value == 19); param = request.index; }
      if (request.request == 0xdf) alternative = request.value;
      return {Status::Ok, 0, {}};
    }
    CHECK(request.kind == Kind::ControlRead);
    if (request.request == kF4CapabilityRequest) {
      CHECK(f4); CHECK(request.read_length == 16); CHECK(request.value == 0); CHECK(request.index == 0);
      return {capability_status, capabilities.size(), capabilities};
    }
    Bytes response(request.read_length);
    if (request.request == 0xd2) {
      response[0] = 42; put16(response, 4, 12001); put16(response, 6, 321);
      response[28] = 1; response[30] = 1; response[31] = 19;
      put16(response, 32, param); put16(response, 35, alternative);
      put16(response, 39, spi_count); put16(response, 45, 1234); response[47] = 75;
    } else if (request.request == 0xc2) { put16(response, 41, 500); }
    else if (request.request == 0xb2) { put16(response, 0, 1500); }
    else throw std::runtime_error("unexpected capability fixture request");
    return {Status::Ok, response.size(), response};
  }
  void cancel() noexcept override { cancelled = true; }
  unsigned count(unsigned request) const {
    return std::count_if(calls.begin(), calls.end(), [=](const Request &q) { return q.request == request; });
  }
};
struct Rig {
  FakeClock clock;
  SessionProfile p;
  Board b0, b1;
  Transport t0{b0, clock}, t1{b1, clock};
  PandadRuntime runtime;
  explicit Rig(unsigned dos = 0, bool h7_only = false)
    : p(profile(dos, h7_only)), b0(!h7_only && dos == 0), b1(!h7_only && dos == 1), runtime(p, clock) {
    CHECK(runtime.reconcile({{p.slots[0].expected,1},{p.slots[1].expected,1}}, {{&t0,&t1}}));
  }
  void ready() {
    ++clock.now;
    CHECK(runtime.device_input({true, false, true, 25, clock.now}, runtime.capture()));
    CHECK(runtime.configure({{0,19,11,2},{1,19,22,2}}, runtime.capture()));
    for (unsigned i = 0; i < 12; ++i) { runtime.tick(); ++clock.now; }
    CHECK(runtime.snapshot().configured);
    for (unsigned i = 0; i < 3; ++i) runtime.peripheral_step();
  }
};
struct CoreRig {
  SessionProfile p = profile();
  SessionRegistry registry{p};
  ControlSession control{registry};
  std::uint64_t now = 1000;
  CoreRig() { registry.reconcile({{p.slots[0].expected,1},{p.slots[1].expected,1}}); }
  ControlCommand probe() {
    const auto commands = control.poll(now); CHECK(commands.size() == 1);
    CHECK(commands[0].kind == ControlKind::ReadCapabilities); CHECK(commands[0].token.slot == 0);
    return commands[0];
  }
};
}
int main() {
  std::size_t groups = 0;
  const auto test = [&](const char *name, const std::function<void()> &run) {
    run(); ++groups; std::cout << "PASS " << name << '\n';
  };
  try {
    test("f4c1_exact_bytes_all_single_bit_mutations_and_lengths", [] {
      CHECK(valid_f4_capabilities(kCapabilities));
      for (std::size_t index = 0; index < 16; ++index) {
        for (unsigned bit = 0; bit < 8; ++bit) {
          auto mutated = kCapabilities; mutated[index] ^= 1U << bit;
          CHECK(!valid_f4_capabilities(mutated));
        }
      }
      for (std::size_t size = 0; size < 32; ++size) {
        if (size == 16) continue;
        auto malformed = kCapabilities; malformed.resize(size);
        CHECK(!valid_f4_capabilities(malformed));
      }
    });
    test("first_probe_precedes_all_group_writes_in_both_slot_orders", [] {
      for (unsigned dos : {0U, 1U}) {
        Rig r(dos); CHECK(!r.runtime.snapshot().slots[dos].capabilities_valid);
        r.runtime.tick();
        auto &f4 = dos == 0 ? r.b0 : r.b1; auto &h7 = dos == 0 ? r.b1 : r.b0;
        CHECK(f4.calls.size() == 1); CHECK(f4.calls[0].request == 0xd5); CHECK(h7.calls.empty());
        const auto view = r.runtime.snapshot().slots[dos]; CHECK(view.capabilities_valid);
        CHECK(view.capability_timestamp_us == r.clock.now);
        CHECK(!view.health_valid); CHECK(!view.field_available.voltage); CHECK(!view.field_available.temperature);
        r.runtime.tick(); CHECK(h7.count(0xd5) == 0); CHECK(f4.count(0xd5) == 1);
      }
    });
    test("every_short_capability_reply_quarantines_without_fallback", [] {
      for (std::size_t size = 0; size < 16; ++size) {
        Rig r; r.b0.capabilities.resize(size); r.b0.capability_status = Status::Short;
        r.runtime.tick(); CHECK(!r.runtime.sessions().slots[0].connected);
        CHECK(r.b0.calls.size() == 1); CHECK(r.b0.cancelled); CHECK(r.b1.calls.empty());
        CHECK(!r.runtime.snapshot().panda_states_valid); CHECK(!r.runtime.snapshot().simulated_tx_allowed);
        r.runtime.tick(); CHECK(r.b0.calls.size() == 1);
      }
    });
    test("unknown_capability_payload_quarantines_and_retires_handle", [] {
      for (unsigned byte : {0U,4U,6U,8U,9U,12U,13U}) {
        Rig r; r.b0.capabilities[byte] ^= 1U;
        r.runtime.tick(); CHECK(!r.runtime.sessions().slots[0].connected); CHECK(r.b0.cancelled);
        CHECK(!r.runtime.snapshot().slots[0].capabilities_valid); CHECK(r.b1.calls.empty());
        CHECK(r.runtime.counters().io_failures == 1);
      }
    });
    test("missing_extension_transfer_failure_never_probes_legacy_fallback", [] {
      for (const auto status : {Status::Stall, Status::Disconnected, Status::Cancelled, Status::IoError}) {
        Rig r; r.b0.capabilities.clear(); r.b0.capability_status = status;
        r.runtime.tick(); CHECK(!r.runtime.sessions().slots[0].connected); CHECK(r.b0.calls.size() == 1);
        CHECK(r.b0.calls[0].request == 0xd5); CHECK(r.b1.calls.empty());
      }
    });
    test("capability_timeout_exact_boundary_before_any_write", [] {
      for (std::uint64_t age : {kControlCommandMaxAgeUs - 1, kControlCommandMaxAgeUs}) {
        CoreRig r; const auto request = r.probe(); r.now += age;
        CHECK(r.control.complete(request, true, kCapabilities, r.now) == (age < kControlCommandMaxAgeUs));
        CHECK(r.control.snapshot(r.now).slots[0].capabilities_valid == (age < kControlCommandMaxAgeUs));
        CHECK(!r.control.snapshot(r.now).simulated_tx_allowed);
      }
    });
    test("forged_capability_token_and_duplicate_reply_cannot_bind", [] {
      CoreRig r; const auto original = r.probe();
      for (unsigned field = 0; field < 4; ++field) {
        auto forged = original;
        if (field == 0) forged.token.slot = 1;
        if (field == 1) ++forged.token.epoch;
        if (field == 2) ++forged.token.connection_token;
        if (field == 3) ++forged.token.group_generation;
        CHECK(!r.control.complete(forged, true, kCapabilities, r.now));
        CHECK(!r.control.snapshot(r.now).slots[0].capabilities_valid);
      }
      CHECK(r.control.complete(original, true, kCapabilities, r.now));
      CHECK(!r.control.complete(original, true, kCapabilities, r.now));
    });
    test("reconnection_requires_new_observation_and_old_reply_is_inert", [] {
      CoreRig r; const auto old = r.probe(); CHECK(r.control.complete(old, true, kCapabilities, r.now));
      const auto slot = r.registry.snapshot().slots[0];
      CHECK(r.registry.disconnect(0, slot.epoch, slot.connection_token));
      r.registry.reconcile({{r.p.slots[0].expected,2},{r.p.slots[1].expected,1}});
      CHECK(!r.control.snapshot(r.now).slots[0].capabilities_valid);
      const auto fresh = r.probe();
      CHECK(!r.control.complete(old, true, kCapabilities, 0));
      CHECK(!r.control.snapshot(r.now).slots[0].capabilities_valid);
      CHECK(r.control.complete(fresh, true, kCapabilities, r.now));
    });
    test("f4_health_unsupported_fields_are_null_and_voltage_is_available", [] {
      for (unsigned dos : {0U, 1U}) {
        Rig r(dos); r.ready(); const auto s = r.runtime.snapshot();
        const auto &f4 = s.slots[dos]; const auto &h7 = s.slots[1-dos];
        CHECK(f4.health_valid); CHECK(f4.field_available.voltage); CHECK(f4.field_available.temperature);
        CHECK(!f4.field_available.current); CHECK(!f4.field_available.sound_output); CHECK(!f4.field_available.spi_error_count);
        CHECK(h7.field_available.current); CHECK(h7.field_available.sound_output); CHECK(h7.field_available.spi_error_count);
        const auto p = r.runtime.peripheral_snapshot(); CHECK(p.valid); CHECK(p.voltage == 12001);
        CHECK(p.temperature == 35); CHECK(!p.current); CHECK(!p.sound_output); CHECK(!p.spi_error_count);
        CHECK(p.fan_rpm == 1500); CHECK(!p.field_available.current);
        r.clock.now += kHealthMaxAgeUs;
        const auto stale = r.runtime.snapshot().slots[dos]; CHECK(!stale.health_valid);
        CHECK(!stale.field_available.voltage); CHECK(!stale.field_available.temperature);
        CHECK(!r.runtime.peripheral_snapshot().voltage);
      }
    });
    test("h7_baseline_never_probes_extension_and_preserves_supported_fields", [] {
      Rig r(0, true); r.ready(); CHECK(r.b0.count(0xd5) == 0); CHECK(r.b1.count(0xd5) == 0);
      const auto p = r.runtime.peripheral_snapshot(); CHECK(p.valid); CHECK(p.current == 321);
      CHECK(p.sound_output == 1234); CHECK(p.spi_error_count == 0); CHECK(p.temperature == 35);
      CHECK(p.field_available.current && p.field_available.sound_output && p.field_available.spi_error_count);
    });
    test("f4_absent_spi_counter_is_ignored_h7_counter_still_faults", [] {
      for (unsigned slot : {0U, 1U}) {
        Rig r; r.ready(); (slot == 0 ? r.b0 : r.b1).spi_count = 1;
        r.clock.now += kHealthPeriodUs;
        for (unsigned n = 0; n < 5; ++n) r.runtime.tick();
        CHECK(r.runtime.snapshot().configured == (slot == 0));
        if (slot == 1) CHECK(r.runtime.snapshot().reason == "BOARD_HEALTH_FAULT");
      }
    });
    test("profile_cannot_invent_ir_missing_from_f4_candidate", [] {
      auto p = profile(); p.slots[0].expected.capabilities.ir = true; p.roles.ir_control = 0;
      SessionRegistry registry(p); ControlSession control(registry);
      registry.reconcile({{p.slots[0].expected,1},{p.slots[1].expected,1}});
      const auto commands = control.poll(1000); CHECK(commands.size() == 1);
      CHECK(!control.complete(commands[0], true, kCapabilities, 1000)); CHECK(!registry.snapshot().slots[0].connected);
    });
    std::cout << "{\"suite\":\"p6_capabilities\",\"groups\":" << groups << ",\"assertions\":" << assertions
              << ",\"failures\":0,\"skips\":0,\"device_open\":false,\"real_can_tx\":false}\n";
  } catch (const std::exception &e) { std::cerr << "FAIL " << e.what() << '\n'; return 1; }
  return 0;
}
