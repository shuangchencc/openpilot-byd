#include "native_usb.h"

#include <algorithm>
#include <atomic>
#include <limits>
#include <mutex>
#include <set>
#include <utility>

#include "firmware_capabilities.h"
#include "control_session.h"

namespace openpilot::pandad::compatibility {
namespace {
constexpr std::array<std::uint8_t, 8> kProtocol{{0xf5, 0x12, 0xbc, 0xff, 0x76, 0xf2, 0xab, 0x75}};

Status usb_error(int value) {
  switch (value) {
    case kUsbTimeout: return Status::Timeout;
    case kUsbNoDevice: return Status::Disconnected;
    case kUsbPipe: return Status::Stall;
    case kUsbBusy: return Status::Busy;
    case kUsbInterrupted: return Status::Cancelled;
    case kUsbInvalid: return Status::Invalid;
    default: return Status::IoError;
  }
}
struct Budget {
  Clock &clock;
  std::uint64_t start, previous, limit;
  explicit Budget(Clock &c, std::uint32_t ms) : clock(c), start(c.now_us()), previous(start), limit(ms * 1000ULL) {}
  std::uint32_t slice() {
    const auto now = clock.now_us();
    if (now < previous || now < start || now - start >= limit) return 0;
    previous = now;
    return static_cast<std::uint32_t>(std::min<std::uint64_t>(kBackendSliceMs, (limit - (now - start)) / 1000));
  }
};
struct Context {
  explicit Context(std::unique_ptr<LibusbApi> a) : api(std::move(a)) {}
  ~Context() { if (initialized) api->shutdown(); }
  std::unique_ptr<LibusbApi> api;
  std::mutex mutex;
  bool initialized = false;
};
struct Lease {
  std::shared_ptr<Context> context;
  UsbHandle handle = 0;
  std::uint64_t nonce = 0;
  UsbDeviceInfo descriptor;
  NativeUsbObservation observed;
  bool claimed = false;
  ~Lease() {
    if (!handle) return;
    std::lock_guard<std::mutex> lock(context->mutex);
    if (claimed) context->api->release(handle, 0);
    context->api->close(handle);
  }
};
bool normal(const UsbDeviceInfo &d) {
  return (d.vendor == 0xbbaa || d.vendor == 0x3801) && d.product == 0xddcc;
}
bool valid_descriptor(const UsbDeviceInfo &d) {
  if (!d.id || !d.active_configuration_known || !d.serial_index ||
      (d.bcd_device != 0x600 && d.bcd_device != 0x700) || d.alternates.empty() ||
      d.alternates.size() > kUsbMaxAlternates) return false;
  unsigned found = 0;
  std::set<std::pair<unsigned, unsigned>> alternatives;
  for (const auto &alt : d.alternates) {
    if (!alternatives.insert({alt.interface_number, alt.alternate_setting}).second ||
        alt.endpoints.size() > kUsbMaxEndpoints) return false;
    std::set<unsigned> endpoints;
    bool rx = false, tx = false;
    for (const auto &ep : alt.endpoints) {
      if (!endpoints.insert(ep.address).second) return false;
      if (alt.interface_number == 0 && alt.alternate_setting == 0 &&
          (ep.address == 0x81 || ep.address == 0x03)) {
        if ((ep.attributes & 3) != 2 || ep.max_packet_size != 64) return false;
        rx |= ep.address == 0x81; tx |= ep.address == 0x03;
      }
    }
    if (alt.interface_number == 0 && alt.alternate_setting == 0) {
      if (!rx || !tx) return false;
      ++found;
    }
  }
  return found == 1;
}
bool valid_serial(const std::string &s) {
  return !s.empty() && s.size() <= 128 && std::all_of(s.begin(), s.end(), [](unsigned char c) { return c > 32 && c < 127; });
}
template<std::size_t N> bool uniform(const std::array<std::uint8_t, N> &bytes, std::uint8_t value) {
  return std::all_of(bytes.begin(), bytes.end(), [=](auto b) { return b == value; });
}
Status probe(Lease &lease, const NativeUsbProbePolicy &policy, Budget &budget, NativeUsbObservation &observed) {
  observed.lease_nonce = lease.nonce;
  auto &api = *lease.context->api;
  auto timeout = budget.slice();
  if (!timeout) return Status::Timeout;
  int result = api.serial(lease.handle, lease.descriptor.serial_index, observed.serial, timeout);
  if (!budget.slice()) return Status::Timeout;
  if (result != 0) return usb_error(result);
  if (!valid_serial(observed.serial)) return Status::Poisoned;
  auto read = [&](std::uint8_t request, std::uint8_t *bytes, std::size_t length) {
    const auto slice = budget.slice();
    if (!slice) return Status::Timeout;
    // Immutable, exhaustive read-only discovery allowlist; no bulk, OUT,
    // configuration, reset, clear-halt or bootstub traffic can reach this path.
    if (request != 0xc3 && request != 0xc1 && request != 0xdd && request != 0xd3 && request != 0xd4 && request != 0xd5)
      return Status::Invalid;
    const int count = api.control(lease.handle, 0xc0, request, 0, 0, bytes, static_cast<std::uint16_t>(length), slice);
    if (!budget.slice()) return Status::Timeout;
    if (count < 0) return usb_error(count);
    return count == static_cast<int>(length) ? Status::Ok : Status::Poisoned;
  };
  Status status = read(0xc3, observed.uid.data(), observed.uid.size());
  if (status != Status::Ok) return status;
  if (uniform(observed.uid, 0) || uniform(observed.uid, 0xff)) return Status::Poisoned;
  status = read(0xc1, &observed.board_type, 1);
  if (status != Status::Ok) return status;
  if ((observed.board_type != 6 && observed.board_type != 7) ||
      lease.descriptor.bcd_device != static_cast<std::uint16_t>(observed.board_type << 8)) return Status::Poisoned;
  status = read(0xdd, observed.protocol.data(), observed.protocol.size());
  if (status != Status::Ok) return status;
  if (observed.protocol != kProtocol) return Status::Poisoned;
  status = read(0xd3, observed.application_signature.data(), 64);
  if (status != Status::Ok) return status;
  status = read(0xd4, observed.application_signature.data() + 64, 64);
  if (status != Status::Ok) return status;
  if (uniform(observed.application_signature, 0) || uniform(observed.application_signature, 0xff)) return Status::Poisoned;
  if (std::find(policy.f4_application_signatures.begin(), policy.f4_application_signatures.end(), observed.application_signature) !=
      policy.f4_application_signatures.end()) {
    if (observed.board_type != 6) return Status::Poisoned;
    observed.f4_capabilities.resize(kF4CapabilityLength);
    status = read(kF4CapabilityRequest, observed.f4_capabilities.data(), observed.f4_capabilities.size());
    if (status != Status::Ok) return status;
    if (!valid_f4_capabilities(observed.f4_capabilities)) return Status::Poisoned;
  }
  if (!budget.slice()) return Status::Timeout;
  observed.observed_us = budget.previous;
  return Status::Ok;
}

class NativeUsbBackend final : public Backend {
public:
  NativeUsbBackend(std::unique_ptr<Lease> lease, NativeUsbActivation grant) : lease_(std::move(lease)), grant_(grant) {}
  void cancel() noexcept override { cancelled_.store(true); }
  Result transact(const Request &request, std::uint32_t timeout_ms) override {
    if (cancelled_.load()) return {Status::Cancelled, 0, {}};
    if (!valid_request(request) || !timeout_ms || timeout_ms > kBackendSliceMs) return {Status::Invalid, 0, {}};
    const bool read = is_read(request.kind);
    if ((request.kind == Kind::BulkRead && request.endpoint != 0x81) ||
        (request.kind == Kind::BulkWrite && (request.endpoint != 0x03 || !grant_.allow_can_tx))) return {Status::Invalid, 0, {}};
    if (request.kind == Kind::ControlWrite) {
      // Candidate writes remain noOutput/disengaged only. Permission is not
      // authority to select a vehicle safety model, alternative experience,
      // engaged heartbeat, arbitrary parameters or payload bytes.
      const bool allowed_value =
        (request.request == 0xdc && request.value == kNoOutputSafetyModel) ||
        ((request.request == 0xdf || request.request == 0xf3) && request.value == 0) ||
        (request.request == 0xb1 && request.value <= 100) ||
        (request.request == 0xb0 && request.value <= 50);
      if (!grant_.allow_control_writes || !allowed_value || request.index != 0 || !request.payload.empty())
        return {Status::Invalid, 0, {}};
    }
    // These USB vendor IN codes have mutating firmware handlers despite IN
    // direction. Never permit arbitrary controlRead to bypass write policy.
    if (request.kind == Kind::ControlRead) {
      const std::size_t expected = request.request == 0xc3 ? 12 : request.request == 0xc1 ? 1 :
        request.request == 0xdd ? 8 : (request.request == 0xd3 || request.request == 0xd4 || request.request == 0xc2) ? 64 :
        request.request == 0xd5 ? 16 : request.request == 0xd2 ? 48 : request.request == 0xb2 ? 2 : 0;
      if (!expected || request.read_length != expected || request.index != 0 ||
          (request.request == 0xc2 ? request.value >= 3 : request.value != 0)) return {Status::Invalid, 0, {}};
    }
    std::unique_lock<std::mutex> lock(lease_->context->mutex, std::try_to_lock);
    if (!lock.owns_lock()) return {Status::Busy, 0, {}};
    if (cancelled_.load()) return {Status::Cancelled, 0, {}};
    if (poisoned_) return {Status::Poisoned, 0, {}};
    const std::size_t length = read ? request.read_length : request.payload.size();
    std::vector<std::uint8_t> data = read ? std::vector<std::uint8_t>(length) : request.payload;
    int actual = 0, error = 0;
    if (request.kind == Kind::ControlRead || request.kind == Kind::ControlWrite) {
      const int count = lease_->context->api->control(lease_->handle, read ? 0xc0 : 0x40,
        static_cast<std::uint8_t>(request.request), static_cast<std::uint16_t>(request.value),
        static_cast<std::uint16_t>(request.index), data.data(), static_cast<std::uint16_t>(length), timeout_ms);
      if (count < 0) {
        poisoned_ = true;  // USB control errors have no confirmed partial count.
        return {usb_error(count), 0, {}, false, !read};
      }
      else actual = count;
    } else {
      error = lease_->context->api->bulk(lease_->handle, static_cast<std::uint8_t>(request.endpoint),
        data.data(), static_cast<int>(length), actual, timeout_ms);
    }
    if (error == kUsbOverflow || actual < 0 || static_cast<std::size_t>(actual) > length) {
      poisoned_ = true;
      return {Status::Poisoned, 0, {}, false, !read};
    }
    Status status = error == 0 ? (static_cast<std::size_t>(actual) == length ? Status::Ok : Status::Short) : usb_error(error);
    if (error == kUsbNoDevice || error == kUsbPipe || error == kUsbIo) poisoned_ = true;
    if (read) data.resize(actual); else data.clear();
    return {status, static_cast<std::size_t>(actual), std::move(data), true, !read && status != Status::Ok};
  }
private:
  std::unique_ptr<Lease> lease_;
  NativeUsbActivation grant_;
  std::atomic<bool> cancelled_{false};
  bool poisoned_ = false;
};
}  // namespace

bool operator==(const NativeUsbObservation &a, const NativeUsbObservation &b) {
  return a.lease_nonce == b.lease_nonce && a.serial == b.serial && a.uid == b.uid && a.board_type == b.board_type && a.protocol == b.protocol &&
    a.application_signature == b.application_signature && a.f4_capabilities == b.f4_capabilities;
}
struct NativeUsbInventory::Impl {
  std::vector<std::unique_ptr<Lease>> leases;
  std::vector<NativeUsbObservation> observations;
  NativeUsbProbePolicy policy;
  Status status = Status::Invalid;
  std::string reason = "UNBOUND_OR_PERMISSION_DENIED";
  bool complete = false;
  void fail(Status value, const char *why) { complete = false; status = value; reason = why; leases.clear(); }
};
NativeUsbInventory::NativeUsbInventory() : impl_(std::make_unique<Impl>()) {}
NativeUsbInventory::~NativeUsbInventory() = default;
NativeUsbInventory::NativeUsbInventory(NativeUsbInventory &&) noexcept = default;
NativeUsbInventory &NativeUsbInventory::operator=(NativeUsbInventory &&) noexcept = default;
bool NativeUsbInventory::complete() const { return impl_ && impl_->complete; }
Status NativeUsbInventory::status() const { return impl_ ? impl_->status : Status::Invalid; }
std::string NativeUsbInventory::reason() const { return impl_ ? impl_->reason : "MOVED_INVENTORY"; }
std::vector<NativeUsbObservation> NativeUsbInventory::observations() const { return impl_ ? impl_->observations : std::vector<NativeUsbObservation>{}; }

NativeUsbInventory discover_native_usb(std::unique_ptr<LibusbApi> api, Clock &clock,
                                      const NativeUsbProbePolicy &policy, bool allow_device_io) {
  NativeUsbInventory result;
  auto &out = *result.impl_;
  if (!allow_device_io || !api || policy.expected_count == 0 || policy.expected_count > 2 ||
      policy.budget_ms == 0 || policy.budget_ms > 100 || policy.f4_application_signatures.size() > 2) return result;
  std::set<std::array<std::uint8_t, 128>> signatures;
  for (const auto &s : policy.f4_application_signatures)
    if (uniform(s, 0) || uniform(s, 0xff) || !signatures.insert(s).second) return result;
  out.policy = policy;
  auto context = std::make_shared<Context>(std::move(api));
  Budget budget(clock, policy.budget_ms);
  if (!budget.slice()) { out.fail(Status::Timeout, "DISCOVERY_DEADLINE"); return result; }
  int error = context->api->initialize();
  context->initialized = error == 0;
  if (error != 0) { out.fail(usb_error(error), "INITIALIZE_FAILED"); return result; }
  if (!budget.slice()) { out.fail(Status::Timeout, "DISCOVERY_DEADLINE"); return result; }
  std::vector<UsbDeviceInfo> devices;
  error = context->api->enumerate(devices);
  if (!budget.slice()) { out.fail(Status::Timeout, "DISCOVERY_DEADLINE"); return result; }
  if (error != 0) { out.fail(usb_error(error), "INCOMPLETE_INVENTORY"); return result; }
  if (devices.size() > kUsbMaxInventoryDevices) { out.fail(Status::Poisoned, "INVENTORY_OVERFLOW"); return result; }
  const auto count = std::count_if(devices.begin(), devices.end(), normal);
  if (static_cast<std::size_t>(count) != policy.expected_count) { out.fail(Status::Poisoned, "UNEXPECTED_USB_INVENTORY"); return result; }
  std::set<UsbDeviceId> ids;
  std::set<std::string> serials;
  std::set<std::array<std::uint8_t, 12>> uids;
  for (const auto &d : devices) {
    if (!normal(d)) continue;
    if (!ids.insert(d.id).second || !valid_descriptor(d)) { out.fail(Status::Poisoned, "INVALID_DESCRIPTOR"); return result; }
    if (!budget.slice()) { out.fail(Status::Timeout, "DISCOVERY_DEADLINE"); return result; }
    auto lease = std::make_unique<Lease>(); lease->context = context; lease->descriptor = d;
    error = context->api->open(d.id, lease->handle);
    if (!budget.slice()) { out.fail(Status::Timeout, "DISCOVERY_DEADLINE"); return result; }
    if (error != 0 || !lease->handle) { out.fail(error ? usb_error(error) : Status::IoError, "INACCESSIBLE_CANDIDATE"); return result; }
    lease->nonce = allocate_native_lease_nonce();
    if (!lease->nonce) { out.fail(Status::Poisoned, "LEASE_NONCE_EXHAUSTED"); return result; }
    NativeUsbObservation observation;
    const auto status = probe(*lease, policy, budget, observation);
    if (status != Status::Ok) { out.fail(status, "IDENTITY_PROBE_FAILED"); return result; }
    if (!serials.insert(observation.serial).second || !uids.insert(observation.uid).second) {
      out.fail(Status::Poisoned, "DUPLICATE_SOFTWARE_IDENTITY"); return result;
    }
    lease->observed = observation;
    out.observations.push_back(observation);
    out.leases.push_back(std::move(lease));
  }
  out.complete = true; out.status = Status::Ok; out.reason = "OBSERVED_NOT_TRUSTED";
  return result;
}
NativeUsbInventory discover_native_usb(Clock &clock, const NativeUsbProbePolicy &policy, bool allow_device_io) {
  return discover_native_usb(make_native_libusb_api(), clock, policy, allow_device_io);
}
std::unique_ptr<Backend> NativeUsbInventory::activate(std::size_t index, const NativeUsbObservation &expected,
                                                   const NativeUsbActivation &grant, Clock &clock,
                                                   std::uint64_t *activation_observed_us) {
  if (activation_observed_us) *activation_observed_us = 0;
  if (!impl_ || !impl_->complete || index >= impl_->leases.size() || !impl_->leases[index]) return nullptr;
  if (!grant.allow_device_io || !grant.profile_validated || !grant.bulk_alt0_verified ||
      grant.lease_nonce == 0 || grant.lease_nonce != impl_->observations[index].lease_nonce ||
      !(expected == impl_->observations[index])) {
    impl_->fail(Status::Invalid, "ACTIVATION_EVIDENCE_MISSING"); return nullptr;
  }
  auto lease = std::move(impl_->leases[index]);
  Budget budget(clock, impl_->policy.budget_ms);
  if (!budget.slice()) { impl_->fail(Status::Timeout, "ACTIVATION_DEADLINE"); return nullptr; }
  int error = lease->context->api->claim(lease->handle, 0);
  lease->claimed = error == 0;
  if (!budget.slice()) { impl_->fail(Status::Timeout, "ACTIVATION_DEADLINE"); return nullptr; }
  if (error != 0) { impl_->fail(usb_error(error), "CLAIM_FAILED"); return nullptr; }
  NativeUsbObservation current;
  const auto status = probe(*lease, impl_->policy, budget, current);
  if (status != Status::Ok || !(current == expected)) {
    impl_->fail(status == Status::Ok ? Status::Poisoned : status, "ACTIVATION_IDENTITY_CHANGED"); return nullptr;
  }
  lease->observed = current;
  auto backend = std::make_unique<NativeUsbBackend>(std::move(lease), grant);
  if (activation_observed_us) *activation_observed_us = current.observed_us;
  return backend;
}
}  // namespace openpilot::pandad::compatibility
