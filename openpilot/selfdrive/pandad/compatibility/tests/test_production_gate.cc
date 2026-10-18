#include "../production_gate.h"

#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>

using namespace openpilot::pandad::compatibility;
namespace {
unsigned assertions = 0;
#define CHECK(x) do { ++assertions; if (!(x)) throw std::runtime_error(std::string(__func__) + ": " #x); } while (false)
NativeUsbObservation raw(unsigned i) {
  NativeUsbObservation out;
  out.serial = "offline-gate-" + std::to_string(i);
  out.uid.fill(i);
  out.board_type = 6;
  out.protocol = {0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75};
  out.application_signature.fill(i + 10);
  out.f4_capabilities = {'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};
  return out;
}
LaunchRequest candidate(LaunchPurpose purpose = LaunchPurpose::Deployment) {
  LaunchRequest r;
  r.purpose = purpose;
  r.profile.binding_status = "CANDIDATE";
  r.release_chain_sha256 = std::string(64, 'c');
  r.process_epoch = 23;
  r.discovery_revision = 71;
  r.now_us = 10000000;
  for (unsigned i = 0; i < 2; ++i) {
    auto &b = r.profile.slots[i];
    b.expected = raw(i + 1);
    b.transport = "USB";
    b.exact_mcu = "STM32F413VGT6";
    b.physical_evidence_sha256 = std::string(64, 'a' + i);
    b.capabilities.diagnostic = b.capabilities.harness = b.capabilities.ignition =
      b.capabilities.fan = b.capabilities.power_monitor = true;
    NativeObservedBoard board;
    board.raw = b.expected;
    board.raw.lease_nonce = i + 10;
    board.raw.observed_us = r.now_us;
    board.transport = "USB";
    board.usb_alt0_verified = true;
    r.observations.push_back(board);
  }
  auto &roles = r.profile.roles;
  roles.vehicle_can = roles.diagnostic = roles.harness = roles.fan_control = roles.power_monitor = 0;
  roles.ir_control = kRoleUnsupported;
  roles.ignition_sources = {0};
  for (auto requirement : required_launch_evidence(purpose, true, false)) {
    r.evidence.push_back({requirement, std::string(64, 'd'), r.now_us, r.now_us + 1000});
  }
  return r;
}
NativeEvidenceVerifier physical() { return [](const auto &) { return true; }; }
LaunchEvidenceVerifier verifier() { return [](const auto &, const auto &) { return true; }; }
LaunchDecision evaluate(const LaunchRequest &r) { return evaluate_production_gate(r, physical(), verifier()); }
void no_authority(const LaunchDecision &r) {
  CHECK(!r.device_access_authorized);
  CHECK(!r.runtime_writes_authorized);
  CHECK(!r.can_tx_authorized);
  CHECK(!r.deployment_authorized);
  CHECK(!r.production_start_authorized);
}
void blocked(const LaunchDecision &r) { CHECK(!r.ready_for_review); CHECK(!r.blockers.empty()); no_authority(r); }
bool has(const LaunchDecision &r, const std::string &s) {
  return std::find(r.blockers.begin(), r.blockers.end(), s) != r.blockers.end();
}
LaunchRequest mixed_candidate() {
  auto r = candidate();
  auto &b = r.profile.slots[1];
  b.expected.board_type = 7; b.expected.serial.clear(); b.expected.f4_capabilities.clear();
  b.transport = "SPI"; b.exact_mcu = "STM32H725IGK6";
  auto &s = b.spi_selector;
  s.path = "/dev/spidev0.0"; s.identity_evidence_sha256 = b.physical_evidence_sha256;
  s.physical_identity_verified = true; s.expected_major = 153; s.expected_minor = 0;
  s.expected_mode = 0; s.expected_bits_per_word = 8; s.expected_max_speed_hz = 50000000;
  auto &o = r.observations[1];
  o.raw = b.expected; o.raw.lease_nonce = 11; o.raw.observed_us = r.now_us;
  o.transport = "SPI"; o.usb_alt0_verified = false; o.spi_first_request_id = 100; o.spi_selector = s;
  r.evidence.clear();
  for (auto requirement : required_launch_evidence(r.purpose, true, true)) {
    r.evidence.push_back({requirement, std::string(64, 'd'), r.now_us, r.now_us + 1000});
  }
  return r;
}
SpiFreshnessProof synthetic_live_proof(const LaunchRequest &r) {
  SpiFreshnessProof p; p.synchronized = p.same_fd_lifetime_lock = true;
  p.first_fence_id = 100; p.second_fence_id = 101;
  p.completed_us = r.observations[1].raw.observed_us - 10;
  p.lease_nonce = r.observations[1].raw.lease_nonce; p.expected_uid = r.observations[1].raw.uid;
  return p;
}
}  // namespace

int main() {
  unsigned passed = 0, failed = 0;
  const auto test = [&](const char *id, const std::function<void()> &fn) {
    try { fn(); ++passed; std::cout << "PASS " << id << '\n'; }
    catch (const std::exception &e) { ++failed; std::cerr << id << ": " << e.what() << '\n'; }
  };
  test("p10_gate_default_unbound_no_callbacks", [] {
    unsigned calls = 0;
    auto r = evaluate_production_gate(LaunchRequest{}, [&](const auto &) { ++calls; return true; },
                                     [&](const auto &, const auto &) { ++calls; return true; });
    blocked(r); CHECK(!r.profile_bound); CHECK(calls == 0);
    CHECK(has(r, "PROFILE_UNBOUND:UNBOUND_PROFILE"));
  });
  test("p10_gate_offline_diagnostics_inert", [] {
    auto r = candidate(LaunchPurpose::OfflineDiagnostics);
    unsigned calls = 0;
    auto out = evaluate_production_gate(r, [&](const auto &) { ++calls; return true; },
                                       [&](const auto &, const auto &) { ++calls; return true; });
    CHECK(out.ready_for_review); CHECK(!out.profile_bound); CHECK(out.blockers.empty());
    CHECK(calls == 0); no_authority(out);
  });
  test("p10_gate_all_purposes_review_only", [] {
    for (auto purpose : {LaunchPurpose::NativeReadOnly, LaunchPurpose::NativeNoOutputRuntime,
                         LaunchPurpose::VehicleControl, LaunchPurpose::Deployment}) {
      auto r = candidate(purpose); auto out = evaluate(r);
      CHECK(out.ready_for_review); CHECK(out.profile_bound); CHECK(out.blockers.empty());
      CHECK(out.verified_requirements.size() == r.evidence.size()); no_authority(out);
    }
  });
  test("p10_gate_bound_does_not_verify_launch_evidence", [] {
    auto r = candidate(); blocked(evaluate_production_gate(r, physical()));
    auto out = evaluate_production_gate(r, physical(), [](const auto &, const auto &) { return false; });
    CHECK(out.profile_bound); CHECK(out.verified_requirements.empty()); blocked(out);
  });
  test("p10_gate_physical_verifier_still_required", [] {
    blocked(evaluate_production_gate(candidate(), {}, verifier()));
    blocked(evaluate_production_gate(candidate(), [](const auto &) { return false; }, verifier()));
  });
  test("p10_gate_each_missing_evidence_blocks", [] {
    const auto full = candidate();
    for (std::size_t i = 0; i < full.evidence.size(); ++i) {
      auto r = full;
      auto name = std::string(launch_requirement_name(r.evidence[i].requirement));
      r.evidence.erase(r.evidence.begin() + i);
      auto out = evaluate(r); blocked(out); CHECK(has(out, "EVIDENCE_MISSING:" + name));
    }
  });
  test("p10_gate_each_rejected_evidence_blocks", [] {
    auto r = candidate();
    for (auto missing : r.evidence) {
      auto out = evaluate_production_gate(r, physical(), [&](const auto &e, const auto &) {
        return e.requirement != missing.requirement;
      });
      blocked(out); CHECK(has(out, "EVIDENCE_NOT_VERIFIED:" + std::string(launch_requirement_name(missing.requirement))));
    }
  });
  test("p10_gate_camera_absence_never_bypasses_monitoring", [] {
    auto r = candidate(LaunchPurpose::VehicleControl);
    r.evidence.erase(std::remove_if(r.evidence.begin(), r.evidence.end(), [](const auto &e) {
      return e.requirement == LaunchRequirement::DriverMonitoring;
    }), r.evidence.end());
    auto out = evaluate(r); blocked(out); CHECK(has(out, "EVIDENCE_MISSING:DRIVER_MONITORING"));
  });
  test("p10_gate_readonly_needs_executor_timing_and_scheduling", [] {
    auto r = candidate(LaunchPurpose::NativeReadOnly);
    for (auto requirement : {LaunchRequirement::KernelIoDeadline, LaunchRequirement::DiscoveryScheduling}) {
      auto out = evaluate_production_gate(r, physical(), [&](const auto &e, const auto &) { return e.requirement != requirement; });
      blocked(out);
    }
  });
  test("p10_gate_nooutput_not_vehicle_permission", [] {
    auto r = candidate(LaunchPurpose::NativeNoOutputRuntime);
    const auto has_requirement = [&](LaunchRequirement required) {
      return std::any_of(r.evidence.begin(), r.evidence.end(), [&](const auto &e) { return e.requirement == required; });
    };
    CHECK(has_requirement(LaunchRequirement::GroupHealthSafety));
    CHECK(has_requirement(LaunchRequirement::HeartbeatAndShutdown));
    CHECK(!has_requirement(LaunchRequirement::DriverMonitoring));
    auto out = evaluate(r); CHECK(out.ready_for_review); no_authority(out);
  });
  test("p10_gate_profile_permission_bits_cannot_unlock", [] {
    for (unsigned i = 0; i < 3; ++i) {
      auto r = candidate();
      if (i == 0) r.profile.device_access_enabled = true;
      if (i == 1) r.profile.can_tx_enabled = true;
      if (i == 2) r.profile.firmware_install_enabled = true;
      blocked(evaluate(r));
    }
  });
  test("p10_gate_replacement_role_and_interface_revalidated", [] {
    for (unsigned i = 0; i < 4; ++i) {
      auto r = candidate();
      if (i == 0) ++r.observations[0].raw.uid[0];
      if (i == 1) ++r.observations[1].raw.application_signature[111];
      if (i == 2) r.profile.roles.vehicle_can = -1;
      if (i == 3) r.observations[0].usb_alt0_verified = false;
      blocked(evaluate(r));
    }
  });
  test("p10_gate_current_complete_probe_required", [] {
    for (unsigned i = 0; i < 5; ++i) {
      auto r = candidate();
      if (i == 0) r.observations[0].raw.observed_us = 0;
      if (i == 1) r.observations[1].raw.observed_us = r.now_us + 1;
      if (i == 2) r.observations[0].raw.observed_us = r.now_us - kLoopMaxGapUs;
      if (i == 3) r.now_us = 0;
      if (i == 4) r.observations.pop_back();
      auto out = evaluate(r); blocked(out); CHECK(has(out, "COMPLETE_PROBE_NOT_FRESH"));
    }
  });
  test("p10_gate_probe_freshness_boundary", [] {
    auto r = candidate(); r.observations[0].raw.observed_us = r.now_us - kLoopMaxGapUs + 1;
    CHECK(evaluate(r).ready_for_review);
  });
  test("p10_gate_process_and_revision_nonzero", [] {
    auto r = candidate(); r.process_epoch = 0; blocked(evaluate(r));
    r = candidate(); r.discovery_revision = 0; blocked(evaluate(r));
  });
  test("p10_gate_release_digest_shape_not_authority", [] {
    for (const auto &digest : {std::string{}, std::string(64, '0'), std::string(64, 'A'), std::string(63, 'a')}) {
      auto r = candidate(); r.release_chain_sha256 = digest; blocked(evaluate(r));
    }
    auto r = candidate(); blocked(evaluate_production_gate(r, physical()));
  });
  test("p10_gate_bad_evidence_digest_rejected_before_verifier", [] {
    auto r = candidate(); r.evidence[0].content_sha256 = std::string(64, '0');
    unsigned calls = 0;
    auto out = evaluate_production_gate(r, physical(), [&](const auto &, const auto &) { ++calls; return true; });
    blocked(out); CHECK(calls == r.evidence.size() - 1);
  });
  test("p10_gate_attestation_times_enforced", [] {
    for (unsigned i = 0; i < 7; ++i) {
      auto r = candidate(); auto &e = r.evidence[0];
      if (i == 0) e.attested_us = 0;
      if (i == 1) e.attested_us = r.now_us + 1;
      if (i == 2) e.attested_us = r.now_us - kLaunchAttestationMaxAgeUs;
      if (i == 3) e.expires_us = r.now_us;
      if (i == 4) e.expires_us = e.attested_us - 1;
      if (i == 5) e.expires_us = e.attested_us + kLaunchAttestationMaxAgeUs + 1;
      if (i == 6) e.expires_us = UINT64_MAX;
      blocked(evaluate(r));
    }
  });
  test("p10_gate_duplicate_evidence_denied", [] {
    auto r = candidate(LaunchPurpose::NativeReadOnly); r.evidence.push_back(r.evidence[0]);
    auto out = evaluate(r); blocked(out); CHECK(has(out, "DUPLICATE_EVIDENCE"));
  });
  test("p10_gate_unexpected_evidence_denied", [] {
    auto r = candidate(LaunchPurpose::NativeReadOnly);
    r.evidence[0].requirement = LaunchRequirement::InstallationAndRollback;
    auto out = evaluate(r); blocked(out); CHECK(has(out, "UNEXPECTED_EVIDENCE"));
  });
  test("p10_gate_invalid_enum_denied", [] {
    auto r = candidate(); r.purpose = static_cast<LaunchPurpose>(55); blocked(evaluate(r));
    CHECK(required_launch_evidence(r.purpose, true, true).empty());
    CHECK(std::string(launch_purpose_name(r.purpose)) == "invalid");
    CHECK(std::string(launch_requirement_name(static_cast<LaunchRequirement>(55))) == "INVALID_REQUIREMENT");
  });
  test("p10_gate_bounded_inputs", [] {
    auto r = candidate(); r.observations.push_back(r.observations[0]); blocked(evaluate(r));
    r = candidate(); while (r.evidence.size() <= kLaunchEvidenceLimit) r.evidence.push_back(r.evidence[0]);
    blocked(evaluate(r));
  });
  test("p10_gate_verifier_exceptions_fail_closed", [] {
    auto out = evaluate_production_gate(candidate(), physical(), [](const auto &, const auto &) -> bool {
      throw std::runtime_error("unavailable verification service");
    });
    blocked(out); CHECK(out.verified_requirements.empty());
  });
  test("p10_gate_verifier_gets_complete_current_context", [] {
    auto r = candidate(); unsigned calls = 0;
    auto out = evaluate_production_gate(r, physical(), [&](const auto &, const auto &c) {
      ++calls; CHECK(&c.request == &r); CHECK(c.binding.bound);
      CHECK(c.binding.observations[0].raw.uid == r.profile.slots[0].expected.uid);
      CHECK(c.request.process_epoch == 23); CHECK(c.request.discovery_revision == 71);
      CHECK(c.request.release_chain_sha256 == std::string(64, 'c'));
      CHECK(c.binding.profile.roles.ignition_sources == r.profile.roles.ignition_sources);
      return true;
    });
    CHECK(out.ready_for_review); CHECK(calls == r.evidence.size()); no_authority(out);
  });
  test("p10_gate_reverification_on_context_change", [] {
    auto r = candidate();
    const auto check = [&](const auto &, const auto &c) { return c.request.process_epoch == 23 && c.request.discovery_revision == 71; };
    CHECK(evaluate_production_gate(r, physical(), check).ready_for_review);
    ++r.process_epoch; blocked(evaluate_production_gate(r, physical(), check));
    --r.process_epoch; ++r.discovery_revision; blocked(evaluate_production_gate(r, physical(), check));
  });
  test("p10_gate_no_cache_of_previous_success", [] {
    auto r = candidate(); bool present = true; unsigned calls = 0;
    const auto check = [&](const auto &, const auto &) { ++calls; return present; };
    CHECK(evaluate_production_gate(r, physical(), check).ready_for_review); present = false;
    blocked(evaluate_production_gate(r, physical(), check)); CHECK(calls == r.evidence.size() * 2);
  });
  test("p10_gate_spi_requirements_distinct_from_usb", [] {
    const auto list = required_launch_evidence(LaunchPurpose::Deployment, true, true);
    CHECK(list.size() == kLaunchEvidenceLimit);
    for (auto requirement : {LaunchRequirement::UsbActiveInterface, LaunchRequirement::SpiConfiguration,
                             LaunchRequirement::SpiCrossProcessFreshness}) {
      CHECK(std::find(list.begin(), list.end(), requirement) != list.end());
    }
    const auto usb = required_launch_evidence(LaunchPurpose::NativeReadOnly, true, false);
    CHECK(std::find(usb.begin(), usb.end(), LaunchRequirement::SpiCrossProcessFreshness) == usb.end());
  });
  test("p10_gate_lower_scope_evidence_cannot_be_promoted", [] {
    auto r = candidate(LaunchPurpose::NativeReadOnly); r.purpose = LaunchPurpose::Deployment;
    auto out = evaluate(r); blocked(out); CHECK(has(out, "EVIDENCE_MISSING:DRIVER_MONITORING"));
    CHECK(has(out, "EVIDENCE_MISSING:INDEPENDENT_PANDA_SAFETY"));
    CHECK(has(out, "EVIDENCE_MISSING:INSTALLATION_AND_ROLLBACK"));
  });
  test("p10_gate_spi_requires_live_reader_not_attestation_only", [] {
    auto r = mixed_candidate(); auto out = evaluate(r); blocked(out);
    CHECK(has(out, "LIVE_SPI_FRESHNESS_PROOF_REQUIRED:1"));
    blocked(evaluate_production_gate(r, physical(), verifier(), [](auto, const auto &) { return std::nullopt; }));
  });
  test("p10_gate_live_spi_reader_receives_current_slot_context", [] {
    auto r = mixed_candidate(); unsigned calls = 0;
    auto out = evaluate_production_gate(r, physical(), verifier(), [&](auto slot, const auto &c) {
      ++calls; CHECK(slot == 1); CHECK(&c.request == &r); CHECK(c.binding.bound);
      CHECK(c.binding.observations[slot].raw.uid == r.observations[1].raw.uid);
      return synthetic_live_proof(r);
    });
    CHECK(calls == 1); CHECK(out.ready_for_review); no_authority(out);
  });
  test("p10_gate_spi_proof_identity_fence_and_lock_must_match", [] {
    auto r = mixed_candidate();
    for (unsigned i = 0; i < 9; ++i) {
      auto proof = synthetic_live_proof(r);
      if (i == 0) proof.synchronized = false;
      if (i == 1) proof.same_fd_lifetime_lock = false;
      if (i == 2) proof.first_fence_id = 0;
      if (i == 3) proof.second_fence_id = proof.first_fence_id;
      if (i == 4) proof.completed_us = 0;
      if (i == 5) proof.completed_us = r.now_us + 1;
      if (i == 6) ++proof.expected_uid[0];
      if (i == 7) ++proof.lease_nonce;
      if (i == 8) proof.second_fence_id = 0;
      blocked(evaluate_production_gate(r, physical(), verifier(), [&](auto, const auto &) { return proof; }));
    }
  });
  test("p10_gate_spi_cancel_or_context_change_revokes_next_review", [] {
    auto r = mixed_candidate(); bool active = true; unsigned calls = 0;
    const auto reader = [&](auto, const auto &c) -> std::optional<SpiFreshnessProof> {
      ++calls;
      if (!active || c.request.process_epoch != 23 || c.request.discovery_revision != 71) return std::nullopt;
      return synthetic_live_proof(r);
    };
    CHECK(evaluate_production_gate(r, physical(), verifier(), reader).ready_for_review);
    active = false; blocked(evaluate_production_gate(r, physical(), verifier(), reader));
    active = true; ++r.process_epoch; blocked(evaluate_production_gate(r, physical(), verifier(), reader));
    --r.process_epoch; ++r.discovery_revision; blocked(evaluate_production_gate(r, physical(), verifier(), reader));
    CHECK(calls == 4);
  });
  test("p10_gate_spi_reader_exception_denies", [] {
    auto r = mixed_candidate();
    blocked(evaluate_production_gate(r, physical(), verifier(), [](auto, const auto &) -> std::optional<SpiFreshnessProof> {
      throw std::runtime_error("owner retired");
    }));
  });
  test("p10_gate_spi_live_lock_not_replaced_by_fence_age_heuristic", [] {
    auto r = mixed_candidate(); auto proof = synthetic_live_proof(r);
    proof.completed_us = 1;  // Old fence is valid only while the same live fd lock remains held.
    auto out = evaluate_production_gate(r, physical(), verifier(), [&](auto, const auto &) { return proof; });
    CHECK(out.ready_for_review); no_authority(out);
    proof.same_fd_lifetime_lock = false;
    blocked(evaluate_production_gate(r, physical(), verifier(), [&](auto, const auto &) { return proof; }));
  });
  std::cout << "{\"suite\":\"p10_production_gate\",\"passed\":" << passed << ",\"failed\":" << failed
            << ",\"skipped\":0,\"assertions\":" << assertions << ",\"real_device_io\":false}\n";
  return failed ? 1 : 0;
}
