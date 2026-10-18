#pragma once

#include <array>
#include <memory>

#include "native_spi_probe.h"

namespace openpilot::pandad::compatibility {

// A conditional protocol proof, never hardware ownership authorization. It is
// valid only for the locked firmware's one-entry (id, request CRC) cache, and
// while every master of this physical node obeys the same advisory flock.
// Neither a random seed nor a persistent counter supplies this proof.
struct SpiFreshnessProof {
  bool synchronized = false;
  bool same_fd_lifetime_lock = false;
  bool kernel_deadline_proven = false;
  std::uint32_t first_fence_id = 0, second_fence_id = 0;
  std::uint64_t completed_us = 0, lease_nonce = 0;
  std::array<std::uint8_t, 12> expected_uid{};
};

// Inert on construction. Successful activation keeps the native fd and flock
// until destruction, including across all later requests. cancel is local only.
// The caller must serialize activation, transact, proof access and destruction.
// Fork-inherited owners fail closed. Child destruction closes only its inherited
// fd and never unlocks the parent's shared open-file description.
class SpiFreshnessBackend final : public Backend {
public:
  SpiFreshnessBackend(std::shared_ptr<NativeSpiSyscalls>, Clock &);
  ~SpiFreshnessBackend() override;
  SpiFreshnessBackend(const SpiFreshnessBackend &) = delete;
  SpiFreshnessBackend &operator=(const SpiFreshnessBackend &) = delete;
  Status activate(const NativeSpiSelector &, const std::array<std::uint8_t, 12> &expected_uid,
                  std::uint32_t first_request_id, std::uint32_t budget_ms,
                  bool cooperative_ownership_verified = false, bool allow_device_io = false);
  Result transact(const Request &, std::uint32_t timeout_ms) override;
  void cancel() noexcept override;
  void permit_f4_read();
  SpiFreshnessProof proof() const;
private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

struct FreshNativeSpiProbeResult {
  NativeSpiProbeResult probe;
  SpiFreshnessProof freshness;
  bool complete() const { return probe.complete() && freshness.synchronized && freshness.same_fd_lifetime_lock; }
};

// P10 entry point. P9 discovery remains a legacy/offline regression path and
// does not furnish freshness. Unknown ownership denies before opening anything.
FreshNativeSpiProbeResult discover_fresh_native_spi(
    std::shared_ptr<NativeSpiSyscalls>, Clock &, const NativeSpiProbePolicy &,
    const std::array<std::uint8_t, 12> &expected_uid,
    bool cooperative_ownership_verified = false, bool allow_device_io = false);
FreshNativeSpiProbeResult discover_fresh_native_spi(
    Clock &, const NativeSpiProbePolicy &, const std::array<std::uint8_t, 12> &expected_uid,
    bool cooperative_ownership_verified = false, bool allow_device_io = false);

}  // namespace openpilot::pandad::compatibility
