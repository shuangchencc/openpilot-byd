#include "production_gate.h"

#include <algorithm>
#include <iostream>
#include <set>

namespace openpilot::pandad::compatibility {
namespace {
bool digest(const std::string &s) {
  return s.size() == 64 && std::any_of(s.begin(), s.end(), [](char c) { return c != '0'; }) &&
    std::all_of(s.begin(), s.end(), [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); });
}
bool valid_purpose(LaunchPurpose p) {
  return p >= LaunchPurpose::OfflineDiagnostics && p <= LaunchPurpose::Deployment;
}
void print_default_decision(const char *mode, const LaunchDecision &decision) {
  std::cout << "{\"suite\":\"p10_production_main\",\"mode\":\"" << mode
    << "\",\"entry\":\"" << mode
    << "\",\"passed\":" << (std::string(mode) == "offline_check" ? 1 : 0)
    << ",\"failed\":0,\"skipped\":0"
    << ",\"ready_for_review\":false,\"profile_bound\":false,\"production_start_authorized\":false,"
       "\"real_device_io\":false,\"device_access_authorized\":false,\"runtime_writes_authorized\":false,\"can_tx_authorized\":false,"
       "\"deployment_authorized\":false,\"blockers\":[";
  for (std::size_t i = 0; i < decision.blockers.size(); ++i) {
    if (i) std::cout << ',';
    std::cout << '"' << decision.blockers[i] << '"';
  }
  std::cout << ",\"PRODUCTION_AUTHORITY_AND_NATIVE_LAUNCHER_UNAVAILABLE\"]}\n";
}
}  // namespace

const char *launch_requirement_name(LaunchRequirement r) {
  switch (r) {
    case LaunchRequirement::LockedRelease: return "LOCKED_RELEASE";
    case LaunchRequirement::PhysicalIdentity: return "PHYSICAL_IDENTITY";
    case LaunchRequirement::RecoveryChain: return "RECOVERY_CHAIN";
    case LaunchRequirement::PowerAndTopology: return "POWER_AND_TOPOLOGY";
    case LaunchRequirement::ExclusiveOwnership: return "EXCLUSIVE_OWNERSHIP";
    case LaunchRequirement::UsbActiveInterface: return "USB_ACTIVE_INTERFACE";
    case LaunchRequirement::SpiConfiguration: return "SPI_CONFIGURATION";
    case LaunchRequirement::SpiCrossProcessFreshness: return "SPI_CROSS_PROCESS_FRESHNESS";
    case LaunchRequirement::DiscoveryScheduling: return "DISCOVERY_SCHEDULING";
    case LaunchRequirement::KernelIoDeadline: return "KERNEL_IO_DEADLINE";
    case LaunchRequirement::ResourceBudget: return "RESOURCE_BUDGET";
    case LaunchRequirement::GroupHealthSafety: return "GROUP_HEALTH_SAFETY";
    case LaunchRequirement::HeartbeatAndShutdown: return "HEARTBEAT_AND_SHUTDOWN";
    case LaunchRequirement::PeripheralRoles: return "PERIPHERAL_ROLES";
    case LaunchRequirement::DriverMonitoring: return "DRIVER_MONITORING";
    case LaunchRequirement::AudibleAlerts: return "AUDIBLE_ALERTS";
    case LaunchRequirement::VehicleSignals: return "VEHICLE_SIGNALS";
    case LaunchRequirement::VehicleController: return "VEHICLE_CONTROLLER";
    case LaunchRequirement::IndependentPandaSafety: return "INDEPENDENT_PANDA_SAFETY";
    case LaunchRequirement::ClosedCourse: return "CLOSED_COURSE";
    case LaunchRequirement::InstallationAndRollback: return "INSTALLATION_AND_ROLLBACK";
  }
  return "INVALID_REQUIREMENT";
}
const char *launch_purpose_name(LaunchPurpose p) {
  switch (p) {
    case LaunchPurpose::OfflineDiagnostics: return "offline_diagnostics";
    case LaunchPurpose::NativeReadOnly: return "native_read_only";
    case LaunchPurpose::NativeNoOutputRuntime: return "native_no_output_runtime";
    case LaunchPurpose::VehicleControl: return "vehicle_control";
    case LaunchPurpose::Deployment: return "deployment";
  }
  return "invalid";
}
std::vector<LaunchRequirement> required_launch_evidence(LaunchPurpose purpose, bool usb, bool spi) {
  if (!valid_purpose(purpose) || purpose == LaunchPurpose::OfflineDiagnostics) return {};
  using R = LaunchRequirement;
  std::vector<R> out{R::LockedRelease, R::PhysicalIdentity, R::RecoveryChain, R::PowerAndTopology,
                     R::ExclusiveOwnership, R::PeripheralRoles, R::DiscoveryScheduling, R::KernelIoDeadline};
  if (usb) out.push_back(R::UsbActiveInterface);
  if (spi) { out.push_back(R::SpiConfiguration); out.push_back(R::SpiCrossProcessFreshness); }
  if (purpose >= LaunchPurpose::NativeNoOutputRuntime) {
    for (auto r : {R::ResourceBudget, R::GroupHealthSafety,
                   R::HeartbeatAndShutdown}) out.push_back(r);
  }
  if (purpose >= LaunchPurpose::VehicleControl) {
    for (auto r : {R::DriverMonitoring, R::AudibleAlerts, R::VehicleSignals, R::VehicleController,
                   R::IndependentPandaSafety, R::ClosedCourse}) out.push_back(r);
  }
  if (purpose == LaunchPurpose::Deployment) out.push_back(R::InstallationAndRollback);
  return out;
}

