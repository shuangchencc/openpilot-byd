#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "can_router.h"
#include "timing_budget.h"

namespace openpilot::pandad::compatibility {

inline constexpr std::uint8_t kNoOutputSafetyModel = 19;
inline constexpr std::uint64_t kSelfdriveMaxAgeUs = 100000;
inline constexpr std::uint64_t kControlCommandMaxAgeUs = 100000;

struct SlotSafetyConfig {
  std::uint32_t slot = 0;
  std::uint32_t model = kNoOutputSafetyModel;
  std::uint32_t param = 0;
  std::uint32_t alternative_experience = 0;
};
struct SelfdriveSample {
  bool valid = false;
  bool enabled = false;
  std::uint64_t timestamp_us = 0;
  std::uint64_t generation = 0;
};

enum class ControlKind { ReadHealth, ReadCanHealth, SetAlternativeExperience, SetSafety, Heartbeat };
const char *control_kind_name(ControlKind kind);
struct ControlCommand {
  std::uint64_t id = 0;
  RouteToken token{};
  ControlKind kind = ControlKind::ReadHealth;
  std::uint32_t value = 0;
  std::uint32_t param = 0;
  bool engaged = false;
  std::uint64_t issued_us = 0;
  bool real_device_authorized = false;
  bool exit_command = false;
};

// Raw validated snapshots are retained for the unchanged cereal serializer.
// Absent/invalid data is represented explicitly; no zero-health placeholder is
// ever reported as fresh. Timestamp is the original read-completion time.
struct BoardControlState {
  RouteToken token{};
  bool connected = false;
  bool health_valid = false;
  std::array<bool, 3> can_health_valid{};
  std::vector<std::uint8_t> health;
  std::array<std::vector<std::uint8_t>, 3> can_health;
  std::uint64_t health_timestamp_us = 0;
  std::array<std::uint64_t, 3> can_health_timestamp_us{};
  bool safety_readback_matched = false;
  bool heartbeat_fresh = false;
  bool heartbeat_engaged = false;
};
struct ControlSnapshot {
  std::array<BoardControlState, 2> slots{};
  std::uint64_t generation = 0;
  std::uint64_t control_epoch = 0;
  bool panda_states_valid = false;
  bool configured = false;
  bool armed = false;
  bool simulated_tx_allowed = false;
  bool real_tx_authorized = false;
  bool recovery_required = true;
  std::string reason = "OFFROAD_CONFIGURATION_REQUIRED";
};

// Seeds only an empty offline scheduler for deterministic overflow acceptance.
struct ControlCounterSeed {
  std::uint64_t control_epoch = 0;
  std::uint64_t next_command_id = 1;
};

// Single serialized owner, no I/O and no runtime permission. Every poll returns
// at most one command per connected slot. The caller must complete each command
// exactly once with the original command and exact response bytes. All writes
// are one-shot; a failure retires that connection before any further command.
class ControlSession {
public:
  explicit ControlSession(SessionRegistry &registry, ControlCounterSeed seed = {});
  bool configure(const std::vector<SlotSafetyConfig> &configs, bool offroad,
                 std::uint64_t generation, std::uint64_t now_us);
  bool update_selfdrive(const SelfdriveSample &sample, std::uint64_t now_us);
  std::vector<ControlCommand> poll(std::uint64_t now_us);
  bool complete(const ControlCommand &command, bool success,
                const std::vector<std::uint8_t> &response, std::uint64_t now_us);
  ControlSnapshot snapshot(std::uint64_t now_us);
  // Thermal/consumer/shutdown failures use this same group gate; this does not
  // fabricate a physical fan output or reset/flash a board.
  void fail_group(const std::string &reason, std::uint64_t now_us);
  // Final check for commands held outside the core before mock dispatch.
  bool validate_command(const ControlCommand &command, std::uint64_t now_us);

private:
  enum class Stage { Unconfigured, NeedAlternative, NeedSafety, NeedReadback, Configured };
  struct Board {
    BoardControlState view{};
    Stage stage = Stage::Unconfigured;
    SlotSafetyConfig config{};
    std::optional<ControlCommand> pending;
    bool heartbeat_seen = false;
    std::uint64_t heartbeat_timestamp_us = 0;
    std::uint8_t exit_phase = 0;
  };
  bool prepare(std::uint64_t now_us);
  void synchronize(std::uint64_t now_us);
  void trip(const std::string &reason);
  void advance_control_epoch();
  void retire_command_slot(std::size_t slot, const std::string &reason);
  bool health_complete(std::uint64_t now_us) const;
  bool ready(std::uint64_t now_us) const;
  bool current(const RouteToken &token) const;
  ControlCommand issue(std::size_t slot, ControlKind kind, std::uint64_t now_us,
                       std::uint32_t value = 0, std::uint32_t param = 0, bool engaged = false, bool exit_command = false);
  SessionRegistry &registry_;
  SessionSnapshot registry_state_{};
  std::array<Board, 2> boards_{};
  bool initialized_ = false;
  bool clock_failed_ = false;
  std::optional<std::uint64_t> last_now_;
  std::uint64_t next_command_id_ = 1;
  std::uint64_t control_epoch_ = 0;
  std::uint64_t generation_started_us_ = 0;
  bool recovery_required_ = true;
  bool armed_ = false;
  bool disabled_edge_seen_ = false;
  bool selfdrive_seen_ = false;
  SelfdriveSample selfdrive_{};
  std::string reason_ = "OFFROAD_CONFIGURATION_REQUIRED";
};

}  // namespace openpilot::pandad::compatibility
