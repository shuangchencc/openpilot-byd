#include "../can_router.h"

#include <algorithm>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;
namespace {
std::size_t assertions = 0;
void check(bool value, int line) {
  ++assertions;
  if (!value) throw std::runtime_error("assertion at line " + std::to_string(line));
}
#define CHECK(value) check(static_cast<bool>(value), __LINE__)
constexpr std::uint64_t kNow = 1000000;
SessionProfile profile() {
  SessionProfile p; p.binding_status = "SYNTHETIC_BOUND";
  for (std::size_t i = 0; i < 2; ++i) {
    const auto suffix = std::to_string(i);
    auto &binding = p.slots[i]; auto &id = binding.expected;
    id.identity = "fixture:router-board-" + suffix;
    id.board_type = i == 0 ? "DOS" : "RED_PANDA";
    id.mcu_family = i == 0 ? "F4" : "H7";
    id.exact_mcu = "fixture:router-mcu-" + suffix;
    id.transport = i == 0 ? "SPI" : "USB";
    id.protocol_profile = kSessionProtocol;
    id.application_signature = "fixture:router-signature-" + suffix;
    id.capabilities = {{{0, 1, 2}}, i == 1, true, true, true, i == 0, i == 0, i == 0};
    binding.safety_config_semantics = "noOutput";
  }
  p.roles = {1, 1, 1, {1}, 0, 0, 0};
  return p;
}
struct Fixture {
  SessionProfile p = profile();
  SessionRegistry registry{p};
  CanRouter router{registry};
  Fixture() { CHECK(registry.reconcile(inventory()).group_available); }
  std::vector<SessionObservation> inventory(std::uint64_t first = 1, std::uint64_t second = 1) {
    return {{p.slots[0].expected, first}, {p.slots[1].expected, second}};
  }
  RouteToken token(std::uint32_t slot, std::uint64_t now = kNow) {
    const auto result = router.current_token(slot, now); CHECK(result); return result.value;
  }
};
HostTxFrame tx(std::uint32_t source = 0, std::uint64_t address = 0x123) {
  return {address, source, false, false, {static_cast<std::uint8_t>(source)}};
}
TxBatch batch(std::vector<HostTxFrame> frames = {tx()}, std::uint64_t timestamp = kNow, std::uint64_t generation = 1) {
  return {true, timestamp, std::move(frames), generation};
}
Bytes wire(std::uint32_t bus = 0, bool returned = false, bool rejected = false,
           bool fd = false, Bytes payload = {1, 2, 3}, std::uint64_t address = 0x123) {
  auto result = encode_can_frame({address, bus, false, fd, returned, rejected, std::move(payload)},
                                CanCapability::CanFd, FrameDirection::Rx);
  CHECK(result); return result.value;
}
void receive(Fixture &f, const RouteToken &token, const Bytes &bytes, std::uint64_t received = kNow, std::uint64_t now = kNow) {
  CHECK(f.router.receive(token, bytes.data(), bytes.size(), received, now));
}
}

