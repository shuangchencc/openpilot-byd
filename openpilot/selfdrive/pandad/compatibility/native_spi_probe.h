#pragma once

#include "native_spi.h"
#include "native_usb.h"
#include "runtime_integration.h"

namespace openpilot::pandad::compatibility {
struct NativeSpiProbePolicy {
  // The observed configuration must explicitly be mode0/8-bit and <=50MHz,
  // matching the locked byte-protocol ceiling. This does not establish target
  // timing or compatibility at any selected speed; physical evidence is needed.
  NativeSpiSelector selector;
  // Explicit nonzero seed, with room for all identity reads. The caller must
  // supply freshness across physical sessions/process restarts: nonzero alone
  // does not prevent the MCU from recognizing an earlier id+CRC as a replay.
  std::uint32_t first_request_id = 0;
  std::uint32_t budget_ms = 100;
  std::vector<std::array<std::uint8_t, 128>> f4_application_signatures;
};
struct NativeSpiProbeResult {
  Status status = Status::Invalid;
  std::string reason = "UNBOUND_OR_DEVICE_IO_DENIED";
  NativeUsbObservation observation;
  // Filled only after the complete same-fd probe succeeds. These immutable
  // observations let handoff validate selector/seed and retain original age.
  NativeSpiSelector validated_selector;
  std::uint32_t first_request_id = 0;
  std::uint64_t started_us = 0, completed_us = 0;
  std::unique_ptr<OwnedTransport> transport;
  bool complete() const { return status == Status::Ok && transport != nullptr; }
};

// Read-only describes the firmware operations, not passive SPI observation:
// requesting identity actively transmits SPI framing and consumes reply state.
// Explicit permission is mandatory. Discovery and all later reads retain the
// same fd/backend; no reopen, fallback, reset, configuration or OUT is allowed.
// Returned facts are untrusted software observations, not MCU identity proof.
NativeSpiProbeResult discover_native_spi(std::shared_ptr<NativeSpiSyscalls>, Clock &,
                                         const NativeSpiProbePolicy &, bool allow_device_io = false);
NativeSpiProbeResult discover_native_spi(Clock &, const NativeSpiProbePolicy &, bool allow_device_io = false);
}  // namespace openpilot::pandad::compatibility
