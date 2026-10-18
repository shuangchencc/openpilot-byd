#include "../native_spi_probe.h"

#include <algorithm>
#include <cerrno>
#include <functional>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;
namespace {
std::size_t assertions = 0;
void check(bool condition, int line) { ++assertions; if (!condition) throw std::runtime_error("SPI probe assertion at line " + std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
class FakeClock final : public Clock {
public:
  std::uint64_t now = 0;
  std::function<void()> on_now;
  std::uint64_t now_us() override { if (on_now) on_now(); return now; }
  void wait_us(std::uint32_t us) override { now += us; }
};
std::uint32_t le(const Bytes &bytes, std::size_t offset, unsigned count) { std::uint32_t n = 0; for (unsigned i = 0; i < count; ++i) n |= std::uint32_t(bytes.at(offset + i)) << (8 * i); return n; }
void append(Bytes &bytes, std::uint32_t n, unsigned count) { for (unsigned i = 0; i < count; ++i) bytes.push_back(static_cast<std::uint8_t>(n >> (8 * i))); }
std::uint32_t crc(const Bytes &bytes) { std::uint32_t n = 0xffffffffU; for (auto byte : bytes) { n ^= byte; for (unsigned i = 0; i < 8; ++i) n = (n >> 1) ^ ((n & 1) ? 0xedb88320U : 0); } return n ^ 0xffffffffU; }
std::array<std::uint8_t, 128> signature(std::uint8_t seed = 17) { std::array<std::uint8_t, 128> out; for (std::size_t i = 0; i < out.size(); ++i) out[i] = static_cast<std::uint8_t>(i + seed); return out; }
class FakeCalls final : public NativeSpiSyscalls {
public:
  FakeClock &clock;
  explicit FakeCalls(FakeClock &c) : clock(c) {
    replies[0xc3] = {1,2,3,4,5,6,7,8,9,10,11,12}; replies[0xc1] = {7};
    replies[0xdd] = {0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75};
    auto sig = signature(); replies[0xd3] = Bytes(sig.begin(), sig.begin() + 64); replies[0xd4] = Bytes(sig.begin() + 64, sig.end());
    replies[0xd5] = {'F','4','C','1',1,0,16,0,0x1f,0,0,0,7,0,0,0};
    replies[0xd2] = Bytes(48, 5); replies[0xc2] = Bytes(64, 6); replies[0xb2] = {1,2};
  }
  int opens = 0, closes = 0, messages = 0, locks = 0, unlocks = 0;
  bool lease = false, opened = false, corrupt_crc = false, wrong_id = false;
  std::uint64_t delay_us = 0;
  int fail_request = -1;
  int fail_unlock_at = -1;
  std::map<std::uint8_t, Bytes> replies;
  std::vector<std::uint8_t> controls;
  std::vector<std::uint32_t> ids;
  std::uint8_t pending = 0;
  std::uint32_t pending_id = 0;
  NativeSpiCall open_device(const std::string &path, int) override { CHECK(path == "/dev/spidev0.0" && !opened); ++opens; opened = true; return {23,0}; }
  NativeSpiCall stat_device(int fd, NativeSpiNode &node) override { CHECK(fd == 23 && opened); node = {true,153,0}; return {0,0}; }
  NativeSpiCall read_mode(int fd, std::uint32_t &out) override { CHECK(fd == 23 && lease); out = 0; return {0,0}; }
  NativeSpiCall read_bits(int fd, std::uint8_t &out) override { CHECK(fd == 23 && lease); out = 8; return {0,0}; }
  NativeSpiCall read_speed(int fd, std::uint32_t &out) override { CHECK(fd == 23 && lease); out = 10000000; return {0,0}; }
  NativeSpiCall lock_device(int fd, bool acquire) override {
    CHECK(fd == 23 && opened); CHECK(lease != acquire); lease = acquire;
    if (acquire) ++locks; else if (++unlocks == fail_unlock_at) return {-1,EIO};
    return {0,0};
  }
  NativeSpiCall close_device(int fd) override { CHECK(fd == 23 && opened && !lease); opened = false; ++closes; return {0,0}; }
  NativeSpiCall message(int fd, const std::uint8_t *tx, std::uint8_t *rx, std::size_t size) override {
    CHECK(fd == 23 && opened && lease); ++messages; clock.now += delay_us;
    Bytes bytes(tx, tx + size), response(size);
    if (bytes[0] != 0xff) {
      CHECK(bytes[0] == 0 || bytes[0] == 0x81);
      CHECK(le(bytes, size - 4, 4) == crc(Bytes(bytes.begin(), bytes.end() - 4)));
      pending_id = le(bytes, 1, 4); ids.push_back(pending_id);
      if (bytes[0] == 0) { CHECK(le(bytes, 5, 2) == 7); pending = bytes[9]; controls.push_back(pending); CHECK(bytes[12] == 0 && bytes[13] == 0); }
      else pending = 0x81;
      if (pending == fail_request) return {-1, EINTR};
      response[0] = 0x3c;
    } else {
      auto data = pending == 0x81 ? Bytes(size - 11, 42) : replies.at(pending);
      if (data.size() > size - 11) data.resize(size - 11); // Oversize field exercised by length/CRC tests elsewhere.
      response = {0xc3}; append(response, wrong_id ? pending_id + 1 : pending_id, 4); append(response, static_cast<std::uint32_t>(data.size()), 2);
      response.insert(response.end(), data.begin(), data.end()); append(response, crc(response), 4);
      if (corrupt_crc) response.back() ^= 1;
      response.resize(size);
    }
    std::copy(response.begin(), response.end(), rx); return {static_cast<int>(size),0};
  }
};
NativeSpiProbePolicy policy() {
  NativeSpiProbePolicy result; result.first_request_id = 100;
  result.selector.path = "/dev/spidev0.0"; result.selector.identity_evidence_sha256 = std::string(64, 'b'); result.selector.physical_identity_verified = true;
  result.selector.expected_major = 153; result.selector.expected_minor = 0; result.selector.expected_mode = 0;
  result.selector.expected_bits_per_word = 8; result.selector.expected_max_speed_hz = 10000000;
  return result;
}
struct Fixture {
  FakeClock clock;
  std::shared_ptr<FakeCalls> calls = std::make_shared<FakeCalls>(clock);
  NativeSpiProbePolicy options = policy();
  NativeSpiProbeResult probe() { return discover_native_spi(calls, clock, options, true); }
};
Request read(std::uint8_t command, std::size_t length) { Request request; request.kind = Kind::ControlRead; request.request = command; request.read_length = length; return request; }
void native_default_denied_no_open() {
  FakeClock clock;
  auto result = discover_native_spi(clock, policy());
  CHECK(!result.complete() && result.status == Status::Invalid && !result.transport && result.observation.lease_nonce == 0);
  CHECK(result.validated_selector == NativeSpiSelector{} && result.first_request_id == 0 && result.started_us == 0 && result.completed_us == 0);
  Fixture f;
  auto denied = discover_native_spi(f.calls, f.clock, f.options);
  CHECK(!denied.complete() && f.calls->opens == 0);
}
void missing_identity_or_request_nonce_denied() {
  for (unsigned variant = 0; variant < 12; ++variant) {
    Fixture f;
    if (variant == 0) f.options.selector.physical_identity_verified = false;
    if (variant == 1) f.options.selector.expected_mode.reset();
    if (variant == 2) f.options.first_request_id = 0;
    if (variant == 3) f.options.first_request_id = UINT32_MAX;
    if (variant == 4) f.options.budget_ms = 0;
    if (variant == 5) f.options.budget_ms = 101;
    if (variant == 6) f.options.f4_application_signatures.resize(3, signature());
    if (variant == 7) f.options.f4_application_signatures.resize(1, {});
    if (variant == 8) f.options.f4_application_signatures.resize(2, signature());
    if (variant == 9) f.options.selector.expected_mode = 1;
    if (variant == 10) f.options.selector.expected_bits_per_word = 16;
    if (variant == 11) f.options.selector.expected_max_speed_hz = 50000001;
    auto result = f.probe(); CHECK(!result.complete() && f.calls->opens == 0);
  }
}
void exact_probe_retains_same_fd() {
  Fixture f; f.clock.now = 1000; f.calls->delay_us = 10; auto result = f.probe();
  CHECK(result.complete() && result.observation.serial.empty() && result.observation.lease_nonce != 0);
  CHECK(result.observation.board_type == 7 && result.observation.application_signature == signature());
  CHECK(result.observation.f4_capabilities.empty());
  CHECK(f.calls->controls == std::vector<std::uint8_t>({0xc3,0xc1,0xdd,0xd3,0xd4}));
  CHECK(f.calls->ids == std::vector<std::uint32_t>({100,101,102,103,104}));
  CHECK(f.calls->opens == 1 && f.calls->closes == 0 && f.calls->messages == 10);
  CHECK(result.validated_selector == f.options.selector && result.first_request_id == 100);
  CHECK(result.started_us == 1000 && result.completed_us == 1100);
  auto health = result.transport->transport->perform(read(0xd2,48), 5);
  CHECK(health.status == Status::Ok && health.data == Bytes(48,5));
  CHECK(f.calls->opens == 1 && f.calls->ids.back() == 105);
  CHECK(result.completed_us == 1100 && f.clock.now == 1120);
  result.transport.reset(); CHECK(f.calls->closes == 1 && f.calls->locks == f.calls->unlocks);
}
void f4_capability_requires_explicit_signature() {
  Fixture f; f.calls->replies[0xc1] = {6}; auto unrecognized = f.probe();
  CHECK(unrecognized.complete() && unrecognized.observation.f4_capabilities.empty());
  CHECK(f.calls->controls.size() == 5);
  const auto before = f.calls->messages;
  CHECK(unrecognized.transport->transport->perform(read(0xd5,16), 5).status == Status::Invalid);
  CHECK(f.calls->messages == before);
  Fixture recognized; recognized.calls->replies[0xc1] = {6}; recognized.options.f4_application_signatures = {signature()};
  auto result = recognized.probe();
  CHECK(result.complete() && result.observation.f4_capabilities == recognized.calls->replies[0xd5]);
  CHECK(recognized.calls->controls == std::vector<std::uint8_t>({0xc3,0xc1,0xdd,0xd3,0xd4,0xd5}));
}
void f4_board_conflict_and_capability_mismatch() {
  Fixture conflict; conflict.options.f4_application_signatures = {signature()};
  auto result = conflict.probe(); CHECK(!result.complete() && result.reason == "F4_SIGNATURE_BOARD_CONFLICT");
  CHECK(conflict.calls->controls.size() == 5 && conflict.calls->closes == 1);
  Fixture mismatch; mismatch.options.f4_application_signatures = {signature()}; mismatch.calls->replies[0xc1] = {6}; mismatch.calls->replies[0xd5][8] = 0;
  result = mismatch.probe(); CHECK(!result.complete() && result.reason == "F4_CAPABILITY_MISMATCH" && mismatch.calls->closes == 1);
}
void zero_uniform_identity_rejected() {
  for (unsigned variant = 0; variant < 5; ++variant) {
    Fixture f;
    if (variant < 2) f.calls->replies[0xc3] = Bytes(12,variant == 0 ? 0 : 0xff);
    if (variant == 2) f.calls->replies[0xc1] = {0};
    if (variant > 2) { f.calls->replies[0xd3] = Bytes(64,variant == 3 ? 0 : 0xff); f.calls->replies[0xd4] = f.calls->replies[0xd3]; }
    auto result = f.probe(); CHECK(!result.complete() && result.status == Status::Poisoned && result.observation.lease_nonce == 0);
    CHECK(result.validated_selector == NativeSpiSelector{} && result.first_request_id == 0 && result.started_us == 0 && result.completed_us == 0);
    CHECK(f.calls->closes == 1);
  }
}
void protocol_mismatch_rejected() {
  Fixture f; f.calls->replies[0xdd][0] ^= 1; auto result = f.probe();
  CHECK(!result.complete() && result.reason == "PROTOCOL_MISMATCH" && f.calls->controls.size() == 3 && f.calls->closes == 1);
}
void short_each_identity_reply_rejected() {
  for (const auto command : {0xc3,0xc1,0xdd,0xd3,0xd4,0xd5}) {
    Fixture f; f.calls->replies[0xc1] = {6}; f.options.f4_application_signatures = {signature()};
    f.calls->replies[static_cast<std::uint8_t>(command)].pop_back();
    auto result = f.probe(); CHECK(!result.complete() && result.status == Status::Poisoned && f.calls->closes == 1);
    CHECK(f.calls->controls.back() == command);
  }
}
void wire_errors_no_retry_or_reopen() {
  for (unsigned variant = 0; variant < 3; ++variant) {
    Fixture f;
    if (variant == 0) f.calls->fail_request = 0xc3;
    if (variant == 1) f.calls->wrong_id = true;
    if (variant == 2) f.calls->corrupt_crc = true;
    auto result = f.probe(); CHECK(!result.complete() && result.status == Status::IoError);
    CHECK(f.calls->opens == 1 && f.calls->closes == 1 && f.calls->controls.size() == 1);
  }
}
void probe_deadline_and_wrap_fail_closed() {
  for (auto start : {std::uint64_t{0}, std::numeric_limits<std::uint64_t>::max() - 3000}) {
    Fixture f; f.clock.now = start; f.calls->delay_us = 6000;
    auto result = f.probe(); CHECK(!result.complete() && result.status == (start == 0 ? Status::Timeout : Status::IoError) && f.calls->messages == 1);
    CHECK(f.calls->opens == 1 && f.calls->closes == 1);
  }
  Fixture f; f.options.budget_ms = 2; f.calls->delay_us = 500;
  auto result = f.probe(); CHECK(!result.complete() && result.status == Status::Timeout && f.calls->closes == 1);
}
void all_out_and_mutating_in_rejected() {
  Fixture f; auto result = f.probe(); CHECK(result.complete()); const auto before = f.calls->messages;
  Request control; control.kind = Kind::ControlWrite; control.request = 0xdc;
  CHECK(result.transport->backend->transact(control,5).status == Status::Invalid);
  Request bulk; bulk.kind = Kind::BulkWrite; bulk.endpoint = 3; bulk.payload = {1};
  CHECK(result.transport->backend->transact(bulk,5).status == Status::Invalid);
  for (const auto command : {0xb0,0xb1,0xc0,0xd1,0xd8,0xdc,0xdf,0xf3,0xff})
    CHECK(result.transport->backend->transact(read(static_cast<std::uint8_t>(command),1),5).status == Status::Invalid);
  auto malformed = read(0xc3,12); malformed.value = 1;
  CHECK(result.transport->backend->transact(malformed,5).status == Status::Invalid);
  malformed = read(0xc3,12); malformed.index = 1;
  CHECK(result.transport->backend->transact(malformed,5).status == Status::Invalid);
  CHECK(result.transport->backend->transact(read(0xc3,11),5).status == Status::Invalid);
  CHECK(f.calls->messages == before);
}
void cross_request_clock_rollback_rejected() {
  Fixture f; f.calls->delay_us = 500;
  unsigned idle_reads = 0;
  f.clock.on_now = [&] {
    if (f.calls->controls.size() == 1 && !f.calls->lease && ++idle_reads == 3) f.clock.now = 500;
  };
  const auto result = f.probe();
  CHECK(!result.complete() && result.status == Status::IoError);
  CHECK(result.reason == "BOARD_TYPE_PROBE_FAILED" && f.calls->controls.size() == 1 && f.calls->closes == 1);
}
void unlock_failure_invalidates_final_probe_and_runtime_read() {
  Fixture final_probe; final_probe.calls->fail_unlock_at = 6;
  auto failed = final_probe.probe();
  CHECK(!failed.complete() && failed.status == Status::IoError && failed.observation.lease_nonce == 0);
  CHECK(final_probe.calls->controls.back() == 0xd4 && final_probe.calls->closes == 1);
  Fixture runtime; auto retained = runtime.probe(); CHECK(retained.complete());
  runtime.calls->fail_unlock_at = 7;
  auto health = retained.transport->transport->perform(read(0xd2,48),5);
  CHECK(health.status == Status::IoError && health.actual == 48 && health.actual_known && health.data == Bytes(48,5));
  CHECK(runtime.calls->closes == 1 && runtime.calls->opens == 1);
  const auto before = runtime.calls->messages;
  CHECK(retained.transport->transport->perform(read(0xd2,48),5).status == Status::Poisoned && runtime.calls->messages == before);
}
void exact_bulk_in_and_can_health_allowed() {
  Fixture f; auto result = f.probe(); CHECK(result.complete());
  Request bulk; bulk.kind = Kind::BulkRead; bulk.endpoint = 0x81; bulk.read_length = 16;
  CHECK(result.transport->transport->perform(bulk,5).data == Bytes(16,42));
  bulk.endpoint = 0x82; const auto before = f.calls->messages;
  CHECK(result.transport->backend->transact(bulk,5).status == Status::Invalid && f.calls->messages == before);
  auto health = read(0xc2,64); health.value = 2;
  CHECK(result.transport->transport->perform(health,5).data == Bytes(64,6));
  health.value = 3;
  CHECK(result.transport->backend->transact(health,5).status == Status::Invalid);
}
void retained_transport_cancel_and_dual_isolation() {
  Fixture a,b; auto first = a.probe(), second = b.probe(); CHECK(first.complete() && second.complete());
  CHECK(first.observation.lease_nonce != second.observation.lease_nonce);
  const auto before = a.calls->messages; first.transport->transport->cancel();
  CHECK(first.transport->backend->transact(read(0xd2,48),5).status == Status::Cancelled);
  CHECK(first.transport->spi_io->begin(5) == Status::Cancelled && a.calls->messages == before);
  CHECK(second.transport->transport->perform(read(0xd2,48),5).status == Status::Ok);
}
}  // namespace
int main() {
  try {
    const std::vector<std::pair<const char *, std::function<void()>>> groups{
      {"native_default_denied_no_open", native_default_denied_no_open}, {"missing_identity_or_request_nonce_denied", missing_identity_or_request_nonce_denied},
      {"exact_probe_retains_same_fd", exact_probe_retains_same_fd}, {"f4_capability_requires_explicit_signature", f4_capability_requires_explicit_signature},
      {"f4_board_conflict_and_capability_mismatch", f4_board_conflict_and_capability_mismatch}, {"zero_uniform_identity_rejected", zero_uniform_identity_rejected},
      {"protocol_mismatch_rejected", protocol_mismatch_rejected}, {"short_each_identity_reply_rejected", short_each_identity_reply_rejected},
      {"wire_errors_no_retry_or_reopen", wire_errors_no_retry_or_reopen}, {"probe_deadline_and_wrap_fail_closed", probe_deadline_and_wrap_fail_closed},
      {"all_out_and_mutating_in_rejected", all_out_and_mutating_in_rejected}, {"exact_bulk_in_and_can_health_allowed", exact_bulk_in_and_can_health_allowed},
      {"cross_request_clock_rollback_rejected", cross_request_clock_rollback_rejected},
      {"unlock_failure_invalidates_final_probe_and_runtime_read", unlock_failure_invalidates_final_probe_and_runtime_read},
      {"retained_transport_cancel_and_dual_isolation", retained_transport_cancel_and_dual_isolation},
    };
    for (const auto &[name, test] : groups) { test(); std::cout << "PASS " << name << '\n'; }
    std::cout << "{\"suite\":\"p9_native_spi_probe\",\"passed\":" << groups.size() << ",\"failed\":0,\"skipped\":0,\"assertions\":" << assertions << ",\"real_device_io\":false}\n";
    return 0;
  } catch (const std::exception &error) { std::cerr << error.what() << '\n'; return 1; }
}
