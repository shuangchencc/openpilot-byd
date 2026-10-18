#pragma once

#include <functional>
#include "native_usb.h"
#include "native_spi_probe.h"

namespace openpilot::pandad::compatibility {
struct NativeObservedBoard {
  NativeUsbObservation raw;
  std::string transport;
  bool usb_alt0_verified = false;
  std::uint32_t spi_first_request_id = 0;
  NativeSpiSelector spi_selector;
};
struct NativeBoardBinding {
  std::uint32_t slot = 0;
  // Expected raw identity must have nonce zero: leases belong to discovery,
  // never to persisted profiles. SPI serial is empty; USB serial is explicit.
  NativeUsbObservation expected;
  std::string transport;
  std::string exact_mcu;
  SessionCapabilities capabilities;
  std::string physical_evidence_sha256;
  NativeSpiSelector spi_selector;
};
struct NativeBindingProfile {
  NativeBindingProfile() { slots[1].slot = 1; }
  std::string schema = "songplus-native-binding/v1";
  std::string binding_status = "UNBOUND";
  std::array<NativeBoardBinding,2> slots{{NativeBoardBinding{},NativeBoardBinding{}}};
  SessionRoles roles;
  bool device_access_enabled = false, can_tx_enabled = false, firmware_install_enabled = false;
};
// This callback must check the referenced evidence content AND its association
// with this exact board/MCU/capability/transport tuple. A digest or true boolean
// in a JSON file is not verification. No production verifier ships in P9.
using NativeEvidenceVerifier = std::function<bool(const NativeBoardBinding &)>;
struct NativeBindingResult {
  bool bound = false;
  std::string reason = "UNBOUND_PROFILE";
  SessionProfile profile;
  std::array<NativeObservedBoard,2> observations;
};
NativeBindingResult validate_native_binding(const NativeBindingProfile &,
  const std::vector<NativeObservedBoard> &, const NativeEvidenceVerifier & = {});

struct NativeLease {
  NativeObservedBoard observed;
  std::uint64_t observed_us = 0;
  std::unique_ptr<OwnedTransport> io;
};
// Adapter helpers never discover/reopen devices and default-deny activation.
NativeLease native_usb_read_only_lease(NativeUsbInventory &, std::size_t,
  const NativeBindingResult &, std::size_t slot, Clock &, bool allow_read_only_activation = false);
NativeLease native_spi_read_only_lease(NativeSpiProbeResult &&, const NativeObservedBoard &);

// Bridges retained native providers to the existing epoch/whole-group owner.
// Staging requires all two identities and transfers each lease exactly once.
// Snapshot timestamps are NOT renewed without a new complete probe: the P8
// freshness barrier will retire a stale group. There is no production launcher.
class NativeReadOnlyProviders final : public DiscoveryProvider, public TransportFactory {
public:
  NativeReadOnlyProviders(const NativeBindingProfile &profile, NativeEvidenceVerifier verifier, Clock &clock)
    : profile_(profile), verifier_(std::move(verifier)), clock_(clock) {}
  bool stage(std::vector<NativeLease> leases, std::uint64_t revision,
             std::uint64_t observed_us, bool allow_read_only_handoff = false);
  DiscoverySnapshot snapshot() override { return snapshot_; }
  std::unique_ptr<OwnedTransport> open(const DiscoveryItem &, const SessionBinding &) override;
  NativeBindingResult binding() const { return binding_; }
private:
  const NativeBindingProfile profile_;
  NativeEvidenceVerifier verifier_;
  Clock &clock_;
  NativeBindingResult binding_;
  DiscoverySnapshot snapshot_;
  std::array<std::unique_ptr<OwnedTransport>,2> leases_;
  std::set<std::uint64_t> consumed_;
  std::uint64_t last_revision_ = 0;
};
}  // namespace openpilot::pandad::compatibility
