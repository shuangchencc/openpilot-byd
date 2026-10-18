#pragma once

#include <array>
#include <atomic>
#include <memory>
#include <string>
#include <vector>

#include "libusb_api.h"
#include "transport.h"

namespace openpilot::pandad::compatibility {

// One process-local namespace shared by USB and SPI retained leases. Exhaustion
// closes discovery; nonce zero never describes a session. This is freshness,
// not a cryptographic device identity or permission token.
inline std::uint64_t allocate_native_lease_nonce() {
  static std::atomic<std::uint64_t> next{1};
  auto value = next.load();
  while (value != UINT64_MAX) {
    if (next.compare_exchange_weak(value, value + 1)) return value;
  }
  return 0;
}

// Untrusted software observations. A UID or signature reply is not physical
// MCU evidence or proof that the device authenticates the application image.
struct NativeUsbObservation {
  // Process-local monotonically unique handle lease, not physical identity.
  std::uint64_t lease_nonce = 0;
  // Actual completion time from the probe clock. It is observation freshness,
  // not part of software identity; persisted profiles must leave it zero.
  std::uint64_t observed_us = 0;
  std::string serial;
  std::array<std::uint8_t, 12> uid{};
  std::uint8_t board_type = 0;
  std::array<std::uint8_t, 8> protocol{};
  std::array<std::uint8_t, 128> application_signature{};
  std::vector<std::uint8_t> f4_capabilities;
};
bool operator==(const NativeUsbObservation &, const NativeUsbObservation &);

struct NativeUsbProbePolicy {
  // Exact expected normal-firmware USB inventory. Zero is unbound/denied.
  std::size_t expected_count = 0;
  std::uint32_t budget_ms = 100;
  // Request 0xd5 only for explicitly recognized candidate application bytes;
  // never infer F4 from bcdDevice, board type, USB serial or outer enclosure.
  std::vector<std::array<std::uint8_t, 128>> f4_application_signatures;
};
struct NativeUsbActivation {
  // Alt0 verification must name this retained session, never only a profile.
  std::uint64_t lease_nonce = 0;
  bool allow_device_io = false;
  bool profile_validated = false;
  // Separate session evidence. The pinned firmware lacks GET_INTERFACE;
  // seeing an alt0 descriptor therefore cannot set this flag.
  bool bulk_alt0_verified = false;
  bool allow_control_writes = false;
  bool allow_can_tx = false;
};

class NativeUsbInventory {
public:
  NativeUsbInventory();
  ~NativeUsbInventory();
  NativeUsbInventory(NativeUsbInventory &&) noexcept;
  NativeUsbInventory &operator=(NativeUsbInventory &&) noexcept;
  NativeUsbInventory(const NativeUsbInventory &) = delete;
  NativeUsbInventory &operator=(const NativeUsbInventory &) = delete;
  bool complete() const;
  Status status() const;
  std::string reason() const;
  std::vector<NativeUsbObservation> observations() const;

  // The owner serializes inventory activation/destruction and does not run
  // backend transfers until the complete group has finished activation.
  // Consumes one retained handle, claims interface0, then re-probes every
  // identity byte on that SAME handle. No enumerate/reopen/fallback is allowed.
  // Failure retires that lease permanently. Backend cancellation is local.
  // If requested, writes the actual re-probe completion time; failure always
  // writes zero. Callers must not substitute a later handoff timestamp.
  std::unique_ptr<Backend> activate(std::size_t index, const NativeUsbObservation &expected,
                                    const NativeUsbActivation &, Clock &clock,
                                    std::uint64_t *activation_observed_us = nullptr);
private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  friend NativeUsbInventory discover_native_usb(std::unique_ptr<LibusbApi>, Clock &,
                                                const NativeUsbProbePolicy &, bool);
};

// Construction and default calls are inert. The caller must explicitly grant
// read-only discovery. OS initialize/enumerate/open/claim latency is not
// preemptible: bounds cover inventory sizes and transfer deadlines, with clock
// checks before/after every API call. Budget failure never yields a usable lease.
NativeUsbInventory discover_native_usb(std::unique_ptr<LibusbApi> api, Clock &clock,
                                      const NativeUsbProbePolicy &policy,
                                      bool allow_device_io = false);
NativeUsbInventory discover_native_usb(Clock &clock, const NativeUsbProbePolicy &policy,
                                      bool allow_device_io = false);

}  // namespace openpilot::pandad::compatibility
