#pragma once

#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "native_binding.h"
#include "spi_freshness.h"

namespace openpilot::pandad::compatibility {

// A review purpose is not a permission. In particular, passing this pure
// evaluator cannot open a device, construct a write-capable provider, or install
// software. P10 ships no production authority or production native launcher.
enum class LaunchPurpose { OfflineDiagnostics, NativeReadOnly, NativeNoOutputRuntime, VehicleControl, Deployment };
enum class LaunchRequirement {
  LockedRelease, PhysicalIdentity, RecoveryChain, PowerAndTopology,
  ExclusiveOwnership, UsbActiveInterface, SpiConfiguration, SpiCrossProcessFreshness,
  DiscoveryScheduling, KernelIoDeadline, ResourceBudget, GroupHealthSafety,
  HeartbeatAndShutdown, PeripheralRoles, DriverMonitoring, AudibleAlerts,
  VehicleSignals, VehicleController, IndependentPandaSafety, ClosedCourse,
  InstallationAndRollback,
};
const char *launch_requirement_name(LaunchRequirement);
const char *launch_purpose_name(LaunchPurpose);

struct LaunchEvidence {
  LaunchRequirement requirement = LaunchRequirement::LockedRelease;
  std::string content_sha256;
  // A verifier may re-attest historical physical evidence for this current
  // session. These are attestation times, not a rewrite of acquisition time.
  std::uint64_t attested_us = 0;
  std::uint64_t expires_us = 0;
};
struct LaunchRequest {
  LaunchPurpose purpose = LaunchPurpose::Deployment;
  NativeBindingProfile profile;
  std::vector<NativeObservedBoard> observations;
  std::string release_chain_sha256;
  std::uint64_t process_epoch = 0;
  std::uint64_t discovery_revision = 0;
  std::uint64_t now_us = 0;
  std::vector<LaunchEvidence> evidence;
};
struct LaunchEvidenceContext {
  const LaunchRequest &request;
  const NativeBindingResult &binding;
};
// Must inspect referenced evidence and match the complete current context,
// including identities, MCU/capabilities/roles, application signatures, lock,
// process epoch, discovery revision and retained leases. A JSON boolean/hash,
// an offline test receipt, or caller-supplied timestamps are not verification.
// This callback is injected for offline tests. No production verifier ships.
using LaunchEvidenceVerifier = std::function<bool(const LaunchEvidence &, const LaunchEvidenceContext &)>;
// The serialized executor supplies a current proof() from its owned live SPI
// backend, matching the requested fixed slot and context. Do not load a saved
// proof from JSON or cache this callback's result across evaluations/handoff.
// No default reader exists. A readiness result is only about this invocation;
// post-handoff production admission/revocation is not implemented by this API.
using LiveSpiProofReader = std::function<std::optional<SpiFreshnessProof>(std::size_t, const LaunchEvidenceContext &)>;
struct LaunchDecision {
  bool ready_for_review = false;
  bool profile_bound = false;
  // These remain false in every P10 evaluation, including fully injected tests.
  bool device_access_authorized = false;
  bool runtime_writes_authorized = false;
  bool can_tx_authorized = false;
  bool deployment_authorized = false;
  bool production_start_authorized = false;
  std::vector<std::string> blockers;
  std::vector<LaunchRequirement> verified_requirements;
};
inline constexpr std::size_t kLaunchEvidenceLimit = 21;
inline constexpr std::uint64_t kLaunchAttestationMaxAgeUs = 1000000;
std::vector<LaunchRequirement> required_launch_evidence(LaunchPurpose, bool has_usb, bool has_spi);
LaunchDecision evaluate_production_gate(const LaunchRequest &, const NativeEvidenceVerifier & = {},
                                       const LaunchEvidenceVerifier & = {}, const LiveSpiProofReader & = {});

// Called only before hardware setup. Both use the built-in UNBOUND profile;
// neither reads a config/environment/file nor constructs an I/O provider.
int pandad_production_check_offline_main();
int pandad_production_start_denied_main();

}  // namespace openpilot::pandad::compatibility
