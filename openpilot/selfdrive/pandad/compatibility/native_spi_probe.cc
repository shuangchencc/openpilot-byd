#include "native_spi_probe.h"

#include <algorithm>
#include <limits>
#include <set>

#include "firmware_capabilities.h"

namespace openpilot::pandad::compatibility {
namespace {
constexpr std::array<std::uint8_t, 8> kProtocol{{0xf5, 0x12, 0xbc, 0xff, 0x76, 0xf2, 0xab, 0x75}};
template<std::size_t N> bool uniform(const std::array<std::uint8_t, N> &bytes, std::uint8_t value) {
  return std::all_of(bytes.begin(), bytes.end(), [=](auto byte) { return byte == value; });
}
class ReadOnlySpiBackend final : public Backend {
public:
  ReadOnlySpiBackend(NativeSpiIo &io, Clock &clock, std::uint32_t first_id) : io_(io), inner_(io, clock, first_id) {}
  void permit_f4_read() { f4_ = true; }
  void cancel() noexcept override { cancelled_.store(true); inner_.cancel(); }
  Result transact(const Request &request, std::uint32_t timeout_ms) override {
    if (cancelled_.load()) return {Status::Cancelled, 0, {}};
    if (!valid_request(request) || !timeout_ms || timeout_ms > kBackendSliceMs ||
        (request.kind != Kind::ControlRead && request.kind != Kind::BulkRead)) return {Status::Invalid, 0, {}};
    if (request.kind == Kind::BulkRead && request.endpoint != 0x81) return {Status::Invalid, 0, {}};
    if (request.kind == Kind::ControlRead) {
      const std::size_t expected = request.request == 0xc3 ? 12 : request.request == 0xc1 ? 1 :
        request.request == 0xdd ? 8 : (request.request == 0xd3 || request.request == 0xd4 || request.request == 0xc2) ? 64 :
        request.request == 0xd5 && f4_ ? 16 : request.request == 0xd2 ? 48 : request.request == 0xb2 ? 2 : 0;
      if (!expected || request.read_length != expected || request.index != 0 ||
          (request.request == 0xc2 ? request.value >= 3 : request.value != 0)) return {Status::Invalid, 0, {}};
    }
    auto result = inner_.transact(request, timeout_ms);
    // SpiBackend's lease destructor runs before this check. An unlock failure
    // closes/poisons the fd and must invalidate even a fully received reply.
    if (!io_.active() && (result.status == Status::Ok || result.status == Status::Short)) result.status = Status::IoError;
    return result;
  }
private:
  NativeSpiIo &io_;
  SpiBackend inner_;
  bool f4_ = false;
  std::atomic<bool> cancelled_{false};
};
}  // namespace

NativeSpiProbeResult discover_native_spi(std::shared_ptr<NativeSpiSyscalls> calls, Clock &clock,
                                         const NativeSpiProbePolicy &policy, bool allow_device_io) {
  NativeSpiProbeResult result;
  if (!allow_device_io || !calls || !valid_native_spi_selector(policy.selector) || !policy.first_request_id ||
      *policy.selector.expected_mode != 0 || *policy.selector.expected_bits_per_word != 8 ||
      *policy.selector.expected_max_speed_hz > 50000000 ||
      policy.first_request_id > std::numeric_limits<std::uint32_t>::max() - 5 ||
      !policy.budget_ms || policy.budget_ms > kMaxTransportTimeoutMs || policy.f4_application_signatures.size() > 2) return result;
  std::set<std::array<std::uint8_t, 128>> signatures;
  for (const auto &signature : policy.f4_application_signatures)
    if (uniform(signature, 0) || uniform(signature, 0xff) || !signatures.insert(signature).second) return result;
  const auto start = clock.now_us();
  auto previous = start;
  bool clock_fault = false;
  const auto budget = static_cast<std::uint64_t>(policy.budget_ms) * 1000;
  auto remaining_ms = [&]() -> std::uint32_t {
    const auto now = clock.now_us();
    if (now < previous) { clock_fault = true; return 0; }
    previous = now;
    const auto elapsed = now - start;
    return elapsed >= budget ? 0 : static_cast<std::uint32_t>((budget - elapsed) / 1000);
  };
  const auto expired_status = [&] { return clock_fault ? Status::IoError : Status::Timeout; };
  auto fail = [&](Status status, const std::string &reason) {
    NativeSpiProbeResult failed;
    failed.status = status; failed.reason = reason;
    return failed;
  };
  if (!remaining_ms()) return fail(expired_status(), "PROBE_CLOCK_OR_DEADLINE");
  auto io = std::make_unique<NativeSpiIo>(std::move(calls), clock);
  auto status = io->activate(policy.selector, NativeSpiActivation::VerifiedProfile);
  if (status != Status::Ok) return fail(status, "SPI_ACTIVATION_FAILED");
  if (!remaining_ms()) return fail(expired_status(), "PROBE_CLOCK_OR_DEADLINE");
  auto owner = std::make_unique<OwnedTransport>();
  auto backend = std::make_unique<ReadOnlySpiBackend>(*io, clock, policy.first_request_id);
  auto *read_only = backend.get();
  owner->spi_io = std::move(io);
  owner->backend = std::move(backend);
  owner->transport = std::make_unique<Transport>(*owner->backend, clock);
  NativeUsbObservation observed;
  observed.lease_nonce = allocate_native_lease_nonce();
  if (!observed.lease_nonce) return fail(Status::Poisoned, "LEASE_NONCE_EXHAUSTED");
  auto read = [&](std::uint8_t request, std::uint8_t *data, std::size_t length) {
    const auto remaining = remaining_ms();
    if (!remaining) return expired_status();
    Request read_request; read_request.kind = Kind::ControlRead; read_request.request = request; read_request.read_length = length;
    const auto reply = owner->transport->perform(read_request, remaining);
    if (!remaining_ms()) return expired_status();
    if (reply.status != Status::Ok) return reply.status == Status::Short ? Status::Poisoned : reply.status;
    if (!reply.actual_known || reply.delivery_uncertain || reply.actual != length || reply.data.size() != length) return Status::Poisoned;
    std::copy(reply.data.begin(), reply.data.end(), data);
    return Status::Ok;
  };
  status = read(0xc3, observed.uid.data(), observed.uid.size());
  if (status != Status::Ok) return fail(status, "UID_PROBE_FAILED");
  if (uniform(observed.uid, 0) || uniform(observed.uid, 0xff)) return fail(Status::Poisoned, "INVALID_UID_REPLY");
  status = read(0xc1, &observed.board_type, 1);
  if (status != Status::Ok) return fail(status, "BOARD_TYPE_PROBE_FAILED");
  if (observed.board_type != 6 && observed.board_type != 7) return fail(Status::Poisoned, "UNKNOWN_BOARD_TYPE");
  status = read(0xdd, observed.protocol.data(), observed.protocol.size());
  if (status != Status::Ok) return fail(status, "PROTOCOL_PROBE_FAILED");
  if (observed.protocol != kProtocol) return fail(Status::Poisoned, "PROTOCOL_MISMATCH");
  status = read(0xd3, observed.application_signature.data(), 64);
  if (status != Status::Ok) return fail(status, "SIGNATURE_FIRST_PROBE_FAILED");
  status = read(0xd4, observed.application_signature.data() + 64, 64);
  if (status != Status::Ok) return fail(status, "SIGNATURE_SECOND_PROBE_FAILED");
  if (uniform(observed.application_signature, 0) || uniform(observed.application_signature, 0xff)) return fail(Status::Poisoned, "INVALID_SIGNATURE_REPLY");
  if (signatures.count(observed.application_signature)) {
    if (observed.board_type != 6) return fail(Status::Poisoned, "F4_SIGNATURE_BOARD_CONFLICT");
    read_only->permit_f4_read();
    observed.f4_capabilities.resize(kF4CapabilityLength);
    status = read(kF4CapabilityRequest, observed.f4_capabilities.data(), observed.f4_capabilities.size());
    if (status != Status::Ok) return fail(status, "F4_CAPABILITY_PROBE_FAILED");
    if (!valid_f4_capabilities(observed.f4_capabilities)) return fail(Status::Poisoned, "F4_CAPABILITY_MISMATCH");
  }
  if (!remaining_ms()) return fail(expired_status(), "PROBE_CLOCK_OR_DEADLINE");
  result.status = Status::Ok;
  result.reason = "READ_ONLY_SPI_OBSERVATION_RETAINED";
  result.observation = std::move(observed);
  result.validated_selector = policy.selector;
  result.first_request_id = policy.first_request_id;
  result.started_us = start;
  result.completed_us = previous;
  result.transport = std::move(owner);
  return result;
}

NativeSpiProbeResult discover_native_spi(Clock &clock, const NativeSpiProbePolicy &policy, bool allow_device_io) {
  return discover_native_spi(make_linux_spi_syscalls(), clock, policy, allow_device_io);
}
}  // namespace openpilot::pandad::compatibility
