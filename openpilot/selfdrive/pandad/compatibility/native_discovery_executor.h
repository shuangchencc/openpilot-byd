#pragma once

#include <functional>
#include <optional>
#include "discovery_scheduler.h"
#include "native_binding.h"
#include "spi_freshness.h"

namespace openpilot::pandad::compatibility {

// A port never owns a native fd/handle. Its destruction/cancellation only
// changes local state. The engine exclusively owns and destroys the real I/O.
class NativeDiscoveryPort : public Backend {
public:
  virtual std::optional<SpiFreshnessProof> live_spi_proof() = 0;
  virtual bool active() const = 0;
};
struct NativeDiscoveryHandoff final : DiscoveryTaskPayload {
  SessionProfile profile;
  std::array<std::shared_ptr<NativeDiscoveryPort>,2> ports;
};
class NativeDiscoveryEngine {
public:
  virtual ~NativeDiscoveryEngine() = default;
  // Only the one worker invokes these methods. execute may block in a native
  // kernel call. Cancellation is cooperative and never kills that thread.
  virtual DiscoveryTaskResult execute(const DiscoveryTaskRequest &, const std::atomic<bool> &cancelled) = 0;
  virtual void retire() noexcept = 0;
};
struct NativeDiscoveryEngineConfig {
  NativeBindingProfile profile;
  NativeEvidenceVerifier physical_evidence;
  std::function<bool(const NativeUsbObservation &)> usb_session_alt0;
  std::function<bool(const NativeBoardBinding &)> spi_cooperative_ownership;
  // Shared lifetime and safe concurrent reads are mandatory: the serialized
  // owner and the worker may consult this clock at the same time.
  std::shared_ptr<Clock> clock;
  std::function<std::unique_ptr<LibusbApi>()> usb_api;
  std::function<std::shared_ptr<NativeSpiSyscalls>()> spi_syscalls;
  std::uint32_t spi_first_request_id = 1;
  bool allow_read_only_device_io = false;
};
// Inert creation. Default-denied configuration does not construct native APIs,
// enumerate, open or claim. The concrete engine uses the P9 implementations,
// the P10 SPI freshness fence, and same-handle identity reprobes.
std::unique_ptr<NativeDiscoveryEngine> make_native_discovery_engine(NativeDiscoveryEngineConfig);

using NativeEpochRetirer = std::function<void(const char *)>;
class NativeDiscoveryExecutor final : public DiscoveryTaskProvider, public DiscoveryCommitSink, public TransportFactory {
public:
  NativeDiscoveryExecutor(std::unique_ptr<NativeDiscoveryEngine>, std::shared_ptr<Clock>,
                          NativeEpochRetirer = {}, bool allow_jobs = false);
  ~NativeDiscoveryExecutor();
  NativeDiscoveryExecutor(const NativeDiscoveryExecutor &)=delete;
  NativeDiscoveryExecutor &operator=(const NativeDiscoveryExecutor &)=delete;
  NativeDiscoveryExecutor(NativeDiscoveryExecutor &&)=delete;
  NativeDiscoveryExecutor &operator=(NativeDiscoveryExecutor &&)=delete;
  std::unique_ptr<DiscoveryTask> start(const DiscoveryTaskRequest &) override;
  bool commit(const DiscoveryTaskRequest &, DiscoveryTaskResult &) override;
  void retire(const char *) noexcept override;
  std::unique_ptr<OwnedTransport> open(const DiscoveryItem &, const SessionBinding &) override;
  // Reads the CURRENT owned live backend proof through a try-lock, never a
  // saved admission receipt. The caller must compare it with current binding.
  std::optional<SpiFreshnessProof> live_spi_proof(std::size_t slot);
  // Process-wide lane cap also covers a destroyed executor with a stuck ioctl.
  // This read-only diagnostic is used by offline lifecycle tests.
  static std::size_t occupied_worker_lanes();
private:
  struct State;
  class Task;
  std::shared_ptr<State> state_;
  std::shared_ptr<Clock> clock_;
  NativeEpochRetirer revoke_;
  bool allowed_ = false;
  DiscoverySnapshot committed_;
  SessionProfile committed_profile_;
  std::array<std::shared_ptr<NativeDiscoveryPort>,2> ports_;
  std::array<bool,2> opened_{};
  std::uint64_t last_committed_revision_=0;
};
}  // namespace openpilot::pandad::compatibility