int main() {
  std::size_t groups = 0;
  std::vector<std::string> names;
  const auto test = [&](const char *name, const std::function<void()> &run) {
    run(); ++groups; names.emplace_back(name); std::cout << "PASS " << name << '\n';
  };
  try {
    test("unbound_and_missing_group_have_no_queues_or_tx_permission", [] {
      SessionRegistry registry{SessionProfile{}}; CanRouter router(registry);
      CHECK(router.enqueue_tx(batch(), kNow).error == RouterError::GroupUnavailable);
      CHECK(router.take_tx(0, kNow).error == RouterError::GroupUnavailable);
      CHECK(router.current_token(0, kNow).error == RouterError::GroupUnavailable);
      const auto state = router.inspect(kNow); CHECK(!state); CHECK(state.value.tx[0] == 0);
      CHECK(!registry.can_tx_enabled()); CHECK(!registry.device_access_enabled());
      CHECK(router.current_token(0x100, kNow).error == RouterError::InvalidArgument);
      CHECK(router.take_tx(0xffffffffU, kNow).error == RouterError::InvalidArgument);
      CHECK(router.take_rx(2, kNow).error == RouterError::InvalidArgument);
    });
    test("six_tx_routes_use_fixed_slots_and_per_slot_fifo_once", [] {
      Fixture f; const std::vector<std::uint32_t> order{6, 0, 4, 2, 1, 5};
      std::vector<HostTxFrame> frames;
      for (const auto source : order) frames.push_back(tx(source, 0x321 + source));
      CHECK(f.router.enqueue_tx(batch(frames), kNow).value == 6);
      const auto depths = f.router.inspect(kNow).value; CHECK(depths.tx[0] == 3); CHECK(depths.tx[1] == 3);
      for (std::uint32_t slot = 0; slot < 2; ++slot) {
        for (const auto source : order) {
          if (source / 4 != slot) continue;
          const auto result = f.router.take_tx(slot, kNow); CHECK(result);
          CHECK(result.value.token.slot == slot); CHECK(result.value.frame.local_bus == source % 4);
          CHECK(result.value.frame.address == 0x321 + source); CHECK(!result.value.real_tx_authorized);
          CHECK(f.router.validate_intent(result.value, kNow).value);
          const auto decoded = decode_can_frame(result.value.encoded.data(), result.value.encoded.size(),
                                                CanCapability::CanFd, FrameDirection::Tx);
          CHECK(decoded); CHECK(decoded.value.local_bus == source % 4); CHECK(decoded.value.payload == Bytes{static_cast<std::uint8_t>(source)});
        }
        CHECK(f.router.take_tx(slot, kNow).error == RouterError::Empty);
      }
    });
    test("six_rx_routes_preserve_ordinary_returned_and_rejected_flags", [] {
      Fixture f;
      for (std::uint32_t slot = 0; slot < 2; ++slot) {
        const auto token = f.token(slot);
        for (std::uint32_t bus = 0; bus < 3; ++bus) {
          for (std::uint32_t flag = 0; flag < 3; ++flag) {
            receive(f, token, wire(bus, flag == 1, flag == 2));
            const auto got = f.router.take_rx(slot, kNow); CHECK(got);
            CHECK(got.value.source == slot * 4 + bus + (flag == 1 ? 0x80 : (flag == 2 ? 0xc0 : 0)));
            CHECK(got.value.frame.local_bus == bus); CHECK(got.value.frame.returned == (flag == 1));
            CHECK(got.value.frame.rejected == (flag == 2)); CHECK(got.value.receive_timestamp_us == kNow);
          }
        }
      }
    });
    test("invalid_event_reserved_flagged_and_wide_tx_routes_are_atomic", [] {
      Fixture f; auto event = batch({tx(0), tx(4)}); event.valid = false;
      CHECK(f.router.enqueue_tx(event, kNow).error == RouterError::InvalidEvent);
      for (const auto source : {3U, 7U, 8U, 0x40U, 0x80U, 0x81U, 0x84U, 0xc0U, 0xc6U, 0x100U, 0x104U, 0xffffffffU}) {
        CHECK(f.router.enqueue_tx(batch({tx(0), tx(source)}), kNow).error == RouterError::InvalidRoute);
        CHECK(f.router.inspect(kNow).value.tx == (std::array<std::size_t, 2>{0, 0}));
      }
      CHECK(f.router.enqueue_tx(batch({}), kNow).error == RouterError::InvalidEvent);
      for (const auto address : {0x800ULL, 0x100000000ULL, 0xffffffffffffffffULL}) {
        CHECK(f.router.enqueue_tx(batch({tx(4), tx(0, address)}), kNow).error == RouterError::Malformed);
        CHECK(f.router.inspect(kNow).value.tx[1] == 0);
      }
      auto extended = tx(4, 0x20000000); extended.extended = true;
      CHECK(f.router.enqueue_tx(batch({tx(), extended}), kNow).error == RouterError::Malformed);
      auto oversized = tx(4); oversized.payload.resize(1024 * 1024);
      CHECK(f.router.enqueue_tx(batch({tx(), oversized}), kNow).error == RouterError::Malformed);
      CHECK(f.router.inspect(kNow).value.tx == (std::array<std::size_t, 2>{0, 0}));
      CHECK(f.registry.snapshot().group_available);
      CHECK(f.router.counters().invalid_batches >= 17);
    });
    test("fd_is_bound_capability_in_both_directions", [] {
      Fixture f; auto fd = tx(0); fd.fd = true;
      CHECK(f.router.enqueue_tx(batch({fd}), kNow).error == RouterError::Malformed);
      fd.source = 4; fd.payload.resize(64, 0xa5);
      CHECK(f.router.enqueue_tx(batch({fd}), kNow)); CHECK(f.router.take_tx(1, kNow).value.frame.fd);
      const auto token1 = f.token(1); receive(f, token1, wire(2, false, false, true, Bytes(64, 0x5a)));
      CHECK(f.router.take_rx(1, kNow).value.frame.fd);
      const auto token0 = f.token(0); const auto bytes = wire(0, false, false, true, {});
      CHECK(f.router.receive(token0, bytes.data(), bytes.size(), kNow, kNow).error == RouterError::Malformed);
      CHECK(!f.registry.snapshot().group_available);
    });
    test("fragmentation_is_per_slot_and_preserves_first_receive_timestamp", [] {
      Fixture f; const auto t0 = f.token(0), t1 = f.token(1); const auto bytes0 = wire(2), bytes1 = wire(1);
      CHECK(f.router.receive(t0, bytes0.data(), 2, kNow, kNow).value == 0);
      CHECK(f.router.receive(t1, bytes1.data(), 3, kNow + 1, kNow + 1).value == 0);
      CHECK(f.router.receive(t0, bytes0.data() + 2, bytes0.size() - 2, kNow + 10, kNow + 10).value == 1);
      const auto got0 = f.router.take_rx(0, kNow + 10); CHECK(got0.value.source == 2); CHECK(got0.value.receive_timestamp_us == kNow);
      CHECK(f.router.inspect(kNow + 10).value.fragment_bytes[1] == 3);
      CHECK(f.router.receive(t1, bytes1.data() + 3, bytes1.size() - 3, kNow + 20, kNow + 20).value == 1);
      const auto got1 = f.router.take_rx(1, kNow + 20); CHECK(got1.value.source == 5); CHECK(got1.value.receive_timestamp_us == kNow + 1);
      CHECK(f.router.inspect(kNow + 20).value.fragment_bytes == (std::array<std::size_t, 2>{0, 0}));
    });
    test("completion_and_next_fragment_use_separate_original_timestamps", [] {
      Fixture f; const auto token = f.token(0); const auto bytes = wire();
      CHECK(f.router.receive(token, bytes.data(), 2, kNow, kNow));
      Bytes next(bytes.begin() + 2, bytes.end()); next.insert(next.end(), bytes.begin(), bytes.begin() + 3);
      CHECK(f.router.receive(token, next.data(), next.size(), kNow + 10, kNow + 10).value == 1);
      CHECK(f.router.take_rx(0, kNow + 10).value.receive_timestamp_us == kNow);
      CHECK(f.router.receive(token, bytes.data() + 3, bytes.size() - 3, kNow + 20, kNow + 20).value == 1);
      CHECK(f.router.take_rx(0, kNow + 20).value.receive_timestamp_us == kNow + 10);
    });
    test("sendcan_and_queue_age_threshold_minus_one_equal_plus_one", [] {
      for (const auto delta : {99999ULL, 100000ULL, 100001ULL}) {
        Fixture f; const auto got = f.router.enqueue_tx(batch({tx()}, kNow - delta), kNow);
        CHECK((got.error == RouterError::None) == (delta < 100000));
      }
      for (const auto delta : {19999ULL, 20000ULL, 20001ULL}) {
        Fixture f; CHECK(f.router.enqueue_tx(batch(), kNow));
        const auto got = f.router.take_tx(0, kNow + delta);
        CHECK((got.error == RouterError::None) == (delta < 20000));
        CHECK(f.router.counters().expired_tx == (delta < 20000 ? 0U : 1U));
      }
      Fixture f; CHECK(f.router.enqueue_tx(batch({tx()}, kNow - 99990), kNow));
      CHECK(f.router.take_tx(0, kNow + 10).error == RouterError::Empty);
      CHECK(f.router.counters().expired_tx == 1);
    });
    test("rx_queue_age_threshold_and_publication_never_refresh_age", [] {
      for (const auto delta : {99999ULL, 100000ULL, 100001ULL}) {
        Fixture f; receive(f, f.token(0), wire());
        const auto got = f.router.take_rx(0, kNow + delta);
        CHECK((got.error == RouterError::None) == (delta < 100000));
        if (got) CHECK(got.value.receive_timestamp_us == kNow);
        CHECK(f.router.counters().expired_rx == (delta < 100000 ? 0U : 1U));
      }
      Fixture f; const auto token = f.token(0); const auto bytes = wire();
      CHECK(f.router.receive(token, bytes.data(), 1, kNow, kNow));
      CHECK(f.router.receive(token, bytes.data() + 1, bytes.size() - 1, kNow + 99999, kNow + 99999).value == 1);
      CHECK(f.router.take_rx(0, kNow + 100000).error == RouterError::Empty);
      CHECK(f.router.counters().expired_rx == 1);
    });
    test("expired_partial_prefix_closes_group_without_unsafe_resync", [] {
      for (const auto delta : {99999ULL, 100000ULL, 100001ULL}) {
        Fixture f; const auto token = f.token(0); const auto bytes = wire();
        CHECK(f.router.receive(token, bytes.data(), 1, kNow, kNow));
        const auto inspected = f.router.inspect(kNow + delta);
        CHECK(static_cast<bool>(inspected) == (delta < 100000));
        if (delta >= 100000) {
          CHECK(!f.registry.snapshot().group_available); CHECK(inspected.value.fragment_bytes[0] == 0);
          CHECK(f.router.counters().expired_fragments == 1);
          CHECK(f.router.receive(token, bytes.data() + 1, bytes.size() - 1, kNow + delta, kNow + delta).error == RouterError::StaleSession);
        }
      }
    });
    test("drop_and_reconnect_clear_both_queues_fragments_and_old_callbacks", [] {
      Fixture f; const auto old0 = f.token(0), old1 = f.token(1); const auto bytes = wire();
      CHECK(f.router.enqueue_tx(batch({tx(0), tx(4)}), kNow));
      receive(f, old1, bytes); CHECK(f.router.receive(old0, bytes.data(), 3, kNow, kNow));
      CHECK(f.registry.disconnect(0, old0.epoch, old0.connection_token));
      CHECK(f.router.take_tx(1, kNow).error == RouterError::GroupUnavailable);
      const auto down = f.router.inspect(kNow); CHECK(down.value.tx == (std::array<std::size_t, 2>{0, 0}));
      CHECK(down.value.rx[1] == 0); CHECK(down.value.fragment_bytes[0] == 0);
      CHECK(f.router.counters().epoch_tx_drops == 2); CHECK(f.router.counters().epoch_rx_drops == 1);
      CHECK(f.router.counters().epoch_fragment_bytes == 3);
      CHECK(f.registry.reconcile(f.inventory(2, 1)).group_available);
      const auto fresh0 = f.token(0); CHECK(fresh0.epoch != old0.epoch);
      CHECK(f.router.receive(old0, nullptr, 1000000, 0, 0).error == RouterError::StaleSession);
      CHECK(f.router.receive(old1, nullptr, 1000000, 0, 0).error == RouterError::StaleSession);
      CHECK(f.registry.snapshot().group_available); CHECK(f.router.counters().stale_callbacks == 2);
      receive(f, fresh0, bytes); CHECK(f.router.take_rx(0, kNow).value.frame.payload == Bytes({1, 2, 3}));
      CHECK(f.router.take_tx(0, kNow).error == RouterError::Empty); CHECK(f.router.take_tx(1, kNow).error == RouterError::Empty);
    });
    test("unobserved_outage_and_reconnection_still_invalidate_queued_generation", [] {
      Fixture f; const auto old = f.token(0); CHECK(f.router.enqueue_tx(batch({tx(4)}), kNow));
      CHECK(f.registry.disconnect(0, old.epoch, old.connection_token));
      CHECK(f.registry.reconcile(f.inventory(2, 1)).group_available);
      CHECK(f.router.take_tx(1, kNow).error == RouterError::Empty);
      CHECK(f.router.counters().epoch_tx_drops == 1);
    });
    test("caller_held_old_batch_cannot_enter_reconnected_generation", [] {
      Fixture f; const auto old = f.token(0);
      auto captured = batch({tx(0), tx(4)}, kNow, old.group_generation);
      CHECK(f.registry.disconnect(0, old.epoch, old.connection_token));
      CHECK(f.registry.reconcile(f.inventory(2, 1)).group_available);
      CHECK(f.router.enqueue_tx(captured, kNow).error == RouterError::StaleSession);
      CHECK(f.router.enqueue_tx(captured, 0).error == RouterError::StaleSession);
      CHECK(f.registry.snapshot().group_available);
      CHECK(f.router.inspect(kNow).value.tx == (std::array<std::size_t, 2>{0, 0}));
      captured.group_generation = 0;
      CHECK(f.router.enqueue_tx(captured, kNow).error == RouterError::StaleSession);
      auto fresh = batch({tx()}, kNow, f.token(0).group_generation);
      CHECK(f.router.enqueue_tx(fresh, kNow).value == 1);
    });
    test("replacement_never_inherits_fixed_slot_or_queued_traffic", [] {
      Fixture f; CHECK(f.router.enqueue_tx(batch({tx(0), tx(4)}), kNow));
      auto inventory = f.inventory(2, 1); inventory[0].identity.identity = "fixture:replacement";
      CHECK(!f.registry.reconcile(inventory).group_available);
      CHECK(f.router.take_tx(0, kNow).error == RouterError::GroupUnavailable);
      CHECK(f.router.take_tx(1, kNow).error == RouterError::GroupUnavailable);
      CHECK(f.router.inspect(kNow).value.tx == (std::array<std::size_t, 2>{0, 0}));
      CHECK(!f.registry.reconcile(inventory).group_available);
    });
    test("tx_capacity_and_mixed_batch_overflow_reject_atomically", [] {
      Fixture f; CHECK(f.router.enqueue_tx(batch(std::vector<HostTxFrame>(64, tx(0))), kNow).value == 64);
      CHECK(f.router.enqueue_tx(batch({tx(4), tx(0)}), kNow).error == RouterError::Overflow);
      CHECK(f.router.inspect(kNow).value.tx == (std::array<std::size_t, 2>{64, 0}));
      CHECK(f.router.enqueue_tx(batch(std::vector<HostTxFrame>(65, tx(4))), kNow).error == RouterError::Overflow);
      CHECK(f.router.inspect(kNow).value.tx[1] == 0); CHECK(f.registry.snapshot().group_available);
      CHECK(f.router.counters().tx_overflows == 2);
    });
    test("rx_overflow_retires_group_and_requires_new_session_tokens", [] {
      Fixture f; const auto token = f.token(0); const auto bytes = wire(); Bytes combined;
      for (std::size_t i = 0; i < 256; ++i) combined.insert(combined.end(), bytes.begin(), bytes.end());
      CHECK(f.router.receive(token, combined.data(), combined.size(), kNow, kNow).value == 256);
      CHECK(f.router.enqueue_tx(batch({tx(4)}), kNow));
      CHECK(f.router.receive(token, bytes.data(), bytes.size(), kNow, kNow).error == RouterError::Overflow);
      CHECK(f.router.counters().rx_overflows == 1); CHECK(!f.registry.snapshot().group_available);
      CHECK(f.router.inspect(kNow).value.rx[0] == 0); CHECK(f.router.inspect(kNow).value.tx[1] == 0);
      CHECK(!f.registry.reconcile(f.inventory()).group_available);
      CHECK(f.registry.reconcile(f.inventory(2, 2)).group_available);
      CHECK(f.router.take_rx(0, kNow).error == RouterError::Empty);
    });
    test("rx_malformed_batch_has_no_valid_prefix_publication", [] {
      for (const auto mode : {0, 1, 2}) {
        Fixture f; const auto token = f.token(0); const auto bytes = wire(); Bytes data = bytes;
        if (mode == 0) { auto bad = bytes; bad[5] ^= 1; data.insert(data.end(), bad.begin(), bad.end()); }
        else if (mode == 1) data.resize(kCanMaxInputSize + 1);
        const auto got = f.router.receive(token, mode == 2 ? nullptr : data.data(), data.size(), kNow, kNow);
        CHECK(got.error == RouterError::Malformed); CHECK(!f.registry.snapshot().group_available);
        CHECK(f.router.inspect(kNow).value.rx[0] == 0); CHECK(f.router.counters().malformed == 1);
      }
    });
    test("future_timestamps_rejected_and_stale_callbacks_cannot_poison_new_group", [] {
      Fixture f; CHECK(f.router.enqueue_tx(batch({tx()}, kNow + 1), kNow).error == RouterError::FutureTimestamp);
      CHECK(f.registry.snapshot().group_available);
      const auto token = f.token(0); const auto bytes = wire();
      CHECK(f.router.receive(token, bytes.data(), bytes.size(), kNow + 1, kNow).error == RouterError::FutureTimestamp);
      CHECK(!f.registry.snapshot().group_available);
      CHECK(f.registry.reconcile(f.inventory(2, 2)).group_available);
      CHECK(f.router.receive(token, nullptr, 0xffffffffU, UINT64_MAX, 0).error == RouterError::StaleSession);
      CHECK(f.registry.snapshot().group_available); CHECK(f.router.current_token(0, kNow));
    });
    test("clock_rollback_and_wrap_fail_closed_without_unsigned_age_bypass", [] {
      for (const auto start : {kNow, std::numeric_limits<std::uint64_t>::max() - 1}) {
        Fixture f; CHECK(f.router.enqueue_tx(batch({tx()}, start), start));
        CHECK(f.router.take_tx(0, start - 1).error == RouterError::ClockRollback);
        CHECK(!f.registry.snapshot().group_available); CHECK(f.router.counters().clock_faults == 1);
        CHECK(f.registry.reconcile(f.inventory(2, 2)).group_available);
        CHECK(f.router.current_token(0, start).error == RouterError::ClockRollback);
      }
      Fixture f; const auto top = std::numeric_limits<std::uint64_t>::max();
      CHECK(f.router.enqueue_tx(batch({tx()}, top - 19999), top - 19999));
      CHECK(f.router.take_tx(0, top).error == RouterError::None);
      CHECK(f.router.inspect(0).error == RouterError::ClockRollback);
    });
    test("held_intent_is_revalidated_after_drop_expiry_and_mutation", [] {
      Fixture f; CHECK(f.router.enqueue_tx(batch(), kNow)); const auto held = f.router.take_tx(0, kNow).value;
      CHECK(f.router.validate_intent(held, kNow).value);
      auto changed = held; changed.encoded.back() ^= 1;
      CHECK(f.router.validate_intent(changed, kNow).error == RouterError::Malformed);
      changed = held; changed.real_tx_authorized = true;
      CHECK(f.router.validate_intent(changed, kNow).error == RouterError::Malformed);
      CHECK(f.router.validate_intent(held, kNow + 19999));
      CHECK(f.router.validate_intent(held, kNow + 20000).error == RouterError::Expired);
      CHECK(f.registry.disconnect(held.token.slot, held.token.epoch, held.token.connection_token));
      CHECK(f.router.validate_intent(held, kNow + 20000).error == RouterError::GroupUnavailable);
      CHECK(f.router.validate_intent(held, 0).error == RouterError::GroupUnavailable);
      CHECK(f.registry.reconcile(f.inventory(2, 1)).group_available);
      CHECK(f.router.validate_intent(held, kNow + 20000).error == RouterError::StaleSession);
      CHECK(f.router.validate_intent(held, 0).error == RouterError::StaleSession);
      CHECK(f.registry.snapshot().group_available);
      CHECK(f.router.take_tx(0, kNow + 20000).error == RouterError::Empty);
    });
    test("held_rx_republication_preserves_age_and_rechecks_generation", [] {
      Fixture f; const auto token = f.token(0); receive(f, token, wire());
      const auto held = f.router.take_rx(0, kNow).value;
      CHECK(f.router.validate_rx_frame(held, kNow + 99999).value);
      CHECK(held.receive_timestamp_us == kNow);
      CHECK(f.router.validate_rx_frame(held, kNow + 100000).error == RouterError::Expired);
      CHECK(held.receive_timestamp_us == kNow);
      CHECK(f.registry.disconnect(0, token.epoch, token.connection_token));
      CHECK(f.router.validate_rx_frame(held, 0).error == RouterError::GroupUnavailable);
      CHECK(f.registry.reconcile(f.inventory(2, 1)).group_available);
      CHECK(f.router.validate_rx_frame(held, 0).error == RouterError::StaleSession);
      CHECK(f.registry.snapshot().group_available);
      const auto fresh_token = f.token(0, kNow + 100000);
      receive(f, fresh_token, wire(), kNow + 100000, kNow + 100000);
      auto fresh = f.router.take_rx(0, kNow + 100000).value;
      fresh.source = 4;
      CHECK(f.router.validate_rx_frame(fresh, kNow + 100000).error == RouterError::Malformed);
    });
    test("empty_callbacks_do_not_refresh_partial_timestamp", [] {
      Fixture f; const auto token = f.token(0); const auto bytes = wire();
      CHECK(f.router.receive(token, bytes.data(), 2, kNow, kNow));
      CHECK(f.router.receive(token, nullptr, 0, kNow + 99999, kNow + 99999));
      CHECK(f.router.inspect(kNow + 100000).error == RouterError::Expired);
      CHECK(f.router.counters().expired_fragments == 1);
    });
    std::cout << "{\"passed\":" << groups << ",\"failed\":0,\"assertions\":" << assertions
              << ",\"device_access\":false,\"real_can_tx\":false,\"cases\":[";
    for (std::size_t i = 0; i < names.size(); ++i) std::cout << (i == 0 ? "\"" : ",\"") << names[i] << '"';
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "FAIL after " << groups << " groups: " << error.what() << '\n'; return 1;
  }
}
