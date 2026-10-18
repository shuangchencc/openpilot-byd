#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

#include "control_session.h"
#include "transport.h"

namespace openpilot::pandad::compatibility {

inline constexpr std::uint32_t kRuntimeIoBudgetMs = 5;
inline constexpr std::uint64_t kDeviceInputMaxAgeUs = 750000;
inline constexpr std::uint64_t kPeripheralMaxAgeUs = 250000;
struct InputEpoch {
  std::uint64_t generation = 0;
  std::uint64_t control_epoch = 0;
};
struct DeviceInput {
  bool valid = false;
  bool started = false;
  bool thermal_ok = false;
  std::uint32_t fan_percent = 0;
  std::uint64_t timestamp_us = 0;
};
struct CameraInput {
  bool valid = false;
  std::uint32_t ir_percent = 0;
  std::uint64_t timestamp_us = 0;
};
struct PeripheralSnapshot {
  bool valid = false;
  std::int32_t fan_slot = kRoleUnsupported;
  std::optional<std::uint16_t> fan_rpm;
  std::uint64_t timestamp_us = 0;  // fan observation, retained for fan-age checks
  std::uint64_t power_timestamp_us = 0;
  std::optional<bool> ignition;
  std::optional<std::uint32_t> harness;
  std::optional<std::uint16_t> voltage;
  std::optional<std::uint16_t> current;
  std::optional<std::int16_t> temperature;
  std::optional<std::uint16_t> sound_output;
  std::optional<std::uint16_t> spi_error_count;
  HealthFieldAvailability field_available{};
};
struct RuntimeCounters {
  std::uint64_t denied_tx = 0;
  std::uint64_t simulated_tx = 0;
  std::uint64_t discarded_inputs = 0;
  std::uint64_t io_failures = 0;
};

// The shared pandad scheduler used by the explicit offline entry. It has no
// discovery/factory/reset/flash route. Inject ONLY transports over fake Backend
// instances; profiles remain synthetic and all device permissions stay false.
// One serialized event-loop owns this object, the registry and all completions.
class PandadRuntime {
public:
  PandadRuntime(const SessionProfile &profile, Clock &clock);
  // Each pointer is bound to its exact slot/token. Changing a live handle under
  // the same token is rejected. Failed/cancelled handles cannot be recycled.
  bool reconcile(const std::vector<SessionObservation> &inventory,
                 const std::array<Transport *, 2> &transports);
  InputEpoch capture();
  bool device_input(const DeviceInput &input, InputEpoch epoch);
  bool camera_input(const CameraInput &input, InputEpoch epoch);
  bool configure(const std::vector<SlotSafetyConfig> &configs, InputEpoch epoch);
  bool selfdrive(bool valid, bool enabled, std::uint64_t timestamp_us, InputEpoch epoch);
  bool sendcan(TxBatch batch, InputEpoch epoch);
  // Each tick dispatches at most one <=5ms control operation per slot. Peripheral
  // and CAN steps are separate bounded opportunities so neither board drains a
  // whole queue ahead of a sibling. No background work or implicit retry exists.
  void tick();
  bool peripheral_step();
  bool receive_step(std::uint32_t slot);
  bool transmit_step(std::uint32_t slot);
  std::vector<RoutedRxFrame> receive_frames();
  ControlSnapshot snapshot();
  PeripheralSnapshot peripheral_snapshot();
  SessionSnapshot sessions() const { return registry_.snapshot(); }
  SessionProfile profile() const { return registry_.profile(); }
  const RuntimeCounters &counters() const { return counters_; }
  std::uint64_t rejection_count() const { return router_.counters().safety_rejections; }
  std::optional<RejectionEvidence> rejection_evidence() const { return router_.latest_rejection(); }
  void shutdown();
  void invalid_input(const char *reason);
private:
  bool epoch_matches(InputEpoch epoch);
  void synchronize();
  bool device_fresh();
  bool fan_observable() const;
  bool fresh(std::uint64_t source, std::uint64_t limit) const;
  Result perform(std::uint32_t slot, const Request &request);
  bool exact(const Result &result, std::size_t expected) const;
  void retire(std::uint32_t slot, const char *reason);
  SessionRegistry registry_;
  CanRouter router_;
  ControlSession control_;
  Clock &clock_;
  std::array<Transport *, 2> transports_{};
  std::array<std::uint64_t, 2> attached_tokens_{};
  std::optional<DeviceInput> device_;
  std::uint64_t device_control_epoch_ = 0;
  std::optional<CameraInput> camera_;
  std::optional<std::uint64_t> last_selfdrive_;
  std::optional<std::uint64_t> last_sendcan_;
  std::optional<std::uint64_t> fan_time_;
  std::optional<std::uint16_t> fan_rpm_;
  std::uint64_t seen_generation_ = 0;
  std::uint64_t seen_control_epoch_ = 0;
  std::uint64_t control_started_us_ = 0;
  unsigned peripheral_phase_ = 0;
  bool stopped_ = false;
  RuntimeCounters counters_{};
};
}  // namespace openpilot::pandad::compatibility
