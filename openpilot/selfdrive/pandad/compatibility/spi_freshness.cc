#include "spi_freshness.h"

#include <algorithm>
#include <cerrno>
#include <unistd.h>
#include <limits>
#include <set>

#include "firmware_capabilities.h"

namespace openpilot::pandad::compatibility {
namespace {
using Bytes = std::vector<std::uint8_t>;
constexpr std::array<std::uint8_t, 8> kProtocol{{0xf5, 0x12, 0xbc, 0xff, 0x76, 0xf2, 0xab, 0x75}};
template<std::size_t N> bool uniform(const std::array<std::uint8_t, N> &data, std::uint8_t byte) {
  return std::all_of(data.begin(), data.end(), [=](auto x) { return x == byte; });
}
Request read_request(std::uint8_t command, std::size_t length) {
  Request request; request.kind = Kind::ControlRead; request.request = command; request.read_length = length; return request;
}
bool read_only_allowed(const Request &request, bool f4) {
  if (!valid_request(request)) return false;
  if (request.kind == Kind::BulkRead) return request.endpoint == 0x81;
  if (request.kind != Kind::ControlRead || request.index != 0) return false;
  const std::size_t expected = request.request == 0xc3 ? 12 : request.request == 0xc1 ? 1 :
    request.request == 0xdd ? 8 : (request.request == 0xd3 || request.request == 0xd4 || request.request == 0xc2) ? 64 :
    request.request == 0xd5 && f4 ? 16 : request.request == 0xd2 ? 48 : request.request == 0xb2 ? 2 : 0;
  return expected && request.read_length == expected && (request.request == 0xc2 ? request.value < 3 : request.value == 0);
}
// flock belongs to an open-file description shared across fork. A child must
// neither use the inherited session nor unlock its parent's description during
// destruction. Closing only its inherited descriptor preserves the parent's
// lock; CLOEXEC handles exec separately. No child read/write/open is forwarded.
class ProcessBoundSyscalls final : public NativeSpiSyscalls {
public:
  explicit ProcessBoundSyscalls(std::shared_ptr<NativeSpiSyscalls> calls)
      : calls_(std::move(calls)), owner_(::getpid()) {}
  bool owner() const { return owner_ == ::getpid(); }
  NativeSpiCall open_device(const std::string &path, int flags) override { return owner() && calls_ ? calls_->open_device(path, flags) : denied(); }
  NativeSpiCall stat_device(int fd, NativeSpiNode &node) override { return owner() && calls_ ? calls_->stat_device(fd, node) : denied(); }
  NativeSpiCall read_mode(int fd, std::uint32_t &mode) override { return owner() && calls_ ? calls_->read_mode(fd, mode) : denied(); }
  NativeSpiCall read_bits(int fd, std::uint8_t &bits) override { return owner() && calls_ ? calls_->read_bits(fd, bits) : denied(); }
  NativeSpiCall read_speed(int fd, std::uint32_t &speed) override { return owner() && calls_ ? calls_->read_speed(fd, speed) : denied(); }
  NativeSpiCall lock_device(int fd, bool acquire) override {
    if (!owner()) return acquire ? denied() : NativeSpiCall{0, 0};
    return calls_ ? calls_->lock_device(fd, acquire) : denied();
  }
  NativeSpiCall message(int fd, const std::uint8_t *tx, std::uint8_t *rx, std::size_t bytes) override {
    return owner() && calls_ ? calls_->message(fd, tx, rx, bytes) : denied();
  }
  NativeSpiCall close_device(int fd) override { return calls_ ? calls_->close_device(fd) : denied(); }
private:
  static NativeSpiCall denied() { return {-1, EPERM}; }
  std::shared_ptr<NativeSpiSyscalls> calls_;
  pid_t owner_;
};
// Owns the native descriptor and the OS lock for its entire active lifetime.
// SpiBackend's per-request begin/end operates only the local transaction flag.
class SessionIo final : public SpiIo {
public:
  SessionIo(std::shared_ptr<NativeSpiSyscalls> calls, Clock &clock)
      : calls_(std::make_shared<ProcessBoundSyscalls>(std::move(calls))), native_(calls_, clock) {}
  Status activate(const NativeSpiSelector &selector) {
    auto status = native_.activate(selector, NativeSpiActivation::VerifiedProfile);
    if (status == Status::Ok) status = native_.begin(kBackendSliceMs);
    held_ = status == Status::Ok;
    return status;
  }
  Status begin(std::uint32_t timeout_ms) override {
    if (!calls_->owner() || !held_ || !native_.active()) return Status::Disconnected;
    if (!timeout_ms || timeout_ms > kBackendSliceMs) return Status::Invalid;
    if (transaction_) return Status::Busy;
    transaction_ = true; return Status::Ok;
  }
  void end() noexcept override { transaction_ = false; }
  void cancel() noexcept override { native_.cancel(); }
  Result exchange(const Bytes &tx, std::uint32_t timeout_ms) override {
    if (!held_ || !transaction_) return {Status::Invalid, 0, {}};
    return native_.exchange(tx, timeout_ms);
  }
  bool active() const { return calls_->owner() && held_ && native_.active(); }
  bool owning_process() const { return calls_->owner(); }
private:
  std::shared_ptr<ProcessBoundSyscalls> calls_;
  NativeSpiIo native_;
  bool held_ = false, transaction_ = false;
};
}  // namespace

class SpiFreshnessBackend::Impl {
public:
  Impl(std::shared_ptr<NativeSpiSyscalls> calls, Clock &clock) : io(std::move(calls), clock), clock(clock) {}
  SessionIo io;
  Clock &clock;
  std::unique_ptr<SpiBackend> backend;
  SpiFreshnessProof evidence;
  std::atomic<bool> cancelled{false};
  bool attempted = false, poisoned = false, f4 = false;
  std::uint64_t last_time = 0;
  bool time_valid() {
    const auto now = clock.now_us();
    if (now < last_time) { poisoned = true; return false; }
    last_time = now; return true;
  }
};
SpiFreshnessBackend::SpiFreshnessBackend(std::shared_ptr<NativeSpiSyscalls> calls, Clock &clock)
    : impl_(std::make_unique<Impl>(std::move(calls), clock)) {}
SpiFreshnessBackend::~SpiFreshnessBackend() = default;
void SpiFreshnessBackend::cancel() noexcept { impl_->cancelled.store(true); impl_->io.cancel(); }
void SpiFreshnessBackend::permit_f4_read() { impl_->f4 = true; }
SpiFreshnessProof SpiFreshnessBackend::proof() const {
  auto proof = impl_->evidence;
  proof.synchronized = proof.synchronized && !impl_->cancelled.load() && !impl_->poisoned && impl_->io.active();
  proof.same_fd_lifetime_lock = proof.synchronized;
  return proof;
}
Status SpiFreshnessBackend::activate(const NativeSpiSelector &selector,
    const std::array<std::uint8_t, 12> &expected_uid, std::uint32_t first_id, std::uint32_t budget_ms,
    bool cooperative_ownership_verified, bool allow_device_io) {
  auto &state = *impl_;
  if (state.cancelled.load()) return Status::Cancelled;
  if (!state.io.owning_process() || state.attempted || !cooperative_ownership_verified || !allow_device_io || !first_id ||
      first_id > std::numeric_limits<std::uint32_t>::max() - 2 || !budget_ms || budget_ms > kMaxTransportTimeoutMs ||
      !valid_native_spi_selector(selector) || *selector.expected_mode != 0 || *selector.expected_bits_per_word != 8 ||
      *selector.expected_max_speed_hz > 50000000 || uniform(expected_uid, 0) || uniform(expected_uid, 0xff)) return Status::Invalid;
  state.attempted = true;
  const auto start = state.clock.now_us(); state.last_time = start;
  auto remaining = [&]() -> std::uint32_t {
    if (!state.time_valid() || state.cancelled.load()) return 0;
    const auto elapsed = state.last_time - start;
    return elapsed >= static_cast<std::uint64_t>(budget_ms) * 1000 ? 0 :
      static_cast<std::uint32_t>(std::min<std::uint64_t>(kBackendSliceMs, (static_cast<std::uint64_t>(budget_ms) * 1000 - elapsed) / 1000));
  };
  auto fail = [&](Status status) { state.poisoned = true; state.io.cancel(); return status; };
  auto expired = [&] { return state.cancelled.load() ? Status::Cancelled : state.poisoned ? Status::IoError : Status::Timeout; };
  if (!remaining()) return fail(expired());
  auto status = state.io.activate(selector);
  if (status != Status::Ok) return fail(status);
  auto timeout = remaining(); if (!timeout) return fail(expired());
  status = state.io.begin(timeout); if (status != Status::Ok) return fail(status);
  // In the locked firmware a queued response is consumed at this CS edge. If
  // ready instead, all-ff is invalid: advertised tx_len=65535 cannot fit 4096.
  // This also drains a partial response left by a crashed predecessor. It never
  // invokes an endpoint or replays a possibly mutating preceding request.
  const auto drained = state.io.exchange(Bytes(kSpiBufferBytes, 0xff), timeout);
  state.io.end();
  if (drained.status != Status::Ok || !drained.actual_known || drained.delivery_uncertain ||
      drained.actual != kSpiBufferBytes || drained.data.size() != kSpiBufferBytes)
    return fail(drained.status == Status::Ok || drained.status == Status::Short ? Status::IoError : drained.status);
  state.backend = std::make_unique<SpiBackend>(state.io, state.clock, first_id);
  for (unsigned fence = 0; fence < 2; ++fence) {
    timeout = remaining(); if (!timeout) return fail(expired());
    const auto result = state.backend->transact(read_request(0xc3, expected_uid.size()), timeout);
    if (result.status != Status::Ok || !result.actual_known || result.delivery_uncertain ||
        result.actual != expected_uid.size() || result.data.size() != expected_uid.size() ||
        !std::equal(result.data.begin(), result.data.end(), expected_uid.begin()))
      return fail(result.status == Status::Ok || result.status == Status::Short ? Status::IoError : result.status);
  }
  if (!remaining()) return fail(expired());
  // The first complete response proves the most recent handled id is first_id.
  // Hence second_id differs, and cannot hit the one-entry response cache. Every
  // later id is unique in this still-locked session; SpiBackend refuses wrap.
  const auto nonce = allocate_native_lease_nonce();
  if (!nonce) return fail(Status::Poisoned);
  state.evidence = {true, true, false, first_id, first_id + 1, state.last_time, nonce, expected_uid};
  return Status::Ok;
}
Result SpiFreshnessBackend::transact(const Request &request, std::uint32_t timeout_ms) {
  auto &state = *impl_;
  if (!state.io.owning_process()) return {Status::Poisoned, 0, {}};
  if (state.cancelled.load()) return {Status::Cancelled, 0, {}};
  if (!read_only_allowed(request, state.f4) || !timeout_ms || timeout_ms > kBackendSliceMs) return {Status::Invalid, 0, {}};
  if (!state.evidence.synchronized || state.poisoned || !state.io.active()) return {Status::Poisoned, 0, {}};
  if (!state.time_valid()) return {Status::IoError, 0, {}};
  auto result = state.backend->transact(request, timeout_ms);
  if (!state.time_valid() || !state.io.active()) result.status = state.cancelled.load() ? Status::Cancelled : Status::IoError;
  if (result.status != Status::Ok && result.status != Status::Short) state.poisoned = true;
  return result;
}

FreshNativeSpiProbeResult discover_fresh_native_spi(std::shared_ptr<NativeSpiSyscalls> calls, Clock &clock,
    const NativeSpiProbePolicy &policy, const std::array<std::uint8_t, 12> &expected_uid,
    bool cooperative_ownership_verified, bool allow_device_io) {
  FreshNativeSpiProbeResult result;
  if (!calls || !cooperative_ownership_verified || !allow_device_io || policy.f4_application_signatures.size() > 2 ||
      !policy.first_request_id || policy.first_request_id > std::numeric_limits<std::uint32_t>::max() - 7 ||
      !policy.budget_ms || policy.budget_ms > kMaxTransportTimeoutMs) return result;
  std::set<std::array<std::uint8_t, 128>> signatures;
  for (const auto &signature : policy.f4_application_signatures)
    if (uniform(signature, 0) || uniform(signature, 0xff) || !signatures.insert(signature).second) return result;
  const auto start = clock.now_us(); auto previous = start;
  bool clock_fault = false;
  auto remaining = [&]() -> std::uint32_t {
    const auto now = clock.now_us(); if (now < previous) { clock_fault = true; return 0; } previous = now;
    const auto elapsed = now - start, budget = static_cast<std::uint64_t>(policy.budget_ms) * 1000;
    return elapsed >= budget ? 0 : static_cast<std::uint32_t>((budget - elapsed) / 1000);
  };
  auto failed = [](Status status, const std::string &reason) {
    FreshNativeSpiProbeResult failed; failed.probe.status = status; failed.probe.reason = reason; return failed;
  };
  auto backend = std::make_unique<SpiFreshnessBackend>(std::move(calls), clock);
  auto status = backend->activate(policy.selector, expected_uid, policy.first_request_id, policy.budget_ms,
                                  cooperative_ownership_verified, allow_device_io);
  if (status != Status::Ok) return failed(status, "SPI_FRESHNESS_FENCE_FAILED");
  auto *fresh = backend.get();
  auto owner = std::make_unique<OwnedTransport>(); owner->backend = std::move(backend);
  owner->transport = std::make_unique<Transport>(*owner->backend, clock);
  NativeUsbObservation observed; observed.lease_nonce = fresh->proof().lease_nonce;
  if (!observed.lease_nonce) return failed(Status::Poisoned, "LEASE_NONCE_EXHAUSTED");
  auto read = [&](std::uint8_t command, std::uint8_t *data, std::size_t length) {
    const auto timeout = remaining(); if (!timeout) return clock_fault ? Status::IoError : Status::Timeout;
    const auto reply = owner->transport->perform(read_request(command, length), timeout);
    if (!remaining()) return clock_fault ? Status::IoError : Status::Timeout;
    if (reply.status != Status::Ok || !reply.actual_known || reply.delivery_uncertain || reply.actual != length || reply.data.size() != length)
      return reply.status == Status::Ok || reply.status == Status::Short ? Status::Poisoned : reply.status;
    std::copy(reply.data.begin(), reply.data.end(), data); return Status::Ok;
  };
  status = read(0xc3, observed.uid.data(), observed.uid.size());
  if (status != Status::Ok || observed.uid != expected_uid) return failed(status == Status::Ok ? Status::Poisoned : status, "UID_PROBE_FAILED");
  status = read(0xc1, &observed.board_type, 1);
  if (status != Status::Ok || (observed.board_type != 6 && observed.board_type != 7)) return failed(status == Status::Ok ? Status::Poisoned : status, "BOARD_TYPE_PROBE_FAILED");
  status = read(0xdd, observed.protocol.data(), observed.protocol.size());
  if (status != Status::Ok || observed.protocol != kProtocol) return failed(status == Status::Ok ? Status::Poisoned : status, "PROTOCOL_PROBE_FAILED");
  status = read(0xd3, observed.application_signature.data(), 64);
  if (status != Status::Ok) return failed(status, "SIGNATURE_FIRST_PROBE_FAILED");
  status = read(0xd4, observed.application_signature.data() + 64, 64);
  if (status != Status::Ok || uniform(observed.application_signature, 0) || uniform(observed.application_signature, 0xff))
    return failed(status == Status::Ok ? Status::Poisoned : status, "SIGNATURE_SECOND_PROBE_FAILED");
  if (signatures.count(observed.application_signature)) {
    if (observed.board_type != 6) return failed(Status::Poisoned, "F4_SIGNATURE_BOARD_CONFLICT");
    fresh->permit_f4_read(); observed.f4_capabilities.resize(kF4CapabilityLength);
    status = read(kF4CapabilityRequest, observed.f4_capabilities.data(), observed.f4_capabilities.size());
    if (status != Status::Ok || !valid_f4_capabilities(observed.f4_capabilities))
      return failed(status == Status::Ok ? Status::Poisoned : status, "F4_CAPABILITY_PROBE_FAILED");
  }
  result.freshness = fresh->proof();
  if (!remaining() || !result.freshness.synchronized) return failed(clock_fault ? Status::IoError : Status::Timeout, "PROBE_CLOCK_OR_DEADLINE");
  result.probe.status = Status::Ok; result.probe.reason = "FENCED_READ_ONLY_SPI_OBSERVATION_RETAINED";
  observed.observed_us = previous;
  result.probe.observation = std::move(observed); result.probe.validated_selector = policy.selector;
  result.probe.first_request_id = policy.first_request_id; result.probe.started_us = start; result.probe.completed_us = previous;
  result.probe.transport = std::move(owner); return result;
}
FreshNativeSpiProbeResult discover_fresh_native_spi(Clock &clock, const NativeSpiProbePolicy &policy,
    const std::array<std::uint8_t, 12> &expected_uid, bool cooperative_ownership_verified, bool allow_device_io) {
  return discover_fresh_native_spi(make_linux_spi_syscalls(), clock, policy, expected_uid, cooperative_ownership_verified, allow_device_io);
}
}  // namespace openpilot::pandad::compatibility
