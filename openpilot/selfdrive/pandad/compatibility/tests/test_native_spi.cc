#include "../native_spi.h"

#include <algorithm>
#include <cerrno>
#include <fcntl.h>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <utility>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;
namespace {
std::size_t assertions = 0;
void check(bool condition, int line) { ++assertions; if (!condition) throw std::runtime_error("native SPI assertion at line " + std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
class FakeClock final : public Clock {
public:
  std::uint64_t now = 0;
  bool frozen = false;
  std::function<void()> on_wait;
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t us) override { if (!frozen) now += us; if (on_wait) on_wait(); }
};
class FakeCalls final : public NativeSpiSyscalls {
public:
  int fd = 17, flags = 0, opens = 0, closes = 0, locks = 0, unlocks = 0, messages = 0;
  int error_open = 0, error_stat = 0, error_mode = 0, error_bits = 0, error_speed = 0, error_lock = 0, error_unlock = 0, error_close = 0;
  NativeSpiNode node{true, 153, 1};
  std::uint32_t mode = 0, speed = 30000000;
  std::uint8_t bits = 8;
  std::string opened_path;
  bool lease = false;
  std::vector<std::string> events;
  std::vector<Bytes> requests;
  std::function<void()> on_lock;
  std::function<NativeSpiCall(const Bytes &, std::uint8_t *)> action;
  NativeSpiCall open_device(const std::string &path, int f) override {
    ++opens; opened_path = path; flags = f; events.push_back("open");
    return error_open ? NativeSpiCall{-1, error_open} : NativeSpiCall{fd, 0};
  }
  NativeSpiCall stat_device(int value, NativeSpiNode &out) override {
    CHECK(value == fd); events.push_back("stat"); out = node; return status(error_stat);
  }
  NativeSpiCall read_mode(int value, std::uint32_t &out) override {
    CHECK(value == fd && lease); events.push_back("mode"); out = mode; return status(error_mode);
  }
  NativeSpiCall read_bits(int value, std::uint8_t &out) override {
    CHECK(value == fd && lease); events.push_back("bits"); out = bits; return status(error_bits);
  }
  NativeSpiCall read_speed(int value, std::uint32_t &out) override {
    CHECK(value == fd && lease); events.push_back("speed"); out = speed; return status(error_speed);
  }
  NativeSpiCall lock_device(int value, bool acquire) override {
    CHECK(value == fd);
    if (acquire) {
      ++locks; events.push_back("lock"); if (on_lock) on_lock();
      if (error_lock) return {-1, error_lock};
      CHECK(!lease); lease = true; return {0, 0};
    }
    ++unlocks; events.push_back("unlock"); CHECK(lease); lease = false; return status(error_unlock);
  }
  NativeSpiCall message(int value, const std::uint8_t *tx, std::uint8_t *rx, std::size_t bytes) override {
    CHECK(value == fd && lease && bytes > 0 && bytes <= kSpiBufferBytes);
    ++messages; events.push_back("message"); requests.emplace_back(tx, tx + bytes);
    if (action) return action(requests.back(), rx);
    std::copy(tx, tx + bytes, rx); return {static_cast<int>(bytes), 0};
  }
  NativeSpiCall close_device(int value) override { CHECK(value == fd); ++closes; lease = false; events.push_back("close"); return status(error_close); }
private:
  static NativeSpiCall status(int error) { return error ? NativeSpiCall{-1, error} : NativeSpiCall{0, 0}; }
};
NativeSpiSelector selector() {
  NativeSpiSelector result;
  result.path = "/dev/spidev0.0"; result.identity_evidence_sha256 = std::string(64, 'a'); result.physical_identity_verified = true;
  result.expected_major = 153; result.expected_minor = 1; result.expected_mode = 0;
  result.expected_bits_per_word = 8; result.expected_max_speed_hz = 30000000;
  return result;
}
struct Fixture {
  FakeClock clock;
  std::shared_ptr<FakeCalls> calls = std::make_shared<FakeCalls>();
  NativeSpiIo io{calls, clock};
  void activate() { CHECK(io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Ok); }
};
void native_construction_denied() {
  FakeClock clock;
  auto native = make_linux_spi_syscalls();
  CHECK(native != nullptr);
  NativeSpiIo io(native, clock);
  CHECK(!io.active());
  CHECK(io.activate(selector()) == Status::Invalid);
  CHECK(io.begin(5) == Status::Disconnected);
  CHECK(io.exchange({1}, 5).status == Status::Disconnected);
  CHECK(!kNativeSpiKernelDeadlineProven);
}
void inert_and_default_deny() {
  Fixture f;
  CHECK(f.calls->events.empty());
  CHECK(f.io.activate(selector()) == Status::Invalid);
  CHECK(f.calls->events.empty());
  f.io.end(); f.io.cancel();
  CHECK(f.io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Cancelled);
  CHECK(f.calls->events.empty());
}
void exact_path_only() {
  for (const auto &path : {"", "/dev/spidev", "/dev/spidev0", "/dev/spidev.0", "/dev/spidev0.", "/dev/spidev00.0", "/dev/spidev0.00", "/dev/spidev-1.0", "/dev/spidev0.0/", "/dev/spidev0.0/../1", "/dev/spidev0.0\n", "/dev/spidev0.0.0", "/dev/spidev4294967296.0", "/tmp/spidev0.0"}) {
    Fixture f; auto candidate = selector(); candidate.path = path;
    CHECK(f.io.activate(candidate, NativeSpiActivation::VerifiedProfile) == Status::Invalid);
    CHECK(f.calls->opens == 0);
  }
  auto valid = selector(); valid.path = "/dev/spidev4294967295.4294967295";
  CHECK(valid_native_spi_selector(valid));
}
void incomplete_profile_no_open() {
  for (unsigned missing = 0; missing < 11; ++missing) {
    Fixture f; auto candidate = selector();
    if (missing == 0) candidate.physical_identity_verified = false;
    if (missing == 1) candidate.identity_evidence_sha256.clear();
    if (missing == 2) candidate.identity_evidence_sha256[1] = 'G';
    if (missing == 3) candidate.expected_major.reset();
    if (missing == 4) candidate.expected_minor.reset();
    if (missing == 5) candidate.expected_mode.reset();
    if (missing == 6) candidate.expected_bits_per_word.reset();
    if (missing == 7) candidate.expected_max_speed_hz.reset();
    if (missing == 8) candidate.expected_major = 0;
    if (missing == 9) candidate.expected_bits_per_word = 0;
    if (missing == 10) candidate.expected_max_speed_hz = 0;
    CHECK(f.io.activate(candidate, NativeSpiActivation::VerifiedProfile) == Status::Invalid);
    CHECK(f.calls->events.empty());
  }
}
void flags_and_existing_configuration() {
  Fixture f; f.activate();
  CHECK(f.calls->opened_path == selector().path);
  CHECK(f.calls->flags == (O_RDWR | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK));
  CHECK(f.calls->events == std::vector<std::string>({"open", "lock", "stat", "mode", "bits", "speed", "unlock"}));
  CHECK(f.calls->messages == 0 && f.io.active());
  CHECK(f.io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Invalid);
  CHECK(f.calls->opens == 1);
}
void node_identity_failure() {
  for (unsigned mismatch = 0; mismatch < 3; ++mismatch) {
    Fixture f;
    if (mismatch == 0) f.calls->node.character_device = false;
    if (mismatch == 1) ++f.calls->node.major;
    if (mismatch == 2) ++f.calls->node.minor;
    CHECK(f.io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Invalid);
    CHECK(f.calls->closes == 1 && f.calls->unlocks == 1 && f.calls->messages == 0);
    CHECK(!f.io.active());
  }
}
void exact_configuration_failure() {
  for (unsigned mismatch = 0; mismatch < 3; ++mismatch) {
    Fixture f;
    if (mismatch == 0) f.calls->mode = 1;
    if (mismatch == 1) f.calls->bits = 16;
    if (mismatch == 2) ++f.calls->speed;
    CHECK(f.io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Invalid);
    CHECK(f.calls->closes == 1 && f.calls->messages == 0);
  }
}
void syscall_failures_cleanup() {
  for (unsigned failure = 0; failure < 7; ++failure) {
    Fixture f;
    if (failure == 0) f.calls->error_open = ELOOP;
    if (failure == 1) f.calls->error_lock = EWOULDBLOCK;
    if (failure == 2) f.calls->error_stat = ENODEV;
    if (failure == 3) f.calls->error_mode = ENOTTY;
    if (failure == 4) f.calls->error_bits = ENOTTY;
    if (failure == 5) f.calls->error_speed = ENOTTY;
    if (failure == 6) f.calls->error_unlock = EIO;
    CHECK(f.io.activate(selector(), NativeSpiActivation::VerifiedProfile) != Status::Ok);
    CHECK(f.calls->opens == 1 && f.calls->closes == (failure == 0 ? 0 : 1));
    CHECK(f.calls->messages == 0 && !f.io.active());
    CHECK(f.io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Invalid);
  }
}
void lock_lifetime_and_single_message() {
  Fixture f; f.activate();
  CHECK(f.io.begin(5) == Status::Ok);
  CHECK(f.io.begin(5) == Status::Busy);
  const auto result = f.io.exchange({1, 2, 3}, 5);
  CHECK(result.status == Status::Ok && result.actual == 3 && result.data == Bytes({1, 2, 3}));
  CHECK(f.calls->locks == 2 && f.calls->unlocks == 1 && f.calls->messages == 1 && f.calls->lease);
  f.io.end(); f.io.end();
  CHECK(f.calls->unlocks == 2 && !f.calls->lease);
  CHECK(f.io.exchange({1}, 5).status == Status::Invalid);
}
void configuration_rechecked_each_request() {
  Fixture f; f.activate(); f.calls->speed += 1;
  CHECK(f.io.begin(5) == Status::Invalid);
  CHECK(!f.io.active() && f.calls->unlocks == 2 && f.calls->messages == 0);
  f.calls->speed -= 1;
  CHECK(f.io.begin(5) == Status::Disconnected);
}
void bounded_lock_timeout_and_wrap() {
  for (const auto start : {std::uint64_t{0}, std::numeric_limits<std::uint64_t>::max() - 500}) {
    Fixture f; f.activate(); f.clock.now = start; f.calls->error_lock = EWOULDBLOCK;
    CHECK(f.io.begin(1) == (start == 0 ? Status::Timeout : Status::IoError));
    if (start == 0) CHECK(f.clock.now - start == 1000 && f.calls->locks == 11);
    else CHECK(!f.io.active());
    CHECK(f.calls->messages == 0);
  }
  Fixture f; f.activate(); f.clock.frozen = true; f.calls->error_lock = EWOULDBLOCK;
  CHECK(f.io.begin(5) == Status::Busy);
  CHECK(f.calls->locks == 1 + static_cast<int>(kNativeSpiLockAttempts));
}
void lock_eintr_not_retried() {
  Fixture f; f.activate(); f.calls->error_lock = EINTR;
  CHECK(f.io.begin(5) == Status::IoError);
  CHECK(f.calls->locks == 2 && f.calls->messages == 0);
}
void clock_rollback_and_wrap_poison_provider() {
  Fixture locked; locked.activate(); locked.clock.now = 1000;
  locked.calls->on_lock = [&] { locked.clock.now = 500; };
  CHECK(locked.io.begin(5) == Status::IoError && !locked.io.active());
  CHECK(locked.calls->messages == 0 && !locked.calls->lease);
  for (const auto start : {std::uint64_t{1000}, std::numeric_limits<std::uint64_t>::max() - 500}) {
    Fixture f; f.activate(); CHECK(f.io.begin(5) == Status::Ok); f.clock.now = start;
    f.calls->action = [&](const Bytes &tx, std::uint8_t *rx) {
      f.clock.now = start == 1000 ? 500 : start + 1000;
      std::copy(tx.begin(), tx.end(), rx); return NativeSpiCall{static_cast<int>(tx.size()), 0};
    };
    const auto result = f.io.exchange({1,2},5);
    CHECK(result.status == Status::IoError && result.actual == 2 && result.actual_known && !f.io.active());
    CHECK(f.io.exchange({1},5).status == Status::Disconnected && f.calls->messages == 1);
  }
}
void cancelled_lock_no_device_calls() {
  Fixture f; f.activate(); f.calls->error_lock = EWOULDBLOCK;
  f.clock.on_wait = [&] { auto count = f.calls->events.size(); f.io.cancel(); CHECK(f.calls->events.size() == count); };
  CHECK(f.io.begin(5) == Status::Cancelled);
  CHECK(f.calls->locks == 2 && f.calls->messages == 0);
}
void cancellation_after_lock_releases() {
  Fixture f; f.activate(); f.calls->on_lock = [&] { f.io.cancel(); };
  CHECK(f.io.begin(5) == Status::Cancelled);
  CHECK(f.calls->unlocks == 2 && !f.calls->lease && f.calls->messages == 0);
}
void short_message_no_retry() {
  Fixture f; f.activate(); CHECK(f.io.begin(5) == Status::Ok);
  f.calls->action = [](const Bytes &, std::uint8_t *rx) { rx[0] = 7; return NativeSpiCall{1, 0}; };
  auto result = f.io.exchange({1, 2, 3}, 5);
  CHECK(result.status == Status::Short && result.actual == 1 && result.data == Bytes({7}));
  CHECK(result.actual_known && result.delivery_uncertain && f.calls->messages == 1);
  CHECK(f.io.exchange({1, 2, 3}, 5).status == Status::Disconnected && f.calls->messages == 1);
}
void uncertain_syscall_no_retry() {
  for (const auto error : {EINTR, ETIMEDOUT, ENODEV, ENXIO, EIO, EAGAIN}) {
    Fixture f; f.activate(); CHECK(f.io.begin(5) == Status::Ok);
    f.calls->action = [=](const Bytes &, std::uint8_t *) { return NativeSpiCall{-1, error}; };
    const auto result = f.io.exchange({1, 2, 3}, 5);
    CHECK(result.status != Status::Ok && result.status != Status::Short);
    CHECK(!result.actual_known && result.delivery_uncertain && result.actual == 0 && result.data.empty());
    CHECK(f.calls->messages == 1);
    CHECK(f.io.exchange({1, 2, 3}, 5).status == Status::Disconnected && f.calls->messages == 1);
  }
}
void invalid_ioctl_count_fails_closed() {
  Fixture f; f.activate(); CHECK(f.io.begin(5) == Status::Ok);
  f.calls->action = [](const Bytes &, std::uint8_t *) { return NativeSpiCall{4, 0}; };
  const auto result = f.io.exchange({1, 2, 3}, 5);
  CHECK(result.status == Status::IoError && !result.actual_known && result.delivery_uncertain && f.calls->messages == 1);
}
void late_ioctl_completion_is_not_deadline_proof() {
  Fixture f; f.activate(); CHECK(f.io.begin(5) == Status::Ok);
  f.calls->action = [&](const Bytes &tx, std::uint8_t *rx) { f.clock.now += 6000; std::copy(tx.begin(), tx.end(), rx); return NativeSpiCall{static_cast<int>(tx.size()), 0}; };
  const auto result = f.io.exchange({1, 2}, 5);
  CHECK(result.status == Status::Timeout && result.actual == 2 && result.actual_known && !result.delivery_uncertain);
  CHECK(f.calls->messages == 1 && !kNativeSpiKernelDeadlineProven);
  CHECK(!f.io.active());
}
void cancel_during_ioctl_preserves_completion() {
  Fixture f; f.activate(); CHECK(f.io.begin(5) == Status::Ok);
  f.calls->action = [&](const Bytes &tx, std::uint8_t *rx) { f.io.cancel(); std::copy(tx.begin(), tx.end(), rx); return NativeSpiCall{static_cast<int>(tx.size()), 0}; };
  const auto result = f.io.exchange({1, 2}, 5);
  CHECK(result.status == Status::Cancelled && result.actual == 2 && result.actual_known);
  CHECK(f.io.exchange({1}, 5).status == Status::Cancelled && f.calls->messages == 1);
}
void invalid_request_no_message() {
  Fixture f; f.activate();
  CHECK(f.io.begin(0) == Status::Invalid && f.io.begin(6) == Status::Invalid);
  CHECK(f.io.begin(5) == Status::Ok);
  CHECK(f.io.exchange({}, 5).status == Status::Invalid);
  CHECK(f.io.exchange(Bytes(4097), 5).status == Status::Invalid);
  CHECK(f.io.exchange({1}, 0).status == Status::Invalid);
  CHECK(f.io.exchange({1}, 6).status == Status::Invalid);
  CHECK(f.calls->messages == 0);
}
void destructor_unlock_close_once() {
  FakeClock clock; auto calls = std::make_shared<FakeCalls>();
  {
    NativeSpiIo io(calls, clock);
    CHECK(io.activate(selector(), NativeSpiActivation::VerifiedProfile) == Status::Ok);
    CHECK(io.begin(5) == Status::Ok);
    calls->error_close = EINTR;
  }
  CHECK(calls->unlocks == 2 && calls->closes == 1);
}
void isolated_devices_and_cancellation_seam() {
  Fixture a, b; a.activate(); b.activate();
  SpiBackend backend(a.io, a.clock, 10);
  backend.cancel();
  CHECK(!a.io.active() && b.io.active());
  CHECK(a.io.begin(5) == Status::Cancelled && b.io.begin(5) == Status::Ok);
  CHECK(b.io.exchange({1, 2}, 5).status == Status::Ok);
  CHECK(a.calls->messages == 0 && b.calls->messages == 1 && a.calls->closes == 0);
}
std::uint32_t le(const Bytes &data, std::size_t offset, unsigned count) { std::uint32_t n = 0; for (unsigned i = 0; i < count; ++i) n |= std::uint32_t(data[offset + i]) << (i * 8); return n; }
void append(Bytes &data, std::uint32_t n, unsigned count) { for (unsigned i = 0; i < count; ++i) data.push_back(static_cast<std::uint8_t>(n >> (i * 8))); }
std::uint32_t crc(const Bytes &data) { std::uint32_t n = 0xffffffffU; for (auto byte : data) { n ^= byte; for (unsigned i = 0; i < 8; ++i) n = (n >> 1) ^ ((n & 1) ? 0xedb88320U : 0); } return n ^ 0xffffffffU; }
void complete_backend_request_lease() {
  Fixture f; f.activate(); std::uint32_t request_id = 0;
  f.calls->action = [&](const Bytes &tx, std::uint8_t *rx) {
    Bytes reply(tx.size());
    if (tx[0] != 0xff) { request_id = le(tx, 1, 4); reply[0] = 0x3c; }
    else { reply = {0xc3}; append(reply, request_id, 4); append(reply, 0, 2); append(reply, crc(reply), 4); reply.resize(tx.size()); }
    std::copy(reply.begin(), reply.end(), rx); return NativeSpiCall{static_cast<int>(tx.size()), 0};
  };
  SpiBackend backend(f.io, f.clock, 77);
  Request request; request.kind = Kind::BulkWrite; request.endpoint = 3; request.payload = Bytes(9000, 42);
  const auto result = backend.transact(request, 5);
  CHECK(result.status == Status::Ok && result.actual == 9000 && f.calls->messages == 6);
  CHECK(f.calls->locks == 2 && f.calls->unlocks == 2 && !f.calls->lease);
  CHECK(le(f.calls->requests[0], 1, 4) == 77 && le(f.calls->requests[2], 1, 4) == 78 && le(f.calls->requests[4], 1, 4) == 79);
}
void transport_uncertain_never_replays() {
  Fixture f; f.activate();
  f.calls->action = [](const Bytes &, std::uint8_t *) { return NativeSpiCall{-1, EINTR}; };
  SpiBackend backend(f.io, f.clock, 99); Transport transport(backend, f.clock);
  Request request; request.kind = Kind::BulkRead; request.endpoint = 0x81; request.read_length = 16;
  const auto result = transport.perform(request, 100);
  CHECK(result.status == Status::IoError && !result.actual_known && f.calls->messages == 1);
  CHECK(transport.perform(request, 100).status == Status::Poisoned && f.calls->messages == 1);
}
}  // namespace
int main() {
  try {
    const std::vector<std::pair<const char *, std::function<void()>>> groups{
      {"native_construction_denied", native_construction_denied}, {"inert_and_default_deny", inert_and_default_deny},
      {"exact_path_only", exact_path_only}, {"incomplete_profile_no_open", incomplete_profile_no_open},
      {"flags_and_existing_configuration", flags_and_existing_configuration}, {"node_identity_failure", node_identity_failure},
      {"exact_configuration_failure", exact_configuration_failure}, {"syscall_failures_cleanup", syscall_failures_cleanup},
      {"lock_lifetime_and_single_message", lock_lifetime_and_single_message}, {"configuration_rechecked_each_request", configuration_rechecked_each_request},
      {"bounded_lock_timeout_and_wrap", bounded_lock_timeout_and_wrap}, {"lock_eintr_not_retried", lock_eintr_not_retried},
      {"clock_rollback_and_wrap_poison_provider", clock_rollback_and_wrap_poison_provider},
      {"cancelled_lock_no_device_calls", cancelled_lock_no_device_calls}, {"cancellation_after_lock_releases", cancellation_after_lock_releases},
      {"short_message_no_retry", short_message_no_retry}, {"uncertain_syscall_no_retry", uncertain_syscall_no_retry},
      {"invalid_ioctl_count_fails_closed", invalid_ioctl_count_fails_closed}, {"late_ioctl_completion_is_not_deadline_proof", late_ioctl_completion_is_not_deadline_proof},
      {"cancel_during_ioctl_preserves_completion", cancel_during_ioctl_preserves_completion}, {"invalid_request_no_message", invalid_request_no_message},
      {"destructor_unlock_close_once", destructor_unlock_close_once}, {"isolated_devices_and_cancellation_seam", isolated_devices_and_cancellation_seam},
      {"complete_backend_request_lease", complete_backend_request_lease}, {"transport_uncertain_never_replays", transport_uncertain_never_replays},
    };
    for (const auto &[name, test] : groups) { test(); std::cout << "PASS " << name << '\n'; }
    std::cout << "{\"suite\":\"p9_native_spi\",\"passed\":" << groups.size() << ",\"failed\":0,\"skipped\":0,\"assertions\":" << assertions << ",\"real_device_io\":false}\n";
    return 0;
  } catch (const std::exception &error) { std::cerr << error.what() << '\n'; return 1; }
}