LaunchDecision evaluate_production_gate(const LaunchRequest &request, const NativeEvidenceVerifier &physical,
                                       const LaunchEvidenceVerifier &verify, const LiveSpiProofReader &live_spi) {
  LaunchDecision out;
  if (!valid_purpose(request.purpose)) { out.blockers.push_back("INVALID_PURPOSE"); return out; }
  // Pure offline diagnostics need neither profile nor attestation. This only
  // approves the review operation; every authority field stays false.
  if (request.purpose == LaunchPurpose::OfflineDiagnostics) { out.ready_for_review = true; return out; }
  if (request.observations.size() > 2 || request.evidence.size() > kLaunchEvidenceLimit) {
    out.blockers.push_back("INPUT_CAPACITY_EXCEEDED"); return out;
  }
  const auto binding = validate_native_binding(request.profile, request.observations, physical);
  out.profile_bound = binding.bound;
  if (!binding.bound) out.blockers.push_back("PROFILE_UNBOUND:" + binding.reason);
  if (!digest(request.release_chain_sha256)) out.blockers.push_back("RELEASE_CHAIN_INVALID");
  if (!request.process_epoch || !request.discovery_revision) out.blockers.push_back("SESSION_CONTEXT_MISSING");
  bool fresh = request.now_us && request.observations.size() == 2;
  bool usb = false, spi = false;
  for (const auto &board : request.observations) {
    usb |= board.transport == "USB";
    spi |= board.transport == "SPI";
    fresh &= board.raw.observed_us && board.raw.observed_us <= request.now_us &&
             request.now_us - board.raw.observed_us < kLoopMaxGapUs;
  }
  if (!fresh) out.blockers.push_back("COMPLETE_PROBE_NOT_FRESH");
  const LaunchEvidenceContext context{request, binding};
  if (binding.bound && out.blockers.empty()) {
    for (std::size_t slot = 0; slot < binding.observations.size(); ++slot) {
      const auto &board = binding.observations[slot];
      if (board.transport != "SPI") continue;
      std::optional<SpiFreshnessProof> current;
      try { if (live_spi) current = live_spi(slot, context); } catch (...) { current.reset(); }
      const auto proof = current.value_or(SpiFreshnessProof{});
      if (!proof.synchronized || !proof.same_fd_lifetime_lock || !proof.first_fence_id ||
          !proof.second_fence_id || proof.first_fence_id == proof.second_fence_id ||
          !proof.completed_us || proof.completed_us > board.raw.observed_us ||
          proof.completed_us > request.now_us ||
          proof.expected_uid != board.raw.uid || proof.lease_nonce != board.raw.lease_nonce) {
        out.blockers.push_back("LIVE_SPI_FRESHNESS_PROOF_REQUIRED:" + std::to_string(slot));
      }
    }
  }
  if (!out.blockers.empty()) {
    // Still enumerate unresolved hardware gates for a useful UNBOUND report;
    // never invoke evidence verifiers against an invalid/unbound context.
    const auto diagnostic = required_launch_evidence(request.purpose, usb || !binding.bound, spi || !binding.bound);
    for (auto requirement : diagnostic) {
      out.blockers.push_back("EVIDENCE_NOT_EVALUATED:" + std::string(launch_requirement_name(requirement)));
    }
    return out;
  }
  const auto required = required_launch_evidence(request.purpose, usb, spi);
  std::set<LaunchRequirement> seen;
  for (const auto &evidence : request.evidence) {
    if (std::find(required.begin(), required.end(), evidence.requirement) == required.end()) {
      out.blockers.push_back("UNEXPECTED_EVIDENCE"); return out;
    }
    if (!seen.insert(evidence.requirement).second) {
      out.blockers.push_back("DUPLICATE_EVIDENCE"); return out;
    }
  }
  for (auto requirement : required) {
    const auto found = std::find_if(request.evidence.begin(), request.evidence.end(),
                                   [&](const auto &e) { return e.requirement == requirement; });
    const auto name = std::string(launch_requirement_name(requirement));
    if (found == request.evidence.end()) { out.blockers.push_back("EVIDENCE_MISSING:" + name); continue; }
    if (!digest(found->content_sha256) || !found->attested_us || found->attested_us > request.now_us ||
        request.now_us - found->attested_us >= kLaunchAttestationMaxAgeUs ||
        found->expires_us <= request.now_us || found->expires_us <= found->attested_us ||
        found->expires_us - found->attested_us > kLaunchAttestationMaxAgeUs) {
      out.blockers.push_back("EVIDENCE_INVALID_OR_EXPIRED:" + name); continue;
    }
    bool accepted = false;
    try { accepted = verify && verify(*found, context); } catch (...) { accepted = false; }
    if (!accepted) { out.blockers.push_back("EVIDENCE_NOT_VERIFIED:" + name); continue; }
    out.verified_requirements.push_back(requirement);
  }
  out.ready_for_review = out.blockers.empty();
  return out;
}

int pandad_production_check_offline_main() {
  const auto decision = evaluate_production_gate(LaunchRequest{});
  if (decision.ready_for_review || decision.profile_bound || decision.production_start_authorized ||
      decision.device_access_authorized || decision.runtime_writes_authorized || decision.can_tx_authorized ||
      decision.deployment_authorized || decision.blockers.empty()) return 1;
  std::cout << "PASS p10_main_production_check_defaults_blocked\n";
  print_default_decision("offline_check", decision);
  return 0;
}
int pandad_production_start_denied_main() {
  print_default_decision("production_denied", evaluate_production_gate(LaunchRequest{}));
  return 3;
}

}  // namespace openpilot::pandad::compatibility
